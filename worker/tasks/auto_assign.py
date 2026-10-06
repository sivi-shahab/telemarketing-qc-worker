"""Celery task: batch auto assign tiket ke QC pada jam terjadwal.

Dijadwalkan Celery beat (``worker/celery_app.py``) pada ``qc_auto_assign.SCHEDULE_WIB``.
Diport dari 4-service@e17ecca (5 Oktober 2026). Saklarnya ``QC_AUTO_ASSIGN_ENABLED``
(default MATI): beat tetap memanggil task ini di tiap slot, tetapi task langsung
pulang tanpa menyentuh DB selama saklarnya mati. Slot yang antreannya sudah habis
(atau tidak ada QC aktif) di-skip begitu saja — batch berikutnya jalan seperti biasa.
"""
import logging

import qc_auto_assign
from worker.celery_app import celery_app
from worker.tasks.maintenance import _session_factory

logger = logging.getLogger(__name__)


@celery_app.task(name="worker.tasks.auto_assign.scheduled_auto_assign")
def scheduled_auto_assign():
    if not qc_auto_assign.schedule_enabled():
        logger.info("auto assign terjadwal dimatikan (QC_AUTO_ASSIGN_ENABLED)")
        return {"assigned": 0, "skipped": "disabled"}
    db = _session_factory()()
    try:
        result = qc_auto_assign.run_scheduled_batch(db)
    finally:
        db.close()
    logger.info("auto assign terjadwal: %s", result)
    return result
