"""QC yang dinonaktifkan Team Leader QC (Kelola QC) aktif kembali lewat 00.00 WIB.

Perilaku UPDATE-nya diuji di repo api (``tests/test_reactivate_expired_qc.py``).
Di sini hanya kabel worker-nya: jadwal beat dan task yang menutup sesinya.
"""
from types import SimpleNamespace

TASK = "worker.tasks.maintenance.reactivate_expired_qc_users"


def test_beat_menjadwalkan_tiap_120_detik():
    from worker.celery_app import celery_app

    (entry,) = [e for e in celery_app.conf.beat_schedule.values() if e["task"] == TASK]
    assert entry["schedule"] == 120.0


def test_task_memanggil_crud_dan_menutup_sesi(monkeypatch):
    from worker.tasks import maintenance as mod

    calls, closed = [], []
    session = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(mod, "_session_factory", lambda: (lambda: session))
    monkeypatch.setattr(mod.crud, "reactivate_expired_qc_users",
                        lambda db: calls.append(db) or ["qc1", "qc2"])

    assert mod.reactivate_expired_qc_users.run() == {"reactivated": 2}
    assert calls == [session] and closed == [True]
