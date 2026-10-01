"""Tugas pemeliharaan periodik (dijadwalkan Celery beat, lihat ``celery_app``).

``fail_stale_processing_results`` menutup result yang tertahan ``processing``
karena worker-nya mati di tengah jalan — lihat ``crud.STALE_PROCESSING_AFTER``.
``fail_stale_ocr_images`` melakukan hal serupa untuk gambar menu OCR Gambar yang
menggantung ``pending``/``processing`` — lihat ``crud.fail_stale_ocr_images``.
"""
import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import crud
from worker.celery_app import celery_app
from worker.config import get_worker_settings

logger = logging.getLogger(__name__)


def _session_factory():
    settings = get_worker_settings()
    engine = create_engine(
        settings.database_url, pool_pre_ping=True, connect_args=settings.db_connect_args
    )
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


# ignore_result: jalan tiap 2 menit; dengan result_expires 30 hari hasilnya saja akan
# menumpuk ±21.600 key di Redis yang tidak pernah dibaca siapa pun.
@celery_app.task(name="worker.tasks.maintenance.fail_stale_processing_results",
                 ignore_result=True)
def fail_stale_processing_results():
    db = _session_factory()()
    try:
        ids = crud.fail_stale_processing_results(db)
    finally:
        db.close()
    if ids:
        logger.warning("%d result processing basi ditutup jadi failed: %s",
                       len(ids), ", ".join(ids))
    return {"failed": len(ids)}


# OCR Gambar (1 Oktober 2026): tanpa ini gambar yang task-nya hilang atau worker-nya
# mati tertahan pending/processing selamanya dan tombol "Proses ulang" tak muncul.
@celery_app.task(name="worker.tasks.maintenance.fail_stale_ocr_images",
                 ignore_result=True)
def fail_stale_ocr_images():
    db = _session_factory()()
    try:
        count = crud.fail_stale_ocr_images(db)
    finally:
        db.close()
    if count:
        logger.warning("%d gambar OCR pending/processing basi ditutup jadi failed", count)
    return {"failed": count}


# Snapshot Statistics global disegarkan proaktif (28 September 2026): API sudah
# menyajikan snapshot lama sambil menghitung ulang di latar, tugas ini membuat angka
# global hampir selalu terbaru. Bila data tidak berubah hanya signature yang dihitung.
@celery_app.task(name="worker.tasks.maintenance.refresh_stats_snapshot",
                 ignore_result=True)
def refresh_stats_snapshot():
    db = _session_factory()()
    try:
        crud.get_or_build_stats_snapshot(db)
    finally:
        db.close()
