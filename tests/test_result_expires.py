"""Hasil task Celery di Redis disimpan 30 hari, bukan bawaan Celery 1 hari.

Diubah 28 September 2026 atas permintaan pemilik sistem. Nilainya bisa diatur lewat
env ``CELERY_RESULT_EXPIRES_DAYS``. Task pembersih periodik (tiap 2 menit) tidak
menyimpan hasil — dengan TTL 30 hari ia saja akan menumpuk ±21.600 key yang tidak
pernah dibaca.
"""
import importlib
from datetime import timedelta


def _reload(monkeypatch, value=None):
    if value is None:
        monkeypatch.delenv("CELERY_RESULT_EXPIRES_DAYS", raising=False)
    else:
        monkeypatch.setenv("CELERY_RESULT_EXPIRES_DAYS", value)
    import worker.celery_app as mod
    return importlib.reload(mod).celery_app


def test_hasil_task_disimpan_30_hari(monkeypatch):
    app = _reload(monkeypatch)
    assert app.conf.result_expires == timedelta(days=30)


def test_masa_simpan_bisa_diatur_lewat_env(monkeypatch):
    app = _reload(monkeypatch, "7")
    assert app.conf.result_expires == timedelta(days=7)
    _reload(monkeypatch)   # kembalikan untuk test lain


def test_pembersih_periodik_tidak_menyimpan_hasil():
    from worker.tasks import maintenance

    assert maintenance.fail_stale_processing_results.ignore_result is True
