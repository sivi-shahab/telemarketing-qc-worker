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
