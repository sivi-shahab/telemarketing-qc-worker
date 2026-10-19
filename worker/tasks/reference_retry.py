"""Retry otomatis data acuan TMS/Ascend (19 Oktober 2026).

Tiket yang saat dievaluasi tidak punya baris CASHLINE (TMS) dan/atau CARD HOLDER
(Ascend) di DWH TIDAK dinilai LLM — gerbang data acuan di ``process_transcript``
langsung menyimpannya PENDING ("Data TMS Kosong · Data Ascend Kosong"). Sebelum
task ini tidak ada yang menilainya ulang begitu datanya masuk DWH, jadi tiketnya
PENDING selamanya kecuali Admin menekan Reprocess.

Tiap ``INTERVAL_MIN`` menit (beat, lihat ``celery_app``):

  1. ambil tiket ber-gap TMS/Ascend yang diunggah dalam ``REFERENCE_RETRY_MAX_AGE``
     (``crud.reference_gap_retry_plan`` — tanpa yang sedang direproses / punya
     vonis Manual Status yang sudah final; pengajuan pending/ditolak tidak menahan);
  2. tanyakan ulang DWH per tiket (``data_dwh.fetch_bundle_checked``; worker tidak
     membaca cache Redis, jadi jawabannya segar);
  3. bila CASHLINE dan CARD HOLDER kini KEDUANYA ada, kirim job reproses
     ``scope="ticket"`` — alur yang sama dengan tombol Reprocess di Results, BEDANYA
     pekerjaan QC row lama (Manual Status, riwayat, cek manual, dokumen) dibawa ke row
     baru (``crud.carry_over_qc_work``). Satu sisi saja belum cukup: gerbangnya akan
     memPENDING-kan tiket itu lagi;
  4. catat ringkasan putaran di ``app_settings`` (dibaca halaman Results).

Saklar: ``REFERENCE_RETRY_ENABLED`` (bawaan menyala; ``0`` mematikan).
"""
import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from db import crud
from services import data_dwh
from worker.celery_app import celery_app
from worker.config import get_worker_settings

logger = logging.getLogger(__name__)

INTERVAL_MIN = 30
# Rem per putaran: tiap tiket yang lolos = satu evaluasi LLM penuh. Sisanya diambil
# putaran berikutnya.
MAX_PER_RUN = int(os.getenv("REFERENCE_RETRY_MAX_PER_RUN", "50"))
SYSTEM_USERNAME = crud.REFERENCE_RETRY_USERNAME
# Worker kube (10.158.3.13) menulis ke DB yang sama; kunci ini mencegah dua beat
# mengantre tiket yang sama pada putaran yang sama.
_LOCK_KEY = 20261019


def enabled() -> bool:
    return os.getenv("REFERENCE_RETRY_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def run_once(db, now: datetime = None) -> dict:
    now = now or _utcnow()
    summary = {"checked": 0, "requeued": 0, "still_missing": 0, "dwh_unreachable": 0,
               "skipped_bulk_job": False}

    if crud.running_reprocess_job(db, scope="campaign") is not None:
        # Job massal membekukan row lama tiket-tiketnya; jangan bertabrakan (aturan
        # yang sama dengan tombol Reprocess, lihat api/routers/reprocess.py).
        summary["skipped_bulk_job"] = True
        plans = []
    else:
        plans = crud.reference_gap_retry_plan(db, now - crud.REFERENCE_RETRY_MAX_AGE)

    for plan in plans:
        if summary["requeued"] >= MAX_PER_RUN:
            break
        summary["checked"] += 1
        bundle, pasti = data_dwh.fetch_bundle_checked(plan["ticket_id"])
        if not pasti:
            summary["dwh_unreachable"] += 1
            continue
        if bundle.get("cashline") is None or bundle.get("customer") is None:
            summary["still_missing"] += 1
            continue
        job = crud.create_reprocess_job(
            db, [plan["campaign"]], [plan], SYSTEM_USERNAME, scope="ticket"
        )
        for item in crud.reprocess_job_items(db, job.id):
            celery_app.send_task("worker.tasks.reprocess_ticket.reprocess_ticket",
                                 args=[item.id])
        summary["requeued"] += 1
        logger.info("retry data acuan: ticket %s (gap %s) kini lengkap di DWH -> reproses job %s",
                    plan["ticket_id"], "+".join(plan["gaps"]), job.id)

    summary["pending_tickets"] = len(plans)
    summary["last_run_at"] = now.replace(tzinfo=timezone.utc).isoformat()
    summary["interval_min"] = INTERVAL_MIN
    summary["max_age_days"] = crud.REFERENCE_RETRY_MAX_AGE.days
    crud.set_app_setting(db, crud.REFERENCE_RETRY_SETTING_KEY, json.dumps(summary),
                         SYSTEM_USERNAME)
    return summary


@celery_app.task(name="worker.tasks.reference_retry.retry_reference_gaps",
                 ignore_result=True)
def retry_reference_gaps():
    if not enabled():
        return {"disabled": True}
    # Kunci advisory tingkat SESI hanya dilepas oleh koneksi yang memegangnya, jadi
    # Session diikat ke satu koneksi tetap — Session biasa mengembalikan koneksinya ke
    # pool di setiap commit dan unlock-nya bisa jatuh ke koneksi lain.
    settings = get_worker_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True,
                           connect_args=settings.db_connect_args)
    try:
        with engine.connect() as conn:
            got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}).scalar()
            conn.commit()
            if not got:
                return {"locked": True}
            db = Session(bind=conn, autoflush=False)
            try:
                summary = run_once(db)
            finally:
                db.close()
                conn.rollback()
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
                conn.commit()
    finally:
        engine.dispose()
    if summary["requeued"] or summary["dwh_unreachable"]:
        logger.warning("retry data acuan: %s", summary)
    return summary
