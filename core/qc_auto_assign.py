"""Auto assign tiket ke QC: pembagian merata + jadwal batch harian.

Dipakai dua pintu masuk dengan aturan pembagian yang sama persis:

* tombol **Assign Otomatis** di menu Assign Ticket (``api/routers/qc_assignment.py``,
  dalam cakupan pemanggil), dan
* **jadwal batch** otomatis (``worker/tasks/auto_assign.py``, Celery beat) pada jam
  ``SCHEDULE_WIB`` — tanpa pemanggil, jadi cakupannya SELURUH tiket.

Sejak 13 Oktober 2026 batch terjadwal membaca App C ``/tickets-daily`` (H-0 dan H-1),
bukan ``results``, dan memakai GERBANG STATUS (lihat :func:`slot_pool`): pada tiap slot,
data baru (masuk sejak slot sebelumnya) baru dibagi bila SEMUANYA sudah done di App C.
Selama belum, beat mengecek ulang tiap ``interval_minutes()`` menit sampai slot
berikutnya. Pembagiannya rata HARIAN (:func:`distribute_daily`). Tombol manual tetap
memakai :func:`distribute_evenly`.

Diport dari 4-service@e17ecca (5 Oktober 2026). Bedanya dengan versi itu:

* Campaign Collection (``COLLECTION_CAMPAIGNS``) dikecualikan dari antrean, sama
  seperti tombol manual di prod — Collection tidak mengenal Assign Ticket.
* Jadwal otomatis MATI kecuali ``QC_AUTO_ASSIGN_ENABLED`` diisi true (keputusan
  6 Oktober 2026). Worker kube yang berbagi DB prod ikut membaca env ini, jadi
  default mati mencegahnya membagi tiket tanpa sengaja.

Modul ini sengaja tidak mengimpor FastAPI supaya bisa dipakai worker.
"""
import logging
import os
import random
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError

from compliance.campaign_kind import collection_campaigns_from_env, is_collection
from db import crud
from db.models import Role, RoleCampaign, User, UserCampaign
from services import tickets_daily

logger = logging.getLogger(__name__)

# Jam batch (WIB, 24 jam): 08.00, 11.00, 13.00, 15.00, 16.30.
SCHEDULE_WIB = [(8, 0), (11, 0), (13, 0), (15, 0), (16, 30)]

# Jarak cek ulang status (menit) selama data baru sebuah slot belum done semua, env
# ``QC_AUTO_ASSIGN_INTERVAL_MIN``. Beat berdetak sesering ini; pembagian tetap per slot.
DEFAULT_INTERVAL_MIN = 5

# load_date App C yang dibaca batch: hari ini (WIB) dan kemarin. Tunggakan lebih lama
# sengaja tidak disapu — itu pekerjaan tombol manual per tanggal.
POOL_DAYS = 2

# WIB = UTC+7 tetap (tanpa DST).
_WIB = timezone(timedelta(hours=7))

SCHEDULER_USERNAME = "scheduler"


def schedule_enabled() -> bool:
    """Saklar jadwal otomatis (env ``QC_AUTO_ASSIGN_ENABLED``, default MATI)."""
    return os.getenv("QC_AUTO_ASSIGN_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def interval_minutes() -> int:
    """Menit antar-cek ulang status (env ``QC_AUTO_ASSIGN_INTERVAL_MIN``); nilai rusak = bawaan."""
    try:
        n = int(os.getenv("QC_AUTO_ASSIGN_INTERVAL_MIN", "").strip())
    except ValueError:
        return DEFAULT_INTERVAL_MIN
    return n if n > 0 else DEFAULT_INTERVAL_MIN


def schedule_labels() -> list:
    return [f"{h:02d}:{m:02d}" for h, m in SCHEDULE_WIB]


def _slots_around(now_wib: datetime) -> list:
    """Semua slot (aware, WIB) dari kemarin sampai besok, urut waktu."""
    out = []
    for day_offset in (-1, 0, 1):
        day = (now_wib + timedelta(days=day_offset)).date()
        for h, m in SCHEDULE_WIB:
            out.append(datetime(day.year, day.month, day.day, h, m, tzinfo=_WIB))
    return out


def next_run(now: Optional[datetime] = None) -> datetime:
    """Slot berikutnya (aware, WIB) setelah ``now`` (default: sekarang)."""
    now_wib = (now or datetime.now(timezone.utc)).astimezone(_WIB)
    return next(s for s in _slots_around(now_wib) if s > now_wib)


def current_slot(now: Optional[datetime] = None):
    """``(slot_aktif, slot_sebelumnya)``: slot terakhir yang ``<= now`` dan slot sebelum
    itu (aware, WIB). Jendela cek ulang sebuah slot berlaku sampai slot berikutnya."""
    now_wib = (now or datetime.now(timezone.utc)).astimezone(_WIB)
    past = [s for s in _slots_around(now_wib) if s <= now_wib]
    return past[-1], past[-2]


def collection_only(campaigns, collection) -> bool:
    """True bila ``campaigns`` (cakupan role/orang) berisi HANYA campaign Collection.

    List kosong = tidak dibatasi, jadi bukan Collection-only. Grup ``Telemarketing``
    bukan nama Collection, jadi ikut membatalkan.
    """
    names = [c for c in campaigns or [] if str(c or "").strip()]
    return bool(names) and all(is_collection(c, collection) for c in names)


def active_qc_users(db) -> list:
    """User QC aktif yang menerima tiket Assign Ticket, urut nama.

    QC yang cakupannya HANYA Collection (``user_campaigns`` atau ``role_campaigns``
    role-nya) dikecualikan (12 Oktober 2026): Collection tidak mengenal Assign
    Ticket, dan antreannya memang hanya berisi tiket non-Collection. Sebelumnya
    QC Collection ikut di dropdown dan kebagian tiket Telemarketing.
    """
    rows = (
        db.query(User)
        .filter(User.role == "qc", User.is_active == True)  # noqa: E712
        .order_by(User.name, User.username)
        .all()
    )
    collection = collection_campaigns_from_env()
    if not collection or not rows:
        return rows
    role_campaigns = [
        r[0] for r in db.query(RoleCampaign.campaign)
        .join(Role, Role.id == RoleCampaign.role_id)
        .filter(Role.key == "qc")
        .all()
    ]
    if collection_only(role_campaigns, collection):
        return []
    per_user: dict = {}
    for uid, campaign in (
        db.query(UserCampaign.user_id, UserCampaign.campaign)
        .filter(UserCampaign.user_id.in_([u.id for u in rows]))
        .all()
    ):
        per_user.setdefault(uid, []).append(campaign)
    return [u for u in rows if not collection_only(per_user.get(u.id), collection)]


def active_qc_usernames(db) -> list:
    """Username QC aktif (lihat :func:`active_qc_users`) — populasi penerima auto assign."""
    return [
        (u.username or "").strip() for u in active_qc_users(db) if (u.username or "").strip()
    ]


def ticket_id_for_result(result) -> Optional[str]:
    """Ticket id (prefix sebelum ``_`` pertama) dari nama berkas sumber pertama.

    Salinan ``api.qc_scope.ticket_id_for_result`` — worker tidak membawa paket ``api``.
    """
    sf = getattr(result, "source_files", None) or []
    if sf and isinstance(sf[0], str) and sf[0]:
        return sf[0].split("_", 1)[0]
    return None


def unassigned_pool(db, campaigns=None, customer_ids=None):
    """``(ticket_ids, pool)`` — pool = ticket id yang belum punya assignment.

    ``campaigns`` / ``customer_ids`` = cakupan pemanggil; keduanya ``None`` berarti
    tanpa batas (dipakai jadwal otomatis). Upload QC Support dan campaign Collection
    dikecualikan, sama seperti daftar di menu Assign Ticket.

    ``limit`` sengaja dibuka lebar: yang perlu dibagi adalah SELURUH antrean, bukan
    satu halaman.
    """
    results, _total = crud.list_results(
        db,
        campaigns=campaigns,
        customer_ids=customer_ids,
        page=1,
        limit=1_000_000,
        exclude_uploaded_by_role="qc_support",
        # Campaign Collection tidak mengenal Assign Ticket (lihat
        # COLLECTION_REMOVED_PERMISSIONS) — tiketnya tidak boleh ikut dibagikan.
        exclude_campaigns=sorted(collection_campaigns_from_env()) or None,
    )
    # Satu ticket id bisa punya lebih dari satu baris Result (tiket dua-agent), dan
    # assignment-nya per TIKET — jadi di-unique-kan dulu, kalau tidak tiket yang sama
    # akan terhitung (dan menghabiskan jatah) dua kali.
    seen, ticket_ids = set(), []
    for r in results:
        tid = (ticket_id_for_result(r) or "").strip()
        if tid and tid not in seen:
            seen.add(tid)
            ticket_ids.append(tid)
    assigned_ids = {(a.ticket_id or "").strip() for a in crud.list_qc_assignments(db)}
    return ticket_ids, [t for t in ticket_ids if t not in assigned_ids]


def campaign_map(db, ticket_ids, load_date=None) -> dict:
    """``{ticket_id: campaign}`` untuk pembagian rata per campaign.

    Campaign yang dimaksud adalah yang dilihat orang di menu Assign Ticket: field
    ``campaign`` App C ``/tickets-daily`` (``Personal Loan``, ``CashLine NTB``, ...).
    ``Result.campaign`` tidak bisa dipakai sendirian — untuk seluruh tiket
    Telemarketing isinya sama, ``Cashline``, jadi pembagian "per campaign" di atasnya
    tidak membagi apa pun.

    App C ditanya untuk ``load_date`` (kosong = H-1) — tanggal yang sama dengan yang
    dimuat layar Assign Ticket, jadi permintaannya kena cache ``tickets_daily``.
    Tiket di luar tanggal itu (mis. sisa antrean lama yang dibagi batch terjadwal),
    atau saat App C tidak bisa dihubungi, jatuh ke ``Result.campaign`` — pembagian
    tetap jalan, hanya kelompoknya lebih kasar.
    """
    wanted = {(t or "").strip() for t in ticket_ids or [] if (t or "").strip()}
    out = {t: c for t, c in crud.campaign_by_ticket_ids(db, wanted).items() if t in wanted}
    try:
        items = tickets_daily.fetch_all(load_date=(load_date or "").strip() or None)["items"]
    except Exception as exc:  # noqa: BLE001 — App C mati tidak boleh memblokir assign
        logger.warning("[auto-assign] campaign App C tidak terbaca, pakai Result.campaign: %s", exc)
        return out
    for it in items:
        tid = str(it.get("id") or it.get("tiket_id") or "").strip()
        campaign = str(it.get("campaign") or "").strip()
        if tid in wanted and campaign:
            out[tid] = campaign
    return out


def distribute_evenly(pool, qc_users, rnd=None, campaign_of=None) -> list:
    """Bagi ``pool`` acak & merata ke ``qc_users``: ``[(ticket_id, qc_username), ...]``.

    Merata per MOMEN pembagian (2 Oktober 2026, mengganti aturan "merata atas total
    beban" 4 September): beban dimulai dari 0 untuk semua QC aktif saat ini, jadi
    8 QC aktif -> 8 QC kebagian, 7 QC aktif -> 7 QC kebagian. Beban lama TIDAK
    dihitung — QC yang baru aktif tidak diborong tiket sampai "menyusul" total QC
    lain.

    Merata PER CAMPAIGN (13 Oktober 2026): ``campaign_of`` = ``{ticket_id: campaign}``.
    Tiket dikelompokkan per campaign lalu dibagi bergiliran ke QC dengan putaran yang
    BERSAMBUNG antar-campaign. Hasilnya selisih antar-QC paling banyak 1 tiket di
    SETIAP campaign sekaligus di total — sebelumnya hanya total yang rata, komposisi
    campaign tiap QC mengikuti untung-untungan acak. Tanpa ``campaign_of`` (atau
    tiket yang campaign-nya tidak diketahui) semuanya dianggap satu kelompok.

    Urutan QC, urutan campaign, dan urutan tiket di tiap campaign DIKOCOK, supaya
    tidak ada QC yang selalu kebagian sisa, tiket tertua/termuda, atau nama pertama
    secara alfabetis. ``rnd`` bisa diisi ``random.Random(seed)`` agar hasilnya bisa
    diuji.
    """
    if not pool or not qc_users:
        return []
    rnd = rnd or random.Random()
    campaign_of = campaign_of or {}
    groups: dict = {}
    for tid in pool:
        groups.setdefault(campaign_of.get(tid) or "", []).append(tid)
    # Urutkan dulu baru kocok: urutan dict mengikuti antrean, dan hasil untuk seed
    # yang sama harus sama walaupun antreannya datang dalam urutan lain.
    campaigns = sorted(groups)
    rnd.shuffle(campaigns)
    qcs = list(qc_users)
    rnd.shuffle(qcs)
    pairs, i = [], 0
    for campaign in campaigns:
        tickets = groups[campaign]
        rnd.shuffle(tickets)
        for tid in tickets:
            pairs.append((tid, qcs[i % len(qcs)]))
            i += 1
    return pairs


def _collection_contexts() -> frozenset:
    """Context App C milik campaign Collection, dari ``CAMPAIGN_CONTEXT_MAP``
    (bawaan ``Collection:collection``, sama dengan ``api.campaign_context``)."""
    collection = collection_campaigns_from_env()
    out = set()
    for chunk in os.getenv("CAMPAIGN_CONTEXT_MAP", "Telemarketing:*,Collection:collection").split(","):
        name, sep, contexts = chunk.partition(":")
        if sep and is_collection(name.strip(), collection):
            out |= {c.strip().casefold() for c in contexts.split("|") if c.strip()}
    out.discard("*")
    return frozenset(out)


def slot_pool(items, assigned_ids, slot_start, prev_start):
    """``(pool, campaign_of, waiting)`` untuk slot ``slot_start`` dari baris App C.

    Satu ticket id bisa membawa beberapa rekaman (``tiket_id``); tiket DONE bila semua
    rekamannya punya ``processed_at`` — aturan kolom Status menu Assign Ticket. Waktu
    masuk tiket = ``inserted_at`` rekaman terbarunya.

    * masuk setelah ``slot_start``: belum giliran, menunggu slot berikutnya;
    * masuk dalam ``(prev_start, slot_start]`` = DATA BARU slot ini. Yang belum done
      dihitung di ``waiting`` — selama ``waiting > 0`` pemanggil TIDAK membagi apa pun;
    * masuk sebelum ``prev_start`` = sisa slot lalu (pernah macet): ikut dibagi bila
      sudah done, tetapi tidak pernah menahan slot ini.

    Tiket yang sudah punya QC dan tiket Collection (nama campaign atau context) dilewati.
    """
    collection = collection_campaigns_from_env()
    contexts = _collection_contexts()
    slot_ts, prev_ts = slot_start.timestamp(), prev_start.timestamp()
    groups: dict = {}
    for it in items or []:
        tid = str(it.get("id") or it.get("tiket_id") or "").strip()
        if not tid:
            continue
        groups.setdefault(tid, []).append(it)
    pool, campaign_of, waiting = [], {}, 0
    for tid, rows in groups.items():
        if tid in assigned_ids:
            continue
        if any(
            is_collection(str(r.get("campaign") or "").strip(), collection)
            or str(r.get("context") or "").strip().casefold() in contexts
            for r in rows
        ):
            continue
        inserted = max(float(r.get("inserted_at") or 0) for r in rows)
        if inserted > slot_ts:
            continue
        if not all(r.get("processed_at") is not None for r in rows):
            if inserted > prev_ts:
                waiting += 1
            continue
        pool.append(tid)
        campaign = next((str(r.get("campaign")).strip() for r in rows if r.get("campaign")), "")
        if campaign:
            campaign_of[tid] = campaign
    return pool, campaign_of, waiting


def daily_loads(assignments, since, campaign_of):
    """Beban QC hari ini: ``(total, per_campaign)`` dari assignment dengan
    ``assigned_at >= since`` (naive UTC, seperti kolomnya). ``per_campaign`` dikunci
    ``(campaign, qc)``; tiket yang campaign-nya tidak diketahui masuk kelompok ``""``."""
    total: dict = {}
    per_campaign: dict = {}
    for a in assignments or []:
        if a.assigned_at is None or a.assigned_at < since:
            continue
        qc = (a.qc_username or "").strip()
        if not qc:
            continue
        key = (campaign_of.get((a.ticket_id or "").strip(), ""), qc)
        total[qc] = total.get(qc, 0) + 1
        per_campaign[key] = per_campaign.get(key, 0) + 1
    return total, per_campaign


def distribute_daily(pool, qc_users, campaign_of, total_load, campaign_load, rnd=None) -> list:
    """Bagi ``pool`` ke ``qc_users`` dengan memperhitungkan beban HARI INI.

    Tiap tiket jatuh ke QC dengan beban paling kecil di campaign tiket itu, lalu total
    hari ini, lalu urutan acak. Putaran 5 menit hanya membawa 1–2 tiket; tanpa beban
    harian, "rata per batch" berarti tiap tiket jatuh ke QC acak dan selisihnya
    menumpuk sepanjang hari. QC yang baru aktif siang hari kebagian lebih dulu sampai
    bebannya menyusul. Dict beban yang diberikan TIDAK diubah.
    """
    if not pool or not qc_users:
        return []
    rnd = rnd or random.Random()
    total = dict(total_load or {})
    per_campaign = dict(campaign_load or {})
    qcs = list(qc_users)
    rnd.shuffle(qcs)
    order = {q: i for i, q in enumerate(qcs)}
    tickets = sorted(pool)
    rnd.shuffle(tickets)
    pairs = []
    for tid in tickets:
        c = campaign_of.get(tid, "")
        q = min(qcs, key=lambda u: (per_campaign.get((c, u), 0), total.get(u, 0), order[u]))
        pairs.append((tid, q))
        per_campaign[(c, q)] = per_campaign.get((c, q), 0) + 1
        total[q] = total.get(q, 0) + 1
    return pairs


def run_scheduled_batch(db, now: Optional[datetime] = None) -> dict:
    """Satu cek terjadwal (tiap ``interval_minutes()`` menit) untuk slot yang aktif.

    Data baru slot itu dibagi hanya bila SEMUANYA sudah done (:func:`slot_pool`);
    selama belum, hasilnya ``skipped="waiting_not_done"`` dan cek berikutnya mencoba
    lagi. Setelah dibagi, cek-cek sesudahnya dalam slot yang sama tidak menemukan apa
    pun lagi kecuali sisa lama yang baru done. ``slot`` = jam slot aktif (WIB).
    """
    qc_users = active_qc_usernames(db)
    if not qc_users:
        return {"assigned": 0, "skipped": "no_active_qc"}
    now_wib = (now or datetime.now(timezone.utc)).astimezone(_WIB)
    today: date = now_wib.date()
    slot_start, prev_start = current_slot(now_wib)
    slot_label = slot_start.strftime("%H:%M")
    items = []
    try:
        for d in range(POOL_DAYS):
            items += tickets_daily.fetch_all(load_date=(today - timedelta(days=d)).isoformat())["items"]
    except tickets_daily.TicketsDailyError as exc:
        logger.warning("[auto-assign] App C tidak terbaca, putaran dilewati: %s", exc)
        return {"assigned": 0, "skipped": "app_c_error"}
    assignments = crud.list_qc_assignments(db)
    assigned_ids = {(a.ticket_id or "").strip() for a in assignments}
    pool, campaign_of, waiting = slot_pool(items, assigned_ids, slot_start, prev_start)
    if waiting:
        return {"assigned": 0, "skipped": "waiting_not_done", "waiting": waiting, "slot": slot_label}
    if not pool:
        return {"assigned": 0, "skipped": "no_ready_tickets", "slot": slot_label}
    midnight_wib = datetime(today.year, today.month, today.day, tzinfo=_WIB)
    since = midnight_wib.astimezone(timezone.utc).replace(tzinfo=None)
    # Campaign SEMUA baris App C yang dibaca, bukan hanya pool: tiket hari ini yang
    # sudah punya QC harus terhitung di campaign-nya sendiri.
    campaign_all = {}
    for it in items:
        tid = str(it.get("id") or it.get("tiket_id") or "").strip()
        if tid and it.get("campaign") and tid not in campaign_all:
            campaign_all[tid] = str(it.get("campaign")).strip()
    total, per_campaign = daily_loads(assignments, since, {**campaign_all, **campaign_of})
    pairs = distribute_daily(pool, qc_users, campaign_of, total, per_campaign)
    try:
        created = crud.bulk_assign_tickets_to_qc(
            db, pairs, assigned_by_username=SCHEDULER_USERNAME, source="scheduler",
        )
    except IntegrityError:
        # ``ticket_id`` unik: orang lain (tombol manual, atau beat kedua yang mengarah
        # ke DB yang sama) meng-assign salah satunya di sela pembacaan dan penulisan.
        # Putaran ini dibatalkan utuh; sisanya ikut putaran berikutnya.
        db.rollback()
        return {"assigned": 0, "skipped": "conflict"}
    per_qc: dict = {}
    for _tid, owner in pairs:
        per_qc[owner] = per_qc.get(owner, 0) + 1
    return {"assigned": created, "pool": len(pool), "slot": slot_label,
            "qc_count": len(qc_users), "per_qc": per_qc}
