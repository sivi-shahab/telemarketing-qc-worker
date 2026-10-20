"""Sinkron ``load_date`` App C ke ``ticket_load_dates`` (20 Oktober 2026).

Filter tanggal Results & Stats memakai tanggal muat (load_date) App C. Tiap
beat (xx.05 & xx.35 WIB) task ini menyalin tiket ``LOAD_DATE_SYNC_DAYS`` hari
terakhir (bawaan 3, termasuk hari ini WIB) — tanggal lama tidak berubah di App C.
Data lama diisi sekali lewat ``scripts/backfill_ticket_load_dates.py`` di repo api.

Saklar: ``LOAD_DATE_SYNC_ENABLED`` (bawaan menyala; ``0`` mematikan).
"""
import logging
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from db import crud
from services import load_date_sync
from worker.celery_app import celery_app
from worker.config import get_worker_settings

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return os.getenv("LOAD_DATE_SYNC_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def _days() -> int:
    try:
        return max(0, int(os.getenv("LOAD_DATE_SYNC_DAYS", "3")))
    except ValueError:
        return 3


def _run(fn):
    settings = get_worker_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True,
                           connect_args=settings.db_connect_args)
    try:
        with Session(engine, autoflush=False) as db:
            return fn(db)
    finally:
        engine.dispose()


@celery_app.task(name="worker.tasks.load_date_sync.sync_ticket_load_dates",
                 ignore_result=True)
def sync_ticket_load_dates():
    if not enabled():
        return {"disabled": True}
    dates = load_date_sync.recent_dates(crud.now_wib().date(), _days())
    result = _run(lambda db: load_date_sync.sync_dates(db, dates))
    if len(result) < len(dates):
        logger.warning("sinkron load_date: %s dari %s tanggal berhasil", len(result), len(dates))
    return {"dates": len(result), "rows": sum(result.values())}
