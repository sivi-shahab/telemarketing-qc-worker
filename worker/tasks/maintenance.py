"""Tugas pemeliharaan periodik (dijadwalkan Celery beat, lihat ``celery_app``).

``fail_stale_processing_results`` menutup result yang tertahan ``processing``
karena worker-nya mati di tengah jalan — lihat ``crud.STALE_PROCESSING_AFTER``.
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


@celery_app.task(name="worker.tasks.maintenance.fail_stale_processing_results")
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
