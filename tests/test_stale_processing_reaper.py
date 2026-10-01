"""Pembersih result ``processing`` basi dijadwalkan lewat Celery beat.

Perilaku terhadap DB-nya diuji di repo api (``tests/test_stale_processing_reaper.py``,
memakai fixture ``db``). Di sini hanya kabel worker-nya: jadwal beat, ambang yang
aman terhadap batas waktu task, dan task yang menutup sesinya. Tanpa DB/Redis.
"""
from types import SimpleNamespace

TASK = "worker.tasks.maintenance.fail_stale_processing_results"


def test_beat_menjadwalkan_pembersih():
    from worker.celery_app import celery_app

    entries = [e for e in celery_app.conf.beat_schedule.values() if e["task"] == TASK]
    assert len(entries) == 1
    assert "worker.tasks.maintenance" in celery_app.conf.include


def test_ambang_basi_di_atas_batas_keras_task():
    """Task yang masih sah berjalan tidak boleh ikut ditutup: selama batas keras
    (``task_time_limit``) belum lewat, worker-nya mungkin masih hidup."""
    from db import crud
    from worker.celery_app import celery_app

    assert crud.STALE_PROCESSING_AFTER.total_seconds() > celery_app.conf.task_time_limit


def test_pembersih_selalu_sempat_jalan_sebelum_redis_mengirim_ulang():
    """Task yang tidak di-ack dikirim ulang Redis tiap ``visibility_timeout`` dan
    ``started_at``-nya ditulis ulang. Kalau ambang + interval beat melewati batas itu,
    baris macet di-reset sebelum sempat dianggap basi — terjadi 28 Sep 2026: worker
    kube me-reset 40 baris tepat 60 menit sesudahnya, pembersih mendapati 0."""
    from db import crud
    from worker.celery_app import celery_app

    visibility = celery_app.conf.broker_transport_options["visibility_timeout"]
    (entry,) = [e for e in celery_app.conf.beat_schedule.values() if e["task"] == TASK]

    assert crud.STALE_PROCESSING_AFTER.total_seconds() + entry["schedule"] < visibility


def test_task_memanggil_crud_dan_menutup_sesi(monkeypatch):
    from worker.tasks import maintenance as mod

    calls, closed = [], []
    session = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(mod, "_session_factory", lambda: (lambda: session))
    monkeypatch.setattr(mod.crud, "fail_stale_processing_results",
                        lambda db: calls.append(db) or ["r1", "r2"])

    res = mod.fail_stale_processing_results.run()

    assert calls == [session]
    assert closed == [True]
    assert res == {"failed": 2}


# --- OCR Gambar: ocr_images pending/processing basi (review akhir F4) ---------
# Perilaku UPDATE-nya diuji di repo api (tests/test_stale_processing_reaper.py).

OCR_TASK = "worker.tasks.maintenance.fail_stale_ocr_images"


def test_beat_menjadwalkan_pembersih_ocr_tiap_120_detik():
    from worker.celery_app import celery_app

    (entry,) = [e for e in celery_app.conf.beat_schedule.values() if e["task"] == OCR_TASK]
    assert entry["schedule"] == 120.0


def test_ambang_ocr_aman_terhadap_batas_task_dan_redis():
    from db import crud
    from worker.celery_app import celery_app

    visibility = celery_app.conf.broker_transport_options["visibility_timeout"]
    (entry,) = [e for e in celery_app.conf.beat_schedule.values() if e["task"] == OCR_TASK]
    processing = crud.OCR_STALE_PROCESSING_AFTER.total_seconds()
    assert processing == 55 * 60 and crud.OCR_STALE_PENDING_AFTER.total_seconds() == 60 * 60
    assert celery_app.conf.task_time_limit < processing
    assert processing + entry["schedule"] < visibility


def test_task_ocr_memanggil_crud_dan_menutup_sesi(monkeypatch):
    from worker.tasks import maintenance as mod

    calls, closed = [], []
    session = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(mod, "_session_factory", lambda: (lambda: session))
    monkeypatch.setattr(mod.crud, "fail_stale_ocr_images", lambda db: calls.append(db) or 3)

    res = mod.fail_stale_ocr_images.run()

    assert (calls, closed, res) == ([session], [True], {"failed": 3})
