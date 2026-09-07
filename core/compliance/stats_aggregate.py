"""Statistics dashboard aggregation.

Computes the full Statistics payload consumed by the revamped Stats page:

  - ``overview``        : status counts + system-wide error rate + donut breakdown
  - ``agents``          : per-agent submissions / errors / error rate (sales table)
  - ``campaign_monthly``: per (campaign, month) submissions, Risk Base breakdown
                          (High/Medium/Low/System/New — one highest risk base per
                          ticket) and error rate
  - ``hierarchy``       : Area Manager -> Team Leader -> Agent error rates + total

"Error rate" for a bucket = ``results-with-AI-Status-RETURN / evaluated-results``
(the SAME source as the Approve/Return donut & KPIs), where a result is *evaluated*
when it is ``done`` and has a usable evaluation JSON. A result counts as an "error"
only when its final AI Status is RETURN (FAIL) — NOT merely when it has ≥1 error code,
because a ticket can carry tolerable error codes yet still PASS. The Risk Base
breakdown below tallies ONE risk base per ticket — its single highest-severity
error code — using the priority order ``H > M > L > N > O`` (see ``_RISK_PRIORITY``).
Rows are derived per result via ``build_error_code_table`` (after applying approved
Error Code appeals, mirroring the per-result Error Code table). Each error-code row
carries a ``risk_base`` of ``H``/``M``/``L`` (see ``compliance.error_codes.ERROR_CODES``);
rows for a new-joiner agent's submission get ``L``/``M`` softened to ``N`` (mirrors the
per-result Error Code table — see ``override_risk_base_for_new_joiner``), and any row
whose error code carries no catalogued risk base counts as ``O`` (System). A ticket
with no error-code rows contributes to no bucket, so H+M+L+N+O ≤ submissions.

This scan is expensive (it reads every done result's evaluation JSON), so it is run
at most once per WIB day and cached — see ``crud.get_or_build_stats_snapshot``.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import false, func

from sales_lookup import NEW_JOINER_THRESHOLD_DAYS, active_sales_map
from compliance.badwords import has_badword
from compliance.error_codes import (
    _appeal_kind,
    added_appeals_only,
    apply_added_score_appeals,
    apply_approved_appeals,
    apply_approved_card_holder_appeals,
    apply_approved_cashline_appeals,
    apply_approved_critical_compliance_appeals,
    appeals_that_flip,
    approved_appeals_only,
    build_error_code_table,
    document_error_code_rows,
    apply_cashline_document_status,
    apply_static_document_status,
    normalize_dynamic_verification,
    normalize_static_verification,
    static_consistency_failures,
    CARD_HOLDER_DYNAMIC_FIELDS,
    CARD_HOLDER_STATIC_SCORECARD,
    inject_added_rows,
    override_risk_base_for_new_joiner,
)
from compliance.scoring import base_ai_status, has_blocking_intolerable_item
from db import crud
from db.models import Result, ResultData, User

# submit_time strings look like "2026-06-17 15:24:53" (mirrors
# sales_lookup._SUBMIT_FORMATS).
_SUBMIT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


_UNKNOWN = "(Tidak diketahui)"
_WIB = ZoneInfo("Asia/Jakarta")

# Per-ticket risk severity order (H highest → O lowest). The Performa Campaign
# tally counts only ONE risk base per ticket: its single highest-severity code.
# ``N`` (new-joiner softened L/M) only appears during an agent's grace period.
_RISK_PRIORITY = {"H": 0, "M": 1, "L": 2, "N": 3, "O": 4}

# Risk Base yang membentuk "Total Risk" — dan karenanya membentuk Error Rate.
# ``N`` (new joiner) dan ``O`` (System) sengaja di luar: keduanya bukan kesalahan
# yang dibebankan ke agent.
_RISK_COUNTED = ("H", "M", "L")


def _tolerable_item_codes(evaluation) -> set:
    """``item_code`` scorecard yang ``tolerable = YES``.

    Menerima ``result_json`` MAUPUN objek ``evaluation``-nya langsung; pemanggil di
    agregasi memegang yang pertama, pemanggil per-tiket memegang yang kedua.

    Membaca dari evaluasi MENTAH tidak masalah: ``tolerable`` adalah properti tetap
    sebuah item scorecard, tidak pernah digeser banding — yang digeser banding
    adalah hasil SESUAI/BELUM_SESUAI-nya.

    Dipakai aturan pengecualian di ``top_risk_base`` — lihat di sana.
    """
    if not isinstance(evaluation, dict):
        return set()
    if "scorecard_result" not in evaluation:
        inner = evaluation.get("evaluation")
        evaluation = inner if isinstance(inner, dict) else evaluation
    out = set()
    for item in evaluation.get("scorecard_result") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("tolerable") or "").strip().upper() == "YES":
            code = item.get("item_code")
            if code:
                out.add(str(code))
    return out


def top_risk_base(rows, evaluation=None) -> "str | None":
    """Risk Base TERTINGGI sebuah tiket (H > M > L > N > O), atau None.

    Satu tiket menyumbang PALING BANYAK satu hitungan risk base, berapa pun error
    code yang dikandungnya — inilah definisi yang dipakai seluruh Error Rate di
    dashboard (lihat ``_rate_of``). Baris tanpa risk base terkatalog dihitung
    ``O`` (System).

    **Pengecualian L-tolerable (14 Agustus 2026).** Bila risk base tertinggi tiket
    ini ternyata ``L`` DAN setiap baris ``L``-nya menempel pada item scorecard yang
    ``tolerable = YES``, tiket ini tidak menyumbang risk base sama sekali (None).
    Alasannya: pelanggaran seperti itu tidak membuat tiket Not Qualified — agent
    yang hanya gagal di Greeting tetap lolos — sehingga memasukkannya ke Total Risk
    membuat Error Rate menghukum kesalahan yang sistem sendiri maafkan.

    Yang TIDAK ikut dikecualikan, dan itu disengaja:
      * ``L`` dari verifikasi data (B02) — tidak punya ``item_code``, jadi tidak
        punya ``tolerable``. Salah input data tetap salah input data;
      * tiket yang risk base tertingginya ``M``/``H``, walau kebetulan juga
        mengandung ``L`` yang tolerable.

    ``evaluation`` boleh None; tanpa evaluasi tidak ada informasi ``tolerable``,
    jadi tidak ada yang dikecualikan.
    """
    top = None
    for row in rows or []:
        rb = (row or {}).get("risk_base") or "O"
        if rb not in _RISK_PRIORITY:
            rb = "O"
        if top is None or _RISK_PRIORITY[rb] < _RISK_PRIORITY[top]:
            top = rb
    if top != "L":
        return top
    tolerable = _tolerable_item_codes(evaluation)
    if not tolerable:
        return top
    low_rows = [r for r in (rows or []) if ((r or {}).get("risk_base") or "") == "L"]
    if low_rows and all(str((r or {}).get("item_code") or "") in tolerable for r in low_rows):
        return None
    return top


def risk_base_tally(rows, evaluation=None) -> dict:
    """Hitung SEMUA risk base sebuah tiket — bukan hanya yang tertinggi.

    Dipakai pohon **Hierarki Failure Rate** (28 Agustus 2026). Di tabel itu kolom
    High/Medium/Low menghitung **PELANGGARAN**, bukan tiket: satu tiket yang
    melanggar empat hal berat menyumbang 4 ke High, bukan 1. Konsekuensinya
    ``Total Failure`` BISA melebihi jumlah tiket Not Qualified — itu memang yang
    diminta, karena bobot sebuah tiket dengan 5 pelanggaran memang bukan 1.

    Bedakan dari ``top_risk_base``, yang TETAP dipakai Performa Campaign dan KPI
    Overview: di sana satu tiket = paling banyak satu hitungan.

    Pengecualian L-tolerable diterapkan PER BARIS di sini: baris ``L`` yang menempel
    pada item scorecard ``tolerable = YES`` tidak dihitung — alasannya sama dengan
    ``top_risk_base``, sistem sendiri memaafkan pelanggaran itu.

    Pemanggil bertanggung jawab menerapkan ``override_risk_base_for_new_joiner``
    lebih dulu bila perlu, persis seperti ``top_risk_base``.
    """
    tally = {k: 0 for k in _RISK_PRIORITY}
    tolerable = _tolerable_item_codes(evaluation)
    for row in rows or []:
        rb = (row or {}).get("risk_base") or "O"
        if rb not in _RISK_PRIORITY:
            rb = "O"
        if rb == "L" and str((row or {}).get("item_code") or "") in tolerable:
            continue
        tally[rb] += 1
    return tally


def _rate_of(risk: dict, submissions: int) -> float:
    """Error Rate = Total Risk (H+M+L) ÷ Submission.

    Dipakai oleh **Hierarki Failure Rate** (global + versi ter-scope Area Manager /
    Team Leader), Overview per campaign, dan tabel Daftar Sales Agent milik Team
    Leader. Di UI hasilnya berlabel "Failure Rate" dengan pembilang "Total Failure".

    Dulu (14 Agustus 2026) ini rumus tunggal seluruh dashboard. Sejak 28 Agustus
    2026 tiap tabel punya definisinya sendiri atas permintaan bisnis:
      * Performa Sales     -> Not Qualified ÷ Submission  (``_rate``)
      * Performa Campaign  -> Total Risk ÷ tiket Not Qualified (``_rate``)
      * Hierarki           -> tetap fungsi ini, TAPI akumulator yang disuapkan
        hanya berisi risk base milik tiket Not Qualified, dan menghitung SEMUA
        risk base tiap tiket (``risk_base_tally``), bukan satu yang tertinggi.
    Jadi angka ketiga tabel itu memang tidak lagi saling sebanding begitu saja.

    RASIONYA BISA MELEWATI 100% dan itu benar. Kalimat lama di sini ("tiap tiket
    menyumbang paling banyak 1 ke H/M/L, jadi rasionya selalu <= 100%") sudah tidak
    berlaku sejak pembilangnya pindah ke ``risk_base_tally``: satu tiket Not Qualified
    menyumbang SEMUA risk base-nya, sementara penyebutnya jumlah transkrip. Tiket
    ``160908U5GK`` misalnya menyumbang 11 risk base atas 2 transkrip (550%).

    Yang di atas 100% DIBATASI SAAT DITAMPILKAN menjadi "100%+" (``rateText`` di
    StatsView.vue, permintaan bisnis 31 Agustus 2026). Pembatasan itu murni tampilan —
    angka yang dikembalikan fungsi ini tetap apa adanya, karena pengurutan dan
    pewarnaan masih membutuhkan nilai sebenarnya.

    Sengaja TIDAK dipakai di: tabel Daftar QC (mengukur beban & hasil kerja QC,
    bukan kesalahan sales), breakdown Manual Status (vonis human tidak punya risk
    base), dan tab Failure Reason (mengukur kegagalan kategori scorecard).
    """
    return _rate(sum(risk.get(k, 0) for k in _RISK_COUNTED), submissions)


def _avg(total_risk: int, recordings: int) -> float:
    """Rata-rata risk base per RECORDING (1 desimal); 0.0 bila nol.

    Pasangan ``_rate`` yang TIDAK mengalikan 100: hasilnya kelipatan, bukan persen.
    """
    if not recordings:
        return 0.0
    return round(total_risk / recordings, 1)


def _avg_of(risk: dict, recordings: int) -> float:
    """Avg Failure Rate = Total Failure (H+M+L) / **Total Recording**.

    Dipakai **hanya** oleh tab Hierarki Failure Rate (pohon global, versi ter-scope
    Area Manager / Team Leader, dan Daftar Sales Agent). Di UI berlabel
    "Avg Failure Rate" dan ditulis sebagai kelipatan, mis. ``2.8x``.

    Menggantikan ``_rate_of`` di sana sejak 2 September 2026: karena satu tiket Not
    Qualified menyumbang SEMUA risk base-nya (``risk_base_tally``), pembilangnya
    rutin melebihi penyebutnya dan angkanya melewati 100% — sehingga tidak terbaca
    sebagai persentase. Yang berubah HANYA penyajiannya (kelipatan, bukan persen);
    penyebutnya tetap jumlah rekaman yang dinilai.

    Penyebutnya sempat dipindah ke tiket Not Qualified pada 2 September 2026 pagi
    ("rata-rata pelanggaran per tiket gagal") lalu DIKEMBALIKAN ke Total Recording
    sore harinya atas permintaan bisnis: kolom penyebutnya ikut berganti nama
    Submissions -> **Total Recording**, dan angka yang dibaca pengawas harus
    sepasang dengan kolom itu.

    ``_rate_of`` sengaja DIPERTAHANKAN untuk Overview per campaign dan Overview
    global, yang tidak ikut berubah.
    """
    return _avg(sum(risk.get(k, 0) for k in _RISK_COUNTED), recordings)


def _customer_id(source_files) -> "str | None":
    """Customer/session id = prefix before the first ``_`` of the first source file."""
    if not source_files:
        return None
    first = source_files[0]
    if not isinstance(first, str) or not first:
        return None
    return first.split("_", 1)[0]


def _agent_name_fallback(agent_id) -> "str | None":
    """Fallback agent name = the alphabetic chars of the id (``rizqi801`` -> ``rizqi``)."""
    if not agent_id:
        return None
    letters = "".join(c for c in agent_id if c.isalpha())
    return letters or None


def _wib_month(dt) -> "str | None":
    """WIB ``YYYY-MM`` for a naive-UTC ``uploaded_at`` (None if missing)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(_WIB).strftime("%Y-%m")


def done_results_query(db, campaign: str = None, campaigns: list = None):
    """Query tiket ``done`` dengan DUA lapis filter campaign yang berbeda artinya:

    - ``campaign``  : PILIHAN pemakai (dropdown filter) — satu campaign, opsional;
    - ``campaigns`` : BATAS campaign role (``rbac.effective_campaigns_for``) —
      ``None`` berarti tidak dibatasi, daftar KOSONG berarti dibatasi ke tidak ada
      apa pun sehingga tidak satu tiket pun lolos.

    Keduanya dipasang bersama, jadi memilih campaign di luar cakupan menghasilkan
    himpunan kosong alih-alih diam-diam melebar.
    """
    q = exclude_hidden_results(db.query(Result).filter(Result.status == "done"))
    if campaign:
        q = q.filter(func.lower(func.trim(Result.campaign)) == campaign.strip().casefold())
    if campaigns is not None:
        allowed = [str(c).strip().casefold() for c in campaigns if str(c).strip()]
        if not allowed:
            return q.filter(false())
        q = q.filter(func.lower(func.trim(Result.campaign)).in_(allowed))
    return q


def _rate(errors: int, total: int) -> float:
    """Error rate percentage (1 decimal); 0.0 when there are no submissions."""
    if not total:
        return 0.0
    return round(errors / total * 100, 1)


def _parse_submit_date(value):
    """Coerce a ``submit_time`` string into a ``date`` (None if missing/unparseable).
    Mirrors ``sales_lookup._to_date``."""
    if not value:
        return None
    s = str(value).strip()
    for fmt in _SUBMIT_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# "H+2" = 48 jam sejak submit_time — tenggat unggah dokumen pendukung.
# Selama tenggat BELUM lewat & dokumen belum diunggah, tiket berstatus PENDING;
# setelah lewat tanpa unggah → FAIL (Not Qualified). Sama dengan timer di menu
# Pending Check (SLA_HOURS di ResultsView.vue).
SLA_HOURS = 48

# --- SAKELAR aturan tenggat H+2 -------------------------------------------
# True (default, aturan aktif): tiket kekurangan dokumen jadi FAIL begitu 48 jam
# sejak submit_time terlewat; sebelum itu PENDING.
#
# Sempat DIMATIKAN 11 Agustus 2026 supaya status PENDING bisa diuji — seluruh data
# uji sudah jauh melewati tenggatnya sehingga tiket kekurangan dokumen selalu jatuh
# ke FAIL dan PENDING tak pernah muncul di layar. DIAKTIFKAN KEMBALI 12 Agustus 2026;
# konsekuensinya tiket uji lama yang tadinya PENDING kembali dibaca FAIL (Not
# Qualified) karena tenggatnya memang sudah lewat.
#
# SEJAK 24 Agustus 2026 sakelarnya TIDAK LAGI di sini. Nilainya disimpan di
# ``app_settings.doc_sla_enabled`` dan diubah dari menu Results (khusus role
# ``admin``, capability ``admin.doc_sla.write``). Alasannya: mengedit konstanta
# berarti edit file + restart container — tidak bisa dilakukan operator, dan
# statusnya tidak terlihat sama sekali di layar.
#
# ``DOC_SLA_ENABLED`` dipertahankan sebagai NILAI CADANGAN yang dipakai hanya bila
# nilai dari DB belum sempat terbaca (mis. dipanggil di luar request, tanpa sesi DB).
# JANGAN membaca konstanta ini untuk memutuskan sesuatu — pakai
# ``doc_sla_enabled()`` supaya sakelar operator ikut terbaca.
DOC_SLA_ENABLED = True

# Nilai terakhir yang diketahui dari DB, disegarkan oleh ``refresh_doc_sla_cache``
# (dipanggil dependency get_db di setiap request dan sesudah sakelarnya diubah).
# Disimpan di modul supaya ``_doc_sla_expired`` tidak perlu sesi DB — ia dipanggil
# ribuan kali per permintaan dari fungsi yang tidak semuanya memegang ``db``.
_DOC_SLA_RUNTIME = {"value": None}


def refresh_doc_sla_cache(db) -> bool:
    """Baca sakelar dari DB dan simpan ke cache modul. Kembalikan nilainya.

    Gagal baca (tabel belum ada saat migrasi berjalan, dst.) TIDAK boleh menjatuhkan
    permintaan: nilai lama dipertahankan, dan bila belum ada sama sekali dipakai
    ``DOC_SLA_ENABLED``.
    """
    try:
        from db import crud

        val = crud.get_doc_sla_enabled(db)
    except Exception:
        cur = _DOC_SLA_RUNTIME["value"]
        return DOC_SLA_ENABLED if cur is None else cur
    _DOC_SLA_RUNTIME["value"] = val
    return val


def doc_sla_enabled() -> bool:
    """Kebijakan tenggat H+2 sedang aktif? Sumber kebenarannya ``app_settings``."""
    val = _DOC_SLA_RUNTIME["value"]
    return DOC_SLA_ENABLED if val is None else val


# Ticket id yang disembunyikan dari SELURUH permukaan pembacaan (Statistik, Results,
# Transcripts). Disegarkan tiap permintaan lewat ``refresh_hidden_tickets_cache``,
# sama seperti sakelar tenggat dokumen di atas — penyaringnya dipanggil dari fungsi
# yang tidak semuanya memegang sesi DB.
_HIDDEN_TICKETS_RUNTIME = {"value": None}


def refresh_hidden_tickets_cache(db) -> tuple:
    """Baca daftar tiket tersembunyi dari DB ke cache modul. Kembalikan daftarnya.

    Gagal baca tidak boleh menjatuhkan permintaan: daftar lama dipertahankan, dan bila
    belum pernah terbaca dianggap KOSONG — artinya tidak ada yang disembunyikan.
    Menyembunyikan tiket karena kegagalan baca akan jauh lebih berbahaya daripada
    menampilkannya.
    """
    try:
        from db import crud

        val = crud.get_hidden_ticket_ids(db)
    except Exception:
        cur = _HIDDEN_TICKETS_RUNTIME["value"]
        return () if cur is None else cur
    _HIDDEN_TICKETS_RUNTIME["value"] = val
    return val


def hidden_ticket_ids() -> tuple:
    """Ticket id yang sedang disembunyikan (kosong = tidak ada)."""
    val = _HIDDEN_TICKETS_RUNTIME["value"]
    return () if val is None else val


def is_hidden_ticket(ticket_id) -> bool:
    """Apakah satu ticket id termasuk yang disembunyikan (case-insensitive)."""
    tk = str(ticket_id or "").strip().lower()
    if not tk:
        return False
    return tk in {h.lower() for h in hidden_ticket_ids()}


def exclude_hidden_results(query):
    """Saring query ``Result`` agar tidak memuat tiket tersembunyi.

    Satu-satunya tempat ekspresi penyaringnya ditulis untuk sisi agregasi — dipakai
    ``done_results_query`` dan setiap query ``Result`` lain di modul ini yang tidak
    lewat sana.
    """
    from db import crud

    return crud.hidden_ticket_filter(query)


def _parse_submit_datetime(value):
    """Parse ``submit_time`` into a ``datetime`` (None bila tak ada/tak terbaca).
    "%Y-%m-%d" (tanpa jam) dianggap tengah malam hari itu."""
    if not value:
        return None
    s = str(value).strip()
    for fmt in _SUBMIT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _doc_sla_expired(submit_time, now=None) -> bool:
    """True bila tenggat H+2 (48 jam) sejak ``submit_time`` sudah LEWAT. submit_time
    kosong/tak terbaca → True (tak ada masa tenggang yang bisa dibuktikan → perlakukan
    sebagai lewat, mempertahankan perilaku lama missing-docs → FAIL).

    Saat kebijakan H+2 DIMATIKAN dari menu Results, selalu False — tenggatnya
    dianggap tidak pernah lewat, sehingga tiket kekurangan dokumen tetap PENDING."""
    if not doc_sla_enabled():
        return False
    dt = _parse_submit_datetime(submit_time)
    if dt is None:
        return True
    now = now or datetime.now()
    return now >= dt + timedelta(hours=SLA_HOURS)


def _submit_time_map(db, results) -> dict:
    """Map ``Result.id -> raw submit_time string`` (untuk menghitung tenggat H+2
    status PENDING). Mirror ``_submit_date_map`` tapi menyimpan string mentah.

    [FIX] Sumbernya ``crud.cashline_agent_index()`` — snapshot ``reference_data`` di
    ``result_json`` — BUKAN tabel ``tms_cashline``, yang tidak terisi lagi sejak
    reference data pindah ke DWH API. Query lama tetap jalan tanpa error tetapi
    SELALU mengembalikan kosong.
    Akibatnya submit_time selalu None -> ``_doc_sla_expired`` selalu True -> status
    PENDING tidak pernah muncul di layar."""
    cid_by_rid = {}
    for r in results:
        cid = _customer_id(r.source_files)
        if cid:
            cid_by_rid[r.id] = cid.strip()
    if not cid_by_rid:
        return {}
    index = crud.cashline_agent_index(db)
    return {
        rid: (index.get(cid) or {}).get("submit_time")
        for rid, cid in cid_by_rid.items()
    }


def _submit_agent_map(db, results) -> tuple:
    """``({Result.id: submit_time}, {Result.id: agent_id})`` dari snapshot reference_data.

    Satu sumber untuk dua hal yang selalu dibutuhkan bersama saat menghitung Risk
    Base: tenggat H+2 (dari submit_time) dan aturan new joiner (dari agent_id +
    tanggal join). ``_submit_time_map`` menyediakan yang pertama saja dan tetap
    dipakai di tempat yang memang hanya butuh itu.

    [ADAPTASI] Sumbernya ``crud.cashline_agent_index()`` — snapshot reference_data di
    ``result_json`` — BUKAN tabel ``tms_cashline`` seperti versi upstream. Tabel itu
    sudah tidak terisi sejak reference data pindah ke DWH API: query-nya tetap jalan
    tanpa error tetapi SELALU mengembalikan kosong, sehingga submit_time dan agent_id
    selalu None -> tenggat H+2 dianggap lewat untuk semua tiket dan pelunakan new
    joiner tidak pernah aktif. Sejalan dengan ``_submit_time_map``, ``_submit_date_map``,
    ``compute_stats_snapshot`` dan ``compute_team_agents`` yang sudah lebih dulu
    dialihkan ke snapshot.
    """
    cid_by_rid = {}
    for r in results:
        cid = _customer_id(r.source_files)
        if cid:
            cid_by_rid[r.id] = cid.strip()
    if not cid_by_rid:
        return {}, {}
    # [FIX] Sumbernya snapshot ``reference_data`` lewat ``crud.cashline_agent_index()``,
    # bukan tabel ``tms_cashline`` yang sudah tidak terisi sejak reference data pindah
    # ke DWH API. Index itu justru mengembalikan agent_id + submit_time sekaligus,
    # jadi sifat "satu sumber untuk dua kebutuhan" di docstring tetap terjaga.
    index = crud.cashline_agent_index(db)
    return (
        {rid: (index.get(cid) or {}).get("submit_time") for rid, cid in cid_by_rid.items()},
        {rid: ((index.get(cid) or {}).get("agent_id") or None) for rid, cid in cid_by_rid.items()},
    )


def risk_tally(db, results, eval_by_id: dict, appeal_map: dict) -> dict:
    """Hitungan ``{H, M, L, N, O}`` untuk sekumpulan result.

    Aturannya HARUS sama dengan yang dipakai ``compute_stats_snapshot``, karena
    keluarannya masuk ke Error Rate yang ditampilkan berdampingan dengan angka
    snapshot: satu risk base tertinggi per tiket, pelunakan new joiner (L/M -> N),
    dan pengecualian L-tolerable. Lihat ``top_risk_base``.

    ``eval_by_id`` / ``appeal_map`` di-key oleh ``str(Result.id)``.
    """
    out = {"H": 0, "M": 0, "L": 0, "N": 0, "O": 0}
    if not results:
        return out
    submit_by_rid, agent_by_rid = _submit_agent_map(db, results)
    sales_map = active_sales_map(db)
    mdocs = _missing_docs_map(db, results, eval_by_id)
    docs_by_rid = crud.document_ocr_by_result(db, [str(r.id) for r in results])
    doc_map = document_status_map(db, results)
    _now = datetime.now()
    for r in results:
        rid = str(r.id)
        rows = _error_code_rows(
            eval_by_id.get(rid), appeal_map.get(rid),
            mdocs.get(rid, False) and _doc_sla_expired(submit_by_rid.get(r.id), _now),
            docs_by_rid.get(rid, ()),
            doc_map.get(rid),
        )
        if rows is None:
            continue
        if _is_new_joiner(submit_by_rid.get(r.id), agent_by_rid.get(r.id), sales_map):
            rows = override_risk_base_for_new_joiner(rows)
        top = top_risk_base(rows, eval_by_id.get(rid))
        if top is not None:
            out[top] += 1
    return out


def _is_new_joiner(submit_time, agent_id, sales_map) -> bool:
    """Batched equivalent of ``sales_lookup.new_joiner_info(...)["is_new_joiner"]``
    — reuses the already-fetched ``sales_map`` (join dates) instead of re-querying
    the active sales database per result."""
    if not agent_id:
        return False
    submit_date = _parse_submit_date(submit_time)
    entry = sales_map.get(agent_id.casefold())
    join_date = entry.get("join_date") if entry else None
    if not submit_date or not join_date:
        return False
    return (submit_date - join_date).days < NEW_JOINER_THRESHOLD_DAYS


def _adjusted_evaluation(result_json, appeals, doc_status=None):
    """The result's ``evaluation`` dict with approved appeals applied, or None when
    the result has no usable evaluation (i.e. it is not evaluable).

    ``doc_status`` = ``(uploaded_types, sla_expired)`` dari ``document_status_map``.
    Bila diberikan, baris verifikasi statik di zona abu-abu mendapat status dokumennya
    (MATCH / PENDING / MISMATCH). Bila ``None``, barisnya dibiarkan MATCH seperti
    sebelumnya — dipakai pemanggil yang memang tidak menilai skor."""
    if not isinstance(result_json, dict):
        return None
    evaluation = result_json.get("evaluation")
    if not isinstance(evaluation, dict):
        return None
    # Ambang zona abu-abu ditegakkan di kode, sebelum apa pun dihitung dari evaluasi
    # ini — supaya tidak bergantung pada kepatuhan LLM. Lihat fungsinya.
    evaluation = normalize_static_verification(evaluation)
    # Baris verifikasi dinamis tanpa dua sisi pembanding (Ascend/transkrip kosong)
    # turun ke SKIPPED_NULL, bukan MISMATCH — tidak menerbitkan B17.
    evaluation = normalize_dynamic_verification(evaluation)
    if doc_status is not None:
        uploaded, expired = doc_status
        evaluation = apply_static_document_status(evaluation, uploaded, expired)
        # Sisi cashline menyusul: baris yang menunggu cover buku tabungan menjadi
        # PENDING selama tenggat H+2, agar jalur dokumennya tidak terpotong.
        evaluation = apply_cashline_document_status(evaluation, uploaded, expired)
    approved = approved_appeals_only(appeals or [])
    if approved:
        # 'change' bandings to a deduction-bearing code keep their deduction; the
        # rest flip the item/field to lift the score. 'add' bandings lower the score.
        flip = [a for a in appeals_that_flip(approved) if _appeal_kind(a) != "add"]
        evaluation = apply_approved_appeals(evaluation, flip)
        evaluation = apply_approved_card_holder_appeals(evaluation, flip)
        evaluation = apply_approved_cashline_appeals(evaluation, flip)
        evaluation = apply_approved_critical_compliance_appeals(evaluation, flip)
    # SELALU dijalankan, ada banding atau tidak (31 Agustus 2026). Di dalamnya bukan
    # cuma applier banding 'add': ada VERIFICATION -> SCORECARD PROPAGATION (card holder
    # DAN cashline) serta sinkronisasi item kritis — tiga hal yang sama sekali tidak
    # bergantung pada banding.
    #
    # Sebelumnya seluruh pemanggilan ini terkurung di balik ``if approved:``, sehingga
    # tiket TANPA banding — mayoritas — tidak pernah dipropagasikan di jalur ini.
    # Akibatnya AI Status kanonik membaca evaluasi yang BERBEDA dari yang dipakai
    # menghitung skor: tiket 060311W3hz berskor 134 (di bawah passing 135) tetapi
    # divonis PASS, karena SC_CL_9 yang dijatuhkan MISMATCH cashline hanya terlihat di
    # sisi skor. Fungsi ini sendiri sudah no-op bila memang tidak ada yang berubah.
    evaluation = apply_added_score_appeals(evaluation, added_appeals_only(appeals or []))
    return evaluation


def _error_code_rows(result_json, appeals, doc_overdue: bool = False,
                     documents=(), doc_status=None) -> "list | None":
    """The result's error-code rows (after approved appeals), or None when it has
    no usable evaluation.

    ``doc_overdue`` — dokumen pendukung yang diminta belum diunggah DAN tenggat H+2
    sudah lewat. Menerbitkan B09 (Risk Base M, lihat ``document_error_code_rows``).
    Sampai 14 Agustus 2026 keadaan ini hanya membuat tiket Not Qualified tanpa error
    code apa pun, sehingga di tally Risk Base ia jatuh ke ``O`` (System) — terbaca
    seolah kesalahan sistem, padahal kelalaian melengkapi berkas.

    ``documents`` — ``[(doc_type, ocr_json), ...]`` dokumen yang OCR-nya selesai.
    Slot yang berisi jenis dokumen keliru menerbitkan C03 (Risk Base O).

    Keduanya bawaannya kosong: pemanggil yang tidak menghitung tenggat / tidak
    memuat dokumen tidak boleh diam-diam menerbitkan error code.
    """
    evaluation = _adjusted_evaluation(result_json, appeals, doc_status)
    if evaluation is None:
        return None
    # Approved adds only — a banding still awaiting review must not inflate the
    # aggregate error counts.
    rows = inject_added_rows(build_error_code_table(evaluation), added_appeals_only(appeals or []))
    extra = document_error_code_rows(
        missing=bool(doc_overdue),
        missing_labels=doc_requirement_labels(result_json) if doc_overdue else (),
        wrong_type=wrong_document_types(documents),
    )
    return rows + extra if extra else rows


def _wrong_document_type(doc_type, ocr_json):
    """Alias impor-malas ``compliance.documents.wrong_document_type``.

    Diimpor di dalam fungsi, bukan di kepala berkas: ``compliance.documents`` memuat
    modul prompt secara lazy dan tidak semestinya ikut ditarik hanya karena agregasi
    statistik di-import.
    """
    from compliance.documents import wrong_document_type

    return wrong_document_type(doc_type, ocr_json)


def wrong_document_types(documents) -> list:
    """``[{"expected", "detected"}, ...]`` untuk tiap slot dokumen yang isinya jenis
    lain. ``documents`` = ``[(doc_type, ocr_json), ...]``."""
    out = []
    for doc_type, ocr_json in documents or []:
        hit = _wrong_document_type(doc_type, ocr_json)
        if hit:
            out.append(hit)
    return out


def doc_requirement_labels(result_json) -> list:
    """Label dokumen yang diminta oleh zona abu-abu verifikasi statik, mis.
    ``["KK"]``.

    Kosong bila kewajibannya datang dari perubahan data TMS / limit >= 50 juta —
    di sana dokumen APA PUN memenuhi syarat, jadi tidak ada satu jenis yang bisa
    disebut. Baris B09 tetap terbit, hanya kalimat alasannya yang lebih umum.
    """
    from compliance.documents import DOCUMENT_TYPES, card_holder_doc_types

    return [
        DOCUMENT_TYPES.get(t, {}).get("label", t.upper())
        for t in card_holder_doc_types(result_json)
    ]


#: Kekurangan data acuan sebuah tiket, urut dari yang PALING mendasar. Sebuah tiket
#: bisa membawa LEBIH DARI SATU sekaligus dan semuanya dilaporkan (permintaan
#: 28 Agustus 2026) — orang yang menindaklanjuti perlu tahu seluruh data yang harus
#: dilengkapi, bukan hanya yang pertama ditemukan.
DATA_GAP_TRANSCRIPT = "transcript"
DATA_GAP_TMS = "tms"
DATA_GAP_AGENT = "agent"
DATA_GAP_ASCEND = "ascend"

#: Kalimat yang tampil di bawah badge AI Status untuk tiap kekurangan data.
DATA_GAP_REASON = {
    DATA_GAP_TRANSCRIPT: "Transkrip Kosong",
    DATA_GAP_TMS: "Data TMS Kosong",
    DATA_GAP_AGENT: "Agent Tidak Terpetakan",
    DATA_GAP_ASCEND: "Data Ascend Kosong",
}

#: Urutan tampil, sekaligus urutan "perbaiki dari sini dulu".
DATA_GAP_ORDER = (DATA_GAP_TRANSCRIPT, DATA_GAP_TMS, DATA_GAP_AGENT, DATA_GAP_ASCEND)


def data_gap_reasons(gaps) -> str:
    """Kalimat gabungan untuk kolom AI Status, mis. "Data TMS Kosong · Data Ascend
    Kosong". String kosong bila tidak ada kekurangan."""
    return " · ".join(
        DATA_GAP_REASON[g] for g in DATA_GAP_ORDER if g in (gaps or ())
    )


def data_gap_map(db, results) -> dict:
    """``{str(result.id): (DATA_GAP_*, ...)}`` untuk tiket yang kekurangan data acuan.

    Tiket yang datanya lengkap tidak muncul di hasil sama sekali.

    Empat kekurangan yang diperiksa, berurutan dari yang paling mendasar:

    * ``transcript`` — baris ``results``-nya tidak memuat berkas transkrip apa pun
      (``source_files`` kosong), sehingga ticket id-nya pun tidak bisa diturunkan.
      CATATAN PENTING: ticket id yang transkripnya BELUM PERNAH diunggah tidak punya
      baris ``results`` sama sekali, jadi tidak akan pernah muncul di sini — ia bukan
      "tiket dengan status", melainkan tiket yang belum ada. Kelengkapannya dipantau
      lewat laporan terpisah, bukan lewat AI Status.
    * ``tms``    — tidak ada baris CASHLINE untuk ticket id ini.
    * ``agent``  — baris TMS ADA tetapi ``agent_id``-nya kosong, atau tidak ada di
      database sales yang aktif. Tiketnya tidak punya pemilik: ia jatuh ke simpul
      "(Tidak diketahui)" di pohon hierarki dan tidak terhitung ke siapa pun.
      Sengaja TIDAK diterbitkan saat baris TMS-nya sendiri tidak ada — di situ tidak
      ada field agent yang bisa disebut "tidak terpetakan", hanya barisnya yang hilang.
    * ``ascend`` — baris CARD HOLDER tidak ketemu, sehingga SELURUH acuan card holder
      dikirim ke LLM sebagai ``null`` dan verifikasi statik/dinamis tidak punya
      pembanding. Ikut terbit saat baris TMS tidak ada — tanpa cashline, App A tidak
      punya ``no-ktpkitas`` untuk mencocokkan card holder-nya.

    [FIX] Sumbernya ``crud.reference_snapshot_index()`` — snapshot ``reference_data``
    yang PERSIS dikirim ke LLM saat evaluasi — BUKAN tabel ``tms_cashline`` /
    ``ascend_custp``, yang sudah tidak diisi lagi sejak reference data pindah ke DWH
    API. Query lama tetap jalan tanpa error tetapi selalu nol baris, jadi SETIAP tiket
    akan dilaporkan "Data TMS Kosong" + "Data Ascend Kosong" dan dipaksa PENDING.
    Membaca snapshot juga membuat status di layar tidak mungkin berbeda dari acuan
    yang benar-benar dipakai LLM.

    Tiket yang result_json-nya belum memuat ``reference_data`` (dievaluasi sebelum
    snapshot mulai disisipkan) TIDAK dilaporkan kekurangan apa pun: acuannya tidak
    pernah diperiksa, dan menuduhnya kosong akan menggeser status tiket lama.
    """
    cid_by_rid, out = {}, {}
    for r in results or []:
        cid = (_customer_id(r.source_files) or "").strip()
        if cid:
            cid_by_rid[str(r.id)] = cid
        else:
            # Tanpa berkas transkrip tidak ada ticket id yang bisa diturunkan, jadi
            # TMS/agent/Ascend pun tidak bisa dicek — cukup laporkan pangkalnya.
            out[str(r.id)] = (DATA_GAP_TRANSCRIPT,)
    if not cid_by_rid:
        return out

    rows = crud.reference_snapshot_index(db, set(cid_by_rid.values()))
    sales = active_sales_map(db)

    for rid, cid in cid_by_rid.items():
        row = rows.get(cid)
        if row is None:
            continue  # snapshot reference_data belum ada -> belum bisa dinilai
        gaps = []
        if not row["cashline"]:
            # Tanpa baris TMS tidak ada no-ktpkitas, jadi acuan card holder-nya pasti
            # kosong juga — keduanya dilaporkan supaya jelas dua data yang harus
            # dilengkapi.
            gaps = [DATA_GAP_TMS, DATA_GAP_ASCEND]
        else:
            agent_id = row["agent_id"] or ""
            if not agent_id or agent_id.casefold() not in sales:
                gaps.append(DATA_GAP_AGENT)
            if not row["customer"]:
                gaps.append(DATA_GAP_ASCEND)
        if gaps:
            out[rid] = tuple(g for g in DATA_GAP_ORDER if g in gaps)
    return out


def _result_ai_status(result_json, appeals, qc_request, missing_docs=False,
                      doc_sla_expired=True, doc_status=None, *,
                      data_gap=None) -> "str | None":
    """AI status untuk sebuah result. Nilai:
      'PASS'    -> Qualified
      'FAIL'    -> Not Qualified
      'PENDING' -> butuh unggah dokumen & MASIH dalam tenggat H+2 (belum diunggah).

    Urutan keputusan (aturan 7 Agustus 2026):

    0. **Vonis human yang SUDAH DISETUJUI mengunci AI Status.** Begitu Manual Status
       ditetapkan dan di-approve, AI Status mengikutinya dan berhenti dihitung ulang —
       error code yang ditambah/diubah/dihapus SESUDAH itu tetap diterapkan seperti
       biasa ke skor, tabel Error Code, dan Ringkasan Kategori, tetapi TIDAK boleh lagi
       menggeser AI Status karena statusnya sudah di-override manusia.
    1. skor deterministik (banding error code yang disetujui sudah diterapkan);
    2. veto non-tolerable — item ``tolerable=NO`` yang BELUM_SESUAI memaksa FAIL;
    3. dokumen kurang DAN tidak ada pelanggaran non-tolerable: dalam tenggat H+2 ->
       PENDING, lewat tenggat -> FAIL;
    4. konsistensi verifikasi statik & badword -> FAIL;
    5. ``data_gap`` -> PENDING (28 Agustus 2026), menimpa aturan 2, 3, dan 4 — hanya
       aturan 0 (vonis human) yang masih mengalahkannya.

    Akibat aturan 2-5, PENDING hanya bisa dicapai lewat DUA jalan:
      * ada kekurangan data acuan (transkrip / TMS / agent / Ascend); atau
      * tidak ada pelanggaran non-tolerable, tetapi ada dokumen wajib yang belum
        diunggah dan tenggat H+2-nya belum lewat.

    Usulan Manual Status yang masih MENUNGGU atau DITOLAK bukan vonis, jadi tidak
    mengunci apa pun — lihat ``manual_status_of``.

    Override diperiksa PALING AWAL, sebelum "tiket ini bisa dinilai atau tidak":
    aturannya tidak bersyarat ("kalau Manual Status di-approve, AI Status mengikuti"),
    dan tiket yang sengaja divonis manusia tidak masuk akal dilaporkan tanpa status
    hanya karena evaluasinya tidak terbaca.

    None bila tidak bisa ditentukan. ``missing_docs`` = the customer's TMS data changed
    (atau limit >= 50jt) but no supporting document was uploaded. ``doc_sla_expired`` =
    tenggat H+2 unggah dokumen sudah lewat (default True → perilaku lama FAIL; callers
    yang punya submit_time mengoper nilai sebenarnya agar status PENDING muncul).

    ``data_gap`` = kekurangan data acuan tiket ini, TUPLE dari ``DATA_GAP_*`` (lihat
    ``data_gap_map``); None/kosong bila datanya lengkap. Sebuah tiket bisa membawa
    lebih dari satu, dan semuanya membuahkan vonis yang sama: PENDING. Keyword-only
    supaya 12 pemanggil lama yang mengoper argumen secara posisi tidak diam-diam
    salah memetakannya."""
    override = manual_status_of(qc_request)
    if override:
        return override
    evaluation = _adjusted_evaluation(result_json, appeals, doc_status)
    if evaluation is None:
        return None
    status = base_ai_status(evaluation)
    if status is None:
        return None
    # non-tolerable veto — TANPA syarat status sebelumnya (28 Agustus 2026): sekali
    # ada item ``tolerable=NO`` yang BELUM_SESUAI, tiketnya Not Qualified, berapa pun
    # skornya dan apa pun vonis awal LLM-nya.
    blocking = has_blocking_intolerable_item(evaluation)
    if blocking:
        status = "FAIL"
    # missing-documents: dalam tenggat H+2 -> PENDING; lewat tenggat -> FAIL.
    #
    # ``and not blocking`` (28 Agustus 2026): tiket yang sudah kena pelanggaran
    # non-tolerable TIDAK BOLEH singgah di PENDING. Menunggu dokumen hanya masuk akal
    # kalau vonisnya masih bisa berubah oleh dokumen itu; pelanggaran non-tolerable
    # sudah terbukti dari transkrip dan tidak akan gugur oleh KTP/NPWP yang menyusul.
    if missing_docs and not blocking:
        status = "FAIL" if doc_sla_expired else "PENDING"
    # KONSISTENSI VERIFIKASI STATIK (10 Agustus 2026) — sesudah aturan dokumen karena
    # ia harus MENIMPA PENDING: tiket yang jawabannya berubah-ubah tidak boleh menunggu
    # dokumen, apa pun kekurangan dokumennya. Gugur di TAHAP 1 verifikasi statik
    # (konsistensi antar-penyebutan < 90%) = Not Qualified, titik.
    # (Sejak 28 Agustus 2026 tidak lagi yang paling akhir: aturan Ascend kosong di
    # bawah menimpanya — lihat penjelasan di sana.)
    if static_consistency_failures(evaluation):
        status = "FAIL"
    # BADWORD (13 Agustus 2026) — sejajar dengan aturan konsistensi di atas dan karenanya juga
    # sesudah aturan dokumen: agent yang mengucapkan kalimat bersentimen negatif
    # kepada nasabah membuat tiketnya Not Qualified, tanpa singgah di PENDING.
    if has_badword(evaluation):
        status = "FAIL"
    # KEKURANGAN DATA ACUAN (28 Agustus 2026) — PALING AKHIR, jadi menang atas SEMUA
    # aturan di atas: veto non-tolerable, FAIL-karena-tenggat dokumen, konsistensi
    # verifikasi statik, dan badword. Alasannya sama untuk keempat jenisnya: skor,
    # daftar item BELUM_SESUAI, maupun temuan verifikasi yang memicu aturan-aturan itu
    # lahir dari evaluasi yang acuannya tidak lengkap. Lihat ``data_gap_map``.
    #
    # Vonisnya PENDING, bukan FAIL: data yang hilang bisa dilengkapi lalu tiketnya
    # diproses ulang. Menghukum agent atas data yang memang tidak pernah dikirim ke
    # sistem bukan penilaian, melainkan salah alamat. Karena itu ini juga SATU-SATUNYA
    # jalan sebuah tiket bisa PENDING sambil membawa pelanggaran yang terbukti.
    #
    # Satu-satunya yang masih mengalahkannya adalah vonis human yang sudah di-approve
    # (aturan 0 di paling atas fungsi ini).
    if data_gap:
        status = "PENDING"
    return status


def ai_status_for_result(db, result) -> "str | None":
    """AI Status SATU tiket, dihitung dengan bahan yang sama dengan daftar Results
    (evaluasi + banding error code + kekurangan dokumen + tenggat H+2).

    Dipakai di luar agregasi — ``api/routers/qc_status.py`` membandingkan vonis yang
    diajukan QC dengan AI Status untuk memutuskan perlu-tidaknya approval hierarki.
    Sengaja memanggil ``_result_ai_status`` yang sama alih-alih mempercayai nilai
    kiriman browser: kalau nilainya bisa dikarang klien, alur banding bisa dilewati
    hanya dengan mengubah satu field di request.
    """
    rid = str(result.id)
    eval_by_id = crud.result_json_map(db, [rid])
    appeals = crud.error_code_appeals_for_results(db, [rid]).get(rid)
    qc_req = crud.qc_status_requests_for(db, [rid]).get(rid)
    mdocs = _missing_docs_map(db, [result], eval_by_id)
    submit_times = _submit_time_map(db, [result])
    gaps = data_gap_map(db, [result])
    return _result_ai_status(
        eval_by_id.get(rid),
        appeals,
        qc_req,
        mdocs.get(rid, False),
        _doc_sla_expired(submit_times.get(result.id), datetime.now()),
        document_status_map(db, [result]).get(rid),
        data_gap=gaps.get(rid),
    )


def ai_status_map(db, results) -> dict:
    """``str(result_id) -> AI Status`` untuk BANYAK tiket sekaligus.

    Versi berkelompok dari ``ai_status_for_result``, dengan bahan yang sama persis:
    evaluasi + banding + kekurangan dokumen + tenggat H+2 + **status dokumen** +
    kekurangan data acuan. Dipakai setiap penyaring "AI Status" (daftar Results,
    ekspor XLSX tiket, menu Transcripts) supaya SATU tiket tidak bisa dihitung
    berbeda oleh kolomnya dan oleh filternya.

    ``doc_status`` ADALAH BAHAN WAJIB, dan itulah alasan fungsi ini ada. Ketiga
    penyaring itu dulu memanggil ``_result_ai_status`` sendiri-sendiri TANPA
    mengopernya, sehingga baris verifikasi yang sedang menunggu dokumen tidak pernah
    ditangguhkan: item scorecard-nya tetap BELUM_SESUAI, vetonya jalan, dan tiket yang
    di kolomnya tertulis PENDING ikut muncul saat pengguna menyaring "Not Qualified"
    (tiket ``060228OEvG``, 31 Agustus 2026). Kolomnya sendiri sudah benar — hanya
    penyaringnya yang membaca evaluasi yang berbeda.

    ``results`` boleh berisi hasil yang belum ``done``; yang tidak evaluable
    mengembalikan None seperti ``_result_ai_status``.
    """
    rows = list(results or [])
    if not rows:
        return {}
    ids = [str(r.id) for r in rows]
    eval_by_id = crud.result_json_map(db, ids)
    appeals = crud.error_code_appeals_for_results(db, ids)
    qc_map = crud.qc_status_requests_for(db, ids)
    mdocs = _missing_docs_map(db, rows, eval_by_id)
    submit_times = _submit_time_map(db, rows)
    gaps = data_gap_map(db, rows)
    doc_map = document_status_map(db, rows)
    now = datetime.now()
    return {
        rid: _result_ai_status(
            eval_by_id.get(rid),
            appeals.get(rid),
            qc_map.get(rid),
            mdocs.get(rid, False),
            _doc_sla_expired(submit_times.get(r.id), now),
            doc_map.get(rid),
            data_gap=gaps.get(rid),
        )
        for r, rid in ((r, str(r.id)) for r in rows)
    }


def manual_status_of(qc_request) -> "str | None":
    """Manual Status = vonis HUMAN untuk sebuah tiket: 'PASS' | 'FAIL' | 'PENDING',
    atau None bila belum ada vonis yang final.

    Final berarti ``effective_appeal_status`` sudah 'approved' — entah karena usulan
    QC sudah disetujui hierarki, atau karena Team Leader QC / SPQ Head menetapkannya
    langsung (yang memang tidak butuh approval). Usulan yang masih menunggu atau yang
    ditolak BUKAN vonis, jadi mengembalikan None; keadaan alur kerjanya dibaca dari
    ``qc_request`` sendiri (lihat ``manual_review_state``)."""
    if qc_request is None:
        return None
    from compliance.error_codes import effective_appeal_status
    if effective_appeal_status(qc_request) != "approved":
        return None
    value = str(getattr(qc_request, "requested_status", "") or "").strip().upper()
    return value or None


def effective_manual_status(qc_request, ai_status) -> "str | None":
    """Manual Status yang BERLAKU untuk sebuah tiket.

    Aturannya (7 Agustus 2026): sejak tiket pertama kali dibuat, Manual Status
    **mengikuti AI Status** sebagai nilai bawaan; begitu QC / TL QC / SPQ Head
    menetapkan vonisnya dan vonis itu disetujui, vonis human itulah yang dipakai —
    dan sejak saat itu **AI Status ikut mengunci ke nilai yang sama**
    (lihat ``_result_ai_status``). Jadi kolom Manual Status tidak pernah kosong.

    Konsekuensinya kedua kolom TIDAK PERNAH berbeda: sebelum ada vonis human, Manual
    mengikuti AI; sesudah ada, AI mengikuti Manual. Yang membedakan tiket "dinilai
    mesin" dari "dinilai manusia" bukan lagi selisih nilainya, melainkan
    ``manual_status_of`` (ada/tidaknya vonis final) — itulah yang dipakai hitungan
    "ditetapkan human" di Stats dan tombol Set vs Ubah.

    Nilai bawaannya MENGIKUTI (bukan membeku pada) AI Status: kalau AI Status berubah —
    misalnya dokumen akhirnya diunggah sehingga PENDING hilang — Manual Status yang
    belum disentuh human ikut menyesuaikan. Membekukannya di nilai pertama justru
    membuat tiket tampak Pending selamanya padahal dokumennya sudah masuk.
    """
    return manual_status_of(qc_request) or ai_status


def manual_review_state(qc_request, missing_docs: bool = False) -> "str | None":
    """Keadaan ALUR KERJA vonis human (bukan vonisnya):
      'final'    -> sudah ditetapkan/disetujui;
      'menunggu' -> usulan QC belum diputus hierarki, ATAU tiket kurang dokumen
                    sehingga masih perlu diperiksa human;
      'ditolak'  -> usulan QC ditolak hierarki;
      None       -> tidak ada apa-apa yang perlu ditindak.
    Dipakai antrean menu Pending Check."""
    from compliance.error_codes import effective_appeal_status
    if qc_request is not None:
        eff = effective_appeal_status(qc_request)
        if eff == "approved":
            return "final"
        if eff == "rejected":
            return "ditolak"
        return "menunggu"
    return "menunggu" if missing_docs else None


def _normalized_json(result_json):
    """``result_json`` dengan verifikasi statiknya dinormalkan — bentuk yang sama dengan
    yang dibaca seluruh tampilan. Dipakai agar keputusan "dokumen apa yang kurang"
    memakai angka yang sama dengan yang dilihat QC di layar."""
    if not isinstance(result_json, dict):
        return result_json
    evaluation = result_json.get("evaluation")
    if not isinstance(evaluation, dict):
        return result_json
    # Dua normalisasi berurutan: band zona abu-abu (statik) lalu baris dinamis
    # yang salah satu sisi pembandingnya kosong -> SKIPPED_NULL.
    evaluation = normalize_dynamic_verification(normalize_static_verification(evaluation))
    return {**result_json, "evaluation": evaluation}


def document_status_map(db, results) -> dict:
    """``str(result_id) -> (uploaded_types:set, sla_expired:bool)`` — bahan yang
    dibutuhkan ``error_codes.apply_static_document_status`` untuk memutuskan apakah
    baris zona abu-abu berstatus MATCH, PENDING, atau MISMATCH.

    Dokumen berjenis KELIRU tidak dihitung terunggah, sama dengan ``_missing_docs_map``:
    mengunggah KTP ke slot KK tidak membuat KK-nya ada."""
    ids = [str(r.id) for r in results]
    if not ids:
        return {}
    types_by_rid = crud.document_types_by_result(db, ids)
    for rid, docs in crud.document_ocr_by_result(db, ids).items():
        wrong = {dt for dt, ocr in docs if _wrong_document_type(dt, ocr)}
        if wrong:
            types_by_rid[rid] = set(types_by_rid.get(rid, set())) - wrong
    submits = _submit_time_map(db, results)
    now = datetime.now()
    return {
        str(r.id): (set(types_by_rid.get(str(r.id), set())),
                    _doc_sla_expired(submits.get(r.id), now))
        for r in results
    }


def _missing_docs_map(db, results, eval_by_id: dict | None = None) -> dict:
    """``str(result_id) -> True`` when a required supporting document has not been
    uploaded. Two independent sources of requirement:

    1. **TMS data change / limit** — the customer's TMS data changed (Alamat
       Kantor/Rumah, NPWP, NIK) OR the disbursement limit (CUST_CRLIMIT) is >= Rp 50
       juta. Satisfied by ANY uploaded document (unchanged behaviour).
    2. **Card-holder similarity band** (Fase #5) — ``nama_ibu_kandung`` at 80-89%
       asks for a KK, ``tanggal_lahir`` at 87,5-99% asks for a KTP (see
       ``compliance.documents.CARD_HOLDER_DOC_BANDS``). Satisfied only by THAT
       document type, and only for results uploaded at/after the band cutoff.

    Batched; the credit-limit lookup runs only for tickets without a TMS change flag.
    ``eval_by_id`` (``str(result_id) -> result_json``) is looked up when not supplied.

    Dokumen yang jenisnya KELIRU tidak dihitung sebagai terpenuhi (14 Agustus 2026):
    mengunggah KTP ke slot NPWP tidak membuat NPWP-nya ada. Selain menerbitkan C03,
    kewajibannya tetap berdiri — jadi tiket itu tetap PENDING dan, bila tenggat H+2
    lewat, ikut kena B09. Dokumen yang OCR-nya belum/gagal selesai tetap dianggap
    memenuhi seperti sebelumnya; menghukum tiket karena antrean OCR belum jalan
    bukan penilaian atas pekerjaan agent.
    """
    from compliance.documents import card_holder_bands_apply, card_holder_doc_types
    from compliance.reference_data import get_credit_limit, npwp_required_by_limit
    ids = [str(r.id) for r in results]
    if not ids:
        return {}
    types_by_rid = crud.document_types_by_result(db, ids)
    for rid, docs in crud.document_ocr_by_result(db, ids).items():
        wrong = {
            doc_type for doc_type, ocr_json in docs
            if _wrong_document_type(doc_type, ocr_json)
        }
        if wrong:
            types_by_rid[rid] = set(types_by_rid.get(rid, set())) - wrong
    cid_by_rid = {}
    for r in results:
        sf = r.source_files or []
        cid_by_rid[str(r.id)] = sf[0].split("_", 1)[0] if sf and isinstance(sf[0], str) else None
    # Flag perubahan data TMS dibaca dari snapshot ``reference_data`` yang sudah
    # tersimpan di result_json -- satu query SQL untuk semua cid. DWH API hanya
    # ditembak untuk cid yang snapshot-nya belum ada (tiket lama, dievaluasi sebelum
    # snapshot mulai disisipkan). Sebelumnya SEMUA cid lewat HTTP satu per satu:
    # ~22 detik untuk 342 tiket, dan itu yang membuat halaman Statistik timeout.
    wanted = [c for c in cid_by_rid.values() if c]
    flags = crud.cashline_change_flags_index(db, wanted)
    stale = [c for c in dict.fromkeys(wanted) if c not in flags]
    if stale:
        flags.update(crud.get_tms_cashline_change_flags(db, stale))
    banded = [r for r in results if card_holder_bands_apply(getattr(r, "uploaded_at", None))]
    if banded and eval_by_id is None:
        eval_by_id = crud.result_json_map(db, [str(r.id) for r in banded])
    eval_by_id = eval_by_id or {}
    out = {}
    for r in results:
        rid = str(r.id)
        cid = cid_by_rid.get(rid)
        uploaded = types_by_rid.get(rid, set())
        f = flags.get(cid or "", {})
        needs = any(f.get(k) for k in ("kantor", "rumah", "npwp", "nik"))
        if not needs and cid:
            needs = npwp_required_by_limit(get_credit_limit(cid, db))
        missing = bool(needs and not uploaded)
        if not missing and card_holder_bands_apply(getattr(r, "uploaded_at", None)):
            # Band dibaca dari evaluasi yang SUDAH dinormalkan, bukan angka mentah LLM.
            # Sebelum 21 Agustus 2026 fungsi ini membaca ``result_json`` apa adanya,
            # sementara SELURUH tampilan memakai similarity hasil hitung ulang Python —
            # jadi layar bisa berbunyi "57%, perlu verifikasi dokumen KK" (angka
            # ternormalisasi) sementara sistemnya sendiri tidak pernah menganggap KK itu
            # kurang, karena angka mentah LLM-nya 28% MISMATCH. Tiketnya tetap Qualified
            # dan tidak pernah singgah di PENDING. Terjadi pada 38 tiket cashline.
            missing = any(
                dt not in uploaded
                for dt in card_holder_doc_types(_normalized_json(eval_by_id.get(rid)))
            )
        out[rid] = missing
    return out


def compute_scoped_overview(db, customer_ids) -> dict:
    """Overview KPIs (status counts + error rate + donut breakdown) scoped to a set
    of customer/ticket ids — used by the Sales Agent (Team Leader) Statistics page.

    ``customer_ids`` is the list of ticket ids allowed for the login.
    An empty list yields an all-zero overview. Error Rate memakai rumus tunggal
    dashboard — Total Risk (H+M+L) ÷ tiket yang dinilai, lihat ``_rate_of``.
    """
    ids = list(customer_ids or [])
    if not ids:
        return _empty_overview()

    prefix = func.split_part(Result.source_files[0].astext, "_", 1)
    results = exclude_hidden_results(db.query(Result).filter(prefix.in_(ids))).all()

    counts = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
    for r in results:
        if r.status in counts:
            counts[r.status] += 1
    total = len(results)

    done_ids = [r.id for r in results if r.status == "done"]
    eval_by_id: dict = {}
    if done_ids:
        for rid, rjson in (
            db.query(ResultData.result_id, ResultData.result_json)
            .filter(ResultData.result_id.in_(done_ids))
            .order_by(ResultData.created_at.desc())
            .all()
        ):
            eval_by_id.setdefault(str(rid), rjson)
    appeal_map = crud.error_code_appeals_for_results(db, [str(r) for r in done_ids])
    qc_map = crud.qc_status_requests_for(db, [str(r) for r in done_ids])

    done_results = [r for r in results if r.status == "done"]
    mdocs = _missing_docs_map(db, done_results, eval_by_id)
    gaps = data_gap_map(db, done_results)
    submit_times = _submit_time_map(db, done_results)
    _now = datetime.now()
    total_eval = total_err = approve = ret = pending_ai = 0
    m_pass = m_fail = m_pending = m_by_human = 0
    doc_map = document_status_map(db, done_results)
    for rid in done_ids:
        ev = eval_by_id.get(str(rid))
        ap = appeal_map.get(str(rid))
        ds = doc_map.get(str(rid))
        if _adjusted_evaluation(ev, ap, ds) is None:
            continue  # not evaluable — excluded from every rate
        total_eval += 1
        # Error/failed = AI Status RETURN (FAIL), sama dengan donut & KPI.
        ai = _result_ai_status(
            ev, ap, qc_map.get(str(rid)), mdocs.get(str(rid), False),
            _doc_sla_expired(submit_times.get(rid), _now), ds,
            data_gap=gaps.get(str(rid)),
        )
        if ai == "PASS":
            approve += 1
        elif ai == "FAIL":
            ret += 1
        elif ai == "PENDING":
            pending_ai += 1
        total_err += 1 if ai == "FAIL" else 0
        # Manual Status yang BERLAKU: vonis human bila ada, selain itu mengikuti AI.
        _human = manual_status_of(qc_map.get(str(rid)))
        if _human:
            m_by_human += 1
        _m = _human or ai
        if _m == "PASS":
            m_pass += 1
        elif _m == "FAIL":
            m_fail += 1
        elif _m == "PENDING":
            m_pending += 1

    _scoped_risk = risk_tally(db, done_results, eval_by_id, appeal_map)
    return {
        "total_submissions": total,
        "done": counts["done"],
        "processing": counts["processing"],
        "pending": counts["pending"],
        "failed": counts["failed"],
        "evaluated": total_eval,
        "error_count": total_err,
        "total_risk": sum(_scoped_risk[k] for k in _RISK_COUNTED),
        "error_rate": _rate_of(_scoped_risk, total_eval),
        "status_breakdown": {
            "done": counts["done"],
            "in_progress": counts["pending"] + counts["processing"],
            "failed": counts["failed"],
        },
        "ai_status_breakdown": {"approve": approve, "return": ret, "pending": pending_ai},
        "manual_status_breakdown": _manual_breakdown(m_pass, m_fail, m_pending, total_eval, m_by_human),
    }


def compute_failure_reasons(db, campaign: str = None, campaigns: list = None) -> dict:
    """Agregat 'Failure Reason' (menu Stats, tab untuk SPQ Head & Admin): kategori
    scorecard yang PALING SERING gagal beserta alasannya.

    CAKUPAN (28 Agustus 2026): HANYA tiket **Not Qualified**. Tiket Qualified dan
    Pending tidak masuk, baik ke pembilang maupun penyebut. Sebelumnya seluruh tiket
    evaluable ikut, sehingga persentase kategori terlihat kecil semata-mata karena
    diencerkan tiket yang lulus.

    Untuk setiap result 'done' yang evaluable DAN Not Qualified (dengan banding yang
    di-approve sudah diterapkan), ambil item scorecard berstatus BELUM_SESUAI,
    kelompokkan per ``category``. Per kategori dihitung:
      - ``fail_count``  : JUMLAH TIKET yang punya >=1 item gagal di kategori itu;
      - ``pct``         : fail_count / jumlah tiket Not Qualified * 100;
      - ``top_reasons`` : requirement item yang paling sering gagal (+ contoh reason).
    Diurutkan menurun berdasarkan fail_count. Cakupan GLOBAL (semua tiket), kecuali
    bila ``campaign`` diisi — dipakai filter campaign di tab Failure Reason — dan/atau
    ``campaigns`` (batas campaign role, lihat ``done_results_query``).
    ``total_evaluated`` dan ``pct`` ikut mengecil mengikuti filternya, sehingga
    persentasenya tetap relatif terhadap tiket yang benar-benar ditampilkan.

    ``not_qualified`` — jumlah tiket AI Status FAIL, definisi yang SAMA dengan KPI
    di Overview. Sejak cakupannya dipersempit, angkanya identik dengan
    ``total_evaluated``; keduanya tetap dikirim supaya dashboard lama tidak pecah.

    ``total_submissions`` — SELURUH tiket evaluable (sebelum penyaringan Not
    Qualified). Dikirim sebagai konteks populasi supaya KPI bisa berbunyi "9 dari 98";
    ia BUKAN penyebut ``pct``, yang tetap memakai jumlah tiket Not Qualified."""
    done_results = done_results_query(db, campaign, campaigns).all()
    result_ids = [r.id for r in done_results]
    eval_by_id: dict = {}
    if result_ids:
        for rid, rjson in (
            db.query(ResultData.result_id, ResultData.result_json)
            .filter(ResultData.result_id.in_(result_ids))
            .order_by(ResultData.created_at.desc())
            .all()
        ):
            eval_by_id.setdefault(str(rid), rjson)
    appeal_map = crud.error_code_appeals_for_results(db, [str(r) for r in result_ids])
    qc_map = crud.qc_status_requests_for(db, [str(r) for r in result_ids])
    mdocs = _missing_docs_map(db, done_results, eval_by_id)
    gaps = data_gap_map(db, done_results)
    submit_times = _submit_time_map(db, done_results)
    _now = datetime.now()

    total_eval = 0
    total_submissions = 0                              # SELURUH tiket evaluable (penyebut konteks)
    not_qualified = 0
    cat_fail_tickets: dict = defaultdict(int)          # category -> jumlah tiket gagal
    cat_reason_counts: dict = defaultdict(lambda: defaultdict(int))  # category -> requirement -> n
    cat_reason_example: dict = defaultdict(dict)       # category -> requirement -> contoh reason
    doc_map = document_status_map(db, done_results)
    for r in done_results:
        rid = str(r.id)
        ds = doc_map.get(rid)
        ev = _adjusted_evaluation(eval_by_id.get(rid), appeal_map.get(rid), ds)
        if ev is None:
            continue
        # ``total_submissions`` dihitung SEBELUM gerbang di bawah: KPI-nya memberi
        # konteks "9 dari 98", tanpa itu pembaca tidak tahu populasi asalnya.
        total_submissions += 1
        # AI Status dihitung dengan helper kanonik (bukan dari ``ev`` yang sudah
        # disesuaikan) supaya angkanya identik dengan KPI Overview.
        # Sejak 28 Agustus 2026 tab ini HANYA memuat tiket Not Qualified: tiket
        # Qualified & Pending dilewati sebelum penyebut dinaikkan, sehingga
        # ``total_evaluated`` = jumlah tiket Not Qualified dan ``pct`` terbaca
        # "berapa persen tiket GAGAL yang gagal di kategori ini" — bukan lagi
        # diencerkan tiket yang lulus.
        if _result_ai_status(
            eval_by_id.get(rid), appeal_map.get(rid), qc_map.get(rid),
            mdocs.get(rid, False), _doc_sla_expired(submit_times.get(r.id), _now), ds,
            data_gap=gaps.get(rid),
        ) != "FAIL":
            continue
        total_eval += 1
        not_qualified += 1
        failures = _scorecard_failures(ev)
        for cat, req, reason in failures:
            cat_reason_counts[cat][req] += 1
            if reason and req not in cat_reason_example[cat]:
                cat_reason_example[cat][req] = reason
        for cat in {c for c, _req, _reason in failures}:
            cat_fail_tickets[cat] += 1

    categories = []
    for cat, fails in cat_fail_tickets.items():
        top = sorted(cat_reason_counts[cat].items(), key=lambda kv: kv[1], reverse=True)[:3]
        categories.append({
            "category": cat,
            "fail_count": fails,
            "pct": _rate(fails, total_eval),
            "top_reasons": [
                {
                    "requirement": req,
                    "count": n,
                    "example": cat_reason_example[cat].get(req, ""),
                }
                for req, n in top
            ],
        })
    categories.sort(key=lambda c: (c["fail_count"], c["category"]), reverse=True)
    return {
        # Seluruh tiket evaluable — konteks populasi, BUKAN penyebut ``pct``.
        "total_submissions": total_submissions,
        "total_evaluated": total_eval,
        "not_qualified": not_qualified,
        "categories": categories,
    }


# --- Failure Reason per hierarki (Area Manager -> Team Leader -> Agent) -----
# Sub-tab "Hierarki Based" pada tab Failure Reason. Pertanyaannya berbeda dengan
# agregat di atas: bukan "kategori apa yang paling sering gagal SECARA KESELURUHAN",
# melainkan "kegagalan terbesar ORANG INI ada di kategori apa" — jadi kategorinya
# dihitung ulang per simpul hierarki, bukan dipecah dari angka global.

_FAIL_TOP_CATEGORIES = 5  # kategori teratas yang dikirim per simpul
_FAIL_TOP_REASONS = 3     # requirement teratas per kategori


# Nama kategori scorecard yang diperjelas saat DITAMPILKAN. Kategori "Verifikasi"
# hanya berisi item STATIK (SC_CL_23_1 tanggal lahir, SC_CL_23_2 nama ibu kandung);
# verifikasi dinamis sudah punya kategorinya sendiri ("Verifikasi Dinamis", SC_CL_24),
# jadi nama polos "Verifikasi" menyesatkan. Diganti di sini, BUKAN di scorecard/KB:
# `result_json` tiket lama menyimpan "Verifikasi" apa adanya, sehingga mengganti di
# sumber akan memecah agregat menjadi dua kategori (lama vs baru). Dashboard sudah
# memakai istilah yang sama pada rincian pengurangan skor (VERIF_ITEM_CATEGORY di
# EvaluationView.vue).
CATEGORY_DISPLAY = {"Verifikasi": "Verifikasi Statik"}

#: Kategori scorecard yang BUKAN sebuah ``conversation_phase`` KB, dipetakan ke fase
#: induknya. "Verifikasi Dinamis" adalah turunan fase "Verifikasi" (di dashboard
#: bernama "Verifikasi Statik"), jadi kolomnya duduk TEPAT SESUDAH induknya — bukan
#: terlempar ke ekor tabel seperti kategori tak dikenal lainnya.
CATEGORY_SIBLINGS = {"Verifikasi Statik": ["Verifikasi Dinamis"]}


def category_label(category: str) -> str:
    return CATEGORY_DISPLAY.get(category, category)


def _phase_order_from_kb(kb_text: str) -> list:
    """Nama fase percakapan sesuai URUTAN blok ``conversation_phases`` di KB.

    KB dikirim ke LLM sebagai teks mentah dan TIDAK dijamin JSON yang sah (lihat
    catatan di ``campaign_cashline/``), jadi blok ini dipindai manual: cari kuncinya,
    lalu telusuri karakter demi karakter sambil melacak status string/escape dan
    kedalaman kurung supaya deskripsi yang memuat tanda kutip atau kurung tidak
    mengacaukan hitungan. Kunci pada kedalaman 1 = nama fase.

    Kembalikan ``[]`` bila bloknya tidak ada atau tidak terbaca — pemanggil harus
    tetap bekerja tanpa urutan dari KB.
    """
    if not kb_text:
        return []
    anchor = kb_text.find('"conversation_phases"')
    if anchor < 0:
        return []
    start = kb_text.find("{", anchor)
    if start < 0:
        return []
    phases, depth, i = [], 0, start
    in_str, escaped, buf, key_start = False, False, [], False
    while i < len(kb_text):
        ch = kb_text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
                if key_start and depth == 1:
                    phases.append("".join(buf))
                buf = []
            elif key_start:
                buf.append(ch)
        elif ch == '"':
            in_str, buf = True, []
            key_start = depth == 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return phases


def failure_category_columns(db, campaign: str = None, campaigns: list = None,
                             seen=()) -> list:
    """Urutan KOLOM kategori untuk tab Failure Reason.

    Sumber utamanya ``conversation_phases`` di KB campaign yang sedang dilihat —
    itulah urutan fase percakapan sebenarnya (Greeting -> Probing -> ... -> Legal
    Statement), jauh lebih terbaca daripada urutan abjad atau urutan jumlah kegagalan
    yang berubah-ubah tiap hari.

    Fase dipetakan lewat ``category_label`` (``Verifikasi`` -> ``Verifikasi Statik``),
    lalu turunannya disisipkan lewat ``CATEGORY_SIBLINGS``. Kategori yang muncul di
    data tetapi tidak dikenal KB (``seen``) tetap DITAMBAHKAN di ekor — lebih baik
    tampil di kolom terakhir daripada hilang diam-diam dari laporan.
    """
    from db.models import Campaign

    q = db.query(Campaign).filter(Campaign.is_active.is_(True))
    rows = q.order_by(Campaign.id).all()
    wanted = None
    if campaign:
        wanted = {campaign.strip().casefold()}
    elif campaigns:
        wanted = {str(c).strip().casefold() for c in campaigns if str(c).strip()}

    order: list = []
    for row in rows:
        if wanted is not None and (row.name or "").strip().casefold() not in wanted:
            continue
        for phase in _phase_order_from_kb(row.kb_text or ""):
            label = category_label(phase.strip())
            if label and label not in order:
                order.append(label)
                for sibling in CATEGORY_SIBLINGS.get(label, ()):
                    if sibling not in order:
                        order.append(sibling)
    for cat in seen:
        if cat and cat not in order:
            order.append(cat)
    return order


def _scorecard_failures(evaluation) -> list:
    """``[(category, requirement, reason)]`` untuk tiap item scorecard BELUM_SESUAI.

    ``category`` sudah memakai nama tampilan (lihat ``CATEGORY_DISPLAY``)."""
    out = []
    for it in (evaluation.get("scorecard_result") or []):
        if (it.get("status") or "").upper() != "BELUM_SESUAI":
            continue
        cat = category_label((it.get("category") or "").strip()) or _UNKNOWN
        req = (it.get("requirement") or it.get("item_code") or "(tanpa requirement)").strip()
        out.append((cat, req, (it.get("reason") or "").strip()))
    return out


def _scorecard_failure_codes(evaluation) -> list:
    """``item_code`` tiap item scorecard BELUM_SESUAI, urut sesuai scorecard & unik.

    Dipakai daun per-tiket di sub-tab Hierarki Based: pengawas ingin tahu ITEM mana
    yang gagal (SC_CL_26), bukan sekadar fase percakapannya — nama fase sudah tampil
    sebagai kolom di baris yang sama, jadi mengulangnya sebagai teks tidak menambah
    informasi apa pun.
    """
    out, seen = [], set()
    for it in (evaluation.get("scorecard_result") or []):
        if (it.get("status") or "").upper() != "BELUM_SESUAI":
            continue
        code = (it.get("item_code") or "").strip()
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _new_fail_acc() -> dict:
    return {
        "evaluated": 0,
        "fail_tickets": 0,
        "cat_tickets": defaultdict(int),
        "cat_reqs": defaultdict(lambda: defaultdict(int)),
        "cat_examples": defaultdict(dict),
    }


def _fail_acc_add(acc: dict, failures: list) -> None:
    """Masukkan SATU tiket yang sudah dinilai ke akumulator sebuah simpul."""
    acc["evaluated"] += 1
    cats_here = {cat for cat, _req, _reason in failures}
    if cats_here:
        acc["fail_tickets"] += 1
    for cat in cats_here:
        acc["cat_tickets"][cat] += 1
    for cat, req, reason in failures:
        acc["cat_reqs"][cat][req] += 1
        if reason and req not in acc["cat_examples"][cat]:
            acc["cat_examples"][cat][req] = reason


def _fail_node(acc: dict, limit: "int | None" = _FAIL_TOP_CATEGORIES,
               with_reasons: bool = True, **extra) -> dict:
    """Bentuk simpul untuk payload: ringkasan + kategori terbesar milik simpul itu.

    ``pct`` memakai penyebut simpul itu sendiri (tiket yang dinilai untuk orang ini),
    bukan total global — kalau tidak, seorang agent dengan 10 tiket akan selalu
    tampak "kecil" dibanding areanya dan sub-tab ini kehilangan gunanya.

    ``limit`` = berapa kategori teratas yang dikirim; ``None`` = semuanya. Simpul
    AM/TL cukup 5 teratas (isinya hanya ringkasan chip), sedangkan simpul AGENT
    dikirim lengkap karena barisnya bisa dibuka menjadi tabel rincian per kategori.

    ``with_reasons=False`` mengosongkan ``top_reasons``: rincian per SALES AGENT
    berhenti di tingkat KATEGORI dan tidak boleh menyebut requirement KB-nya. Karena
    itu teksnya tidak dikirim sama sekali, bukan sekadar disembunyikan di UI."""
    total = acc["evaluated"]
    cats = []
    for cat, fails in acc["cat_tickets"].items():
        top = sorted(acc["cat_reqs"][cat].items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
        cats.append({
            "category": cat,
            "fail_count": fails,
            "pct": _rate(fails, total),
            "top_reasons": [
                {"requirement": req, "count": n, "example": acc["cat_examples"][cat].get(req, "")}
                for req, n in top[:_FAIL_TOP_REASONS]
            ] if with_reasons else [],
        })
    cats.sort(key=lambda c: (c["fail_count"], c["category"]), reverse=True)
    return {
        "evaluated": total,
        "fail_tickets": acc["fail_tickets"],
        "fail_rate": _rate(acc["fail_tickets"], total),
        "categories": cats if limit is None else cats[:limit],
        # Peta kategori -> jumlah tiket, SELALU lengkap (tidak ikut dipotong ``limit``).
        # Inilah yang memberi makan kolom per fase percakapan di tab Failure Reason;
        # ``categories`` di atas tetap ada untuk daftar "Alasan Teratas".
        "cat_counts": {c: n for c, n in acc["cat_tickets"].items()},
        **extra,
    }


def compute_failure_reasons_hierarchy(db, campaign: str = None, campaigns: list = None) -> dict:
    """Failure Reason yang dipecah menurut hierarki sales: Area Manager -> Team
    Leader -> Agent, masing-masing dengan kategori scorecard yang paling sering
    gagal DI SIMPUL ITU.

    Bahan, cakupan, dan definisi "gagal"-nya sama persis dengan
    ``compute_failure_reasons`` — termasuk pembatasan 28 Agustus 2026 bahwa HANYA
    tiket **Not Qualified** yang ikut dihitung — supaya kedua sub-tab tidak pernah
    saling membantah. Yang berbeda hanya pengelompokannya.

    Karena pembatasan itu, ``evaluated`` di tiap simpul berarti "tiket Not Qualified
    milik simpul itu", dan ``fail_rate`` = berapa persen di antaranya yang punya >=1
    item scorecard BELUM_SESUAI. Kurang dari 100% berarti sisanya gagal lewat jalur
    lain (verifikasi data, dokumen kurang) yang tidak punya kategori scorecard.

    Satu tiket milik TEPAT SATU agent, jadi angka TL/AM/keseluruhan adalah penjumlahan
    tiket yang sama tanpa risiko dihitung ganda — tiap tiket dimasukkan ke keempat
    akumulator (agent, TL, AM, all) dalam satu lintasan.

    ``campaign`` (pilihan pemakai) & ``campaigns`` (batas campaign role) bekerja sama
    seperti pada ``compute_failure_reasons`` — lihat ``done_results_query``.
    """
    done_results = done_results_query(db, campaign, campaigns).all()
    result_ids = [r.id for r in done_results]
    eval_by_id: dict = {}
    if result_ids:
        for rid, rjson in (
            db.query(ResultData.result_id, ResultData.result_json)
            .filter(ResultData.result_id.in_(result_ids))
            .order_by(ResultData.created_at.desc())
            .all()
        ):
            eval_by_id.setdefault(str(rid), rjson)
    appeal_map = crud.error_code_appeals_for_results(db, [str(r) for r in result_ids])
    # Bahan penentu AI Status — dibutuhkan karena sub-tab ini kini HANYA memuat tiket
    # Not Qualified, dan vonisnya harus dihitung dengan helper kanonik yang sama
    # dengan Overview (bukan dari ``ev`` yang sudah disesuaikan banding).
    qc_map = crud.qc_status_requests_for(db, [str(r) for r in result_ids])
    mdocs = _missing_docs_map(db, done_results, eval_by_id)
    gaps = data_gap_map(db, done_results)
    submit_times = _submit_time_map(db, done_results)
    _now = datetime.now()

    # cid -> agent_id, lalu agent_id -> nama/TL/AM (database sales).
    # [FIX] Sumbernya crud.cashline_agent_index() (snapshot reference_data di
    # result_json), BUKAN tabel tms_cashline yang sudah tidak terisi sejak reference
    # data pindah ke DWH API — query lama membuat SELURUH pohon hierarki di sub-tab
    # ini jatuh ke "(Tidak diketahui)". Sejalan dengan compute_stats_snapshot dan
    # compute_team_agents yang sudah lebih dulu dialihkan.
    cids = {(c or "").strip() for c in (_customer_id(r.source_files) for r in done_results) if c}
    agent_by_cid: dict = {}
    if cids:
        index = crud.cashline_agent_index(db)
        for key in cids:
            entry = index.get(key)
            if entry is None:
                continue
            agent_by_cid[key] = (entry.get("agent_id") or "").strip() or None
    sales_map = active_sales_map(db)

    total_submissions = 0                              # SELURUH tiket evaluable (konteks KPI)
    grand = _new_fail_acc()
    am_acc: dict = defaultdict(_new_fail_acc)          # am -> acc
    tl_acc: dict = defaultdict(_new_fail_acc)          # (am, tl) -> acc
    agent_acc: dict = defaultdict(_new_fail_acc)       # (am, tl, akey) -> acc
    # Daun ke-4: rincian per TIKET, supaya terlihat tiket mana yang gagal di fase mana.
    agent_ticket_rows: dict = defaultdict(list)        # (am, tl, akey) -> [node]
    agent_meta: dict = {}
    doc_map = document_status_map(db, done_results)
    for r in done_results:
        rid = str(r.id)
        ds = doc_map.get(rid)
        ev = _adjusted_evaluation(eval_by_id.get(rid), appeal_map.get(rid), ds)
        if ev is None:
            continue  # tidak evaluable — dikecualikan dari setiap penyebut
        total_submissions += 1                         # dihitung SEBELUM gerbang FAIL
        if _result_ai_status(
            eval_by_id.get(rid), appeal_map.get(rid), qc_map.get(rid),
            mdocs.get(rid, False), _doc_sla_expired(submit_times.get(r.id), _now), ds,
            data_gap=gaps.get(rid),
        ) != "FAIL":
            continue  # Qualified & Pending tidak masuk sub-tab ini
        failures = _scorecard_failures(ev)
        cid = _customer_id(r.source_files)
        agent_id = agent_by_cid.get((cid or "").strip()) if cid else None
        entry = sales_map.get(agent_id.casefold()) if agent_id else None
        akey = agent_id or _UNKNOWN
        am = (entry.get("area_manager") if entry else None) or _UNKNOWN
        tl = (entry.get("team_leader") if entry else None) or _UNKNOWN
        agent_meta.setdefault((am, tl, akey), {
            "agent_id": agent_id,
            "name": (entry.get("name") if entry else None) or _agent_name_fallback(agent_id) or _UNKNOWN,
        })
        for acc in (grand, am_acc[am], tl_acc[(am, tl)], agent_acc[(am, tl, akey)]):
            _fail_acc_add(acc, failures)
        # Satu tiket = satu akumulator mini, lalu dibentuk lewat ``_fail_node`` yang
        # sama dengan simpul di atasnya. Itu yang menjamin kolomnya dihitung dengan
        # aturan identik dan tidak melenceng kalau definisinya diubah lagi nanti.
        _tacc = _new_fail_acc()
        _fail_acc_add(_tacc, failures)
        agent_ticket_rows[(am, tl, akey)].append({
            "ticket_id": (cid or "").strip() or _UNKNOWN,
            # Item scorecard yang gagal, apa adanya (SC_CL_26, ...). Kolom kategori di
            # baris yang sama sudah menyebut FASE-nya, jadi teks pendamping ticket id
            # menyebut ITEM — dua tingkat kedalaman berbeda, bukan pengulangan.
            "item_codes": _scorecard_failure_codes(ev),
            **_fail_node(_tacc, limit=None, with_reasons=False),
        })

    area_managers = []
    for am, acc in am_acc.items():
        tl_list = []
        for (am_key, tl), tacc in tl_acc.items():
            if am_key != am:
                continue
            agents = [
                # limit=None: SEMUA kategori gagalnya dikirim — bukan hanya 5 teratas
                # seperti AM/TL. with_reasons=False: rincian per sales agent berhenti
                # di tingkat KATEGORI, requirement KB tidak sampai ke sisi sales.
                # ``tickets``: daun ke-4, satu baris per ticket id milik agent itu,
                # diurutkan yang paling banyak fase gagalnya di atas.
                _fail_node(aacc, limit=None, with_reasons=False,
                           agent_id=agent_meta[key]["agent_id"], name=agent_meta[key]["name"],
                           tickets=sorted(
                               agent_ticket_rows.get(key, []),
                               key=lambda t: (-len(t["cat_counts"]), t["ticket_id"]),
                           ))
                for key, aacc in agent_acc.items() if key[0] == am and key[1] == tl
            ]
            agents.sort(key=lambda a: (a["fail_tickets"], a["evaluated"]), reverse=True)
            tl_list.append(_fail_node(tacc, name=tl, agents=agents))
        tl_list.sort(key=lambda t: (t["fail_tickets"], t["evaluated"]), reverse=True)
        area_managers.append(_fail_node(acc, name=am, team_leaders=tl_list))
    area_managers.sort(key=lambda a: (a["fail_tickets"], a["evaluated"]), reverse=True)

    return {
        # Seluruh tiket evaluable — konteks populasi untuk KPI, BUKAN penyebut mana pun
        # di dalam pohon (tiap simpul memakai jumlah tiket Not Qualified miliknya).
        "total_submissions": total_submissions,
        "total_evaluated": grand["evaluated"],
        # Urutan kolom kategori = urutan fase percakapan di KB, supaya tabelnya
        # terbaca mengikuti alur telepon (Greeting -> Probing -> ... -> Legal
        # Statement) alih-alih urutan abjad atau urutan jumlah kegagalan.
        "categories_order": failure_category_columns(
            db, campaign, campaigns, seen=sorted(grand["cat_tickets"])
        ),
        "all_telesales": _fail_node(grand, name="All Telesales"),
        "area_managers": area_managers,
    }


# --- Export agregat verifikasi (XLSX) --------------------------------------
# Satu kategori verifikasi, semua tiket yang Not Qualified / Pending. Dipakai
# SPQ Head & Admin untuk menindaklanjuti satu jenis temuan sekaligus, alih-alih
# membuka tiket satu per satu di menu Results.

_VERIF_STATIC_FIELDS = tuple(CARD_HOLDER_STATIC_SCORECARD)  # tanggal_lahir, nama_ibu_kandung

# key -> label, blok evaluasi, penyaring field, dan judul kolom acuannya.
VERIFICATION_EXPORT_CATEGORIES: dict = {
    "verifikasi_statik": {
        "label": "Verifikasi Statik",
        "block": "card_holder_verification",
        "fields": _VERIF_STATIC_FIELDS,
        "reference_label": "Ascend",
    },
    "verifikasi_dinamik": {
        "label": "Verifikasi Dinamik",
        "block": "card_holder_verification",
        "fields": CARD_HOLDER_DYNAMIC_FIELDS,
        "reference_label": "Ascend",
    },
    "cashline_verification": {
        "label": "Cashline Verification",
        "block": "cashline_data_verification",
        "fields": None,  # semua field
        "reference_label": "TMS",
        "with_tnc": True,
    },
    "cardholder_verification": {
        "label": "Cardholder Verification",
        "block": "card_holder_verification",
        "fields": None,  # statik + dinamik
        "reference_label": "Ascend",
    },
}

# Status tiket yang diekspor: yang masih perlu ditindaklanjuti.
_VERIF_EXPORT_STATUSES = ("FAIL", "PENDING")

# XLSX dibaca manusia, bukan mesin: tulis vonisnya dengan kata yang sama dengan yang
# terlihat di dashboard, bukan kode internal PASS/FAIL. (Cermin `aiStatusLabel` di
# dashboard/src/utils/aiStatus.js.)
AI_STATUS_LABELS = {"PASS": "Qualified", "FAIL": "Not Qualified", "PENDING": "Pending"}


def ai_status_label(status) -> str:
    return AI_STATUS_LABELS.get(status, status)


def _titleize(field) -> str:
    return str(field or "").replace("_", " ").title()


def _transcript_cell(v: dict):
    """Isi kolom "Transkrip": SEMUA penyebutan nasabah bila ada (verifikasi statik
    boleh diulang), selain itu nilai tunggal yang dipakai untuk pencocokan.

    Beberapa penyebutan ditulis bernomor dan dipisah baris baru dalam satu sel, agar
    tetap satu baris per parameter di XLSX."""
    from compliance.static_similarity import mention_rows

    def _line(r, i=None):
        # Nama PDF ikut ditulis sejak prompt v56: saringan tanggal membuat "ucapan
        # yang mana, di panggilan yang mana" jadi penentu, bukan sekadar konteks.
        head = f"{i}. " if i else ""
        ts = f"[{r['timestamp']}] " if r["timestamp"] else ""
        src = f" ({r['source_file']})" if r.get("source_file") else ""
        return f"{head}{ts}{r['value']}{src}"

    rows = mention_rows(v.get("extracted_mentions"))
    if len(rows) > 1:
        return "\n".join(_line(r, i) for i, r in enumerate(rows, 1))
    if rows:
        return _line(rows[0])
    return v.get("extracted_value")


def compute_verification_export(db, category: str, campaign: str = None,
                                campaigns: list = None) -> dict:
    """Baris export untuk SATU kategori verifikasi.

    Cakupannya: tiket ``done`` yang AI Status-nya Not Qualified (FAIL) atau PENDING —
    yang Qualified tidak perlu ditindaklanjuti — dan dari tiap tiket hanya baris
    verifikasi kategori itu yang MISMATCH. Tiket tanpa MISMATCH pada kategori ini
    tidak muncul sama sekali, karena gagalnya ada di tempat lain.

    Evaluasinya sudah menerapkan banding yang di-approve (``_adjusted_evaluation``),
    sama seperti tampilan dashboard, jadi baris yang sudah dimenangkan QC lewat
    banding tidak ikut terekspor.

    Mengembalikan ``{"category", "label", "columns", "rows"}``; ``columns`` adalah
    pasangan (key, judul) sesuai kategori — kolom "Ketentuan Produk" hanya ada pada
    Cashline Verification. Kolom AI Status berisi kata yang dibaca manusia
    (Qualified / Not Qualified / Pending), bukan kode PASS/FAIL.
    """
    conf = VERIFICATION_EXPORT_CATEGORIES.get(category)
    if conf is None:
        raise ValueError(f"kategori export tidak dikenal: {category}")

    done_results = done_results_query(db, campaign, campaigns).all()
    result_ids = [r.id for r in done_results]
    eval_by_id: dict = {}
    if result_ids:
        for rid, rjson in (
            db.query(ResultData.result_id, ResultData.result_json)
            .filter(ResultData.result_id.in_(result_ids))
            .order_by(ResultData.created_at.desc())
            .all()
        ):
            eval_by_id.setdefault(str(rid), rjson)
    appeal_map = crud.error_code_appeals_for_results(db, [str(r) for r in result_ids])
    qc_map = crud.qc_status_requests_for(db, [str(r) for r in result_ids])
    mdocs = _missing_docs_map(db, done_results, eval_by_id)
    gaps = data_gap_map(db, done_results)
    submit_times = _submit_time_map(db, done_results)
    doc_map = document_status_map(db, done_results)
    now = datetime.now()

    fields = conf["fields"]
    rows = []
    for r in done_results:
        rid = str(r.id)
        raw = eval_by_id.get(rid)
        ds = doc_map.get(rid)
        ev = _adjusted_evaluation(raw, appeal_map.get(rid), ds)
        if ev is None:
            continue
        # _result_ai_status menerima result_json MENTAH (ia menyesuaikan bandingnya
        # sendiri); mengoper evaluasi yang sudah disesuaikan membuatnya balas None.
        ai = _result_ai_status(
            raw, appeal_map.get(rid), qc_map.get(rid), mdocs.get(rid, False),
            _doc_sla_expired(submit_times.get(r.id), now), ds,
            data_gap=gaps.get(rid),
        )
        if ai not in _VERIF_EXPORT_STATUSES:
            continue
        ticket_id = _customer_id(r.source_files) or rid
        for v in (ev.get(conf["block"]) or []):
            if not isinstance(v, dict):
                continue
            if fields is not None and v.get("field") not in fields:
                continue
            # Hanya MISMATCH. SKIPPED_NULL = acuan banknya memang kosong — tidak
            # dihitung sebagai pelanggaran di mana pun (tidak jadi error code, tidak
            # mengurangi skor), jadi memasukkannya di sini hanya menenggelamkan
            # temuan sungguhan: pada kategori dinamik jumlahnya berkali-kali lipat.
            match = (v.get("match") or "").upper()
            if match != "MISMATCH":
                continue
            row = {
                "ticket_id": ticket_id,
                "campaign": (r.campaign or "").strip(),
                "ai_status": ai_status_label(ai),
                "parameter": _titleize(v.get("field")),
                # Verifikasi statik boleh diulang; tanpa daftar penyebutannya, alasan
                # "tidak konsisten antar pengulangan" tidak bisa diperiksa dari file.
                # `extracted_mentions` baru ada sejak prompt v47 — hasil lama jatuh ke
                # extracted_value (lihat _transcript_cell).
                "transkrip": _transcript_cell(v),
                "reference": v.get("reference_value"),
                "similarity": v.get("similarity_percent"),
                "match": match or None,
                "reason": v.get("reason"),
            }
            if conf.get("with_tnc"):
                row["tnc_product"] = v.get("tnc_product")
            rows.append(row)

    rows.sort(key=lambda x: (x["ticket_id"], x["parameter"]))
    columns = [
        ("ticket_id", "Ticket ID"),
        ("campaign", "Campaign"),
        ("ai_status", "AI Status"),
        ("parameter", "Parameter"),
        ("transkrip", "Transkrip"),
        ("reference", conf["reference_label"]),
    ]
    if conf.get("with_tnc"):
        columns.append(("tnc_product", "Ketentuan Produk"))
    columns += [
        ("similarity", "% Similarity"),
        ("match", "Match"),
        ("reason", "Reason"),
    ]
    return {"category": category, "label": conf["label"], "columns": columns, "rows": rows}


# --- Export SEMUA tiket pada satu rentang (XLSX) ----------------------------
# Pengganti tombol Export Agregat bagi SPQ Head (14 Agustus 2026). Bedanya dengan
# compute_verification_export: yang itu menjawab "tunjukkan semua temuan pada SATU
# kategori verifikasi", yang ini "berikan SELURUH tiket pada periode ini" — apa pun
# statusnya, Qualified sekalipun, satu baris per tiket.

TICKET_EXPORT_COLUMNS = [
    ("ticket_id", "Ticket ID"),
    ("tanggal", "Tanggal"),
    ("campaign", "Campaign"),
    ("agent", "Agent"),
    ("team_leader", "Team Leader"),
    ("area_manager", "Area Manager"),
    ("ai_status", "AI Status"),
    ("manual_status", "Manual Status"),
    ("passing_grade", "Passing Grade"),
    ("error_code", "Error Code"),
    ("risk_base", "Risk Base"),
    ("details_error", "Details Error"),
]


def compute_ticket_export(db, results) -> dict:
    """Satu baris per tiket untuk ``results`` (hasil ``crud.list_results``).

    Penyaringan — rentang tanggal, campaign, cakupan role — sudah dikerjakan oleh
    pemanggil lewat query yang SAMA dengan yang mengisi tabel Results, supaya isi
    file persis sama dengan yang sedang dilihat di layar. Fungsi ini hanya
    memperkaya tiap tiket dengan angka yang butuh evaluasi.

    Tiga kolom terakhir memampatkan tabel Error Code sebuah tiket ke satu baris:
      * ``error_code``   — semua kode yang terbit, dipisah koma;
      * ``risk_base``    — risk base yang BENAR-BENAR dihitung tiket ini (satu
                           tertinggi, sudah termasuk pelunakan new joiner dan
                           pengecualian L-tolerable), jadi angkanya sepakat dengan
                           Error Rate di Stats. Kosong = tiket ini tidak menyumbang
                           Total Risk;
      * ``details_error``— deskripsi tiap kode, dipisah baris baru.

    ``Tanggal`` adalah ``tms_cashline.submit_time`` — dasar yang sama dengan filter
    tanggal di Results, supaya isi file cocok dengan rentang yang diminta. Tiket
    tanpa baris cashline memakai ``generated_at``.
    """
    results = list(results or [])
    if not results:
        return {"columns": TICKET_EXPORT_COLUMNS, "rows": []}

    done_results = [r for r in results if r.status == "done"]
    result_ids = [r.id for r in done_results]
    eval_by_id: dict = {}
    if result_ids:
        for rid, rjson in (
            db.query(ResultData.result_id, ResultData.result_json)
            .filter(ResultData.result_id.in_(result_ids))
            .order_by(ResultData.created_at.desc())
            .all()
        ):
            eval_by_id.setdefault(str(rid), rjson)
    appeal_map = crud.error_code_appeals_for_results(db, [str(r) for r in result_ids])
    qc_map = crud.qc_status_requests_for(db, [str(r) for r in result_ids])
    mdocs = _missing_docs_map(db, done_results, eval_by_id)
    gaps = data_gap_map(db, done_results)
    submit_by_rid, agent_by_rid = _submit_agent_map(db, done_results)
    docs_by_rid = crud.document_ocr_by_result(db, [str(r.id) for r in done_results])
    snap_doc_map = document_status_map(db, done_results)
    sales_map = active_sales_map(db)
    verif_doc_map = document_status_map(db, results)
    now = datetime.now()

    rows = []
    for r in results:
        rid = str(r.id)
        raw = eval_by_id.get(rid)
        appeals = appeal_map.get(rid)
        overdue = mdocs.get(rid, False) and _doc_sla_expired(submit_by_rid.get(r.id), now)
        ai = _result_ai_status(raw, appeals, qc_map.get(rid), mdocs.get(rid, False), overdue,
                               # doc_status WAJIB: tanpa itu baris yang menunggu dokumen
                               # tidak ditangguhkan dan tiket PENDING terbaca FAIL.
                               verif_doc_map.get(rid),
                               data_gap=gaps.get(rid))
        agent_id = agent_by_rid.get(r.id)
        entry = sales_map.get(agent_id.casefold()) if agent_id else None
        code_rows = _error_code_rows(raw, appeals, overdue, docs_by_rid.get(rid, ())) or []
        if _is_new_joiner(submit_by_rid.get(r.id), agent_id, sales_map):
            code_rows = override_risk_base_for_new_joiner(code_rows)
        codes = [c for c in (row.get("error_code") for row in code_rows) if c]
        details = [d for d in (row.get("details_error") for row in code_rows) if d]
        evaluation = _adjusted_evaluation(raw, appeals)
        rows.append({
            "ticket_id": _customer_id(r.source_files) or rid,
            "tanggal": submit_by_rid.get(r.id) or (
                r.generated_at.isoformat(sep=" ") if r.generated_at else None
            ),
            "campaign": (r.campaign or "").strip(),
            "agent": (entry.get("name") if entry else None) or agent_id or _UNKNOWN,
            "team_leader": (entry.get("team_leader") if entry else None) or _UNKNOWN,
            "area_manager": (entry.get("area_manager") if entry else None) or _UNKNOWN,
            "ai_status": ai_status_label(ai) if ai else None,
            "manual_status": ai_status_label(manual_status_of(qc_map.get(rid))) or None,
            "passing_grade": (evaluation or {}).get("passing_grade"),
            "error_code": ", ".join(dict.fromkeys(codes)),
            "risk_base": top_risk_base(code_rows, raw) or "",
            "details_error": "\n".join(dict.fromkeys(details)),
        })
    rows.sort(key=lambda x: (str(x["tanggal"] or ""), x["ticket_id"]))
    return {"columns": TICKET_EXPORT_COLUMNS, "rows": rows}


# --- Satu parameter verifikasi statik, SEMUA tiket -------------------------
# Dipakai endpoint /get_nama_ibu_kandung: menarik hasil pencocokan nama ibu kandung
# apa adanya (MATCH maupun MISMATCH) untuk dianalisis di luar dashboard.

NAMA_IBU_KANDUNG_FIELD = "nama_ibu_kandung"
NAMA_IBU_KANDUNG_ITEM_CODE = CARD_HOLDER_STATIC_SCORECARD[NAMA_IBU_KANDUNG_FIELD]  # SC_CL_23_2


def _pdf_stem(name) -> "str | None":
    """Nama file tanpa direktori & ekstensi ``.pdf`` — bentuk baku ticket id
    ``<customer id>_<timestamp>``."""
    if not isinstance(name, str) or not name.strip():
        return None
    base = name.strip().rsplit("/", 1)[-1]
    return base[:-4] if base.casefold().endswith(".pdf") else base


def _pdf_ticket_ids(source_files) -> list:
    """Ticket id lengkap tiap file sumber, urut seperti ``source_files``."""
    return [s for s in (_pdf_stem(f) for f in (source_files or [])) if s]


def _scorecard_evidence(evaluation: dict, item_code: str) -> "dict | None":
    """``evidence`` (``{quote, timestamp, ticket_id}``) milik satu item scorecard.

    None bila itemnya tidak ada atau evidence-nya bukan objek — hasil lama sempat
    menuliskannya sebagai string kosong."""
    for it in (evaluation.get("scorecard_result") or []):
        if not isinstance(it, dict):
            continue
        if (it.get("item_code") or "").strip().upper() != item_code:
            continue
        ev = it.get("evidence")
        return ev if isinstance(ev, dict) else None
    return None


def compute_nama_ibu_kandung_rows(db, campaigns: list = None) -> list:
    """Satu baris per tiket ``done`` yang punya verifikasi ``nama_ibu_kandung``.

    Tanpa penyaring apa pun: Qualified, Not Qualified, dan Pending semuanya ikut, dan
    baris MATCH tidak dibuang — berbeda dengan ``compute_verification_export`` yang
    memang hanya menyiapkan bahan tindak lanjut. Tiket non-Cashline tersaring dengan
    sendirinya karena tidak punya blok ``card_holder_verification``.

    Evaluasinya lewat ``_adjusted_evaluation``, jadi similarity-nya sudah dihitung
    ulang di Python (``normalize_static_verification``, penyebutan TERBAIK) dan banding
    yang sudah disetujui sudah diterapkan — angka & vonis yang sama dengan yang dilihat
    QC di dashboard, bukan angka mentah LLM.

    ``ticket_id`` memakai id file PDF (``<customer id>_<timestamp>``) dan diambil dari
    ``source_files``, BUKAN langsung dari ``evidence.ticket_id``: LLM kadang menuliskan
    id nasabahnya saja tanpa timestamp ("021001zJFa"). ``evidence.ticket_id`` tetap
    dipakai bila cocok dengan salah satu file sumber — pada tiket multi-transkrip
    itulah satu-satunya penunjuk file mana yang memuat kutipannya; selain itu dipakai
    file sumber pertama.

    ``campaigns`` = batas campaign role (``None`` = tanpa batas), lihat
    ``done_results_query``.
    """
    done_results = done_results_query(db, campaigns=campaigns).all()
    result_ids = [r.id for r in done_results]
    eval_by_id: dict = {}
    if result_ids:
        for rid, rjson in (
            db.query(ResultData.result_id, ResultData.result_json)
            .filter(ResultData.result_id.in_(result_ids))
            .order_by(ResultData.created_at.desc())
            .all()
        ):
            eval_by_id.setdefault(str(rid), rjson)
    appeal_map = crud.error_code_appeals_for_results(db, [str(r) for r in result_ids])
    submit_times = _submit_time_map(db, done_results)
    doc_map = document_status_map(db, done_results)

    rows = []
    for r in done_results:
        rid = str(r.id)
        ev = _adjusted_evaluation(eval_by_id.get(rid), appeal_map.get(rid),
                                  doc_map.get(rid))
        if ev is None:
            continue
        evidence = _scorecard_evidence(ev, NAMA_IBU_KANDUNG_ITEM_CODE)
        tickets = _pdf_ticket_ids(r.source_files)
        quoted = _pdf_stem((evidence or {}).get("ticket_id"))
        ticket_id = (quoted if quoted in tickets else None) or (tickets[0] if tickets else rid)
        for v in (ev.get("card_holder_verification") or []):
            if not isinstance(v, dict) or v.get("field") != NAMA_IBU_KANDUNG_FIELD:
                continue
            rows.append({
                "ticket_id": ticket_id,
                "submit_time": submit_times.get(r.id),
                "ascend": v.get("reference_value"),
                # Semua penyebutan bila nasabah menyebut lebih dari sekali —
                # tanpa itu alasan "tidak konsisten" tak bisa diperiksa dari baris ini.
                "transkrip": _transcript_cell(v),
                "match": (v.get("match") or "").upper() or None,
                "evidence": evidence,
                "similarity": v.get("similarity_percent"),
                "reason": v.get("reason"),
            })

    rows.sort(key=lambda x: x["ticket_id"] or "")
    return rows


# --- Approve/Return time series (100% stacked column chart) -----------------

_TIMESERIES_GRANULARITIES = ("daily", "weekly", "monthly", "quarterly", "semester", "yearly")
_MONTHS_ABBR = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
# Label MINGGUAN memakai nama bulan penuh berbahasa Inggris — "W1 July 2026" —
# atas permintaan bisnis 31 Agustus 2026. Sumbu harian/bulanan tetap memakai
# singkatan Indonesia di atas; keduanya sengaja tidak disamakan.
_MONTHS_FULL_EN = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
# Safety cap so a pathological absolute range (e.g. daily over many years) can't
# emit thousands of columns; keep the most recent buckets.
_MAX_TIMESERIES_BUCKETS = 500


def _wib_date(dt):
    """WIB calendar date for a naive-UTC ``uploaded_at`` (None if missing)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(_WIB).date()


def _wib_today() -> date:
    return datetime.now(timezone.utc).astimezone(_WIB).date()


def _series_date(r):
    """Calendar date used to place a result on the AI-status chart's x-axis WHEN the
    ticket has no ``submit_time`` (see ``_submit_date_map`` / ``_chart_date``).

    Prefers the transcript's wall-clock ``generated_at`` (naive/local — used as-is,
    no UTC→WIB shift, so a 22:58 stamp stays on its own day); falls back to the WIB
    date of ``uploaded_at`` when the ticket has no parsed ``Generated`` timestamp."""
    if getattr(r, "generated_at", None) is not None:
        return r.generated_at.date()
    return _wib_date(r.uploaded_at)


def _submit_date_map(db, results) -> dict:
    """Map ``Result.id -> date`` hasil parse ``submit_time`` (tanggal pengajuan
    pencairan). Hanya result yang submit_time-nya terbaca di snapshot reference_data
    yang muncul. This is the PRIMARY chart x-axis basis, replacing ``generated_at``."""
    # [FIX] Ikut memakai ``_submit_time_map`` (sumber: snapshot reference_data lewat
    # ``crud.cashline_agent_index``) alih-alih query ``tms_cashline`` sendiri — tabel
    # itu sudah tidak terisi, dan menyatukan sumbernya membuat kedua peta tidak mungkin
    # berbeda.
    out = {}
    for rid, submit_time in _submit_time_map(db, results).items():
        d = _parse_submit_date(submit_time)
        if d is not None:
            out[rid] = d
    return out


def _parse_ymd(value):
    """Parse a 'YYYY-MM-DD' string into a date (None if missing/invalid)."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _sub_months(d: date, n: int) -> date:
    """First day of the month ``n`` months before ``d``."""
    total = d.year * 12 + (d.month - 1) - n
    y, m = divmod(total, 12)
    return date(y, m + 1, 1)


def _add_months(d: date, n: int) -> date:
    """``d`` moved by ``n`` months (n may be negative), keeping the day-of-month but
    clamping to the target month's length (e.g. Jan 31 + 1mo → Feb 28/29)."""
    total = d.year * 12 + (d.month - 1) + n
    y, m = divmod(total, 12)
    m += 1
    first_next = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    last_day = (first_next - timedelta(days=1)).day
    return date(y, m, min(d.day, last_day))


def _week_of_month(d: date) -> int:
    """Minggu ke berapa dalam BULANNYA (1..5), dihitung dari tanggal.

    Tanggal 1-7 = W1, 8-14 = W2, 15-21 = W3, 22-28 = W4, 29-31 = W5. Sengaja BUKAN
    minggu ISO (Senin-Minggu): aturan bisnis 31 Agustus 2026 meminta sumbu mingguan
    dibaca sebagai "W1 Jul 2026 … W4 Jul 2026", dan minggu ISO memotong bulan —
    minggu yang membentang dari 29 Juni ke 5 Juli tidak punya satu nama bulan.
    Dengan pembagian per tanggal, satu bucket TIDAK PERNAH melintasi bulan.
    """
    return (d.day - 1) // 7 + 1


def _wom_start(d: date) -> date:
    """Tanggal pertama bucket minggu-dalam-bulan yang memuat ``d`` (1, 8, 15, 22, 29)."""
    return date(d.year, d.month, ((d.day - 1) // 7) * 7 + 1)


def _wom_end(start: date) -> date:
    """Tanggal terakhir bucket yang dimulai di ``start`` — dipotong akhir bulan,
    sehingga W5 bisa berisi 1-3 hari saja (29-31)."""
    y, m = start.year, start.month
    first_next = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    last_day = (first_next - timedelta(days=1)).day
    return date(y, m, min(start.day + 6, last_day))


def _shift_wom(d: date, n: int) -> date:
    """Awal bucket minggu-dalam-bulan sejauh ``n`` bucket dari bucket yang memuat
    ``d`` (``n`` negatif = mundur). Melangkah bucket demi bucket, bukan per 7 hari,
    karena panjang bucket terakhir tiap bulan tidak selalu 7 hari."""
    cur = _wom_start(d)
    for _ in range(abs(n)):
        cur = (
            _wom_start(cur - timedelta(days=1)) if n < 0
            else _wom_start(_wom_end(cur) + timedelta(days=1))
        )
    return cur


def _shift_anchor(anchor: date, granularity: str, offset: int) -> date:
    """Move the window ``anchor`` by ``offset`` whole windows, for the chart's
    Prev/Next paging. Window widths mirror ``_default_timeseries_range`` (daily 7d,
    weekly 4wk, monthly 4mo, quarterly 4q, semester 4sem, yearly 4y). ``offset`` < 0
    pages toward older data, > 0 toward newer."""
    if not offset:
        return anchor
    if granularity == "daily":
        return anchor + timedelta(days=7 * offset)
    if granularity == "weekly":
        return _shift_wom(anchor, 4 * offset)
    if granularity == "quarterly":
        return _add_months(anchor, 12 * offset)
    if granularity == "semester":
        return _add_months(anchor, 24 * offset)
    if granularity == "yearly":
        return _add_months(anchor, 48 * offset)
    # monthly (default)
    return _add_months(anchor, 4 * offset)


def _bucket_of(d: date, granularity: str):
    """(key, label) for the bucket that WIB date ``d`` falls into. ``key`` sorts
    chronologically as a string within a granularity; ``label`` is for the axis."""
    if granularity == "daily":
        return d.isoformat(), f"{d.day} {_MONTHS_ABBR[d.month - 1]}"
    if granularity == "weekly":
        # Minggu DALAM BULAN, bukan minggu ISO — lihat ``_week_of_month``. Kuncinya
        # tetap terurut sebagai string karena bulan ditulis dua digit.
        w = _week_of_month(d)
        return (
            f"{d.year}-{d.month:02d}-W{w}",
            f"W{w} {_MONTHS_FULL_EN[d.month - 1]} {d.year}",
        )
    if granularity == "quarterly":
        q = (d.month - 1) // 3 + 1
        return f"{d.year}-Q{q}", f"Q{q} {d.year}"
    if granularity == "semester":
        s = 1 if d.month <= 6 else 2
        return f"{d.year}-S{s}", f"S{s} {d.year}"
    if granularity == "yearly":
        return str(d.year), str(d.year)
    # monthly (default)
    return f"{d.year}-{d.month:02d}", f"{_MONTHS_ABBR[d.month - 1]} {d.year}"


def _enumerate_buckets(start: date, end: date, granularity: str):
    """Ordered, de-duplicated (key, label) buckets covering [start, end] inclusive.
    Iterates day-by-day so one bucket mapping drives every granularity."""
    out = []
    seen = set()
    d = start
    step = timedelta(days=1)
    while d <= end:
        key, label = _bucket_of(d, granularity)
        if key not in seen:
            seen.add(key)
            out.append((key, label))
        d += step
    return out


def _default_timeseries_range(granularity: str, anchor: date):
    """Default [start, end] window when no explicit dates are given, all ending at
    ``anchor`` (the latest transcript/upload date in scope): daily→7 days,
    weekly→4 weeks, monthly→4 months, quarterly→4 quarters, semester→4 semesters,
    yearly→4 years. Each window starts at the beginning of the period containing
    ``anchor`` and steps back to give the requested number of buckets."""
    if granularity == "daily":
        return anchor - timedelta(days=6), anchor
    if granularity == "weekly":
        # 4 bucket minggu-dalam-bulan terakhir, berakhir di bucket yang memuat anchor.
        return _shift_wom(anchor, -3), anchor
    if granularity == "quarterly":
        q_start_month = ((anchor.month - 1) // 3) * 3 + 1
        return _sub_months(date(anchor.year, q_start_month, 1), 9), anchor
    if granularity == "semester":
        s_start_month = 1 if anchor.month <= 6 else 7
        return _sub_months(date(anchor.year, s_start_month, 1), 18), anchor
    if granularity == "yearly":
        return date(anchor.year - 3, 1, 1), anchor
    # monthly (default): last 4 months
    return _sub_months(anchor, 3), anchor


def compute_ai_status_timeseries(db, customer_ids, campaign, granularity, start, end, offset=0) -> dict:
    """Approve/Return counts bucketed over time (WIB ``uploaded_at``) for the 100%
    stacked column chart on the Statistics page.

    ``customer_ids`` = None -> global (every result); a list -> scoped to those
    ticket ids (Sales Agent / Team Leader / Area Manager). ``campaign`` optionally
    restricts to one campaign. ``granularity`` is one of daily/weekly/monthly/
    quarterly/semester/yearly. ``start``/``end`` are 'YYYY-MM-DD' strings (WIB); when
    absent a per-granularity default window is used, anchored to the latest transcript
    date in scope. ``offset`` pages that default window by whole windows (0 = latest,
    -1 = one window older, +1 = newer); ignored when ``start``/``end`` are given.
    Empty buckets are kept
    (approve=return=0) so the time axis stays continuous. AI status is computed per
    result (``_result_ai_status``), identical to the KPIs/donut it replaces."""
    g = granularity if granularity in _TIMESERIES_GRANULARITIES else "monthly"

    # --- select the results in scope (all statuses — Total Submissions counts every
    # ticket in the window, not just the evaluated ones) ---
    if customer_ids is None:
        results = exclude_hidden_results(db.query(Result)).all()
    elif not customer_ids:
        results = []
    else:
        prefix = func.split_part(Result.source_files[0].astext, "_", 1)
        results = (
            exclude_hidden_results(db.query(Result))
            .filter(prefix.in_(list(customer_ids)))
            .all()
        )
    if campaign:
        want = str(campaign).strip().lower()
        results = [r for r in results if str(r.campaign or "").strip().lower() == want]

    # Chart x-axis date = ``submit_time`` (disbursement submission date); fall
    # back to the transcript date (generated_at, else uploaded_at) only when a ticket
    # has no submit_time in the reference-data snapshot.
    submit_dates = _submit_date_map(db, results)

    def _chart_date(r):
        return submit_dates.get(r.id) or _series_date(r)

    # Anchor the default window to the latest chart date in scope (the newest
    # _chart_date across the selected results), so the last bucket always carries
    # data; fall back to WIB today when the scope is empty.
    series_dates = [d for r in results if (d := _chart_date(r)) is not None]
    anchor = max(series_dates) if series_dates else _wib_today()

    start_d = _parse_ymd(start)
    end_d = _parse_ymd(end)
    if start_d is None or end_d is None:
        # Prev/Next paging shifts the default window by whole windows — only in pure
        # default mode (no manual date bound); an explicit start/end takes precedence.
        anchor_eff = anchor
        if start_d is None and end_d is None:
            try:
                off = int(offset)
            except (TypeError, ValueError):
                off = 0
            anchor_eff = _shift_anchor(anchor, g, off)
        d_start, d_end = _default_timeseries_range(g, anchor_eff)
        start_d = start_d or d_start
        end_d = end_d or d_end
    if start_d > end_d:
        start_d, end_d = end_d, start_d

    # keep only results whose chart date (submit_time, else transcript date)
    # is inside the window
    in_range = []
    for r in results:
        d = _chart_date(r)
        if d is not None and start_d <= d <= end_d:
            in_range.append((r, d))

    # only "done" tickets carry an evaluation → feed approve/return
    done_ids = [r.id for r, _ in in_range if r.status == "done"]
    eval_by_id: dict = {}
    if done_ids:
        for rid, rjson in (
            db.query(ResultData.result_id, ResultData.result_json)
            .filter(ResultData.result_id.in_(done_ids))
            .order_by(ResultData.created_at.desc())
            .all()
        ):
            eval_by_id.setdefault(str(rid), rjson)
    appeal_map = crud.error_code_appeals_for_results(db, [str(r) for r in done_ids])
    qc_map = crud.qc_status_requests_for(db, [str(r) for r in done_ids])
    done_in_range = [r for r, _ in in_range if r.status == "done"]
    mdocs = _missing_docs_map(db, done_in_range, eval_by_id)
    gaps = data_gap_map(db, done_in_range)
    submit_times = _submit_time_map(db, done_in_range)
    _now = datetime.now()

    # New joiner, tolerable, dan kesesuaian jenis dokumen: bahan tally Risk Base
    # per bucket (indeks 10).
    _, agent_by_rid = _submit_agent_map(db, done_in_range)
    docs_by_rid = crud.document_ocr_by_result(db, [str(r.id) for r in done_in_range])
    base_doc_map = document_status_map(db, done_in_range)
    sales_map = active_sales_map(db)

    # bucket key -> [approve, return, submissions, done, in_progress, pending,
    #                m_approve, m_return, m_pending, m_by_human, total_risk]
    # Indeks 6-9 adalah sisi MANUAL STATUS: dihitung di bucket yang sama persis dengan
    # sisi AI supaya kedua grafik bisa ditumpuk berdampingan dan dibandingkan langsung.
    # Indeks 10 adalah Total Risk (H+M+L) — pembilang KPI Error Rate, yang mengikuti
    # rentang tanggal grafik ini dan bukan angka snapshot global.
    counts: dict = {}
    for r, d in in_range:
        key, _ = _bucket_of(d, g)
        c = counts.setdefault(key, [0] * 11)
        c[2] += 1  # submissions — every ticket dated into this bucket
        if r.status == "done":
            c[3] += 1
        elif r.status in ("pending", "processing"):
            c[4] += 1  # in_progress
        if r.status != "done":
            continue
        ev = eval_by_id.get(str(r.id))
        ap = appeal_map.get(str(r.id))
        overdue = mdocs.get(str(r.id), False) and _doc_sla_expired(submit_times.get(r.id), _now)
        code_rows = _error_code_rows(ev, ap, overdue, docs_by_rid.get(str(r.id), ()))
        if code_rows is None:
            continue  # not evaluable — excluded from approve/return, same as the donut/KPIs
        ai = _result_ai_status(
            ev, ap, qc_map.get(str(r.id)), mdocs.get(str(r.id), False), overdue,
            # doc_status WAJIB — lihat catatan yang sama di pemanggil lain.
            base_doc_map.get(str(r.id)),
            data_gap=gaps.get(str(r.id)),
        )
        agent_id = agent_by_rid.get(r.id)
        if _is_new_joiner(submit_times.get(r.id), agent_id, sales_map):
            code_rows = override_risk_base_for_new_joiner(code_rows)
        if top_risk_base(code_rows, ev) in _RISK_COUNTED:
            c[10] += 1
        if ai == "PASS":
            c[0] += 1
        elif ai == "FAIL":
            c[1] += 1
        elif ai == "PENDING":
            c[5] += 1
        # Manual Status: vonis human bila ada, selain itu mengikuti AI Status.
        human = manual_status_of(qc_map.get(str(r.id)))
        if human:
            c[9] += 1
        manual = human or ai
        if manual == "PASS":
            c[6] += 1
        elif manual == "FAIL":
            c[7] += 1
        elif manual == "PENDING":
            c[8] += 1

    _z = [0] * 11
    buckets = [
        {
            "key": key,
            "label": label,
            "approve": counts.get(key, _z)[0],
            "return": counts.get(key, _z)[1],
            "pending": counts.get(key, _z)[5],
            "total_risk": counts.get(key, _z)[10],
            "submissions": counts.get(key, _z)[2],
            "done": counts.get(key, _z)[3],
            "in_progress": counts.get(key, _z)[4],
            # Sisi Manual Status pada bucket yang sama.
            "manual_approve": counts.get(key, _z)[6],
            "manual_return": counts.get(key, _z)[7],
            "manual_pending": counts.get(key, _z)[8],
            "manual_by_human": counts.get(key, _z)[9],
        }
        for key, label in _enumerate_buckets(start_d, end_d, g)
    ]
    if len(buckets) > _MAX_TIMESERIES_BUCKETS:
        buckets = buckets[-_MAX_TIMESERIES_BUCKETS:]

    return {"granularity": g, "start": start_d.isoformat(), "end": end_d.isoformat(), "buckets": buckets}


def compute_team_agents(db, agent_ids) -> list:
    """Per-agent roster + stats for a Team Leader's (or Area Manager's) Statistics table.

    ``agent_ids`` = the USER IDs (casefold) of the agents in scope — under this TL
    (``cashline_agent_ids_for_tl``) or, for an Area Manager, every agent across all
    their team leaders (``cashline_agent_ids_for_am``). Returns one row per agent —
    INCLUDING agents with no tickets yet — each with {agent_id, name, nip_baru,
    team_leader, submissions, errors, total_risk, error_rate}; ``team_leader`` lets
    the Area Manager roster group/label by TL. ``submissions`` counts EVALUATED
    tickets (done + usable evaluation); ``errors`` = tickets with AI Status RETURN
    (FAIL), dipertahankan sebagai informasi.

    ``error_rate`` = Total Failure ÷ submissions, lihat ``_rate_of``. Sejak
    28 Agustus 2026 ``H/M/L/N/O`` HANYA dihitung dari tiket Not Qualified dan
    menghitung SEMUA risk base tiap tiket, sama dengan pohon global. ``total_risk`` ikut dikembalikan supaya
    ``compute_scoped_hierarchy`` bisa menjumlahkannya per Team Leader tanpa
    menghitung ulang tiketnya.
    """
    ids = [str(a).strip().casefold() for a in (agent_ids or []) if str(a).strip()]
    if not ids:
        return []
    sales_map = active_sales_map(db)

    # customer_id -> agent_id, dibatasi ke agen dalam scope. Sumbernya
    # crud.cashline_agent_index() (snapshot reference_data di result_json),
    # BUKAN lagi tabel tms_cashline yang sudah tidak terisi.
    id_set = set(ids)
    cid_to_agent: dict = {}
    for cid, entry in crud.cashline_agent_index(db).items():
        aid = (entry["agent_id"] or "").casefold()
        if cid and aid in id_set:
            cid_to_agent[cid] = aid

    _zero_agent = {"submissions": 0, "transcripts": 0, "errors": 0,
                   "H": 0, "M": 0, "L": 0, "N": 0, "O": 0}
    per_agent = defaultdict(lambda: dict(_zero_agent))
    if cid_to_agent:
        prefix = func.split_part(Result.source_files[0].astext, "_", 1)
        results = exclude_hidden_results(
            db.query(Result).filter(prefix.in_(list(cid_to_agent.keys())))
        ).all()
        done_ids, result_agent = [], {}
        for r in results:
            sf = r.source_files or []
            cid = sf[0].split("_", 1)[0] if sf and isinstance(sf[0], str) else None
            aid = cid_to_agent.get((cid or "").strip())
            if not aid:
                continue
            result_agent[str(r.id)] = aid
            if r.status == "done":
                done_ids.append(r.id)

        eval_by_id: dict = {}
        if done_ids:
            for rid, rjson in (
                db.query(ResultData.result_id, ResultData.result_json)
                .filter(ResultData.result_id.in_(done_ids))
                .order_by(ResultData.created_at.desc())
                .all()
            ):
                eval_by_id.setdefault(str(rid), rjson)
        appeal_map = crud.error_code_appeals_for_results(db, [str(r) for r in done_ids])
        qc_map = crud.qc_status_requests_for(db, [str(r) for r in done_ids])
        done_results = [r for r in results if r.status == "done"]
        _result_by_id = {str(r.id): r for r in done_results}
        team_doc_map = document_status_map(db, done_results)
        mdocs = _missing_docs_map(db, done_results, eval_by_id)
        gaps = data_gap_map(db, done_results)
        submit_by_rid = _submit_time_map(db, done_results)
        docs_by_rid = crud.document_ocr_by_result(db, [str(r.id) for r in done_results])
        _now = datetime.now()
        # submissions = evaluated (evaluable done) tickets; errors = AI Status RETURN;
        # H/M/L/N/O = satu risk base tertinggi per tiket (pembilang Error Rate).
        for rid in done_ids:
            ev = eval_by_id.get(str(rid))
            ap = appeal_map.get(str(rid))
            rows = _error_code_rows(
                ev, ap,
                mdocs.get(str(rid), False)
                and _doc_sla_expired(submit_by_rid.get(rid), _now),
                docs_by_rid.get(str(rid), ()),
            )
            if rows is None:
                continue  # not evaluable
            aid = result_agent.get(str(rid))
            if not aid:
                continue
            per_agent[aid]["submissions"] += 1
            # Transkrip yang DINILAI (panggilan agent lain sudah dibuang) — penyebut
            # Failure Rate di pohon Hierarki Failure Rate versi ter-scope, supaya
            # angkanya sama dengan versi global. Tabel Daftar Sales Agent tetap
            # memakai ``submissions`` (jumlah tiket).
            per_agent[aid]["transcripts"] += _transcript_count(ev, _result_by_id.get(str(rid)))
            # ``doc_sla_expired`` WAJIB dikirim. Nilai bawaannya True, jadi
            # menghilangkannya membuat SETIAP tiket yang dokumennya belum lengkap
            # dianggap sudah lewat tenggat H+2 dan divonis FAIL — padahal jalur
            # global mengirim nilai sebenarnya dan memvonisnya PENDING selama tenggat
            # masih berjalan. Itulah sebabnya pohon Hierarki Failure Rate versi
            # ter-scope (Area Manager / Team Leader) sempat memberi Total Failure
            # yang lebih besar daripada versi global untuk orang yang sama.
            _ai = _result_ai_status(ev, ap, qc_map.get(str(rid)), mdocs.get(str(rid), False),
                                    _doc_sla_expired(submit_by_rid.get(rid), _now),
                                    team_doc_map.get(str(rid)),
                                    data_gap=gaps.get(str(rid)))
            if _ai == "FAIL":
                per_agent[aid]["errors"] += 1
            if _is_new_joiner(submit_by_rid.get(rid), aid, sales_map):
                rows = override_risk_base_for_new_joiner(rows)
            # Sama dengan pohon global: hanya tiket Not Qualified yang menyumbang,
            # dan SEMUA risk base tiket itu dihitung (bukan satu yang tertinggi),
            # supaya Failure Rate versi ter-scope Area Manager / Team Leader memakai
            # pembilang yang sama persis dengan versi global.
            if _ai == "FAIL":
                for _rb, _n in risk_base_tally(rows, ev).items():
                    if _n:
                        per_agent[aid][_rb] += _n

    out = []
    for aid in ids:
        entry = sales_map.get(aid) or {}
        acc = per_agent.get(aid, _zero_agent)
        out.append({
            "agent_id": aid,
            "name": entry.get("name") or aid,
            "nip_baru": entry.get("nip_baru"),
            "team_leader": entry.get("team_leader"),
            "submissions": acc["submissions"],
            "transcripts": acc.get("transcripts", acc["submissions"]),
            "errors": acc["errors"],
            "total_risk": sum(acc[k] for k in _RISK_COUNTED),
            # Avg Failure Rate: Total Failure / Total Recording, ditulis sebagai
            # kelipatan. Penyebutnya angka yang sama dengan kolom "Total Recording"
            # di atas — sama dengan pohon global.
            "error_rate": _avg_of(acc, acc.get("transcripts", acc["submissions"])),
        })
    # Active agents first (by error rate, then volume), then 0-ticket agents by name.
    out.sort(key=lambda a: (a["submissions"] > 0, a["error_rate"], a["submissions"]), reverse=True)
    return out


def compute_scoped_hierarchy(roster) -> dict:
    """Nest a Team-Leader roster (rows from ``compute_team_agents``) into
    Team Leader -> Agent for an **Area Manager**'s scoped "Hierarki Failure Rate".

    Starts at the Team Leader level: there is NO Area Manager node and NO
    "AM (unknown)" bucket (the AM only ever sees the TLs & agents beneath them).
    Each level carries submissions / errors / error_rate, plus an ``all_telesales``
    total over the AM's area. Reuses the roster's per-agent numbers so the tree and
    the "Daftar Sales Agent" table stay consistent — termasuk ``total_risk``, yang
    dijumlahkan ke atas supaya Error Rate tiap simpul memakai rumus yang sama
    dengan pohon global (Total Failure ÷ Submissions, pembilang dari tiket
    Not Qualified saja).
    """
    tls: dict = defaultdict(list)
    for a in roster or []:
        tls[(a.get("team_leader") or "Tanpa Team Leader")].append(a)

    team_leaders = []
    tot_sub = tot_tx = tot_err = tot_risk = 0
    for tl_name, members in tls.items():
        tl_sub = sum(a["submissions"] for a in members)
        # Kolom "Total Recording" = TRANSKRIP yang dinilai (31 Agustus 2026), sama
        # dengan pohon global; jumlah tiketnya tetap dibawa sebagai ``ticket_count``.
        # Angka ini SEKALIGUS penyebut Avg Failure Rate — lihat ``_avg``.
        tl_tx = sum(a.get("transcripts", a["submissions"]) for a in members)
        tl_err = sum(a["errors"] for a in members)
        tl_risk = sum(a.get("total_risk", 0) for a in members)
        tot_sub += tl_sub
        tot_tx += tl_tx
        tot_err += tl_err
        tot_risk += tl_risk
        agents = [
            {
                "agent_id": a["agent_id"],
                "name": a["name"],
                "submissions": a.get("transcripts", a["submissions"]),
                "ticket_count": a["submissions"],
                "errors": a["errors"],
                "total_risk": a.get("total_risk", 0),
                "error_rate": a["error_rate"],
            }
            for a in sorted(members, key=lambda a: (a["error_rate"], a["submissions"]), reverse=True)
        ]
        team_leaders.append({
            "name": tl_name,
            "submissions": tl_tx,
            "ticket_count": tl_sub,
            "errors": tl_err,
            "total_risk": tl_risk,
            "error_rate": _avg(tl_risk, tl_tx),
            "agents": agents,
        })
    team_leaders.sort(key=lambda t: (t["error_rate"], t["submissions"]), reverse=True)

    return {
        "all_telesales": {
            "submissions": tot_tx,
            "ticket_count": tot_sub,
            "errors": tot_err,
            "total_risk": tot_risk,
            "error_rate": _avg(tot_risk, tot_tx),
        },
        "team_leaders": team_leaders,
    }


def compute_qc_performance(db) -> list:
    """Per-QC performance table ("Performa QC") from ticket ASSIGNMENTS: for each QC
    (and QC Support) user, the tickets a Team Leader QC assigned to them, how many are
    EVALUATED, how many are AI Status FAIL, and the error rate. Mirrors
    ``compute_team_agents`` but scoped by QC assignment instead of sales hierarchy.
    QCs with no assignment appear with zeros."""
    assignments = crud.list_qc_assignments(db)
    qc_users = {
        u.username: (u.name or u.username)
        for u in db.query(User).filter(User.role.in_(["qc", "qc_support"]), User.is_active == True).all()  # noqa: E712
    }
    # TEMPORARY supervisor mapping (no stored TL QC -> QC linkage yet): Team Leader QC
    # "Dhea" (NIP 21060814) supervises all real QC/QC Support; test accounts sit under
    # the test Team Leader QC "behati".
    _tlqc_names = {u.username: (u.name or u.username) for u in db.query(User).filter(User.role == "team_leader_qc").all()}
    _TEST_QC, _REAL_TLQC, _TEST_TLQC = {"bella", "doutzen"}, "21060814", "behati"

    def _tlqc_for(qc_username):
        who = _TEST_TLQC if qc_username in _TEST_QC else _REAL_TLQC
        name = _tlqc_names.get(who) or ("Behati Prinsloo" if who == _TEST_TLQC else "DHEA KUMARING TYAS")
        return who, name
    by_qc: dict = defaultdict(list)  # qc_username -> [ticket_id]
    for a in assignments:
        by_qc[a.qc_username].append(a.ticket_id)

    per_ticket: dict = defaultdict(lambda: {"submissions": 0, "errors": 0})
    all_tickets = [a.ticket_id for a in assignments]
    if all_tickets:
        prefix = func.split_part(Result.source_files[0].astext, "_", 1)
        results = exclude_hidden_results(
            db.query(Result).filter(prefix.in_(all_tickets))
        ).all()
        ticket_of, done_ids = {}, []
        for r in results:
            sf = r.source_files or []
            cid = sf[0].split("_", 1)[0] if sf and isinstance(sf[0], str) else None
            if cid:
                ticket_of[str(r.id)] = cid
            if r.status == "done":
                done_ids.append(r.id)
        eval_by_id: dict = {}
        if done_ids:
            for rid, rjson in (
                db.query(ResultData.result_id, ResultData.result_json)
                .filter(ResultData.result_id.in_(done_ids))
                .order_by(ResultData.created_at.desc())
                .all()
            ):
                eval_by_id.setdefault(str(rid), rjson)
        appeal_map = crud.error_code_appeals_for_results(db, [str(r) for r in done_ids])
        qc_map = crud.qc_status_requests_for(db, [str(r) for r in done_ids])
        _done = [r for r in results if r.status == "done"]
        mdocs = _missing_docs_map(db, _done, eval_by_id)
        gaps = data_gap_map(db, _done)
        doc_map = document_status_map(db, _done)
        for rid in done_ids:
            ev = eval_by_id.get(str(rid))
            ap = appeal_map.get(str(rid))
            ds = doc_map.get(str(rid))
            if _adjusted_evaluation(ev, ap, ds) is None:
                continue
            t = ticket_of.get(str(rid))
            if not t:
                continue
            per_ticket[t]["submissions"] += 1
            if _result_ai_status(ev, ap, qc_map.get(str(rid)),
                                 mdocs.get(str(rid), False), True, ds,
                                 data_gap=gaps.get(str(rid))) == "FAIL":
                per_ticket[t]["errors"] += 1

    out = []
    seen = set()
    for qc_username, tickets in by_qc.items():
        seen.add(qc_username)
        sub = sum(per_ticket[t]["submissions"] for t in tickets)
        err = sum(per_ticket[t]["errors"] for t in tickets)
        tlqc_u, tlqc_n = _tlqc_for(qc_username)
        out.append({
            "qc_username": qc_username,
            "name": qc_users.get(qc_username, qc_username),
            "tl_qc_username": tlqc_u,
            "tl_qc_name": tlqc_n,
            "assigned": len(set(tickets)),
            "submissions": sub,
            "errors": err,
            "error_rate": _rate(err, sub),
        })
    for u, n in qc_users.items():  # QCs with no assignment yet
        if u not in seen:
            tlqc_u, tlqc_n = _tlqc_for(u)
            out.append({"qc_username": u, "name": n, "tl_qc_username": tlqc_u,
                        "tl_qc_name": tlqc_n, "assigned": 0,
                        "submissions": 0, "errors": 0, "error_rate": 0.0})
    out.sort(key=lambda a: (a["submissions"] > 0, a["error_rate"], a["submissions"]), reverse=True)
    return out


def _manual_breakdown(m_pass: int, m_fail: int, m_pending: int, evaluated: int,
                     by_human: int = 0) -> dict:
    """Tally Manual Status, sejajar dengan ``ai_status_breakdown``.

    Sejak aturan 7 Agustus 2026 angkanya SELALU sama dengan sisi AI: sebelum ada vonis
    human, Manual mengikuti AI; sesudah vonis disetujui, AI mengikuti Manual. Yang
    membedakan keduanya bukan lagi nilainya, melainkan ``by_human`` = berapa yang
    benar-benar ditetapkan human vs ``default_from_ai`` = sisanya. ``decided`` = jumlah
    tiket yang punya nilai Manual Status (yaitu semua tiket yang dinilai), BUKAN jumlah
    yang diputus human — untuk itu bacalah ``by_human``.

    ``error_rate`` = Not Qualified dibagi tiket yang dinilai (penyebut sama dengan sisi
    AI)."""
    total = m_pass + m_fail + m_pending
    return {
        "approve": m_pass,
        "return": m_fail,
        "pending": m_pending,
        "decided": total,
        "by_human": by_human,
        "default_from_ai": max(0, total - by_human),
        "error_rate": _rate(m_fail, total),
    }


def _empty_overview() -> dict:
    return {
        "total_submissions": 0, "done": 0, "processing": 0, "pending": 0, "failed": 0,
        "evaluated": 0, "error_count": 0, "total_risk": 0, "error_rate": 0.0,
        "status_breakdown": {"done": 0, "in_progress": 0, "failed": 0},
        "ai_status_breakdown": {"approve": 0, "return": 0, "pending": 0},
        "manual_status_breakdown": _manual_breakdown(0, 0, 0, 0, 0),
    }


def compute_stats_snapshot(db, customer_ids=None, roster_uids=None) -> dict:
    """Scan ``done`` results and build the full Statistics payload (see module
    docstring). Safe on empty data (returns zeroed sections).

    ``customer_ids`` (opsional) membatasi scan ke ticket/customer id tertentu —
    dipakai Area Manager supaya seluruh halaman Statistics (KPI, donut, Performa
    Sales, Performa Campaign, hierarki) dihitung dari area-nya sendiri lewat satu
    jalur kode yang sama dengan agregasi global. ``None`` = semua data (default,
    inilah yang di-cache harian). Daftar kosong menghasilkan payload nol.

    ``roster_uids`` (opsional) membatasi ORANG yang di-seed dari roster — dipakai
    mode scoped supaya Area Manager tidak melihat agent di luar areanya. ``None`` =
    seluruh roster.
    """
    base = crud.get_stats(db)  # total_uploaded / pending / processing / done / failed / active_campaigns

    q = exclude_hidden_results(db.query(Result).filter(Result.status == "done"))
    if customer_ids is None:
        done_results = q.all()
    else:
        ids = list(customer_ids)
        # Ticket id = prefix sebelum "_" pada source file pertama, sama dengan
        # _customer_id() di bawah dan compute_scoped_overview().
        prefix = func.split_part(Result.source_files[0].astext, "_", 1)
        done_results = q.filter(prefix.in_(ids)).all() if ids else []

        # crud.get_stats() menghitung SELURUH organisasi. Dalam mode scoped angka
        # itu akan bocor ke KPI card, jadi status count dihitung ulang dari tiket
        # dalam scope saja (semua status, bukan cuma done). active_campaigns tetap
        # global — itu jumlah campaign aktif, bukan angka milik satu area.
        statuses = (
            [s for (s,) in db.query(Result.status).filter(prefix.in_(ids)).all()]
            if ids else []
        )
        counts = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
        for st in statuses:
            if st in counts:
                counts[st] += 1
        base = {**base, **counts, "total_uploaded": len(statuses)}

    result_ids = [r.id for r in done_results]

    # --- Batch lookups (avoid N+1) ---------------------------------------
    # Latest result_json per result_id.
    eval_by_id: dict = {}
    if result_ids:
        for rid, rjson in (
            db.query(ResultData.result_id, ResultData.result_json)
            .filter(ResultData.result_id.in_(result_ids))
            .order_by(ResultData.created_at.desc())
            .all()
        ):
            eval_by_id.setdefault(str(rid), rjson)  # desc order => first seen is latest

    # customer_id -> agent_id / submit_time (trimmed) untuk setiap cid kita.
    # submit_time dipakai cek new-joiner di bawah. Sumbernya
    # crud.cashline_agent_index() -- satu query SQL atas snapshot reference_data
    # yang sudah tersimpan di result_json, BUKAN tabel tms_cashline yang sudah
    # tidak terisi sejak reference data pindah ke DWH API (dulu: 0 dari 34 tiket
    # ketemu, sehingga seluruh hierarki jatuh ke "(Tidak diketahui)").
    cids = {(c or "").strip() for c in (_customer_id(r.source_files) for r in done_results) if c}
    agent_by_cid: dict = {}
    submit_by_cid: dict = {}
    if cids:
        index = crud.cashline_agent_index(db)
        for key in cids:
            entry = index.get(key)
            if entry is None:
                continue
            agent_by_cid[key] = entry["agent_id"]
            submit_by_cid[key] = entry["submit_time"]

    appeal_map = crud.error_code_appeals_for_results(db, [str(r) for r in result_ids])
    qc_map = crud.qc_status_requests_for(db, [str(r) for r in result_ids])
    mdocs = _missing_docs_map(db, done_results)
    gaps = data_gap_map(db, done_results)
    docs_by_rid = crud.document_ocr_by_result(db, [str(r.id) for r in done_results])
    snap_doc_map = document_status_map(db, done_results)
    sales_map = active_sales_map(db)

    # --- Per-result reduction --------------------------------------------
    # Accumulators keyed by agent_id.
    # ``approve`` + H/M/L/N/O feed the per-agent Risk Base columns in the
    # "Hierarki Failure Rate" tree (same one-top-risk-per-ticket rule as
    # campaign_risk_acc below).
    agent_acc: dict = defaultdict(lambda: {
        # ``submissions`` = jumlah TIKET, ``transcripts`` = jumlah PDF yang dinilai.
        # Keduanya disimpan berdampingan: tabel Performa Sales tetap memakai tiket,
        # pohon Hierarki Failure Rate memakai transkrip (lihat ``_risk_node``).
        "submissions": 0, "transcripts": 0, "errors": 0, "approve": 0, "pending": 0,
        "H": 0, "M": 0, "L": 0, "N": 0, "O": 0,
        "campaigns": defaultdict(int),
    })
    agent_meta: dict = {}  # agent_id -> {name, team_leader, area_manager}
    # Per (campaign, month) Risk Base breakdown (H/M/L/N/O) + submissions/errors,
    # for the "Performa Campaign" table (QC / SPQ Head).
    campaign_risk_acc: dict = defaultdict(
        lambda: {"submissions": 0, "errors": 0, "H": 0, "M": 0, "L": 0, "N": 0, "O": 0}
    )
    # Per-campaign overview breakdown (Overview tab campaign filter): the same
    # donut/KPI figures the global overview carries, but keyed by campaign.
    # H/M/L ikut ditally di sini (bukan diturunkan dari camp_agent_acc) karena KPI
    # Error Rate per campaign harus memakai penyebut ``evaluated`` milik campaign
    # itu — termasuk tiket yang agent-nya tidak terpetakan ke roster.
    camp_overview_acc: dict = defaultdict(
        lambda: {"approve": 0, "return": 0, "pending": 0, "evaluated": 0, "errors": 0,
                 "H": 0, "M": 0, "L": 0, "N": 0, "O": 0,
                 "m_pass": 0, "m_fail": 0, "m_pending": 0, "m_by_human": 0}
    )
    # Tally Risk Base global — pembilang Error Rate di KPI Overview.
    global_risk_acc: dict = {"H": 0, "M": 0, "L": 0, "N": 0, "O": 0}
    # Per (campaign, agent) akumulator. Key-nya SAMA PERSIS dengan ``agent_acc``
    # supaya bisa langsung disuapkan ke ``_build_hierarchy`` — itulah yang membuat
    # tab "Hierarki Failure Rate" bisa difilter per campaign tanpa menghitung ulang
    # pohonnya dengan cara berbeda (dan berisiko beda hasil) dari versi global.
    camp_agent_acc: dict = defaultdict(lambda: defaultdict(lambda: {
        "submissions": 0, "transcripts": 0, "errors": 0, "approve": 0, "pending": 0,
        "H": 0, "M": 0, "L": 0, "N": 0, "O": 0,
    }))
    # Rincian PER TIKET untuk daun keempat pohon Hierarki Failure Rate
    # (AM -> TL -> Agent -> Ticket, 28 Agustus 2026). Bentuk tiap entri sama persis
    # dengan akumulator agent, jadi ``_risk_node`` bisa dipakai ulang apa adanya —
    # itu yang menjamin kolom baris tiket dihitung dengan rumus yang sama dengan
    # baris agent/TL/AM di atasnya.
    agent_tickets: dict = defaultdict(list)                       # akey -> [node]
    camp_agent_tickets: dict = defaultdict(lambda: defaultdict(list))  # campaign -> akey -> [node]
    # Total evaluated/errors per campaign, untuk baris "All Telesales" pohon per campaign.
    camp_totals: dict = defaultdict(lambda: {"evaluated": 0, "transcripts": 0, "errors": 0})
    total_eval = 0
    # Jumlah TRANSKRIP yang dinilai — penyebut "All Telesales" di pohon Hierarki
    # Failure Rate. Dipisahkan dari ``total_eval`` (jumlah tiket) karena KPI Overview
    # dan donut tetap menghitung TIKET.
    total_transcripts = 0
    total_err = 0
    approve = ret = pending_ai = 0
    m_pass = m_fail = m_pending = m_by_human = 0
    _now = datetime.now()

    # All-status counts per campaign (pending/processing/done/failed) so the
    # campaign-filtered Overview KPIs cover non-done tickets too, mirroring get_stats.
    camp_status_counts: dict = defaultdict(
        lambda: {"pending": 0, "processing": 0, "done": 0, "failed": 0}
    )
    for camp, st, cnt in (
        exclude_hidden_results(
            db.query(Result.campaign, Result.status, func.count(Result.id))
        )
        .group_by(Result.campaign, Result.status)
        .all()
    ):
        c = (camp or "").strip() or _UNKNOWN
        if st in camp_status_counts[c]:
            camp_status_counts[c][st] += cnt

    for r in done_results:
        cid = _customer_id(r.source_files)
        cid_key = (cid or "").strip()
        rows = _error_code_rows(
            eval_by_id.get(str(r.id)), appeal_map.get(str(r.id)),
            mdocs.get(str(r.id), False)
            and _doc_sla_expired(submit_by_cid.get(cid_key), _now),
            docs_by_rid.get(str(r.id), ()),
        )
        if rows is None:
            continue  # not evaluable — excluded from every rate
        total_eval += 1
        # Jumlah TRANSKRIP tiket ini (panggilan milik agent lain tidak ikut) —
        # mengisi kolom "Submissions" pohon Hierarki. cid/cid_key sudah di-assign
        # di atas, jadi tidak dihitung ulang di sini.
        n_tx = _transcript_count(eval_by_id.get(str(r.id)), r)
        total_transcripts += n_tx
        # "Error"/"failed" untuk SEMUA error rate (agents, campaign, hierarchy) =
        # AI Status RETURN (FAIL) — sumber yang sama dengan donut & KPI. Bukan lagi
        # "punya ≥1 error code": sebuah tiket bisa punya error code tapi tetap PASS
        # bila semua pelanggarannya tolerable. PENDING (butuh dokumen, dalam H+2)
        # bukan error dan bukan approve.
        ai = _result_ai_status(
            eval_by_id.get(str(r.id)), appeal_map.get(str(r.id)), qc_map.get(str(r.id)),
            mdocs.get(str(r.id), False),
            _doc_sla_expired(submit_by_cid.get(cid_key), _now),
            # ``doc_status`` WAJIB dikirim: tanpa itu baris verifikasi yang sedang
            # menunggu dokumen tidak pernah ditangguhkan, sehingga Statistik memvonis
            # FAIL untuk tiket yang di daftar Results masih PENDING.
            snap_doc_map.get(str(r.id)),
            data_gap=gaps.get(str(r.id)),
        )
        if ai == "PASS":
            approve += 1
        elif ai == "FAIL":
            ret += 1
        elif ai == "PENDING":
            pending_ai += 1
        err = 1 if ai == "FAIL" else 0
        total_err += err
        # Manual Status yang BERLAKU: vonis human bila ada, selain itu mengikuti AI.
        _human = manual_status_of(qc_map.get(str(r.id)))
        if _human:
            m_by_human += 1
        _m = _human or ai
        if _m == "PASS":
            m_pass += 1
        elif _m == "FAIL":
            m_fail += 1
        elif _m == "PENDING":
            m_pending += 1

        agent_id = agent_by_cid.get(cid_key) if cid else None
        akey = agent_id or _UNKNOWN
        if akey not in agent_meta:
            entry = sales_map.get(agent_id.casefold()) if agent_id else None
            agent_meta[akey] = {
                "agent_id": agent_id,
                "name": (entry.get("name") if entry else None) or _agent_name_fallback(agent_id) or _UNKNOWN,
                "team_leader": (entry.get("team_leader") if entry else None) or _UNKNOWN,
                "area_manager": (entry.get("area_manager") if entry else None) or _UNKNOWN,
            }
        acc = agent_acc[akey]
        acc["submissions"] += 1
        acc["transcripts"] += n_tx
        acc["errors"] += err
        campaign = (r.campaign or "").strip() or _UNKNOWN
        acc["campaigns"][campaign] += 1

        # Per-campaign donut/KPI + sales-table accumulation (Overview campaign filter).
        co = camp_overview_acc[campaign]
        co["evaluated"] += 1
        co["errors"] += err
        if ai == "PASS":
            co["approve"] += 1
        elif ai == "FAIL":
            co["return"] += 1
        elif ai == "PENDING":
            co["pending"] += 1
        # Manual Status per campaign (sumbu terpisah dari vonis AI di atas).
        if _human:
            co["m_by_human"] += 1
        if _m == "PASS":
            co["m_pass"] += 1
        elif _m == "FAIL":
            co["m_fail"] += 1
        elif _m == "PENDING":
            co["m_pending"] += 1
        ca = camp_agent_acc[campaign][akey]
        ca["submissions"] += 1
        ca["transcripts"] += n_tx
        ca["errors"] += err
        ct = camp_totals[campaign]
        ct["evaluated"] += 1
        ct["transcripts"] += n_tx
        ct["errors"] += err

        # Risk Base tally: a new-joiner's L/M rows soften to N (mirrors the
        # per-result Error Code table); any row without a catalogued risk
        # base (blank/unmatched error code) counts as O (System).
        # Computed once per ticket and shared by the per-agent (hierarchy) and
        # per-(campaign, month) accumulators — the latter is month-gated, the
        # former is not, so this must sit OUTSIDE the `if month:` block.
        is_new_joiner = _is_new_joiner(submit_by_cid.get(cid_key), agent_id, sales_map)
        risk_rows = override_risk_base_for_new_joiner(rows) if is_new_joiner else rows
        # Satu risk base tertinggi per tiket, dengan pengecualian L-tolerable —
        # lihat top_risk_base. Tiket tanpa error code tidak masuk ember mana pun.
        top = top_risk_base(risk_rows, eval_by_id.get(str(r.id)))

        # Per-agent Risk Base + approve tally (Hierarki Failure Rate columns).
        # Dicatat DUA kali: sekali global, sekali per campaign — supaya pohon
        # hierarki versi global dan versi terfilter memakai aturan yang sama persis
        # (satu risk base tertinggi per tiket).
        if ai == "PASS":
            acc["approve"] += 1
            ca["approve"] += 1
        elif ai == "PENDING":
            # Kolom "Pending" tabel Performa Sales: tiket yang masih menunggu
            # dokumen (dalam H+2), bukan Qualified dan bukan Not Qualified.
            acc["pending"] += 1
            ca["pending"] += 1
        # Pohon "Hierarki Failure Rate" (28 Agustus 2026) memakai aturannya sendiri,
        # berbeda dari seluruh tabel lain di halaman ini:
        #   1. HANYA tiket Not Qualified yang menyumbang. Tiket PENDING (vonis belum
        #      final) dan PASS (bisa membawa error code tanpa jatuh di bawah passing
        #      grade) tidak dihitung.
        #   2. SEMUA risk base tiket itu dihitung, bukan satu yang tertinggi — kolom
        #      High/Medium/Low di sana mengukur PELANGGARAN, bukan tiket. Karena itu
        #      Total Failure bisa melebihi jumlah tiket Not Qualified.
        _tally = (
            risk_base_tally(risk_rows, eval_by_id.get(str(r.id)))
            if ai == "FAIL"
            else {k: 0 for k in _RISK_PRIORITY}
        )
        for _rb, _n in _tally.items():
            if _n:
                acc[_rb] += _n
                ca[_rb] += _n
        # Daun ke-4 pohon: satu baris per tiket, memakai kunci yang sama dengan
        # akumulator agent supaya ``_risk_node`` bisa mengolahnya tanpa cabang khusus.
        _ticket_node = {
            "ticket_id": cid_key or _UNKNOWN,
            "submissions": 1,
            "transcripts": n_tx,
            "errors": 1 if ai == "FAIL" else 0,
            "approve": 1 if ai == "PASS" else 0,
            "pending": 1 if ai == "PENDING" else 0,
            **_tally,
        }
        agent_tickets[akey].append(_ticket_node)
        camp_agent_tickets[campaign][akey].append(_ticket_node)
        # KPI Overview (global & per campaign) TIDAK ikut berubah: tetap seluruh tiket,
        # tetap satu risk base tertinggi per tiket.
        if top is not None:
            co[top] += 1
            global_risk_acc[top] += 1

        month = _wib_month(r.uploaded_at)
        if month:
            rc = campaign_risk_acc[(campaign, month)]
            rc["submissions"] += 1
            rc["errors"] += err
            # HANYA tiket Not Qualified (AI Status FAIL) yang menyumbang risk base
            # ke tabel Performa Campaign — permintaan 28 Agustus 2026. Dua populasi
            # yang sengaja dibuang:
            #   * PENDING — masih menunggu dokumen (H+2), vonisnya belum final;
            #   * PASS    — tiket Qualified bisa tetap membawa error code (mis. B03
            #     Salah input data dari Cashline Data Verification, yang tidak punya
            #     flag ``tolerable`` sehingga tidak pernah memveto PASS).
            # Efeknya Total Risk <= Not Qualified, jadi Error Rate tabel ini tidak
            # akan pernah melewati 100%.
            #
            # Konsekuensi yang disadari: kesalahan input data pada tiket Qualified
            # tidak muncul sama sekali di tabel ini. Tabel Error Code per tiket dan
            # Ringkasan Kategori tetap menampilkannya.
            #
            # HANYA tabel ini. KPI Overview, Hierarki Failure Rate, dan Performa
            # Sales tetap menghitung seluruh tiket.
            if top is not None and ai == "FAIL":
                rc[top] += 1

    # --- Seed dari roster -------------------------------------------------
    # Tabel Performa Sales & pohon Hierarki Failure Rate dulu HANYA memuat orang yang
    # punya tiket, sehingga campaign yang transkripnya belum diproses tampil kosong
    # sama sekali dan sebagian agent cashline pun tak pernah muncul. Sekarang setiap
    # orang di roster yang PUNYA AKUN AKTIF ikut dimunculkan dengan angka nol —
    # "belum ada submission" adalah informasi, bukan alasan menyembunyikan orangnya.
    #
    # Sumbernya akun aktif (bukan seluruh baris roster) supaya yang sudah RESIGN
    # tidak ikut memenuhi tabel: akun mereka memang tidak dibuat.
    active_usernames = {
        (u.username or "").strip().casefold()
        for u in db.query(User.username).filter(User.is_active.is_(True)).all()
        if (u.username or "").strip()
    }
    # Samakan kunci campaign dengan yang sudah ada dari tiket ("cashline"), jangan
    # membuat kunci kedua dari ejaan roster ("CASHLINE").
    def _canon_campaign(raw: str) -> str:
        key = (raw or "").strip()
        if not key:
            return _UNKNOWN
        for existing in camp_agent_acc:
            if existing.strip().casefold() == key.casefold():
                return existing
        return key.casefold()

    for uid, e in sales_map.items():
        if roster_uids is not None and uid not in roster_uids:
            continue
        nip = (e.get("nip_baru") or "").strip().casefold()
        if not nip or nip not in active_usernames:
            continue
        if uid not in agent_meta:
            agent_meta[uid] = {
                "agent_id": uid,
                "name": e.get("name") or _agent_name_fallback(uid) or _UNKNOWN,
                "team_leader": e.get("team_leader") or _UNKNOWN,
                "area_manager": e.get("area_manager") or _UNKNOWN,
            }
        camp_key = _canon_campaign(e.get("dedicated"))
        # Menyentuh defaultdict = membuat entri nol; yang sudah punya tiket tidak
        # berubah sama sekali.
        acc = agent_acc[uid]
        acc["campaigns"][camp_key] += 0
        camp_agent_acc[camp_key][uid]

    # --- agents[] (sales performance table) ------------------------------
    agents = []
    for akey, acc in agent_acc.items():
        meta = agent_meta[akey]
        top_campaign = max(acc["campaigns"].items(), key=lambda kv: kv[1])[0] if acc["campaigns"] else _UNKNOWN
        agents.append({
            "agent_id": meta["agent_id"],
            "name": meta["name"],
            "team_leader": meta["team_leader"],
            "area_manager": meta["area_manager"],
            "campaign": top_campaign,
            "submissions": acc["submissions"],
            "pending": acc["pending"],
            "errors": acc["errors"],
            "error_rate": _rate(acc["errors"], acc["submissions"]),
        })
    agents.sort(key=lambda a: (a["error_rate"], a["submissions"]), reverse=True)

    # --- campaign_monthly[] (Performa Campaign, month-to-month) --------------
    campaign_monthly = [
        {
            "campaign": campaign,
            "month": month,
            "submissions": v["submissions"],
            # Penyebut Error Rate, dikirim sebagai kolom sendiri supaya pembaca
            # tabel bisa memverifikasi pecahannya (Total Risk ÷ Not Qualified).
            "not_qualified": v["errors"],
            "high": v["H"],
            "medium": v["M"],
            "low": v["L"],
            "system": v["O"],
            "new": v["N"],
            "total_risk": v["H"] + v["M"] + v["L"],
            # Error Rate Performa Campaign = Total Risk (H+M+L) ÷ SUBMISSION
            # (permintaan 28 Agustus 2026, menggantikan penyebut "tiket Not
            # Qualified" yang dipakai sebelumnya pada hari yang sama).
            #
            # PEMBILANGNYA tetap dibatasi ke tiket Not Qualified — lihat gerbang
            # ``ai == "FAIL"`` di tally ``campaign_risk_acc``. Jadi angkanya terbaca
            # "berapa persen dari seluruh submission yang berujung risk base", dan
            # karena tiap tiket menyumbang paling banyak satu H/M/L sementara
            # penyebutnya mencakup SEMUA tiket, rasionya tidak pernah > 100%.
            "error_rate": _rate(v["H"] + v["M"] + v["L"], v["submissions"]),
        }
        for (campaign, month), v in campaign_risk_acc.items()
    ]
    campaign_monthly.sort(key=lambda x: (x["campaign"], x["month"]))
    months = sorted({x["month"] for x in campaign_monthly})

    # --- hierarchy: Area Manager -> Team Leader -> Agent -----------------
    hierarchy = _build_hierarchy(agent_acc, agent_meta, total_eval, total_err, agent_tickets,
                                 total_transcripts=total_transcripts)
    # Versi per campaign untuk filter di tab "Hierarki Failure Rate". Memakai fungsi
    # pembangun yang SAMA, hanya berbeda akumulator & totalnya.
    hierarchy_by_campaign = {
        c: _build_hierarchy(
            camp_agent_acc[c], agent_meta,
            camp_totals[c]["evaluated"], camp_totals[c]["errors"],
            camp_agent_tickets.get(c),
            total_transcripts=camp_totals[c]["transcripts"],
        )
        for c in camp_agent_acc
    }

    error_rate = _rate_of(global_risk_acc, total_eval)
    total_risk = sum(global_risk_acc[k] for k in _RISK_COUNTED)
    overview = {
        "total_submissions": base["total_uploaded"],
        "done": base["done"],
        "processing": base["processing"],
        "pending": base["pending"],
        "failed": base["failed"],
        "evaluated": total_eval,
        "error_count": total_err,
        # Pembilang Error Rate, dikirim terpisah supaya KPI bisa menuliskan
        # pecahannya apa adanya ("12 / 340") alih-alih memakai error_count yang
        # menghitung hal lain (tiket Not Qualified).
        "total_risk": total_risk,
        "error_rate": error_rate,
        "status_breakdown": {
            "done": base["done"],
            "in_progress": base["pending"] + base["processing"],
            "failed": base["failed"],
        },
        "ai_status_breakdown": {"approve": approve, "return": ret, "pending": pending_ai},
        "manual_status_breakdown": _manual_breakdown(m_pass, m_fail, m_pending, total_eval, m_by_human),
        "active_campaigns": base["active_campaigns"],
    }

    # --- per-campaign Overview (KPIs + donut + sales table), for the filter ------
    # camp_agent_acc ikut disertakan: campaign yang sudah punya ORANG tetapi belum
    # punya tiket harus tetap muncul di dropdown & punya entri Performa Sales.
    campaigns = sorted(set(camp_status_counts) | set(camp_overview_acc) | set(camp_agent_acc))
    overview_by_campaign: dict = {}
    agents_by_campaign: dict = {}
    for c in campaigns:
        sc = camp_status_counts[c]
        # Diindeks langsung, BUKAN .get() dengan default manual: default itu tidak
        # memuat kunci m_pass/m_fail/m_pending/m_by_human yang dipakai beberapa baris
        # di bawah, sehingga campaign yang belum punya tiket menjatuhkan seluruh
        # perhitungan snapshot dengan KeyError. defaultdict-nya sudah menyediakan
        # bentuk lengkap berisi nol.
        co = camp_overview_acc[c]
        total_sub = sc["pending"] + sc["processing"] + sc["done"] + sc["failed"]
        overview_by_campaign[c] = {
            "total_submissions": total_sub,
            "done": sc["done"],
            "processing": sc["processing"],
            "pending": sc["pending"],
            "failed": sc["failed"],
            "evaluated": co["evaluated"],
            "error_count": co["errors"],
            "total_risk": sum(co[k] for k in _RISK_COUNTED),
            "error_rate": _rate_of(co, co["evaluated"]),
            "status_breakdown": {
                "done": sc["done"],
                "in_progress": sc["pending"] + sc["processing"],
                "failed": sc["failed"],
            },
            "ai_status_breakdown": {"approve": co["approve"], "return": co["return"], "pending": co["pending"]},
            "manual_status_breakdown": _manual_breakdown(
                co["m_pass"], co["m_fail"], co["m_pending"], co["evaluated"], co["m_by_human"]),
        }
        camp_agents = []
        for akey, cacc in camp_agent_acc.get(c, {}).items():
            meta = agent_meta[akey]
            camp_agents.append({
                "agent_id": meta["agent_id"],
                "name": meta["name"],
                "team_leader": meta["team_leader"],
                "area_manager": meta["area_manager"],
                "campaign": c,
                "submissions": cacc["submissions"],
                "pending": cacc["pending"],
                "errors": cacc["errors"],
                "error_rate": _rate(cacc["errors"], cacc["submissions"]),
            })
        camp_agents.sort(key=lambda a: (a["error_rate"], a["submissions"]), reverse=True)
        agents_by_campaign[c] = camp_agents

    return {
        "overview": overview,
        "agents": agents,
        "campaign_monthly": {"rows": campaign_monthly, "months": months},
        "hierarchy": hierarchy,
        "campaigns": campaigns,
        "overview_by_campaign": overview_by_campaign,
        "agents_by_campaign": agents_by_campaign,
        "hierarchy_by_campaign": hierarchy_by_campaign,
    }


def empty_hierarchy() -> dict:
    """Pohon Hierarki Failure Rate yang kosong.

    Dipakai saat filter campaign menunjuk campaign aktif yang belum punya tiket:
    lebih jujur menampilkan pohon kosong daripada diam-diam jatuh kembali ke angka
    global — yang akan terbaca seolah campaign itu sudah punya data.
    """
    zero = {k: 0 for k in ("submissions", "transcripts", "errors", "approve", "pending",
                           "H", "M", "L", "N", "O")}
    return {"all_telesales": _risk_node(zero), "area_managers": []}


def _transcript_count(result_json, result) -> int:
    """Berapa TRANSKRIP (PDF) yang benar-benar DINILAI untuk satu tiket.

    Satu ticket id bisa berisi beberapa panggilan, dan sejak 31 Agustus 2026 sebagian
    di antaranya bisa dibuang karena milik agent lain (lihat
    ``compliance.call_ownership``). Yang dihitung di sini adalah yang TERSISA —
    ``num_calls`` pada hasil evaluasi ditulis worker SESUDAH penyaringan itu, jadi
    panggilan agent lain memang tidak ikut. ``results.num_calls`` di database TIDAK
    dipakai: itu catatan berapa PDF diunggah, bukan berapa yang dinilai.

    Berjenjang mundur untuk hasil lama yang bentuknya tidak lengkap, dan minimal 1
    supaya tidak pernah menghasilkan penyebut nol untuk tiket yang jelas ada.
    """
    rj = result_json if isinstance(result_json, dict) else {}
    n = rj.get("num_calls")
    if isinstance(n, int) and not isinstance(n, bool) and n > 0:
        return n
    for files in (rj.get("source_files"), getattr(result, "source_files", None)):
        if isinstance(files, list) and files:
            return len(files)
    n = getattr(result, "num_calls", None)
    return n if isinstance(n, int) and n > 0 else 1


def _risk_node(v: dict) -> dict:
    """Shared field block for every level of the hierarchy tree (agent / TL / AM).

    Tabel ini tampil sebagai **Hierarki Failure Rate**: ``total_risk`` tampil sebagai
    kolom "Total Failure" dan ``error_rate`` sebagai "Failure Rate". Nama kuncinya
    sengaja TIDAK ikut diganti — payload lama masih dibaca dashboard yang ter-cache,
    dan ``compute_scoped_hierarchy`` memakai kunci yang sama.

    ``error_rate`` = Total Failure ÷ Submissions (lihat ``_rate_of``). Sejak
    28 Agustus 2026 pembilangnya HANYA berasal dari tiket Not Qualified, tetapi
    menghitung SEMUA risk base tiket itu (lihat ``risk_base_tally``) — kolom
    High/Medium/Low di sini mengukur PELANGGARAN, bukan tiket.

    Konsekuensinya ``total_risk`` BISA melebihi ``errors``, dan rasionya secara
    teori bisa melewati 100% bila rata-rata pelanggaran per tiket gagal cukup tinggi.
    Itu memang yang diminta: tiket dengan 5 pelanggaran berat tidak sepadan dengan
    tiket yang melanggar sekali.

    Kolom ``errors`` (jumlah tiket Not Qualified) dipertahankan sebagai konteks;
    ``approve``/``pending`` melengkapinya supaya ketiga vonis AI tampil berdampingan
    (Qualified / Pending / Not Qualified) dan menjumlah tepat ke ``submissions``.
    """
    return {
        # "Submissions" = jumlah TRANSKRIP yang dinilai, bukan jumlah tiket
        # (permintaan bisnis 31 Agustus 2026) — 98 tiket di korpus awal berisi 212
        # transkrip. ``tickets`` membawa hitungan tiketnya supaya tidak hilang.
        #
        # KONSEKUENSI YANG DISENGAJA: ``approve`` + ``pending`` + ``errors`` adalah
        # vonis PER TIKET dan karena itu TIDAK LAGI berjumlah sama dengan
        # ``submissions``. Ketiganya menjumlah ke ``tickets``.
        #
        # ``v.get`` berjaga untuk pemanggil yang belum menghitung transkrip
        # (``compute_scoped_hierarchy`` dari roster lama): di sana penyebutnya jatuh
        # kembali ke jumlah tiket, sama seperti sebelumnya.
        "submissions": v.get("transcripts", v["submissions"]),
        # BUKAN "tickets": kunci itu sudah dipakai simpul agent untuk DAFTAR tiket
        # (daun ke-4 pohon), dan menimpanya akan menghapus daftar itu.
        "ticket_count": v["submissions"],
        "errors": v["errors"],
        "approve": v["approve"],
        "pending": v["pending"],
        "risk_high": v["H"],
        "risk_medium": v["M"],
        "risk_low": v["L"],
        "risk_system": v["O"],
        "risk_new": v["N"],
        # Total Risk deliberately excludes System (O) and New (N) — same rule as
        # the Performa Campaign table.
        "total_risk": v["H"] + v["M"] + v["L"],
        # Avg Failure Rate = Total Failure ÷ Total Recording, ditulis sebagai
        # kelipatan (mis. 2.8x) karena satu tiket menyumbang SEMUA risk base-nya
        # sehingga rasionya rutin melewati 100%. Penyebutnya angka yang sama dengan
        # kolom "Total Recording" tepat di sebelahnya.
        "error_rate": _avg_of(v, v.get("transcripts", v["submissions"])),
    }


def _build_hierarchy(agent_acc, agent_meta, total_eval, total_err, agent_tickets=None,
                     total_transcripts=None) -> dict:
    """Nest per-agent accumulators into Area Manager -> Team Leader -> Agent, each
    level carrying submissions / errors / error_rate, plus an ``all_telesales`` total.

    ``agent_tickets`` (opsional) = ``{agent_key: [ticket_node, ...]}``. Bila diberikan,
    tiap simpul agent memuat ``tickets``: daun keempat pohon, satu baris per ticket id,
    dengan kolom yang sama persis dengan baris di atasnya — sehingga pengawas bisa
    membuka seorang agent dan melihat tiket mana yang Not Qualified beserta berapa
    banyak risk base H/M/L yang dibawanya. Tanpa argumen ini pohonnya berperilaku
    seperti sebelumnya (dipakai ``empty_hierarchy`` dan pemanggil lama).
    """
    # am -> tl -> agent_key -> {submissions, errors, approve, H, M, L, N, O}
    _AGENT_KEYS = ("submissions", "transcripts", "errors", "approve", "pending",
                   "H", "M", "L", "N", "O")
    tree: dict = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: {k: 0 for k in _AGENT_KEYS}))
    )
    for akey, acc in agent_acc.items():
        meta = agent_meta[akey]
        node = tree[meta["area_manager"]][meta["team_leader"]][akey]
        for k in _AGENT_KEYS:
            node[k] += acc.get(k, 0)

    area_managers = []
    for am_name, tls in tree.items():
        am = {k: 0 for k in _AGENT_KEYS}
        tl_list = []
        for tl_name, agents_map in tls.items():
            tl = {k: 0 for k in _AGENT_KEYS}
            agent_list = []
            for akey, v in agents_map.items():
                meta = agent_meta[akey]
                for k in _AGENT_KEYS:
                    tl[k] += v[k]
                # Tiket paling "berat" di atas: Not Qualified dulu, lalu yang risk
                # base-nya terbanyak, lalu ticket id supaya urutannya stabil.
                _tickets = [
                    {"ticket_id": t["ticket_id"], **_risk_node(t)}
                    for t in (agent_tickets or {}).get(akey, ())
                ]
                _tickets.sort(key=lambda t: (-t["errors"], -t["total_risk"], t["ticket_id"]))
                agent_list.append({
                    "agent_id": meta["agent_id"],
                    "name": meta["name"],
                    **_risk_node(v),
                    "tickets": _tickets,
                })
            agent_list.sort(key=lambda a: (a["error_rate"], a["submissions"]), reverse=True)
            for k in _AGENT_KEYS:
                am[k] += tl[k]
            tl_list.append({"name": tl_name, **_risk_node(tl), "agents": agent_list})
        tl_list.sort(key=lambda t: (t["error_rate"], t["submissions"]), reverse=True)
        area_managers.append({"name": am_name, **_risk_node(am), "team_leaders": tl_list})
    area_managers.sort(key=lambda a: (a["error_rate"], a["submissions"]), reverse=True)

    # Grand total for the Risk Base context columns. ``submissions``/``errors``
    # come from the caller's totals (they also count agents that fall outside the
    # AM/TL mapping, which the tree cannot represent), so the rate is recomputed
    # against ``total_eval`` — pembilangnya tetap Total Risk, sama dengan simpul
    # mana pun di bawahnya.
    grand = {k: 0 for k in _AGENT_KEYS}
    for acc in agent_acc.values():
        for k in _AGENT_KEYS:
            grand[k] += acc.get(k, 0)
    all_telesales = _risk_node(grand)
    # ``total_eval``/``total_transcripts`` datang dari pemanggil karena keduanya IKUT
    # menghitung agent yang tidak terpetakan ke AM/TL — orang-orang yang tidak bisa
    # diwakili pohonnya, tetapi tetap bagian dari populasi "All Telesales".
    _tx = total_transcripts if total_transcripts is not None else total_eval
    all_telesales["submissions"] = _tx
    all_telesales["ticket_count"] = total_eval
    all_telesales["errors"] = total_err
    all_telesales["error_rate"] = _avg_of(grand, _tx)

    return {
        "all_telesales": all_telesales,
        "area_managers": area_managers,
    }
