"""Task sinkron load_date App C (20 Oktober 2026) — kabel worker-nya saja.
``sync_dates`` diuji terhadap DB di repo api (``tests/test_load_date_sync.py``).
"""
from datetime import date, datetime

TASK = "worker.tasks.load_date_sync.sync_ticket_load_dates"


def test_beat_terdaftar():
    from worker.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["sync-ticket-load-dates"]
    assert entry["task"] == TASK
    assert entry["schedule"].minute == {5, 35}
    assert "worker.tasks.load_date_sync" in celery_app.conf.include


def test_task_mati_bila_disabled(monkeypatch):
    from worker.tasks import load_date_sync as mod

    monkeypatch.setenv("LOAD_DATE_SYNC_ENABLED", "0")
    called = []
    monkeypatch.setattr(mod.load_date_sync, "sync_dates", lambda *a, **k: called.append(1))
    assert mod.sync_ticket_load_dates() == {"disabled": True}
    assert called == []


def test_task_sinkron_tiga_hari_terakhir(monkeypatch):
    from worker.tasks import load_date_sync as mod

    monkeypatch.delenv("LOAD_DATE_SYNC_ENABLED", raising=False)
    monkeypatch.delenv("LOAD_DATE_SYNC_DAYS", raising=False)
    monkeypatch.setattr(mod.crud, "now_wib", lambda: datetime(2026, 10, 19, 8, 0))
    seen = {}

    def fake(db, dates):
        seen["dates"] = list(dates)
        return {d: 1 for d in seen["dates"]}

    monkeypatch.setattr(mod.load_date_sync, "sync_dates", fake)
    monkeypatch.setattr(mod, "_run", lambda fn: fn(None))
    out = mod.sync_ticket_load_dates()
    assert seen["dates"][0] == date(2026, 10, 16) and seen["dates"][-1] == date(2026, 10, 19)
    assert out == {"dates": 4, "rows": 4}
