"""Batch auto assign terjadwal: kabel worker-nya saja (jadwal beat + saklar).

Keputusan pembagiannya diuji di repo api (``tests/test_qc_auto_assign_schedule.py``).
Tanpa DB/Redis.
"""
TASK = "worker.tasks.auto_assign.scheduled_auto_assign"


def test_beat_menjadwalkan_lima_slot_wib():
    from worker.celery_app import celery_app

    entries = [e for e in celery_app.conf.beat_schedule.values() if e["task"] == TASK]
    slots = sorted((min(e["schedule"].hour), min(e["schedule"].minute)) for e in entries)
    assert slots == [(8, 0), (11, 0), (13, 0), (15, 0), (16, 30)]
    assert celery_app.conf.timezone == "Asia/Jakarta"
    assert "worker.tasks.auto_assign" in celery_app.conf.include


def test_jadwal_lama_tetap_ada():
    """Menambah slot auto assign tidak boleh menimpa jadwal pemeliharaan."""
    from worker.celery_app import celery_app

    tasks = {e["task"] for e in celery_app.conf.beat_schedule.values()}
    assert "worker.tasks.maintenance.fail_stale_processing_results" in tasks
    assert "worker.tasks.maintenance.refresh_stats_snapshot" in tasks


def test_saklar_mati_tidak_menyentuh_db(monkeypatch):
    from worker.tasks import auto_assign as mod

    monkeypatch.delenv("QC_AUTO_ASSIGN_ENABLED", raising=False)

    def no_db():
        raise AssertionError("sesi DB tidak boleh dibuka saat saklar mati")

    monkeypatch.setattr(mod, "_session_factory", no_db)
    assert mod.scheduled_auto_assign() == {"assigned": 0, "skipped": "disabled"}


def test_saklar_hidup_menjalankan_batch_dan_menutup_sesi(monkeypatch):
    from worker.tasks import auto_assign as mod

    closed = []

    class _S:
        def close(self):
            closed.append(True)

    monkeypatch.setenv("QC_AUTO_ASSIGN_ENABLED", "true")
    monkeypatch.setattr(mod, "_session_factory", lambda: _S)
    monkeypatch.setattr(mod.qc_auto_assign, "run_scheduled_batch",
                        lambda db: {"assigned": 3})
    assert mod.scheduled_auto_assign() == {"assigned": 3}
    assert closed == [True]
