"""Beat menyegarkan snapshot Statistics GLOBAL secara proaktif (28 September 2026).

API sudah menyajikan snapshot lama sambil menghitung ulang di latar
(``crud.get_or_build_stats_snapshot(session_factory=...)``); tugas ini membuat admin
hampir selalu melihat angka terbaru tanpa menunggu ada yang membuka halaman lebih
dulu. Murah bila data tidak berubah: hanya menghitung signature.
"""
from types import SimpleNamespace

TASK = "worker.tasks.maintenance.refresh_stats_snapshot"


def test_beat_menjadwalkan_penyegar_snapshot():
    from worker.celery_app import celery_app

    entries = [e for e in celery_app.conf.beat_schedule.values() if e["task"] == TASK]
    assert len(entries) == 1 and entries[0]["schedule"] <= 300


def test_task_membangun_snapshot_global_lalu_menutup_sesi(monkeypatch):
    from worker.tasks import maintenance as mod

    calls, closed = [], []
    session = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(mod, "_session_factory", lambda: (lambda: session))
    monkeypatch.setattr(mod.crud, "get_or_build_stats_snapshot",
                        lambda db, **kw: calls.append((db, kw)) or {"_signature": "v25|x"})

    mod.refresh_stats_snapshot.run()

    assert calls == [(session, {})]      # scope global, tanpa force
    assert closed == [True]
    assert mod.refresh_stats_snapshot.ignore_result is True
