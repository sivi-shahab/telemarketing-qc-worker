import copy
import hashlib
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Lock
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func, desc


# [FIX] ``AscendCustp`` dan ``TmsCashline`` DIHAPUS dari daftar import: kedua tabel
# lokal itu sudah tidak diisi lagi sejak reference data pindah ke DWH API, dan
# seluruh pemakaiannya di file ini sudah dialihkan (lihat ``tms_submit_time_map``
# dan filter tanggal di ``list_results``). Definisi modelnya tetap ada di
# db/models.py sebagai skema tabel.
from db.models import (
    AppSetting,
    Campaign,
    Document,
    ErrorCodeAppeal,
    QcAssignment,
    QcDatabase,
    QcManualCheck,
    QcStatusEvent,
    QcStatusRequest,
    ReprocessJob,
    ReprocessJobItem,
    Result,
    ResultData,
    SalesDatabase,
    StatsSnapshot,
    User,
)
# Reference data (CASHLINE / CARD HOLDER) sekarang dari DWH API (Aplikasi A),
# bukan lagi tabel DB -> lihat services/data_dwh.py.
from services import data_dwh


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
def create_result(
    db: Session,
    campaign: str,
    source_files: list,
    num_calls: int,
    transcript_path: str,
    uploaded_by_username: str = None,
    uploaded_by_role: str = None,
    id: str = None,            # opsional — kalau diisi, pakai ID ini (mis. STT job_id)
    status: str = "pending",   # opsional — default tetap "pending"
) -> Result:
    result = Result(
        campaign=campaign,
        source_files=source_files,
        num_calls=num_calls,
        transcript_path=transcript_path,
        status=status,
        uploaded_by_username=uploaded_by_username,
        uploaded_by_role=uploaded_by_role,
    )
    if id is not None:
        # uuid.UUID(...) memvalidasi formatnya sekaligus; kalau job_id dari STT
        # bukan UUID valid, ini raise ValueError lebih awal (fail cepat & jelas).
        result.id = uuid.UUID(str(id))
    db.add(result)
    db.commit()
    db.refresh(result)
    return result


def get_result(db: Session, result_id: str) -> Optional[Result]:
    return db.query(Result).filter(Result.id == uuid.UUID(str(result_id))).first()


def get_result_by_source_file(db: Session, filename: str) -> Optional[Result]:
    """Cari Result terbaru yang source_files-nya memuat ``filename`` (mis.
    ``"061058ecB3_20260612171830.pdf"``). Memakai JSONB containment (``@>``)."""
    return (
        db.query(Result)
        .filter(Result.source_files.contains([filename]))
        .order_by(desc(Result.uploaded_at))
        .first()
    )


def pick_reusable_done_result(db: Session, customer_id: str) -> Optional[Result]:
    """Pilih Result ``done`` untuk ``customer_id`` (prefix sebelum ``_`` pada
    file pertama) yang boleh di-reuse saat upload duplikat.

    Prioritas: Result yang punya minimal 1 banding (``ErrorCodeAppeal``, status
    apapun); jika tidak ada, Result dengan ``uploaded_at`` terbaru.
    """
    candidates = (
        db.query(Result)
        .filter(
            Result.status == "done",
            func.split_part(Result.source_files[0].astext, "_", 1) == customer_id,
        )
        .order_by(desc(Result.uploaded_at))
        .all()
    )
    if not candidates:
        return None
    # candidates sudah urut uploaded_at desc -> kandidat pertama yang punya
    # banding = Result terbaru yang punya banding.
    for r in candidates:
        has_appeal = (
            db.query(ErrorCodeAppeal.id)
            .filter(ErrorCodeAppeal.result_id == r.id)
            .first()
        )
        if has_appeal:
            return r
    return candidates[0]


def clone_result_from(db: Session, source: Result, target: Result) -> dict:
    """Klon hasil ``source`` (status ``done``) ke row ``target`` yang baru dibuat.

    Menyalin ``result_json`` (evaluasi LLM), seluruh ``ErrorCodeAppeal`` (banding)
    dan ``QcStatusRequest`` (usulan status QC + approval SPQ Head) milik ``source``
    ke ``target``, lalu menandai ``target`` sebagai ``done`` — TANPA memanggil
    LLM. Return ``result_json`` hasil salinan (untuk di-mirror ke object storage).
    """
    src_data = get_result_data(db, source.id)
    result_json = copy.deepcopy(src_data.result_json) if (src_data and src_data.result_json) else {}
    if isinstance(result_json, dict):
        # Sesuaikan identitas ke row baru.
        result_json["result_id"] = str(target.id)
        result_json["source_files"] = target.source_files
    save_result_data(db, target.id, result_json)
    # Klon semua banding (append-only history dipertahankan apa adanya).
    for a in error_code_appeals_for_result(db, source.id):
        db.add(
            ErrorCodeAppeal(
                result_id=target.id,
                error_code=a.error_code,
                item_code=a.item_code,
                ai_sumber=a.ai_sumber,
                ai_risk_base=a.ai_risk_base,
                ai_details_error=a.ai_details_error,
                ai_reason=a.ai_reason,
                ai_evidence=a.ai_evidence,
                ai_ticket_id=a.ai_ticket_id,
                qc_reason=a.qc_reason,
                qc_evidence=a.qc_evidence,
                qc_ticket_id=a.qc_ticket_id,
                approval_status=a.approval_status,
                requested_by_username=a.requested_by_username,
                requested_at=a.requested_at,
                reviewed_by_username=a.reviewed_by_username,
                reviewed_at=a.reviewed_at,
            )
        )
    # Klon usulan status QC (+ approval SPQ Head) bila ada. result_id target unik
    # sehingga constraint UNIQUE(result_id) aman.
    src_req = get_qc_status_request(db, source.id)
    if src_req is not None:
        db.add(
            QcStatusRequest(
                result_id=target.id,
                requested_status=src_req.requested_status,
                reason=src_req.reason,
                requested_by_username=src_req.requested_by_username,
                requested_by_role=src_req.requested_by_role,
                requested_at=src_req.requested_at,
                approval_status=src_req.approval_status,
                reviewed_by_username=src_req.reviewed_by_username,
                reviewed_at=src_req.reviewed_at,
            )
        )
    target.status = "done"
    target.result_path = f"{target.id}.json"
    target.started_at = func.now()
    target.completed_at = func.now()
    target.processing_sec = source.processing_sec
    db.commit()
    db.refresh(target)
    return result_json


def update_result_status(
    db: Session,
    result_id: str,
    status: str,
    error_message: str = None,
    result_path: str = None,
    started_at: datetime = None,
    completed_at: datetime = None,
    processing_sec: float = None,
    generated_at: datetime = None,
) -> Optional[Result]:
    result = get_result(db, result_id)
    if not result:
        return None
    result.status = status
    if error_message is not None:
        result.error_message = error_message
    if result_path is not None:
        result.result_path = result_path
    if generated_at is not None:
        result.generated_at = generated_at
    if started_at is not None:
        result.started_at = started_at
    if completed_at is not None:
        result.completed_at = completed_at
    if processing_sec is not None:
        result.processing_sec = processing_sec
    db.commit()
    db.refresh(result)
    return result


def result_json_map(db: Session, result_ids: list[str]) -> dict:
    """Latest result_json per result_id, in one batched query (no N+1).
    Mirrors the snapshot builder: rows come back newest-first, so the first seen
    per id is the latest. Ids without result data are simply absent from the map.
    """
    if not result_ids:
        return {}
    uuids = [uuid.UUID(str(rid)) for rid in result_ids]
    out: dict = {}
    for rid, rjson in (
        db.query(ResultData.result_id, ResultData.result_json)
        .filter(ResultData.result_id.in_(uuids))
        .order_by(desc(ResultData.created_at))
        .all()
    ):
        out.setdefault(str(rid), rjson)
    return out


def save_result_data(db: Session, result_id: str, result_json: dict) -> ResultData:
    data = ResultData(result_id=uuid.UUID(str(result_id)), result_json=result_json)
    db.add(data)
    db.commit()
    db.refresh(data)
    return data


def get_result_data(db: Session, result_id: str) -> Optional[ResultData]:
    return (
        db.query(ResultData)
        .filter(ResultData.result_id == uuid.UUID(str(result_id)))
        .order_by(desc(ResultData.created_at))
        .first()
    )


# JSONB path ke submit_time pada snapshot reference_data yang tersimpan di
# ``result_data.result_json`` (disisipkan worker saat evaluasi — lihat
# worker/tasks/process_transcript.py). Ini pengganti kolom
# ``tms_cashline.submit_time`` yang tabelnya sudah tidak diisi lagi.
_SUBMIT_TIME_JSON = ResultData.result_json["reference_data"]["cashline"]["submit_time"]


def list_results(
    db: Session,
    status: Optional[str] = None,
    campaign: Optional[str] = None,
    campaigns: Optional[list[str]] = None,
    ticket_id: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
    customer_ids: Optional[list[str]] = None,
    uploaded_by_role: Optional[str] = None,
    exclude_uploaded_by_role: Optional[str] = None,
    uploaded_by_username: Optional[str] = None,
    date_start=None,
    date_end=None,
) -> tuple[list[Result], int]:
    # ``customer_ids`` (when not None) scopes results to those customer/ticket ids —
    # used to restrict a sales_agent (Team Leader) to their agents' tickets. An
    # empty list means "no allowed ids" => no results (short-circuit).
    # ``uploaded_by_role`` / ``exclude_uploaded_by_role`` isolate QC Support's
    # complaint results: QC Support sees only its own uploads; every other role
    # excludes them (a standalone, isolated result set). ``uploaded_by_username``
    # narrows to a single uploader (Team Leader QC's "Semua QC Support" filter).
    if customer_ids is not None and len(customer_ids) == 0:
        return [], 0
    q = db.query(Result)
    # Tiket yang disembunyikan tidak boleh muncul di menu ini — lihat
    # ``hidden_ticket_filter``.
    q = hidden_ticket_filter(q)
    if uploaded_by_username is not None:
        q = q.filter(
            func.lower(func.trim(Result.uploaded_by_username))
            == (uploaded_by_username or "").strip().casefold()
        )
    if uploaded_by_role is not None:
        q = q.filter(Result.uploaded_by_role == uploaded_by_role)
    if exclude_uploaded_by_role is not None:
        q = q.filter(
            (Result.uploaded_by_role.is_(None))
            | (Result.uploaded_by_role != exclude_uploaded_by_role)
        )
    if status:
        q = q.filter(Result.status == status)
    if campaign:
        q = q.filter(Result.campaign == campaign)
    if campaigns is not None:
        # Pembatasan campaign EFEKTIF (bukan filter pilihan user, lihat
        # ``api.rbac.effective_campaigns_for``): ``None`` = tanpa pembatasan,
        # sedangkan list KOSONG = dibatasi ke himpunan kosong sehingga tidak ada
        # baris yang lolos. Case-insensitive karena nama campaign di ``results``
        # tersimpan apa adanya saat upload.
        if not campaigns:
            return [], 0
        q = q.filter(func.lower(Result.campaign).in_([c.strip().casefold() for c in campaigns]))
    if ticket_id:
        # The displayed "ID" is the prefix before the first "_" of the first
        # source filename (see stats._customer_id_from_files), so match that
        # first filename by prefix — a full or partial ticket id both work.
        q = q.filter(Result.source_files[0].astext.ilike(f"{ticket_id}%"))
    if customer_ids is not None:
        # customer_id = prefix before the first "_" of the first source filename.
        q = q.filter(
            func.split_part(Result.source_files[0].astext, "_", 1).in_(customer_ids)
        )
    if date_start is not None or date_end is not None:
        # Filter by the ticket's ``submit_time`` date (disbursement submission), same
        # basis as the Statistics stacked chart.
        #
        # [FIX] Sumbernya SEKARANG snapshot ``reference_data.cashline.submit_time``
        # di ``result_data.result_json``, BUKAN tabel ``tms_cashline`` yang sudah
        # tidak diisi lagi sejak reference data pindah ke DWH API. Subquery lama
        # tidak pernah error (hanya selalu NULL), jadi filter tanggal diam-diam
        # jatuh ke generated_at/uploaded_at dan tidak sejalan dengan grafik.
        #
        # Tetap subquery berkorelasi ber-LIMIT 1 (bukan LEFT JOIN) supaya satu
        # Result tidak bisa terduplikasi oleh beberapa baris result_data; diurutkan
        # created_at desc agar yang terbaca adalah evaluasi TERBARU, konsisten
        # dengan result_json_map / cashline_agent_index.
        submit_date_subq = (
            db.query(
                func.to_date(func.left(func.trim(_SUBMIT_TIME_JSON.astext), 10), "YYYY-MM-DD")
            )
            .filter(ResultData.result_id == Result.id)
            .filter(
                func.trim(func.coalesce(_SUBMIT_TIME_JSON.astext, "")).op("~")(
                    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}"
                )
            )
            .order_by(desc(ResultData.created_at))
            .limit(1)
            .correlate(Result)
            .scalar_subquery()
        )
        series_date = func.coalesce(
            submit_date_subq,
            func.date(Result.generated_at),
            func.date(func.timezone("Asia/Jakarta", func.timezone("UTC", Result.uploaded_at))),
        )
        if date_start is not None:
            q = q.filter(series_date >= date_start)
        if date_end is not None:
            q = q.filter(series_date <= date_end)
    total = q.count()
    items = (
        q.order_by(desc(Result.uploaded_at))
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    return items, total


def list_transcripts(
    db: Session,
    status: Optional[str] = None,
    campaign: Optional[str] = None,
    ticket_id: Optional[str] = None,
    ai_status: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
    customer_ids: Optional[list[str]] = None,
    uploaded_by_role: Optional[str] = None,
    exclude_uploaded_by_role: Optional[str] = None,
) -> tuple[list[dict], int]:
    """Flatten every Result's ``source_files`` into one row per transcript PDF —
    a single ticket/Result can bundle several call transcripts (``num_calls``).
    Filtering/pagination happens at the flattened-row level, not the Result level.

    ``ai_status`` ("PASS"=Approve / "FAIL"=Reject) mirrors the Results table filter:
    AI Status is derived per Result (not stored), so it is only meaningful for
    completed results — passing it forces ``status="done"`` and filters the results
    in Python before flattening.

    ``customer_ids`` membatasi ke ticket/customer id tertentu — cakupan role, sama
    artinya dengan parameter senama di ``list_results``: ``None`` = tanpa batas,
    daftar KOSONG = tidak ada satu pun yang lolos (bukan "tanpa batas").
    """
    if customer_ids is not None and len(customer_ids) == 0:
        return [], 0
    ai_filter = ai_status.strip().upper() if isinstance(ai_status, str) else None
    # AI Status is only determinable for done results — override the processing status.
    effective_status = "done" if ai_filter in ("PASS", "FAIL") else status

    q = db.query(Result)
    # Tiket yang disembunyikan tidak boleh muncul di menu ini — lihat
    # ``hidden_ticket_filter``.
    q = hidden_ticket_filter(q)
    if customer_ids is not None:
        # customer_id = prefix sebelum "_" pada source file pertama (sama dengan
        # list_results, jadi kedua menu menyaring dengan aturan yang sama).
        q = q.filter(
            func.split_part(Result.source_files[0].astext, "_", 1).in_(customer_ids)
        )
    if uploaded_by_role is not None:
        q = q.filter(Result.uploaded_by_role == uploaded_by_role)
    if exclude_uploaded_by_role is not None:
        q = q.filter(
            (Result.uploaded_by_role.is_(None))
            | (Result.uploaded_by_role != exclude_uploaded_by_role)
        )
    if effective_status:
        q = q.filter(Result.status == effective_status)
    if campaign:
        q = q.filter(Result.campaign == campaign)
    if ticket_id:
        # Coarse SQL pre-filter on the first file's prefix (mirrors list_results);
        # the exact per-file check below covers bundles with mixed prefixes.
        q = q.filter(Result.source_files[0].astext.ilike(f"{ticket_id}%"))
    results = q.order_by(desc(Result.uploaded_at)).all()

    if ai_filter in ("PASS", "FAIL"):
        # Compute each result's AI Status with the same canonical helper the Results
        # table uses, then keep only the matching Approve/Reject subset.
        #
        # ``ai_status_map`` (bukan ``_result_ai_status`` langsung): ia menyusun SELURUH
        # bahannya — kekurangan dokumen, tenggat H+2, STATUS DOKUMEN, kekurangan data
        # acuan. Tanpa itu setiap tiket yang sedang menunggu dokumen terbaca FAIL di
        # sini, sehingga menu Transcripts memasukkannya ke filter "Reject" padahal di
        # daftar Results tiket itu PENDING.
        from compliance.stats_aggregate import ai_status_map

        ai_map = ai_status_map(db, results)
        results = [r for r in results if ai_map.get(str(r.id)) == ai_filter]

    rows = []
    for r in results:
        for fn in (r.source_files or []):
            if not isinstance(fn, str) or not fn:
                continue
            tid = fn.split("_", 1)[0]
            if ticket_id and not tid.lower().startswith(ticket_id.lower()):
                continue
            rows.append({
                "result_id": str(r.id),
                "filename": fn,
                "ticket_id": tid,
                "campaign": r.campaign,
                "status": r.status,
                "uploaded_at": r.uploaded_at,
                "uploaded_by_username": r.uploaded_by_username,
                "uploaded_by_role": r.uploaded_by_role,
            })

    total = len(rows)
    start = (page - 1) * limit
    return rows[start : start + limit], total


def list_results_by_date_range(
    db: Session,
    start_date,
    end_date,
    status: str = "done",
) -> list[Result]:
    """Return results whose ``uploaded_at`` date falls within ``[start_date,
    end_date]`` (both inclusive), filtered by ``status`` (default ``done``).

    ``start_date``/``end_date`` are ``date`` objects compared against the
    Asia/Jakarta (WIB) calendar date of ``uploaded_at`` (stored as naive UTC),
    inclusive on both ends. Ordered ascending by ``uploaded_at`` for stable CSV.
    """
    # naive UTC -> timestamptz (UTC) -> naive WIB -> date
    wib_date = func.date(
        func.timezone("Asia/Jakarta", func.timezone("UTC", Result.uploaded_at))
    )
    q = db.query(Result)
    # Tiket yang disembunyikan tidak boleh muncul di menu ini — lihat
    # ``hidden_ticket_filter``.
    q = hidden_ticket_filter(q)
    if status:
        q = q.filter(Result.status == status)
    q = q.filter(wib_date >= start_date)
    q = q.filter(wib_date <= end_date)
    return q.order_by(Result.uploaded_at).all()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def get_stats(
    db: Session,
    customer_ids: Optional[list[str]] = None,
    campaigns: Optional[list[str]] = None,
) -> dict:
    """Hitungan kasar per status upload, DALAM CAKUPAN pemanggil.

    ``customer_ids`` dan ``campaigns`` dimaknai sama dengan di ``list_results``:
    ``None`` = tanpa batas, daftar KOSONG = tidak ada satu pun yang lolos. Tanpa
    keduanya endpoint ini melaporkan angka SELURUH organisasi — role yang menu
    Results-nya kosong tetap membaca "98 tiket" di sini.
    """
    if customer_ids is not None and len(customer_ids) == 0:
        return {
            "total_uploaded": 0, "pending": 0, "processing": 0, "done": 0,
            "failed": 0, "avg_processing_sec": None, "active_campaigns": [],
        }
    prefix = func.split_part(Result.source_files[0].astext, "_", 1)

    def _scoped(q):
        # Tiket tersembunyi tidak ikut KPI mana pun (total upload/pending/done/failed
        # dan rata-rata waktu proses) — kalau tidak, kartu KPI akan menghitungnya
        # sementara tabel di bawahnya tidak.
        q = hidden_ticket_filter(q)
        return q.filter(prefix.in_(customer_ids)) if customer_ids is not None else q

    counts = (
        _scoped(db.query(Result.status, func.count(Result.id)))
        .group_by(Result.status)
        .all()
    )
    status_map = {row[0]: row[1] for row in counts}
    total = sum(status_map.values())
    avg_row = (
        _scoped(db.query(func.avg(Result.processing_sec)))
        .filter(Result.status == "done")
        .scalar()
    )
    active_campaigns = [c.name for c in list_campaigns(db) if c.is_active]
    if campaigns is not None:
        allowed = {(c or "").strip().casefold() for c in campaigns}
        active_campaigns = [c for c in active_campaigns if c.strip().casefold() in allowed]

    return {
        "total_uploaded": total,
        "pending": status_map.get("pending", 0),
        "processing": status_map.get("processing", 0),
        "done": status_map.get("done", 0),
        "failed": status_map.get("failed", 0),
        "avg_processing_sec": round(avg_row, 2) if avg_row else None,
        "active_campaigns": active_campaigns,
    }


def get_daily_stats(db: Session, customer_ids: Optional[list[str]] = None) -> list[dict]:
    """Upload per hari (30 hari terakhir), DALAM CAKUPAN pemanggil — ``customer_ids``
    dimaknai sama dengan di ``get_stats``."""
    from sqlalchemy import text
    if customer_ids is not None and len(customer_ids) == 0:
        return []
    # uploaded_at is naive UTC; bucket by Asia/Jakarta (WIB) calendar date.
    scope_sql = (
        " AND split_part(source_files->>0, '_', 1) = ANY(:cids)"
        if customer_ids is not None else ""
    )
    params = {"cids": list(customer_ids)} if customer_ids is not None else {}
    # Tiket tersembunyi dikeluarkan juga di sini. Fungsi ini SQL mentah, jadi
    # ``hidden_ticket_filter`` (yang bekerja pada query SQLAlchemy) tidak bisa dipakai —
    # predikatnya ditulis ulang dengan ekspresi ticket id yang sama persis.
    from compliance.stats_aggregate import hidden_ticket_ids

    hidden = [h.lower() for h in hidden_ticket_ids()]
    hidden_sql = ""
    if hidden:
        hidden_sql = " AND lower(split_part(source_files->>0, '_', 1)) <> ALL(:hidden)"
        params["hidden"] = hidden
    rows = db.execute(
        text(f"""
            SELECT
                ((uploaded_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Jakarta')::date AS day,
                COUNT(*) AS uploaded,
                SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done,
                SUM(CASE WHEN status = 'processing' THEN 1 ELSE 0 END) AS processing,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM results
            WHERE uploaded_at >= NOW() - INTERVAL '30 days'{scope_sql}{hidden_sql}
            GROUP BY ((uploaded_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Jakarta')::date
            ORDER BY day DESC
        """),
        params,
    ).fetchall()
    return [
        {
            "date": str(row.day),
            "uploaded": int(row.uploaded),
            "done": int(row.done),
            "processing": int(row.processing),
            "pending": int(row.pending),
            "failed": int(row.failed),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Statistics snapshot (daily cache of the Statistics dashboard payload)
# ---------------------------------------------------------------------------

def _wib_today_str() -> str:
    """Today's Asia/Jakarta (WIB) calendar date as ``"YYYY-MM-DD"``."""
    from datetime import timezone
    from zoneinfo import ZoneInfo

    return (
        datetime.now(timezone.utc)
        .astimezone(ZoneInfo("Asia/Jakarta"))
        .date()
        .isoformat()
    )


# ---------------------------------------------------------------------------
# Index cid -> (agent_id, submit_time)
# ---------------------------------------------------------------------------
# Tabel lokal ``tms_cashline`` sudah TIDAK diisi lagi sejak reference data pindah
# ke DWH API (services/data_dwh.py) -- isinya sisa era CSV. Semua pemetaan
# cid <-> agent_id sekarang lewat index di bawah ini.
#
# Sumber utama: ``reference_data.cashline`` yang SUDAH tersimpan di
# result_data.result_json (disisipkan worker saat evaluasi, lihat
# worker/tasks/process_transcript.py). Itu berarti SATU query SQL, tanpa
# panggilan HTTP sama sekali -- penting karena index ini dipakai agregasi
# Statistics yang menyentuh SELURUH result sekaligus.
#
# Fallback DWH API hanya untuk cid yang snapshot-nya belum punya kedua field
# (hasil evaluasi SEBELUM App A menyimpan agent_id/submit_time di cache-nya).
# Dipanggil paralel & di-memo permanen per proses: untuk tiket yang sudah
# selesai, agent_id/submit_time tidak akan berubah lagi.
_CASHLINE_ID_MEMO: dict = {}
_CASHLINE_ID_MEMO_LOCK = Lock()
_CASHLINE_FALLBACK_WORKERS = 8


def _cashline_ids_from_dwh(cids: list) -> dict:
    """``{cid: {"agent_id", "submit_time"}}`` dari DWH untuk cid yang belum ter-memo.

    Paralel (thread pool kecil) supaya latensi total ~= panggilan paling lambat,
    bukan jumlah semuanya. Kegagalan satu cid tidak menggagalkan yang lain --
    hasilnya cuma dianggap tidak diketahui, sama seperti sebelum ada index ini.
    """
    with _CASHLINE_ID_MEMO_LOCK:
        out = {c: _CASHLINE_ID_MEMO[c] for c in cids if c in _CASHLINE_ID_MEMO}
        todo = [c for c in cids if c not in _CASHLINE_ID_MEMO]
    if not todo:
        return out

    def _one(cid):
        try:
            row = data_dwh.fetch_bundle(cid).get("cashline") or {}
        except Exception:  # noqa: BLE001 — DWH down must not break Statistics
            return cid, None
        return cid, {
            "agent_id": (row.get("agent_id") or "").strip() or None,
            "submit_time": (row.get("submit_time") or "").strip() or None,
        }

    with ThreadPoolExecutor(max_workers=_CASHLINE_FALLBACK_WORKERS) as pool:
        for cid, entry in pool.map(_one, todo):
            if entry is None:
                continue
            out[cid] = entry
            # HANYA hasil positif yang di-memo. agent_id untuk tiket yang sudah
            # selesai tidak akan berubah, jadi aman disimpan selamanya. Sebaliknya
            # hasil KOSONG tidak boleh di-memo: itu bisa berarti DWH sedang error
            # atau barisnya belum masuk, dan kalau dikunci di sini tiket tersebut
            # akan macet "(Tidak diketahui)" sampai proses di-restart.
            if entry["agent_id"]:
                with _CASHLINE_ID_MEMO_LOCK:
                    _CASHLINE_ID_MEMO[cid] = entry
    return out


def cashline_agent_index(db: Session) -> dict:
    """``{cid: {"agent_id": str|None, "submit_time": str|None}}`` untuk SEMUA result.

    ``cid`` = prefix sebelum ``_`` pada source file pertama (sama dengan
    ``_customer_id()`` di compliance/stats_aggregate.py). Satu query SQL untuk
    seluruh dataset; DWH hanya disentuh untuk sisa cid yang snapshot-nya belum
    memuat agent_id (lihat catatan di atas).
    """
    latest = (
        db.query(
            func.split_part(Result.source_files[0].astext, "_", 1).label("cid"),
            ResultData.result_json["reference_data"]["cashline"]["agent_id"]
            .astext.label("agent_id"),
            ResultData.result_json["reference_data"]["cashline"]["submit_time"]
            .astext.label("submit_time"),
        )
        .join(ResultData, ResultData.result_id == Result.id)
        .distinct(Result.id)
        .order_by(Result.id, desc(ResultData.created_at))
        .all()
    )

    index: dict = {}
    for cid, agent_id, submit_time in latest:
        key = (cid or "").strip()
        if not key:
            continue
        index[key] = {
            "agent_id": (agent_id or "").strip() or None,
            "submit_time": (submit_time or "").strip() or None,
        }

    missing = [c for c, v in index.items() if not v["agent_id"]]
    if missing:
        for cid, entry in _cashline_ids_from_dwh(missing).items():
            index[cid] = entry
    return index


def customer_ids_for_agent_ids(db: Session, agent_ids) -> list[str]:
    """Return the customer/ticket ids (cid) handled by the given agent ids
    (matched case-insensitively, trimmed). Empty input => empty list.

    ``cid`` equals the customer-id prefix of a result's source filenames, so the
    returned ids can scope ``list_results(customer_ids=...)``. Dulu dibaca dari
    ``tms_cashline``; sekarang dari ``cashline_agent_index()`` -- tabel itu sudah
    tidak terisi lagi, jadi query lama selalu mengembalikan list kosong dan bikin
    Team Leader / Area Manager tidak melihat tiket apa pun.
    """
    ids = {str(a).strip().casefold() for a in (agent_ids or []) if str(a).strip()}
    if not ids:
        return []
    return [
        cid
        for cid, entry in cashline_agent_index(db).items()
        if (entry["agent_id"] or "").casefold() in ids
    ]


def customer_ids_for_campaigns(db: Session, campaigns) -> list[str]:
    """Customer/ticket id milik campaign tertentu — bentuk DAFTAR dari pembatasan
    campaign sebuah role, supaya batas itu bisa dipasang di mana pun cakupan sudah
    dinyatakan sebagai ``customer_ids`` (Results, Transcripts, snapshot Statistics).

    Diambil dari ``results.campaign``, BUKAN dari tag roster: yang ditanya di sini
    adalah "tiket ini milik campaign apa", dan itu tercatat pada tiketnya sendiri.
    Daftar campaign kosong => daftar id kosong (tidak ada yang lolos), sejalan dengan
    ``rbac.effective_campaigns_for`` yang memaknai list kosong sebagai "dibatasi ke
    tidak ada apa pun".
    """
    names = [str(c).strip().casefold() for c in (campaigns or []) if str(c).strip()]
    if not names:
        return []
    prefix = func.split_part(Result.source_files[0].astext, "_", 1)
    rows = (
        db.query(prefix)
        .filter(func.lower(func.trim(Result.campaign)).in_(names))
        .distinct()
        .all()
    )
    return [r[0] for r in rows if r[0]]


def _stats_signature(db: Session) -> str:
    """A cheap fingerprint of everything the Statistics aggregation depends on.

    Captures result count + latest upload/completion time, evaluation (result_data)
    count + latest time, appeal count + latest review time (approved appeals change
    error counts), and the active sales-database file (drives agent name / TL / AM).
    When this string is unchanged, the cached snapshot is still valid — so the
    expensive full scan only re-runs when the underlying data actually changed.
    """
    res_count, res_up, res_done = db.query(
        func.count(Result.id), func.max(Result.uploaded_at), func.max(Result.completed_at)
    ).one()
    rd_count, rd_max = db.query(
        func.count(ResultData.id), func.max(ResultData.created_at)
    ).one()
    ap_count, ap_rev = db.query(
        func.count(ErrorCodeAppeal.id), func.max(ErrorCodeAppeal.reviewed_at)
    ).one()
    sales = get_active_sales_database(db)
    sales_key = sales.object_path if sales else ""
    # Bump when the snapshot PAYLOAD SHAPE or COMPUTED VALUES change so old cached
    # rows auto-invalidate even if the underlying data is unchanged.
    # v2: added ai_status_breakdown.
    # v3: campaign_monthly reshaped to a flat per-campaign Risk Base breakdown.
    # v4: campaign_monthly restored the per-month breakdown (Risk Base kept).
    # v5: Risk Base tally counts one highest risk base per ticket (H>M>L>N>O).
    # v6: added overview_by_campaign / agents_by_campaign for the Overview campaign filter.
    # v7: agents[] carry team_leader / area_manager (Performa Sales mapping columns).
    # v8: campaign_monthly.error_rate = Total Risk (H+M+L) / submissions, not the
    #     AI-Status FAIL count; hierarchy nodes (agent/TL/AM/all_telesales) gained
    #     approve + risk_high/medium/low + total_risk.
    # v9: hierarchy error_rate reverted to reject-based (FAIL / submissions) —
    #     Total Risk / Submission is the Performa Campaign definition ONLY. The
    #     Risk Base columns stay as context.
    # v10: hierarchy nodes also carry risk_system (O) + risk_new (N).
    # v11: kebijakan SLA H+2 ikut menentukan isi snapshot (PENDING vs FAIL untuk
    #      tiket kekurangan dokumen). Tanpa ini, mematikan/menghidupkan sakelarnya
    #      tidak mengubah apa pun di Statistics sampai ada data baru masuk — angka
    #      di layar akan bertentangan dengan status di menu Results.
    # v12: perombakan metrik 28 Agustus 2026 —
    #      agents[] dapat ``pending`` dan error_rate = Not Qualified / Submissions;
    #      campaign_monthly dapat ``not_qualified`` dan error_rate = Total Risk /
    #      tiket Not Qualified, dengan risk base hanya dari tiket FAIL;
    #      hierarchy dapat ``pending`` + ``tickets`` (daun per ticket id) dan
    #      menghitung SEMUA risk base tiket Not Qualified, bukan satu tertinggi.
    # v13: aturan "Data Ascend Kosong" (28 Agustus 2026) — tiket tanpa baris
    #      ascend_custp dipaksa PENDING, jadi sebaran AI Status bisa berubah tanpa
    #      ada data baru yang masuk.
    # v14: pelanggaran non-tolerable menghalangi PENDING (28 Agustus 2026) — tiket
    #      kekurangan dokumen yang kena item tolerable=NO langsung Not Qualified.
    # v15: "Data Ascend Kosong" dipindah ke urutan PALING AKHIR (28 Agustus 2026) —
    #      kini menimpa badword & konsistensi verifikasi statik juga.
    # v16: kekurangan data acuan digeneralkan (28 Agustus 2026) — TMS kosong /
    #      agent tidak terpetakan -> Not Qualified, Ascend kosong tetap -> Pending.
    # v17: SEMUA kekurangan data acuan (transkrip/TMS/agent/Ascend) -> PENDING.
    # v18: Error Rate Performa Campaign kembali berpenyebut Submission (pembilang
    #      tetap risk base milik tiket Not Qualified saja).
    # v19: submit_time (tenggat H+2 & sumbu-x grafik) dan agent_id dibaca dari
    #      snapshot reference_data, bukan tabel tms_cashline yang sudah kosong —
    #      angka PENDING & hierarki berubah, jadi cache lama harus gugur.
    # v20: Hierarki Failure Rate -> Avg Failure Rate (2 September 2026). Penyebutnya
    #      pindah dari Submissions ke tiket Not Qualified dan hasilnya kelipatan
    #      (4.5x), bukan persen — lihat ``_avg_of``. Nilainya berubah tanpa ada data
    #      baru, jadi snapshot lama HARUS gugur.
    # v21: daftar tiket yang disembunyikan ikut menentukan isi snapshot. Nomornya
    #      LOMPAT ke v21 karena perubahan ini datang dari branch main yang juga
    #      menamainya "v19" — dua arti untuk satu nomor berarti salah satu kelompok
    #      cache tidak akan gugur. Menaikkan nomor jauh lebih murah daripada
    #      menyajikan angka basi tanpa gejala.
    # v22: kolom "Submissions" pohon Hierarki kini berarti jumlah TRANSKRIP yang
    #      dinilai (2 September 2026, dari main) — panggilan milik agent lain pada
    #      tiket dua-agent tidak ikut. Jumlah tiketnya pindah ke ``ticket_count``.
    #      Isi snapshot berubah tanpa ada data baru.
    # v23: penyebut Avg Failure Rate dikembalikan ke Total Recording (2 September
    #      2026 sore). Kolom "Submissions" pohon Hierarki berganti nama menjadi
    #      "Total Recording", dan rasionya kini sepasang dengan kolom itu:
    #      Total Failure / Total Recording, tetap ditulis sebagai kelipatan.
    #      Nilainya berubah (5.5x -> 2.8x) tanpa ada data baru, jadi snapshot lama
    #      HARUS gugur.
    version = "v23"
    sla = "1" if get_doc_sla_enabled(db) else "0"
    # Sidik jari daftar tersembunyi. WAJIB ikut: tanpa ini snapshot yang sudah
    # ter-cache akan terus menyajikan angka tiket yang baru disembunyikan sampai ada
    # perubahan data lain yang kebetulan menggeser tanda tangannya.
    hidden = ",".join(sorted(h.lower() for h in get_hidden_ticket_ids(db)))
    hidden_key = f"{len(hidden.split(',')) if hidden else 0}:{hashlib.md5(hidden.encode()).hexdigest()[:8]}"
    return (
        f"{version}|{res_count}|{res_up}|{res_done}|{rd_count}|{rd_max}"
        f"|{ap_count}|{ap_rev}|{sales_key}|sla{sla}|hid{hidden_key}"
    )


def get_or_build_stats_snapshot(db: Session, force: bool = False) -> dict:
    """Return the Statistics dashboard payload, recomputing it only when the
    underlying data changes (auto-invalidating cache).

    Each call computes a cheap ``_stats_signature``; if it matches the signature
    stored in the latest cached snapshot, that snapshot is returned as-is.
    Otherwise the payload is recomputed via
    ``compliance.stats_aggregate.compute_stats_snapshot`` (imported lazily to avoid
    an import cycle) and cached. ``force=True`` always recomputes.
    """
    sig = _stats_signature(db)
    latest = db.query(StatsSnapshot).order_by(desc(StatsSnapshot.id)).first()
    if (
        latest is not None
        and not force
        and isinstance(latest.payload, dict)
        and latest.payload.get("_signature") == sig
    ):
        return latest.payload

    from compliance.stats_aggregate import compute_stats_snapshot

    payload = compute_stats_snapshot(db)
    payload["_signature"] = sig

    # Keep one row per WIB day (overwrite today's on change; new day => new row).
    today = _wib_today_str()
    row = db.query(StatsSnapshot).filter(StatsSnapshot.snapshot_date == today).first()
    if row is None:
        row = StatsSnapshot(snapshot_date=today, payload=payload)
        db.add(row)
    else:
        row.payload = payload
        row.computed_at = datetime.utcnow()
    try:
        db.commit()
    except Exception:
        # A concurrent request may have inserted today's row; fall back to what is
        # now stored rather than failing the request.
        db.rollback()
        row = db.query(StatsSnapshot).filter(StatsSnapshot.snapshot_date == today).first()
        if row is not None and isinstance(row.payload, dict):
            return row.payload
    return payload


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

def upsert_campaign(
    db: Session,
    name: str,
    prompt_text: str,
    scorecard_text: str,
    kb_text: str,
    prompt_filename: str = None,
    scorecard_filename: str = None,
    kb_filename: str = None,
    kb_text_raw: str = None,
    riplay: dict = None,
) -> Campaign:
    """Create or replace a campaign config.

    ``kb_text`` is what the evaluator reads (KB with the RIPLAY overlay applied);
    ``kb_text_raw`` is the KB exactly as uploaded and defaults to ``kb_text``.
    ``riplay``, when given, carries the RIPLAY columns
    (``filename``/``product_name``/``similarity``/``extraction``/``applied``/
    ``uploaded_at``); omit it to leave any previously stored RIPLAY untouched.
    """
    fields = {
        "prompt_text": prompt_text,
        "scorecard_text": scorecard_text,
        "kb_text": kb_text,
        "kb_text_raw": kb_text_raw if kb_text_raw is not None else kb_text,
        "prompt_filename": prompt_filename,
        "scorecard_filename": scorecard_filename,
        "kb_filename": kb_filename,
        "is_active": True,
    }
    if riplay is not None:
        fields.update(
            riplay_filename=riplay.get("filename"),
            riplay_product_name=riplay.get("product_name"),
            riplay_similarity=riplay.get("similarity"),
            riplay_extraction=riplay.get("extraction"),
            riplay_applied=riplay.get("applied"),
            riplay_uploaded_at=riplay.get("uploaded_at"),
        )

    campaign = db.query(Campaign).filter(Campaign.name == name).first()
    if campaign:
        for key, value in fields.items():
            setattr(campaign, key, value)
    else:
        campaign = Campaign(name=name, **fields)
        db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


def get_campaign_by_name(db: Session, name: str) -> Optional[Campaign]:
    return db.query(Campaign).filter(Campaign.name == name).first()


def delete_campaign(db: Session, name: str) -> bool:
    """Delete a campaign by name. Returns True if a row was removed, else False.

    ``results`` store ``campaign`` as a plain string (no FK), so deleting a
    campaign does not affect existing results.
    """
    campaign = db.query(Campaign).filter(Campaign.name == name).first()
    if not campaign:
        return False
    db.delete(campaign)
    db.commit()
    return True


def delete_results_by_ticket_id(db: Session, ticket_id: str) -> int:
    """Delete ALL Result rows for a ticket and return how many were removed.

    The ticket id is the prefix before the first ``_`` of the first source
    filename (see stats._customer_id_from_files). Dependent rows in
    ``result_data``, ``documents``, ``qc_status_requests`` and
    ``error_code_appeals`` are removed automatically via ON DELETE CASCADE.
    Returns 0 when no matching Result exists.
    """
    rows = (
        db.query(Result)
        .filter(func.split_part(Result.source_files[0].astext, "_", 1) == ticket_id)
        .all()
    )
    count = len(rows)
    for row in rows:
        db.delete(row)
    if count:
        db.commit()
    return count


def get_active_campaign(db: Session, name: str) -> Optional[Campaign]:
    return (
        db.query(Campaign)
        .filter(Campaign.name == name, Campaign.is_active == True)
        .first()
    )


def get_active_campaign_ci(db: Session, name: str) -> Optional[Campaign]:
    """Active campaign matched **case-insensitively** by name (trimmed both sides).

    Used by the webhook to map an external ``product`` (e.g. ``"cashline"``) to its
    campaign config (e.g. ``"Cashline"``). Returns ``None`` if no active campaign
    matches.
    """
    key = str(name or "").strip().lower()
    if not key:
        return None
    return (
        db.query(Campaign)
        .filter(func.lower(func.trim(Campaign.name)) == key, Campaign.is_active == True)
        .first()
    )


def list_campaigns(db: Session) -> list[Campaign]:
    return db.query(Campaign).order_by(desc(Campaign.updated_at)).all()


# ---------------------------------------------------------------------------
# Sales database (Upload Database Sales)
# ---------------------------------------------------------------------------
def create_sales_database(
    db: Session,
    filename: str,
    object_path: str,
    mime_type: str = None,
    uploaded_by_username: str = None,
    uploaded_by_role: str = None,
) -> SalesDatabase:
    """Record a newly-uploaded sales database and make it the only active one.

    Every previously-active row is flipped to inactive first, so exactly one
    sales database (the newest) is active at any time.
    """
    db.query(SalesDatabase).filter(SalesDatabase.is_active == True).update(
        {SalesDatabase.is_active: False}
    )
    row = SalesDatabase(
        filename=filename,
        object_path=object_path,
        mime_type=mime_type,
        is_active=True,
        uploaded_by_username=uploaded_by_username,
        uploaded_by_role=uploaded_by_role,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_active_sales_database(db: Session) -> Optional[SalesDatabase]:
    """The single active sales database (newest active upload), or None."""
    return (
        db.query(SalesDatabase)
        .filter(SalesDatabase.is_active == True)
        .order_by(desc(SalesDatabase.created_at))
        .first()
    )


def list_sales_databases(db: Session) -> list[SalesDatabase]:
    return db.query(SalesDatabase).order_by(desc(SalesDatabase.created_at)).all()


def create_qc_database(
    db: Session,
    filename: str,
    object_path: str,
    mime_type: str = None,
    uploaded_by_username: str = None,
    uploaded_by_role: str = None,
) -> QcDatabase:
    """Record a newly-uploaded QC database and make it the only active one (mirrors
    ``create_sales_database``)."""
    db.query(QcDatabase).filter(QcDatabase.is_active == True).update(  # noqa: E712
        {QcDatabase.is_active: False}
    )
    row = QcDatabase(
        filename=filename,
        object_path=object_path,
        mime_type=mime_type,
        is_active=True,
        uploaded_by_username=uploaded_by_username,
        uploaded_by_role=uploaded_by_role,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_active_qc_database(db: Session) -> Optional[QcDatabase]:
    """The single active QC database (newest active upload), or None."""
    return (
        db.query(QcDatabase)
        .filter(QcDatabase.is_active == True)  # noqa: E712
        .order_by(desc(QcDatabase.created_at))
        .first()
    )


def list_qc_databases(db: Session) -> list[QcDatabase]:
    return db.query(QcDatabase).order_by(desc(QcDatabase.created_at)).all()


# ---------------------------------------------------------------------------
# Documents (Upload Document + OCR)
# ---------------------------------------------------------------------------
def create_document(
    db: Session,
    result_id: str,
    doc_type: str,
    filename: str,
    object_path: str,
    mime_type: str,
) -> Document:
    doc = Document(
        result_id=uuid.UUID(str(result_id)),
        doc_type=doc_type,
        filename=filename,
        object_path=object_path,
        mime_type=mime_type,
        status="pending",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def list_documents(db: Session, result_id: str) -> list[Document]:
    return (
        db.query(Document)
        .filter(Document.result_id == uuid.UUID(str(result_id)))
        .order_by(Document.id)
        .all()
    )


def get_document(db: Session, document_id: int) -> Optional[Document]:
    return db.query(Document).filter(Document.id == int(document_id)).first()


def result_has_documents(db: Session, result_id: str) -> bool:
    return (
        db.query(Document.id)
        .filter(Document.result_id == uuid.UUID(str(result_id)))
        .first()
        is not None
    )


def result_ids_with_documents(db: Session, result_ids: list[str]) -> set[str]:
    """Return the subset of ``result_ids`` that have at least one document.

    Single query (one IN clause) to avoid N+1 when listing the results page.
    """
    if not result_ids:
        return set()
    uuids = [uuid.UUID(str(r)) for r in result_ids]
    rows = (
        db.query(Document.result_id)
        .filter(Document.result_id.in_(uuids))
        .distinct()
        .all()
    )
    return {str(row[0]) for row in rows}


def document_types_by_result(db: Session, result_ids: list[str]) -> dict[str, set[str]]:
    """``result_id -> {doc_type, ...}`` already uploaded. Single query.

    Needed by the per-type document requirements (a card-holder similarity band asks
    for one SPECIFIC document), where "the result has some document" is not enough.
    """
    if not result_ids:
        return {}
    uuids = [uuid.UUID(str(r)) for r in result_ids]
    rows = (
        db.query(Document.result_id, Document.doc_type)
        .filter(Document.result_id.in_(uuids))
        .distinct()
        .all()
    )
    out: dict[str, set[str]] = {}
    for rid, doc_type in rows:
        out.setdefault(str(rid), set()).add(doc_type)
    return out


def result_document_types(db: Session, result_id: str) -> set[str]:
    """The document types already uploaded for one result."""
    return document_types_by_result(db, [result_id]).get(str(result_id), set())


def document_ocr_by_result(db: Session, result_ids: list[str]) -> dict[str, list[tuple]]:
    """``result_id -> [(doc_type, ocr_json), ...]`` untuk dokumen yang OCR-nya
    selesai. Satu query — dipakai agregasi statistik yang memeriksa kesesuaian jenis
    dokumen (error code C03) untuk ratusan tiket sekaligus.

    Hanya dokumen ``done`` yang dikembalikan: yang masih ``pending``/``processing``
    belum punya ``ocr_json``, dan yang ``failed`` tidak bisa dijadikan dasar tuduhan
    salah jenis.
    """
    if not result_ids:
        return {}
    uuids = [uuid.UUID(str(r)) for r in result_ids]
    rows = (
        db.query(Document.result_id, Document.doc_type, Document.ocr_json)
        .filter(Document.result_id.in_(uuids), Document.status == "done")
        .all()
    )
    out: dict[str, list[tuple]] = {}
    for rid, doc_type, ocr_json in rows:
        out.setdefault(str(rid), []).append((doc_type, ocr_json))
    return out


def document_upload_times(db: Session, result_ids: list[str]) -> dict[str, datetime]:
    """Return ``{result_id: latest document created_at}`` for the given results.

    Single grouped query to avoid N+1 when listing the results page.
    """
    if not result_ids:
        return {}
    uuids = [uuid.UUID(str(r)) for r in result_ids]
    rows = (
        db.query(Document.result_id, func.max(Document.created_at))
        .filter(Document.result_id.in_(uuids))
        .group_by(Document.result_id)
        .all()
    )
    return {str(row[0]): row[1] for row in rows}


def update_document_status(db: Session, document_id: int, status: str) -> Optional[Document]:
    doc = get_document(db, document_id)
    if not doc:
        return None
    doc.status = status
    db.commit()
    db.refresh(doc)
    return doc


def set_document_result(db: Session, document_id: int, ocr_json: dict) -> Optional[Document]:
    doc = get_document(db, document_id)
    if not doc:
        return None
    doc.ocr_json = ocr_json
    doc.status = "done"
    doc.error_message = None
    doc.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(doc)
    return doc


def set_document_failed(db: Session, document_id: int, error_message: str) -> Optional[Document]:
    doc = get_document(db, document_id)
    if not doc:
        return None
    doc.status = "failed"
    doc.error_message = error_message
    doc.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(doc)
    return doc


# ---------------------------------------------------------------------------
# Reference data (CASHLINE / CARD HOLDER) — sumber: DWH API (Aplikasi A) via
# services/data_dwh.py (API/V1 :8000), bukan lagi DB/CSV.
#
# CATATAN: `db: Session` sengaja dipertahankan di semua fungsi walau tak dipakai,
# supaya pemanggil (reference_data.py, routes) tidak perlu diubah. Nilai balik
# tetap dict ber-key nama header/kolom asli (mis. "nominal-transfer",
# "CUST_MOM_NAME"), identik dengan bentuk lama dari CSV/DB.
# ---------------------------------------------------------------------------
def get_campaign_bundle(db: Session, result_id: str) -> dict:
    """``{"cashline", "customer"}`` untuk ``result_id`` dari DWH API (di-cache).

    Satu call memuat cashline + customer sekaligus; customer sudah dicocokkan
    oleh Aplikasi A via ``no-ktpkitas`` dari cashline (bukan lagi by
    CUST_LOCAL_NAME / cust_name). ``db`` diabaikan.
    """
    return data_dwh.fetch_bundle(result_id)


def get_tms_cashline_by_result_id(db: Session, result_id: str) -> Optional[dict]:
    """Baris CASHLINE (dict ber-key header) untuk ``result_id``, atau ``None``.

    Sumber: DWH API (dashboard.campaign_cashline_ntb). ``db`` diabaikan.
    """
    return get_campaign_bundle(db, result_id).get("cashline")


def get_ascend_custp_by_result_id(db: Session, result_id: str) -> Optional[dict]:
    """Baris CARD HOLDER (dict ber-key header) untuk ``result_id``, atau ``None``.

    Sumber: DWH API (dashboard.current_cc_scmcustp), dicocokkan oleh Aplikasi A by
    ``no-ktpkitas``. MENGGANTIKAN ``get_ascend_custp_by_local_name`` lama yang
    mencari by cust_name (fungsi itu sudah dihapus — query tabel ``ascend_custp``
    yang tidak diisi lagi, dan badannya memanggil ``_row_to_dict`` yang bahkan
    tidak pernah didefinisikan di modul ini). ``db`` diabaikan.
    """
    return get_campaign_bundle(db, result_id).get("customer")


# TMS change flag -> document types allowed for upload. KK and cover_buku_tabungan
# have no TMS change that triggers them. KK is instead triggered by the
# nama_ibu_kandung similarity band (see ``compliance.documents.CARD_HOLDER_DOC_BANDS``),
# which is unioned onto this list by the callers; cover_buku_tabungan still has no
# trigger at all.
CHANGE_DOC_TYPES = {
    "kantor": ["ktp"],
    "rumah": ["ktp"],
    "nik": ["ktp"],
    "npwp": ["npwp"],
}

# Sub-kolom grup "*-new" (kunci = nama HEADER asli seperti dibalas API,
# bukan atribut ORM yang ter-sanitasi).
_KANTOR_NEW_COLS = [
    "alamat-kantor-1-new", "alamat-kantor-2-new", "kantor-rt-new", "kantor-rw-new",
    "kantor-kelurahan-new", "kantor-kecamatan-new", "kantor-kabupatenkota-new",
    "kantor-provinsi-new", "kantor-kode-pos-new",
]
_RUMAH_NEW_COLS = [
    "alamat-rumah-1-new", "alamat-rumah-2-new", "rumah-rt-new", "rumah-rw-new",
    "rumah-kelurahan-new", "rumah-kecamatan-new", "rumah-kabupatenkota-new",
    "rumah-provinsi-new", "rumah-kode-pos-new",
]


def allowed_doc_types_from_flags(flags: dict) -> list[str]:
    """Ordered list of document types allowed to upload, given the active change
    flags (see ``get_tms_cashline_change_flags``). Union across all active changes."""
    order = ["ktp", "npwp", "kk", "cover_buku_tabungan"]
    allowed = set()
    for k, doctypes in CHANGE_DOC_TYPES.items():
        if flags.get(k):
            allowed.update(doctypes)
    return [d for d in order if d in allowed]


def compute_tms_change_flags(cashline_row: Optional[dict]) -> dict:
    """[NEW] Hitung change flags (kantor/rumah/npwp/nik) dari 1 dict cashline
    yang SUDAH ADA (mis. result_json["reference_data"]["cashline"], sudah
    di-embed di final_json sejak evaluasi LLM -- lihat
    worker/tasks/process_transcript.py) -- MURNI LOKAL, TANPA HTTP ke App A
    sama sekali.

    Dipakai _build_items() (api/routers/stats.py) untuk result yang SUDAH
    punya reference_data tersimpan di result_json, supaya tidak perlu
    panggil get_tms_cashline_change_flags() (HTTP) lagi tiap ResultsView
    dibuka. get_tms_cashline_change_flags() (di bawah, HTTP per-id) TETAP
    dipakai sebagai FALLBACK untuk result LAMA yang diproses SEBELUM
    reference_data mulai disisipkan ke result_json.

    Logic PERSIS SAMA dengan yang dipakai get_tms_cashline_change_flags()
    (kolom _KANTOR_NEW_COLS/_RUMAH_NEW_COLS/no-npwp-new/nik-new yang sama)
    -- cuma menerima dict yang SUDAH ADA, bukan fetch sendiri.
    """
    if not cashline_row:
        return {}

    def _filled(row: dict, cols: list[str]) -> bool:
        return any(str(row.get(c) or "").strip() for c in cols)

    return {
        "kantor": _filled(cashline_row, _KANTOR_NEW_COLS),
        "rumah": _filled(cashline_row, _RUMAH_NEW_COLS),
        "npwp": bool(str(cashline_row.get("no-npwp-new") or "").strip()),
        "nik": bool(str(cashline_row.get("nik-new") or "").strip()),
    }


def cashline_change_flags_index(db: Session, cids=None) -> dict[str, dict]:
    """``{cid: {"kantor","rumah","npwp","nik": bool}}`` dari snapshot cashline yang
    SUDAH tersimpan di ``result_data.result_json["reference_data"]["cashline"]`` --
    SATU query SQL, TANPA HTTP ke App A sama sekali.

    Versi batch & lokal dari ``get_tms_cashline_change_flags()`` (di bawah), yang
    menembak DWH API sekali per cid secara BERURUTAN. Dengan 342 tiket ongkos HTTP-nya
    ~22 detik dan itulah penyebab utama ``/stats/ai_status_timeseries`` menembus
    timeout 30 detik di halaman Statistik. Flag-nya tetap dihitung
    ``compute_tms_change_flags()``, jadi aturannya persis sama -- yang pindah cuma
    sumber datanya.

    ``cids`` = None -> seluruh dataset; sebuah iterable -> dibatasi ke cid itu saja.
    cid yang snapshot cashline-nya BELUM ada sengaja TIDAK muncul di hasil (bukan
    muncul dengan flag serba-False), supaya pemanggil bisa membedakannya dari "sudah
    dibaca, memang tidak ada perubahan" dan menjatuhkan sisanya ke fallback DWH.
    """
    wanted = None
    if cids is not None:
        wanted = {str(c).strip() for c in cids if c}
        if not wanted:
            return {}

    rows = (
        db.query(
            func.split_part(Result.source_files[0].astext, "_", 1).label("cid"),
            ResultData.result_json["reference_data"]["cashline"].label("cashline"),
        )
        .join(ResultData, ResultData.result_id == Result.id)
        .distinct(Result.id)
        .order_by(Result.id, desc(ResultData.created_at))
        .all()
    )

    index: dict[str, dict] = {}
    for cid, cashline in rows:
        key = (cid or "").strip()
        if not key or (wanted is not None and key not in wanted):
            continue
        if not cashline:
            continue
        index[key] = compute_tms_change_flags(cashline)
    return index


def reference_snapshot_index(db: Session, cids=None) -> dict[str, dict]:
    """``{cid: {"cashline": bool, "customer": bool, "agent_id": str|None}}`` dari
    snapshot ``reference_data`` yang SUDAH tersimpan di
    ``result_data.result_json["reference_data"]`` -- SATU query SQL, TANPA HTTP.

    Menjawab "acuan tiket ini lengkap atau tidak" (lihat
    ``compliance.stats_aggregate.data_gap_map``):

    * ``cashline`` False -> tidak ada baris CASHLINE untuk ticket id ini;
    * ``customer`` False -> baris CARD HOLDER tidak ketemu (dicocokkan App A by
      ``no-ktpkitas``), jadi seluruh acuan card holder dikirim ``null`` ke LLM.

    Dulu kedua hal itu ditanyakan langsung ke tabel ``tms_cashline`` /
    ``ascend_custp``. Kedua tabel itu sudah TIDAK diisi lagi sejak reference data
    pindah ke DWH API, jadi query lama tetap jalan tanpa error tetapi SELALU
    mengembalikan nol baris -- artinya SETIAP tiket akan dilaporkan "Data TMS
    Kosong" + "Data Ascend Kosong" dan dipaksa PENDING.

    cid yang result_json-nya BELUM punya ``reference_data`` sama sekali (tiket lama,
    dievaluasi sebelum snapshot mulai disisipkan) sengaja TIDAK muncul di hasil,
    sejalan dengan ``cashline_change_flags_index()``: "belum bisa dinilai" harus bisa
    dibedakan dari "sudah dibaca, memang kosong" supaya tiket lama tidak dituduh
    kekurangan data yang sebenarnya tidak pernah diperiksa.

    ``cids`` = None -> seluruh dataset; sebuah iterable -> dibatasi ke cid itu saja.
    """
    wanted = None
    if cids is not None:
        wanted = {str(c).strip() for c in cids if c}
        if not wanted:
            return {}

    rows = (
        db.query(
            func.split_part(Result.source_files[0].astext, "_", 1).label("cid"),
            # jsonb_typeof membedakan tiga keadaan yang artinya berbeda: 'object'
            # (baris acuan ada), 'null' (sudah dicari, tidak ketemu), dan SQL NULL
            # (key-nya tidak ada sama sekali = snapshot pra-reference_data).
            func.jsonb_typeof(
                ResultData.result_json["reference_data"]["cashline"]
            ).label("cashline_type"),
            func.jsonb_typeof(
                ResultData.result_json["reference_data"]["customer"]
            ).label("customer_type"),
            ResultData.result_json["reference_data"]["cashline"]["agent_id"]
            .astext.label("agent_id"),
        )
        .join(ResultData, ResultData.result_id == Result.id)
        .distinct(Result.id)
        .order_by(Result.id, desc(ResultData.created_at))
        .all()
    )

    index: dict[str, dict] = {}
    for cid, cashline_type, customer_type, agent_id in rows:
        key = (cid or "").strip()
        if not key or (wanted is not None and key not in wanted):
            continue
        if cashline_type is None and customer_type is None:
            continue  # snapshot reference_data belum ada -> belum bisa dinilai
        index[key] = {
            "cashline": cashline_type == "object",
            "customer": customer_type == "object",
            "agent_id": (agent_id or "").strip() or None,
        }
    return index


def get_tms_cashline_change_flags(db: Session, result_ids: list[str]) -> dict[str, dict]:
    """Map ``cid -> {"kantor","rumah","npwp","nik": bool}`` dari kolom ``*-new``
    cashline yang TERISI (ada nilai baru = ada perubahan data), untuk gating tombol
    Upload Document. Sumber: DWH API per ``result_id`` (tidak ada endpoint batch,
    jadi fetch per id; hasil di-cache singkat oleh data_dwh). ``db`` diabaikan.
    """
    ids = [str(x).strip() for x in result_ids if x]
    if not ids:
        return {}

    def _filled(row: dict, cols: list[str]) -> bool:
        return any(str(row.get(c) or "").strip() for c in cols)

    out: dict[str, dict] = {}
    for cid in ids:
        if cid in out:  # dedup, mirroring get_tms_cashline_by_result_id (first wins)
            continue
        cashline = get_tms_cashline_by_result_id(db, cid)
        if not cashline:
            continue
        out[cid] = {
            "kantor": _filled(cashline, _KANTOR_NEW_COLS),
            "rumah": _filled(cashline, _RUMAH_NEW_COLS),
            "npwp": bool(str(cashline.get("no-npwp-new") or "").strip()),
            "nik": bool(str(cashline.get("nik-new") or "").strip()),
        }
    return out


def tms_submit_time_map(db: Session, cids: list[str]) -> dict[str, str]:
    """Batched map ``cid -> submit_time`` (string mentah, mis. "2026-06-17 15:24:53").
    Menyuapi timer SLA H+2 di menu Pending Check, yang menghitung sejak waktu
    pengajuan pencairan.

    [FIX] Sumbernya ``cashline_agent_index()`` — snapshot ``reference_data`` yang
    tersimpan di ``result_data.result_json`` — BUKAN lagi tabel ``tms_cashline``,
    yang sudah tidak diisi sejak reference data pindah ke DWH API. Query lama
    tetap berjalan tanpa error tetapi SELALU mengembalikan peta kosong, sehingga
    submit_time selalu None dan status PENDING tidak pernah muncul di layar.
    """
    ids = [str(x).strip() for x in cids if x]
    if not ids:
        return {}
    index = cashline_agent_index(db)
    out: dict[str, str] = {}
    for cid in ids:
        if cid in out:
            continue
        entry = index.get(cid)
        if not entry:
            continue
        submit_time = entry.get("submit_time")
        if submit_time:
            out[cid] = submit_time
    return out


# ---------------------------------------------------------------------------
# QC status-change requests (QC proposes, SPQ Head approves/rejects)
# ---------------------------------------------------------------------------
def get_qc_status_request(db: Session, result_id: str) -> Optional[QcStatusRequest]:
    return (
        db.query(QcStatusRequest)
        .filter(QcStatusRequest.result_id == uuid.UUID(str(result_id)))
        .first()
    )


def _manual_verdict(req) -> Optional[str]:
    """Manual Status EFEKTIF dari sebuah baris permintaan: vonisnya hanya berlaku
    setelah final. Duplikat kecil dari ``compliance.stats_aggregate.manual_status_of``
    supaya lapisan db/ tidak perlu mengimpor compliance/."""
    if req is None:
        return None
    tl = (getattr(req, "tl_qc_status", None) or "pending")
    if tl == "approved":
        eff = "approved"
    elif tl == "rejected":
        eff = "rejected"
    elif tl == "escalated":
        eff = getattr(req, "approval_status", None) or "pending"
    else:
        eff = "pending"
    if eff != "approved":
        return None
    return (getattr(req, "requested_status", "") or "").strip().upper() or None


def add_qc_status_event(
    db: Session,
    result_id: str,
    event: str,
    actor_username: str = None,
    actor_role: str = None,
    requested_status: str = None,
    status_before: str = None,
    status_after: str = None,
    comment: str = None,
) -> QcStatusEvent:
    """Catat satu kejadian Manual Status (append-only). Tidak pernah dipakai untuk
    menghitung status yang berlaku — murni jejak audit untuk ditampilkan."""
    row = QcStatusEvent(
        result_id=uuid.UUID(str(result_id)),
        event=event,
        actor_username=actor_username,
        actor_role=actor_role,
        requested_status=requested_status,
        status_before=status_before,
        status_after=status_after,
        comment=comment,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def qc_status_events_for_result(db: Session, result_id: str) -> list:
    """Riwayat Manual Status satu tiket, TERLAMA dulu."""
    return (
        db.query(QcStatusEvent)
        .filter(QcStatusEvent.result_id == uuid.UUID(str(result_id)))
        .order_by(QcStatusEvent.created_at, QcStatusEvent.id)
        .all()
    )


def qc_status_events_for_results(db: Session, result_ids: list[str]) -> dict:
    """``str(result_id) -> [event, ...]`` (terlama dulu) dalam satu query."""
    if not result_ids:
        return {}
    uuids = [uuid.UUID(str(r)) for r in result_ids]
    out: dict = {}
    for row in (
        db.query(QcStatusEvent)
        .filter(QcStatusEvent.result_id.in_(uuids))
        .order_by(QcStatusEvent.created_at, QcStatusEvent.id)
        .all()
    ):
        out.setdefault(str(row.result_id), []).append(row)
    return out


def _upsert_event_for(origin: str) -> str:
    """Nama kejadian riwayat Manual Status untuk sebuah ``origin``."""
    if origin == "qc":
        return "usul"
    # QC membenarkan AI Status pada penetapan pertama — final tanpa hierarki, tapi
    # bukan "ditetapkan langsung" oleh reviewer, jadi dicatat sebagai kejadian sendiri.
    if origin == "qc_confirm":
        return "konfirmasi"
    return "set_langsung"


def upsert_qc_status_request(
    db: Session,
    result_id: str,
    requested_status: str,
    reason: str,
    username: str = None,
    role: str = None,
    origin: str = "qc",
) -> QcStatusRequest:
    """Create or replace the human verdict (Manual Status) for a result.

    ``origin='qc'`` -> a QC PROPOSAL that must run the QC -> TL QC -> SPQ Head review;
    the row starts at pending on both tiers (see below).
    ``origin='tl_direct'`` / ``'spq_direct'`` -> Team Leader QC / SPQ Head setting the
    verdict THEMSELVES; they need no approval, so the row is finalized on creation by
    ``finalize_qc_status_request`` right after this call — including when it replaces a
    QC proposal that was still waiting.
    ``origin='qc_confirm'`` -> QC's FIRST verdict on a ticket that merely REPEATS the
    AI Status (nothing changes, so no hierarchy reviews it). Finalized on creation the
    same way; the caller decides this, see ``_confirms_ai_status`` in the router.

    There is ONE row per result, so a QC changing their mind overwrites the existing
    request — which means the whole tiered review (QC -> Team Leader QC -> SPQ Head)
    must start over. BOTH tiers are cleared: leaving ``tl_qc_status`` behind made the
    NEW request inherit the OLD decision, and since ``effective_appeal_status`` reads
    that field first, an inherited ``approved`` applied the new ``requested_status``
    immediately — a QC could flip a ticket to Qualified with nobody reviewing it.
    """
    req = get_qc_status_request(db, result_id)
    status_before = _manual_verdict(req)
    if req is None:
        req = QcStatusRequest(result_id=uuid.UUID(str(result_id)))
        db.add(req)
    req.requested_status = requested_status
    req.reason = reason
    req.requested_by_username = username
    req.requested_by_role = role
    req.requested_at = datetime.utcnow()
    req.origin = origin
    # Tier 1 — Team Leader QC.
    req.tl_qc_status = "pending"
    req.tl_qc_username = None
    req.tl_qc_reviewed_at = None
    req.tl_qc_comment = None
    # Tier 2 — SPQ Head (only reachable once TL QC escalates).
    req.approval_status = "pending"
    req.reviewed_by_username = None
    req.reviewed_at = None
    req.review_comment = None
    db.commit()
    db.refresh(req)
    # Usulan QC belum jadi vonis; baris direct difinalkan tepat setelah ini oleh
    # finalize_qc_status_request, yang mencatat status_after-nya sendiri.
    add_qc_status_event(
        db, result_id=result_id,
        event=_upsert_event_for(origin),
        actor_username=username, actor_role=role,
        requested_status=requested_status,
        status_before=status_before,
        status_after=(None if origin == "qc" else requested_status),
        comment=reason,
    )
    return req


def finalize_qc_status_request(
    db: Session,
    result_id: str,
    reviewer_username: str = None,
) -> Optional[QcStatusRequest]:
    """Mark a Manual Status row as FINAL without any review.

    Used for the direct set by Team Leader QC / SPQ Head, who need no approval: the
    verdict counts the moment they save it. Finalising through ``tl_qc_status`` (the
    tier ``effective_appeal_status`` reads first) means a direct verdict also REPLACES
    a QC proposal that was still waiting — the proposal's pending state is gone.
    """
    req = get_qc_status_request(db, result_id)
    if req is None:
        return None
    req.tl_qc_status = "approved"
    req.tl_qc_username = reviewer_username
    req.tl_qc_reviewed_at = datetime.utcnow()
    db.commit()
    db.refresh(req)
    return req  # kejadiannya sudah dicatat oleh upsert_qc_status_request (set_langsung)


def review_qc_status_request(
    db: Session,
    result_id: str,
    decision: str,
    reviewer_username: str = None,
    comment: str = None,
) -> Optional[QcStatusRequest]:
    """Set the request's approval to ``approved``/``rejected`` (decision is reversible).
    ``comment`` is the reviewer's note (this tier); mandatory on reject, else optional."""
    req = get_qc_status_request(db, result_id)
    if req is None:
        return None
    status_before = _manual_verdict(req)
    req.approval_status = "approved" if decision == "approve" else "rejected"
    req.reviewed_by_username = reviewer_username
    req.reviewed_at = datetime.utcnow()
    req.review_comment = comment
    db.commit()
    db.refresh(req)
    add_qc_status_event(
        db, result_id=result_id,
        event=("spq_approve" if decision == "approve" else "spq_reject"),
        actor_username=reviewer_username, actor_role="spq_head",
        requested_status=req.requested_status,
        status_before=status_before, status_after=_manual_verdict(req),
        comment=comment,
    )
    return req


def qc_status_requests_for(db: Session, result_ids: list[str]) -> dict[str, QcStatusRequest]:
    """Return ``{result_id: QcStatusRequest}`` for the given results.

    Single ``IN`` query to avoid N+1 when listing the results page.
    """
    if not result_ids:
        return {}
    uuids = [uuid.UUID(str(r)) for r in result_ids]
    rows = (
        db.query(QcStatusRequest)
        .filter(QcStatusRequest.result_id.in_(uuids))
        .all()
    )
    return {str(r.result_id): r for r in rows}


# Team Leader QC decision -> stored tl_qc_status. 'approve'/'reject' are FINAL (no
# SPQ Head); 'escalate' forwards to SPQ Head for the final call.
_TL_DECISION = {"approve": "approved", "reject": "rejected", "escalate": "escalated"}


def tl_review_qc_status_request(
    db: Session,
    result_id: str,
    decision: str,
    reviewer_username: str = None,
    comment: str = None,
) -> Optional[QcStatusRequest]:
    """Team Leader QC check of a QC AI-status request: finalize (approve/reject) or
    escalate to SPQ Head (reversible). ``comment`` is TL QC's note (any decision)."""
    req = get_qc_status_request(db, result_id)
    if req is None:
        return None
    status_before = _manual_verdict(req)
    req.tl_qc_status = _TL_DECISION.get(decision, "rejected")
    req.tl_qc_username = reviewer_username
    req.tl_qc_reviewed_at = datetime.utcnow()
    req.tl_qc_comment = comment
    db.commit()
    db.refresh(req)
    add_qc_status_event(
        db, result_id=result_id,
        event={"approve": "tl_approve", "reject": "tl_reject"}.get(decision, "tl_escalate"),
        actor_username=reviewer_username, actor_role="team_leader_qc",
        requested_status=req.requested_status,
        status_before=status_before, status_after=_manual_verdict(req),
        comment=comment,
    )
    return req


# ---------------------------------------------------------------------------
# Error Code appeals ("banding": QC appeals a single error code, SPQ Head reviews)
# ---------------------------------------------------------------------------
def create_error_code_appeal(
    db: Session,
    result_id: str,
    error_code: str,
    item_code: str,
    ai_sumber: str = None,
    ai_risk_base: str = None,
    ai_details_error: str = None,
    ai_reason: str = None,
    ai_evidence: str = None,
    ai_ticket_id: str = None,
    qc_reason: str = "",
    qc_evidence: str = None,
    qc_ticket_id: str = None,
    qc_reference_value: str = None,
    qc_extracted_value: str = None,
    qc_new_error_code: str = None,
    qc_risk_base: str = None,
    appeal_kind: str = "remove",
    add_source: str = None,
    origin: str = "qc",
    username: str = None,
) -> ErrorCodeAppeal:
    """Insert a new (pending) appeal row. Append-only, so repeated appeals on the
    same error code accumulate as history.

    ``origin`` marks a QC submission ('qc', tiered review) vs a reviewer's DIRECT
    edit ('tl_direct'/'spq_direct'); a direct row is finalized by the caller
    (via ``tl_review_error_code_appeal(decision='approve')``) so it applies at once."""
    appeal = ErrorCodeAppeal(
        result_id=uuid.UUID(str(result_id)),
        error_code=error_code,
        item_code=item_code,
        ai_sumber=ai_sumber,
        ai_risk_base=ai_risk_base,
        ai_details_error=ai_details_error,
        ai_reason=ai_reason,
        ai_evidence=ai_evidence,
        ai_ticket_id=ai_ticket_id,
        qc_reason=qc_reason,
        qc_evidence=qc_evidence,
        qc_ticket_id=qc_ticket_id,
        qc_reference_value=qc_reference_value,
        qc_extracted_value=qc_extracted_value,
        qc_new_error_code=qc_new_error_code,
        qc_risk_base=qc_risk_base,
        appeal_kind=(appeal_kind or "remove"),
        add_source=add_source,
        origin=(origin or "qc"),
        requested_by_username=username,
        requested_at=datetime.utcnow(),
        approval_status="pending",
    )
    db.add(appeal)
    db.commit()
    db.refresh(appeal)
    return appeal


def get_error_code_appeal(db: Session, appeal_id: int) -> Optional[ErrorCodeAppeal]:
    return (
        db.query(ErrorCodeAppeal)
        .filter(ErrorCodeAppeal.id == int(appeal_id))
        .first()
    )


def error_code_appeals_for_result(db: Session, result_id: str) -> list[ErrorCodeAppeal]:
    """All appeals for a result, oldest first (history order)."""
    return (
        db.query(ErrorCodeAppeal)
        .filter(ErrorCodeAppeal.result_id == uuid.UUID(str(result_id)))
        .order_by(ErrorCodeAppeal.requested_at.asc(), ErrorCodeAppeal.id.asc())
        .all()
    )


def error_code_appeals_for_results(
    db: Session, result_ids: list[str]
) -> dict[str, list[ErrorCodeAppeal]]:
    """Return ``{result_id: [appeals oldest-first]}`` for the given results.

    Single ``IN`` query to avoid N+1 when listing the results page / export.
    """
    if not result_ids:
        return {}
    uuids = [uuid.UUID(str(r)) for r in result_ids]
    rows = (
        db.query(ErrorCodeAppeal)
        .filter(ErrorCodeAppeal.result_id.in_(uuids))
        .order_by(ErrorCodeAppeal.requested_at.asc(), ErrorCodeAppeal.id.asc())
        .all()
    )
    out: dict[str, list[ErrorCodeAppeal]] = {}
    for r in rows:
        out.setdefault(str(r.result_id), []).append(r)
    return out


def review_error_code_appeal(
    db: Session,
    appeal_id: int,
    decision: str,
    reviewer_username: str = None,
    comment: str = None,
) -> Optional[ErrorCodeAppeal]:
    """Set an appeal's SPQ-Head approval to ``approved``/``rejected`` (reversible).
    ``comment`` is the reviewer's note (this tier); mandatory on reject, else optional."""
    appeal = get_error_code_appeal(db, appeal_id)
    if appeal is None:
        return None
    appeal.approval_status = "approved" if decision == "approve" else "rejected"
    appeal.reviewed_by_username = reviewer_username
    appeal.reviewed_at = datetime.utcnow()
    appeal.review_comment = comment
    db.commit()
    db.refresh(appeal)
    return appeal


def tl_review_error_code_appeal(
    db: Session,
    appeal_id: int,
    decision: str,
    reviewer_username: str = None,
    comment: str = None,
) -> Optional[ErrorCodeAppeal]:
    """Team Leader QC check of a banding appeal: finalize (approve/reject) or escalate
    to SPQ Head (reversible). 'approve'/'reject' are final; 'escalate' -> SPQ Head.
    ``comment`` is TL QC's note (any decision)."""
    appeal = get_error_code_appeal(db, appeal_id)
    if appeal is None:
        return None
    appeal.tl_qc_status = _TL_DECISION.get(decision, "rejected")
    appeal.tl_qc_username = reviewer_username
    appeal.tl_qc_reviewed_at = datetime.utcnow()
    appeal.tl_qc_comment = comment
    db.commit()
    db.refresh(appeal)
    return appeal


# ---------------------------------------------------------------------------
# QC ticket assignments (Team Leader QC assigns a ticket to a QC; 1 ticket -> 1 QC)
# ---------------------------------------------------------------------------

def assign_ticket_to_qc(
    db: Session, ticket_id: str, qc_username: str, assigned_by_username: str = None
) -> QcAssignment:
    """Assign ``ticket_id`` to ``qc_username`` (upsert — reassigns if it exists)."""
    tid = (ticket_id or "").strip()
    row = db.query(QcAssignment).filter(QcAssignment.ticket_id == tid).first()
    if row is None:
        row = QcAssignment(ticket_id=tid)
        db.add(row)
    row.qc_username = (qc_username or "").strip()
    row.assigned_by_username = assigned_by_username
    row.assigned_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


def unassign_ticket(db: Session, ticket_id: str) -> bool:
    """Remove any QC assignment for ``ticket_id``. Returns True if one was removed."""
    tid = (ticket_id or "").strip()
    row = db.query(QcAssignment).filter(QcAssignment.ticket_id == tid).first()
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def list_qc_assignments(db: Session) -> list[QcAssignment]:
    """All QC ticket assignments, newest first."""
    return db.query(QcAssignment).order_by(QcAssignment.assigned_at.desc()).all()


def assigned_ticket_ids_for_qc(db: Session, qc_username: str) -> list[str]:
    """Ticket ids assigned to ``qc_username`` (case-insensitive, trimmed)."""
    key = (qc_username or "").strip().casefold()
    if not key:
        return []
    rows = (
        db.query(QcAssignment.ticket_id)
        .filter(func.lower(func.trim(QcAssignment.qc_username)) == key)
        .all()
    )
    return [r[0] for r in rows if r[0]]


def qc_side_filter_options(db: Session) -> dict:
    """Dropdown options for the Team Leader QC Results filter: every QC and every
    QC Support account. Returns ``{"qc_users": [...], "qc_support_users": [...]}``
    where each entry is ``{"username", "name"}`` (username is the NIP the list
    endpoint filters on; name is display-only), sorted by name."""
    def _rows(role):
        users = db.query(User).filter(User.role == role, User.is_active.is_(True)).all()
        rows = [
            {"username": (u.username or "").strip(), "name": (u.name or u.username or "").strip()}
            for u in users
            if (u.username or "").strip()
        ]
        return sorted(rows, key=lambda x: x["name"].casefold())
    return {"qc_users": _rows("qc"), "qc_support_users": _rows("qc_support")}


def assignment_map_for_tickets(db: Session, ticket_ids: list[str]) -> dict:
    """Return ``{ticket_id: (qc_username, assigned_at)}`` for the given ticket ids."""
    ids = [str(t).strip() for t in (ticket_ids or []) if str(t).strip()]
    if not ids:
        return {}
    rows = db.query(QcAssignment).filter(QcAssignment.ticket_id.in_(ids)).all()
    return {r.ticket_id: (r.qc_username, r.assigned_at) for r in rows}


# ---------------------------------------------------------------------------
# QC manual checks (per-ticket "sudah dicek manual oleh QC"; append-only trail)
# ---------------------------------------------------------------------------

def create_qc_manual_check(
    db: Session, result_id: str, username: str, role: str = None, note: str = None
) -> QcManualCheck:
    """Append a manual-check approval event for ``result_id``.

    Never updates in place — re-approving inserts another row so the audit trail
    keeps every check. The newest row is the authoritative state.
    """
    row = QcManualCheck(
        result_id=result_id,
        checked_by_username=(username or "").strip(),
        checked_by_role=role,
        note=(note or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def qc_manual_checks_for_results(db: Session, result_ids: list[str]) -> dict:
    """Return ``{result_id: latest QcManualCheck}`` for the given result ids.

    Batched to avoid an N+1 in the Results list. Ordered oldest-first so the last
    write per key wins, matching "latest row is authoritative".
    """
    ids = [str(r).strip() for r in (result_ids or []) if str(r).strip()]
    if not ids:
        return {}
    rows = (
        db.query(QcManualCheck)
        .filter(QcManualCheck.result_id.in_(ids))
        .order_by(QcManualCheck.id.asc())
        .all()
    )
    return {str(r.result_id): r for r in rows}


def qc_manual_check_history(db: Session, result_id: str) -> list[QcManualCheck]:
    """Full audit trail for one ticket, oldest first."""
    return (
        db.query(QcManualCheck)
        .filter(QcManualCheck.result_id == result_id)
        .order_by(QcManualCheck.id.asc())
        .all()
    )


def customer_ids_uploaded_by_role(db: Session, role: str) -> list[str]:
    """Customer/ticket id dari seluruh result yang di-upload oleh ``role``.

    Dipakai men-scope Statistics milik QC Support ke himpunan complaint-nya sendiri —
    cakupan yang sama dengan filter ``uploaded_by_role`` di ``list_results``, supaya
    angka Statistics dan daftar Results tidak lagi bercerita berbeda.
    """
    out = []
    for (sf,) in db.query(Result.source_files).filter(Result.uploaded_by_role == role).all():
        if sf and isinstance(sf[0], str) and sf[0]:
            cid = sf[0].split("_", 1)[0].strip()
            if cid:
                out.append(cid)
    return out


def qc_performance_rows(db: Session, campaign: str = None,
                        campaigns: Optional[list[str]] = None) -> list[dict]:
    """Per-QC assignment/approval tally for the QC table in Hierarki Failure Rate.

    Returns ``[{qc_username, name, assigned, approved, approve_rate}]``.
    ``approved`` counts a QC's manual checks that land on a ticket CURRENTLY
    assigned to them, so a reassigned ticket cannot inflate the old owner's
    count. ``approve_rate`` = approved / assigned (%).

    Di UI kedua kolom itu berlabel **Checked** dan **Checked Rate** (28 Agustus 2026) —
    kunci payload sengaja dibiarkan supaya dashboard yang ter-cache tetap membacanya.

    Computed live rather than from the stats snapshot: the snapshot signature
    tracks results/appeals only, so it would not invalidate when a QC approves
    a ticket and this table would read stale.
    """
    # Filter campaign (tab Hierarki Failure Rate): daftar ticket id milik campaign itu.
    # ``QcAssignment`` menyimpan ticket_id, bukan campaign, jadi dipetakan lewat
    # prefix nama berkas sumber di ``results`` — sama seperti kolom ID di dashboard.
    # ``campaign`` = pilihan pemakai, ``campaigns`` = batas campaign role (None =
    # tanpa batas, daftar kosong = tidak ada yang lolos); keduanya dipasang bersama.
    only_tickets = None
    wanted = None
    if campaign:
        wanted = [campaign.strip().casefold()]
    if campaigns is not None:
        allowed = [str(c).strip().casefold() for c in campaigns if str(c).strip()]
        wanted = [c for c in wanted if c in allowed] if wanted is not None else allowed
    if wanted is not None:
        only_tickets = set()
        if wanted:
            for sf in db.query(Result.source_files).filter(
                func.lower(func.trim(Result.campaign)).in_(wanted)
            ).all():
                files = sf[0] or []
                if files and isinstance(files[0], str) and files[0]:
                    only_tickets.add(files[0].split("_", 1)[0].strip())

    # qc_username (casefold) -> {ticket_id}
    assigned: dict = defaultdict(set)
    for row in db.query(QcAssignment).all():
        u = (row.qc_username or "").strip()
        if u and row.ticket_id:
            tid = row.ticket_id.strip()
            if only_tickets is not None and tid not in only_tickets:
                continue
            assigned[u.casefold()].add(tid)

    checks = db.query(QcManualCheck).all()
    # Map only the results that actually carry a check -> their ticket id.
    checked_result_ids = {str(c.result_id) for c in checks}
    ticket_by_result: dict = {}
    if checked_result_ids:
        for r in db.query(Result.id, Result.source_files).filter(
            Result.id.in_(list(checked_result_ids))
        ).all():
            sf = r.source_files or []
            if sf and isinstance(sf[0], str) and sf[0]:
                ticket_by_result[str(r.id)] = sf[0].split("_", 1)[0]

    approved: dict = defaultdict(set)
    for c in checks:
        u = (c.checked_by_username or "").strip().casefold()
        tid = ticket_by_result.get(str(c.result_id))
        if u and tid and tid in assigned.get(u, ()):
            approved[u].add(tid)

    # Every QC account appears, including ones with nothing assigned yet.
    names = {
        (u.username or "").strip().casefold(): (u.name or u.username)
        for u in db.query(User).filter(User.role == "qc").all()
    }
    rows = []
    for key in set(names) | set(assigned):
        n_assigned = len(assigned.get(key, ()))
        n_approved = len(approved.get(key, ()))
        rows.append({
            "qc_username": key,
            "name": names.get(key) or key,
            "assigned": n_assigned,
            "approved": n_approved,
            "approve_rate": round(n_approved / n_assigned * 100, 1) if n_assigned else 0.0,
        })
    rows.sort(key=lambda x: (-x["assigned"], x["name"].casefold()))
    return rows


def qc_username_for_ticket(db: Session, ticket_id: str) -> Optional[str]:
    """The QC assigned to ``ticket_id``, or None."""
    tid = (ticket_id or "").strip()
    if not tid:
        return None
    row = db.query(QcAssignment).filter(QcAssignment.ticket_id == tid).first()
    return row.qc_username if row else None


def latest_appeal_for_row(
    appeals: list[ErrorCodeAppeal], error_code: str, item_code: str
) -> Optional[ErrorCodeAppeal]:
    """Latest appeal matching an error-code row, or None. ``appeals`` is assumed
    to be ordered oldest-first (as returned by the fetch helpers above)."""
    match = [
        a for a in appeals
        if a.error_code == error_code and a.item_code == item_code
    ]
    return match[-1] if match else None


# ---------------------------------------------------------------------------
# Reprocess All Ticket (menu Upload Data, khusus Admin)
# ---------------------------------------------------------------------------

def _ticket_id_of(row: Result) -> Optional[str]:
    """Ticket id sebuah Result = prefix sebelum ``_`` pada file sumber pertama.

    Definisi yang sama dipakai di seluruh sistem (``stats._customer_id_from_files``,
    ``delete_results_by_ticket_id``, ``qc_assignments.ticket_id``); ditulis ulang di
    sini agar pengelompokan job reproses tidak menyimpang darinya.
    """
    files = row.source_files or []
    if not files or not isinstance(files[0], str) or not files[0]:
        return None
    return files[0].split("_", 1)[0]


def reprocess_ticket_plan(db: Session, campaigns: list[str]) -> list[dict]:
    """Rencana reproses: satu entri per UNIQUE ticket id pada ``campaigns``.

    Tiap entri berisi ``ticket_id``, ``campaign``, ``source_result_id`` (row TERBARU
    milik ticket itu — dari sanalah transkrip PDF disalin) dan ``old_result_ids``
    (SELURUH row yang ada sekarang untuk ticket itu, termasuk row terbaru tadi).

    Nama campaign dicocokkan case-insensitive dan ter-trim, karena kolom
    ``results.campaign`` hanyalah string bebas hasil salinan saat upload — bukan FK
    ke tabel ``campaigns`` — sehingga ejaannya bisa berbeda kapitalisasi dari nama
    campaign yang dipilih di layar.

    Row tanpa ``source_files`` dilewati: tanpa nama berkas, ticket id-nya tidak bisa
    ditentukan dan transkripnya tidak bisa ditemukan di storage.
    """
    wanted = {(c or "").strip().casefold() for c in (campaigns or []) if (c or "").strip()}
    if not wanted:
        return []
    rows = (
        db.query(Result)
        .filter(func.lower(func.trim(Result.campaign)).in_(wanted))
        .order_by(desc(Result.uploaded_at))
        .all()
    )
    plan: dict[str, dict] = {}
    for row in rows:  # sudah urut terbaru -> terlama
        tid = _ticket_id_of(row)
        if not tid:
            continue
        entry = plan.get(tid)
        if entry is None:
            plan[tid] = {
                "ticket_id": tid,
                "campaign": row.campaign,
                "source_result_id": str(row.id),
                "old_result_ids": [str(row.id)],
            }
        else:
            entry["old_result_ids"].append(str(row.id))
    return list(plan.values())


def reprocess_plan_for_tickets(db: Session, ticket_ids: list) -> list[dict]:
    """Rencana reproses untuk SEKUMPULAN ticket id — satu query, bukan N.

    Dipakai tombol Reprocess All di menu Results, yang daftar tiketnya datang dari
    filter layar (``_resolve_filtered_results``) dan karena itu tidak bisa
    dinyatakan sebagai "campaign apa" seperti ``reprocess_ticket_plan``.

    Bentuk tiap entri identik dengan kedua fungsi rencana lainnya, dan pencocokan
    ticket id-nya memakai ``split_part(source_files[0], '_', 1)`` yang sama —
    sehingga Reprocess, Delete, dan Reprocess All pada baris yang sama selalu
    berbicara tentang kumpulan row yang sama.

    Ticket id yang tidak punya row dilewati diam-diam: tiket bisa saja terhapus
    antara layar memuat daftarnya dan Admin menekan tombolnya.
    """
    tids = sorted({(t or "").strip() for t in (ticket_ids or []) if (t or "").strip()})
    if not tids:
        return []
    rows = (
        db.query(Result)
        .filter(func.split_part(Result.source_files[0].astext, "_", 1).in_(tids))
        .order_by(desc(Result.uploaded_at))
        .all()
    )
    plan: dict[str, dict] = {}
    for row in rows:  # sudah urut terbaru -> terlama
        tid = _ticket_id_of(row)
        if not tid:
            continue
        entry = plan.get(tid)
        if entry is None:
            plan[tid] = {
                "ticket_id": tid,
                "campaign": row.campaign,
                "source_result_id": str(row.id),
                "old_result_ids": [str(row.id)],
            }
        else:
            entry["old_result_ids"].append(str(row.id))
    return list(plan.values())


def reprocess_plan_for_ticket(db: Session, ticket_id: str) -> Optional[dict]:
    """Rencana reproses untuk SATU ticket id — bentuknya sama dengan satu entri
    ``reprocess_ticket_plan``, atau ``None`` bila tiketnya tidak ada.

    Dipakai tombol Reprocess di menu Results. Pencocokan ticket id-nya memakai
    ``split_part(source_files[0], '_', 1)`` di sisi database, sama persis dengan
    ``delete_results_by_ticket_id``, sehingga tombol Reprocess dan tombol Delete
    pada baris yang sama selalu berbicara tentang kumpulan row yang sama.

    ``source_result_id`` adalah row TERBARU milik tiket itu (dari sanalah transkrip
    PDF disalin), dan ``old_result_ids`` berisi SELURUH row yang ada sekarang —
    termasuk row terbaru tadi.
    """
    tid = (ticket_id or "").strip()
    if not tid:
        return None
    rows = (
        db.query(Result)
        .filter(func.split_part(Result.source_files[0].astext, "_", 1) == tid)
        .order_by(desc(Result.uploaded_at))
        .all()
    )
    if not rows:
        return None
    newest = rows[0]
    return {
        "ticket_id": tid,
        "campaign": newest.campaign,
        "source_result_id": str(newest.id),
        "old_result_ids": [str(r.id) for r in rows],
    }


def create_reprocess_job(
    db: Session,
    campaigns: list[str],
    plan: list[dict],
    username: str = None,
    scope: str = "campaign",
) -> ReprocessJob:
    """Simpan satu job beserta seluruh item-nya (satu item per unique ticket id)."""
    job = ReprocessJob(
        campaigns=list(campaigns or []),
        scope=scope,
        status="running",
        total_tickets=len(plan),
        created_by_username=username,
    )
    db.add(job)
    db.flush()  # butuh job.id untuk item-nya
    for entry in plan:
        db.add(
            ReprocessJobItem(
                job_id=job.id,
                ticket_id=entry["ticket_id"],
                campaign=entry.get("campaign"),
                old_result_ids=entry["old_result_ids"],
                source_result_id=uuid.UUID(str(entry["source_result_id"])),
                status="pending",
            )
        )
    db.commit()
    db.refresh(job)
    return job


def get_reprocess_job(db: Session, job_id: str) -> Optional[ReprocessJob]:
    return (
        db.query(ReprocessJob)
        .filter(ReprocessJob.id == uuid.UUID(str(job_id)))
        .first()
    )


def list_reprocess_jobs(
    db: Session, limit: int = 10, scope: Optional[str] = None
) -> list[ReprocessJob]:
    q = db.query(ReprocessJob)
    if scope:
        q = q.filter(ReprocessJob.scope == scope)
    return q.order_by(desc(ReprocessJob.created_at)).limit(limit).all()


def running_reprocess_job(db: Session, scope: Optional[str] = None) -> Optional[ReprocessJob]:
    """Job yang masih berjalan, kalau ada — dicari lewat status, bukan dengan
    memeriksa N job terakhir, supaya job lama yang tersangkut tetap terlihat.

    ``scope`` menyaring jenis job: ``"campaign"`` untuk job massal saja (dipakai
    layar Reprocess All Ticket saat menyambung kembali job-nya), ``None`` untuk
    keduanya (dipakai pengaman sebelum menjalankan job massal baru).
    """
    q = db.query(ReprocessJob).filter(ReprocessJob.status == "running")
    if scope:
        q = q.filter(ReprocessJob.scope == scope)
    return q.order_by(desc(ReprocessJob.created_at)).first()


# Status item yang berarti "tiket ini sedang direproses": belum dikerjakan atau
# sedang dikerjakan, pada job yang masih berjalan. Dipakai BERSAMA oleh pengaman
# 409 (`active_reprocess_item_for_ticket`) dan penanda tombol per halaman
# (`active_reprocess_ticket_ids`) — dua definisi terpisah akan membuat tombol
# Reprocess berbohong: enable padahal server menolak, atau sebaliknya.
REPROCESS_ACTIVE_ITEM_STATUSES = ["pending", "processing"]


def active_reprocess_item_for_ticket(db: Session, ticket_id: str) -> Optional[ReprocessJobItem]:
    """Item yang sedang mengantre/berjalan untuk sebuah ticket id, kalau ada.

    Pengaman tombol Reprocess di Results: menekan tombol dua kali akan membuat dua
    row baru untuk tiket yang sama, dan job yang kalah cepat mencoba menghapus row
    yang sudah tidak ada. Job yang sudah ``cancelled`` tidak dihitung — itemnya
    memang tidak akan dikerjakan.
    """
    tid = (ticket_id or "").strip()
    if not tid:
        return None
    return (
        db.query(ReprocessJobItem)
        .join(ReprocessJob, ReprocessJob.id == ReprocessJobItem.job_id)
        .filter(
            ReprocessJobItem.ticket_id == tid,
            ReprocessJobItem.status.in_(REPROCESS_ACTIVE_ITEM_STATUSES),
            ReprocessJob.status == "running",
        )
        .order_by(desc(ReprocessJobItem.id))
        .first()
    )


def active_reprocess_ticket_ids(db: Session, ticket_ids: list) -> set:
    """Dari ``ticket_ids``, mana yang sedang direproses — satu query, bukan N.

    Menu Results memakai ini untuk menahan tombol Reprocess-nya SETELAH refresh
    atau pindah menu. Tanpa ini layar hanya tahu job yang ia mulai sendiri di sesi
    itu (``reprocessActive`` di ``ResultsView.vue`` adalah state komponen), jadi
    begitu komponennya dibuang, tiket yang masih mengantre tampil siap ditekan
    lagi — dan Admin baru mendapat 409 sesudah mengonfirmasi modalnya.

    Sengaja MENERIMA daftar tiket, bukan mengembalikan seluruh antrean: dipanggil
    per halaman (<= 100 baris), sementara antreannya bisa ratusan job sekaligus.

    Scope job tidak disaring. Job massal (``campaign``) membekukan row tiket ini
    juga dan ``reprocess_single_ticket`` memang menolaknya — kalau di sini hanya
    scope ``ticket`` yang dihitung, tombolnya akan tampil enable selama job massal
    berjalan lalu ditolak 409.
    """
    tids = sorted({t.strip() for t in (ticket_ids or []) if (t or "").strip()})
    if not tids:
        return set()
    rows = (
        db.query(ReprocessJobItem.ticket_id)
        .join(ReprocessJob, ReprocessJob.id == ReprocessJobItem.job_id)
        .filter(
            ReprocessJobItem.ticket_id.in_(tids),
            ReprocessJobItem.status.in_(REPROCESS_ACTIVE_ITEM_STATUSES),
            ReprocessJob.status == "running",
        )
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def get_reprocess_item(db: Session, item_id: int) -> Optional[ReprocessJobItem]:
    return db.query(ReprocessJobItem).filter(ReprocessJobItem.id == item_id).first()


def reprocess_job_items(db: Session, job_id: str) -> list[ReprocessJobItem]:
    return (
        db.query(ReprocessJobItem)
        .filter(ReprocessJobItem.job_id == uuid.UUID(str(job_id)))
        .order_by(ReprocessJobItem.id)
        .all()
    )


def update_reprocess_item(db: Session, item_id: int, **fields) -> Optional[ReprocessJobItem]:
    item = get_reprocess_item(db, item_id)
    if item is None:
        return None
    for key, value in fields.items():
        setattr(item, key, value)
    db.commit()
    db.refresh(item)
    return item


def reprocess_job_counts(db: Session, job_id: str) -> dict:
    """Jumlah item per status untuk satu job (dasar bar kemajuan di layar)."""
    rows = (
        db.query(ReprocessJobItem.status, func.count(ReprocessJobItem.id))
        .filter(ReprocessJobItem.job_id == uuid.UUID(str(job_id)))
        .group_by(ReprocessJobItem.status)
        .all()
    )
    counts = {"pending": 0, "processing": 0, "done": 0, "failed": 0, "skipped": 0}
    for st, n in rows:
        counts[st] = counts.get(st, 0) + n
    return counts


def cancel_reprocess_job(db: Session, job_id: str) -> Optional[ReprocessJob]:
    """Tandai job dibatalkan dan lewati item yang BELUM mulai.

    Item yang sedang ``processing`` sengaja dibiarkan selesai: panggilan LLM-nya
    sudah dibayar, menghentikannya di tengah jalan hanya membuang hasil itu.
    """
    job = get_reprocess_job(db, job_id)
    if job is None or job.status != "running":
        return job
    job.status = "cancelled"
    db.query(ReprocessJobItem).filter(
        ReprocessJobItem.job_id == job.id,
        ReprocessJobItem.status == "pending",
    ).update({"status": "skipped"}, synchronize_session=False)
    db.commit()
    db.refresh(job)
    finish_reprocess_job_if_complete(db, job_id)
    return job


def finish_reprocess_job_if_complete(db: Session, job_id: str) -> Optional[ReprocessJob]:
    """Tutup job begitu tidak ada lagi item ``pending``/``processing``.

    Dipanggil dari worker setiap kali satu item selesai — job yang dibatalkan tetap
    berstatus ``cancelled``, hanya ``finished_at``-nya yang terisi.
    """
    job = get_reprocess_job(db, job_id)
    if job is None or job.finished_at is not None:
        return job
    counts = reprocess_job_counts(db, job_id)
    if counts["pending"] or counts["processing"]:
        return job
    if job.status == "running":
        job.status = "done"
    job.finished_at = datetime.now()
    db.commit()
    db.refresh(job)
    return job


def delete_results_by_ids(db: Session, result_ids) -> int:
    """Hapus row Result tertentu (beserta turunannya lewat ON DELETE CASCADE).

    Dipakai job reproses untuk membuang row LAMA satu ticket id setelah row barunya
    berstatus ``done``. Berbeda dengan ``delete_results_by_ticket_id`` yang menyapu
    semua row seticket, di sini yang dihapus HANYA id yang dibekukan saat job
    dibuat.
    """
    ids = [uuid.UUID(str(r)) for r in (result_ids or [])]
    if not ids:
        return 0
    rows = db.query(Result).filter(Result.id.in_(ids)).all()
    for row in rows:
        db.delete(row)
    if rows:
        db.commit()
    return len(rows)


# --- Kebijakan tingkat aplikasi (app_settings) -----------------------------
# Sakelar yang dulunya konstanta di kode. Dibaca di jalur panas (setiap baris
# Results/Statistics memanggil _doc_sla_expired), jadi nilainya di-cache di proses
# dan hanya menyentuh DB saat cache dingin atau sesudah diubah.

DOC_SLA_SETTING_KEY = "doc_sla_enabled"

# Cache per-proses. None = belum pernah dibaca. Setiap worker/uvicorn punya
# salinannya sendiri; ``set_app_setting`` hanya membersihkan salinan miliknya, jadi
# proses lain menyusul saat TTL-nya habis.
_APP_SETTING_CACHE: dict = {}
_APP_SETTING_CACHE_AT: dict = {}
_APP_SETTING_TTL_SEC = 10.0


def get_app_setting(db: Session, key: str, default: str = "") -> str:
    """Nilai setting (string) dengan cache pendek berbasis TTL."""
    import time

    now = time.monotonic()
    at = _APP_SETTING_CACHE_AT.get(key)
    if at is not None and (now - at) < _APP_SETTING_TTL_SEC:
        return _APP_SETTING_CACHE[key]
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    val = row.value if row is not None else default
    _APP_SETTING_CACHE[key] = val
    _APP_SETTING_CACHE_AT[key] = now
    return val


def get_app_setting_row(db: Session, key: str):
    """Baris mentah ``app_settings`` (untuk menampilkan kapan & oleh siapa diubah).
    None bila belum pernah disimpan. TIDAK di-cache — hanya dipakai di layar
    pengaturan, bukan di jalur panas."""
    return db.query(AppSetting).filter(AppSetting.key == key).first()


def set_app_setting(db: Session, key: str, value: str, username: str = None) -> str:
    """Simpan setting dan segarkan cache proses ini."""
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row is None:
        row = AppSetting(key=key, value=value, updated_by_username=username)
        db.add(row)
    else:
        row.value = value
        row.updated_by_username = username
        row.updated_at = datetime.utcnow()
    db.commit()
    _APP_SETTING_CACHE.pop(key, None)
    _APP_SETTING_CACHE_AT.pop(key, None)
    return value


HIDDEN_TICKETS_SETTING_KEY = "hidden_ticket_ids"


def get_hidden_ticket_ids(db: Session) -> tuple:
    """Ticket id yang DISEMBUNYIKAN dari seluruh permukaan pembacaan.

    Dipakai untuk menyiapkan tampilan presentasi (permintaan bisnis 31 Agustus 2026):
    tiket yang data acuannya kosong (Ascend/TMS/agent) dan tiket ber-Failure Rate di
    atas 100% ditahan agar tidak muncul di menu Statistik, Results, dan Transcripts.

    Disimpan sebagai DAFTAR TETAP (dipisah koma) di ``app_settings``, bukan aturan yang
    dihitung ulang tiap saat. Dua alasannya:

    * kriteria ">100%" bersifat MELINGKAR — rasionya dihitung oleh Statistik, sementara
      Statistik itu sendiri yang sedang disaring;
    * isinya harus tetap sama sepanjang presentasi, apa pun yang terjadi pada data.

    Mengosongkan nilainya mematikan penyembunyian seluruhnya. Perbandingannya
    case-insensitive dan spasinya dibuang, supaya salin-tempel dari spreadsheet tidak
    diam-diam meleset.
    """
    raw = get_app_setting(db, HIDDEN_TICKETS_SETTING_KEY, "")
    return tuple(
        part.strip() for part in str(raw or "").replace("\n", ",").split(",") if part.strip()
    )


def set_hidden_ticket_ids(db: Session, ticket_ids, username: str = None) -> tuple:
    """Simpan daftar ticket id yang disembunyikan. List kosong = tidak ada yang ditahan."""
    clean = [str(t).strip() for t in (ticket_ids or []) if str(t or "").strip()]
    set_app_setting(db, HIDDEN_TICKETS_SETTING_KEY, ",".join(clean), username)
    return tuple(clean)


def hidden_ticket_filter(query):
    """Sisipkan penyaring "bukan tiket tersembunyi" ke sebuah query ``Result``.

    Ticket id diturunkan dari nama berkas pertama (``<ticket>_<timestamp>.pdf``) —
    ekspresi yang SAMA dengan yang dipakai penyaring cakupan dan penghapusan tiket,
    jadi tidak ada definisi "ticket id" kedua yang bisa melenceng.

    Pemanggil menyuntikkan daftarnya lewat ``compliance.stats_aggregate.hidden_ticket_ids()``
    supaya tidak perlu sesi DB di jalur panas.
    """
    from compliance.stats_aggregate import hidden_ticket_ids

    hidden = hidden_ticket_ids()
    if not hidden:
        return query
    prefix = func.lower(func.split_part(Result.source_files[0].astext, "_", 1))
    return query.filter(~prefix.in_([h.lower() for h in hidden]))


def get_doc_sla_enabled(db: Session) -> bool:
    """True bila kebijakan tenggat H+2 dokumen pendukung sedang AKTIF.

    Default AKTIF bila barisnya belum ada — sama dengan perilaku konstanta lama,
    supaya database yang belum sempat di-seed tidak diam-diam mematikan aturannya.
    """
    return str(get_app_setting(db, DOC_SLA_SETTING_KEY, "true")).strip().lower() == "true"


def set_doc_sla_enabled(db: Session, enabled: bool, username: str = None) -> bool:
    set_app_setting(db, DOC_SLA_SETTING_KEY, "true" if enabled else "false", username)
    return enabled
