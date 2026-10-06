"""Auto assign tiket ke QC: pembagian merata + jadwal batch harian.

Dipakai dua pintu masuk dengan aturan pembagian yang sama persis:

* tombol **Assign Otomatis** di menu Assign Ticket (``api/routers/qc_assignment.py``,
  dalam cakupan pemanggil), dan
* **jadwal batch** otomatis (``worker/tasks/auto_assign.py``, Celery beat) pada jam
  ``SCHEDULE_WIB`` — tanpa pemanggil, jadi cakupannya SELURUH tiket.

Diport dari 4-service@e17ecca (5 Oktober 2026). Bedanya dengan versi itu:

* Campaign Collection (``COLLECTION_CAMPAIGNS``) dikecualikan dari antrean, sama
  seperti tombol manual di prod — Collection tidak mengenal Assign Ticket.
* Jadwal otomatis MATI kecuali ``QC_AUTO_ASSIGN_ENABLED`` diisi true (keputusan
  6 Oktober 2026). Worker kube yang berbagi DB prod ikut membaca env ini, jadi
  default mati mencegahnya membagi tiket tanpa sengaja.

Modul ini sengaja tidak mengimpor FastAPI supaya bisa dipakai worker.
"""
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError

from compliance.campaign_kind import collection_campaigns_from_env
from db import crud
from db.models import User

# Jam batch (WIB, 24 jam): 08.00, 11.00, 13.00, 15.00, 16.30.
SCHEDULE_WIB = [(8, 0), (11, 0), (13, 0), (15, 0), (16, 30)]

# WIB = UTC+7 tetap (tanpa DST).
_WIB = timezone(timedelta(hours=7))

SCHEDULER_USERNAME = "scheduler"


def schedule_enabled() -> bool:
    """Saklar jadwal otomatis (env ``QC_AUTO_ASSIGN_ENABLED``, default MATI)."""
    return os.getenv("QC_AUTO_ASSIGN_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def schedule_labels() -> list:
    return [f"{h:02d}:{m:02d}" for h, m in SCHEDULE_WIB]


def next_run(now: Optional[datetime] = None) -> datetime:
    """Waktu batch berikutnya (aware, WIB) setelah ``now`` (default: sekarang)."""
    now_wib = (now or datetime.now(timezone.utc)).astimezone(_WIB)
    for day_offset in (0, 1):
        day = (now_wib + timedelta(days=day_offset)).date()
        for h, m in SCHEDULE_WIB:
            slot = datetime(day.year, day.month, day.day, h, m, tzinfo=_WIB)
            if slot > now_wib:
                return slot
    raise RuntimeError("SCHEDULE_WIB kosong")  # pragma: no cover


def active_qc_usernames(db) -> list:
    """Username QC aktif, urut nama — populasi penerima auto assign."""
    rows = (
        db.query(User)
        .filter(User.role == "qc", User.is_active == True)  # noqa: E712
        .order_by(User.name, User.username)
        .all()
    )
    return [(u.username or "").strip() for u in rows if (u.username or "").strip()]


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


def distribute_evenly(pool, qc_users, rnd=None) -> list:
    """Bagi ``pool`` acak & merata ke ``qc_users``: ``[(ticket_id, qc_username), ...]``.

    Merata per MOMEN pembagian (2 Oktober 2026, mengganti aturan "merata atas total
    beban" 4 September): beban dimulai dari 0 untuk semua QC aktif saat ini, jadi
    8 QC aktif -> 8 QC kebagian, 7 QC aktif -> 7 QC kebagian. Beban lama TIDAK
    dihitung — QC yang baru aktif tidak diborong tiket sampai "menyusul" total QC
    lain. Selisih antar-QC paling banyak 1 tiket.

    Urutan antrean DIKOCOK dan seri diundi, supaya tidak ada QC yang selalu kebagian
    tiket tertua/termuda dan nama pertama secara alfabetis tidak selalu unggul.
    ``rnd`` bisa diisi ``random.Random(seed)`` agar hasilnya bisa diuji.
    """
    if not pool or not qc_users:
        return []
    rnd = rnd or random.Random()
    load = {u: 0 for u in qc_users}
    pool = list(pool)
    rnd.shuffle(pool)
    pairs = []
    for tid in pool:
        low = min(load.values())
        owner = rnd.choice([u for u, n in load.items() if n == low])
        load[owner] += 1
        pairs.append((tid, owner))
    return pairs


def run_scheduled_batch(db) -> dict:
    """Satu batch terjadwal, seluruh tiket (tanpa cakupan). Slot tanpa pekerjaan di-skip.

    Mengembalikan ringkasan dengan ``skipped`` berisi alasan bila tidak ada yang dibagi.
    """
    qc_users = active_qc_usernames(db)
    if not qc_users:
        return {"assigned": 0, "skipped": "no_active_qc"}
    _ids, pool = unassigned_pool(db)
    if not pool:
        return {"assigned": 0, "skipped": "no_unassigned_tickets"}
    pairs = distribute_evenly(pool, qc_users)
    try:
        created = crud.bulk_assign_tickets_to_qc(db, pairs, assigned_by_username=SCHEDULER_USERNAME)
    except IntegrityError:
        # ``ticket_id`` unik: orang lain (tombol manual, atau beat kedua yang mengarah
        # ke DB yang sama) meng-assign salah satunya di sela pembacaan dan penulisan.
        # Batch ini dibatalkan utuh; sisa antrean ikut batch berikutnya.
        db.rollback()
        return {"assigned": 0, "skipped": "conflict"}
    per_qc: dict = {}
    for _tid, owner in pairs:
        per_qc[owner] = per_qc.get(owner, 0) + 1
    return {"assigned": created, "pool": len(pool), "qc_count": len(qc_users), "per_qc": per_qc}
