"""``process_transcript`` tidak boleh membuka lagi result yang sudah ditutup.

Kasus 28 September 2026 (worker kube): proses worker mati di tengah task, pesan
Celery yang belum di-ack dikembalikan Redis tiap ``visibility_timeout`` (60 menit),
dan setiap pengiriman ulang menulis ``status=processing`` + ``started_at`` baru —
termasuk ke row yang sudah ditutup pembersih jadi ``failed``. Hasilnya bolak-balik
tiap jam tanpa akhir.

Satu-satunya jalur sah ke task ini adalah upload (row ``pending``) dan reproses
(row baru ``pending``), jadi row ``done``/``failed`` berarti pesan basi: lewati.
Tanpa DB/MinIO/LLM — crud palsu.
"""
from types import SimpleNamespace

import pytest


class _Stop(Exception):
    pass


class FakeCrud:
    def __init__(self, status):
        self.row = SimpleNamespace(id="r1", status=status, campaign="Cashline")
        self.updates = []

    def get_result(self, db, result_id):
        return self.row

    def update_result_status(self, db, result_id, status, **kw):
        self.updates.append(status)
        self.row.status = status
        return self.row

    def __getattr__(self, name):   # sisa crud tidak dipakai sebelum unduh
        return lambda *a, **k: None


@pytest.fixture()
def run(monkeypatch):
    from worker.tasks import process_transcript as mod

    def _run(status):
        fake = FakeCrud(status)
        monkeypatch.setattr(mod, "crud", fake)
        session = SimpleNamespace(close=lambda: None, commit=lambda: None,
                                  rollback=lambda: None)
        monkeypatch.setattr(mod, "_session_factory", lambda: (lambda: session))
        monkeypatch.setattr(mod, "get_worker_settings", lambda: SimpleNamespace())

        def _download(result_id):
            raise _Stop()
        monkeypatch.setattr(mod, "_download_transcripts", _download)
        try:
            res = mod.process_transcript.run("r1")
        except _Stop:
            res = "lanjut"
        return fake, res

    return _run


@pytest.mark.parametrize("status", ["done", "failed"])
def test_result_tertutup_dilewati_tanpa_dibuka_lagi(run, status):
    fake, res = run(status)

    assert res == {"result_id": "r1", "status": "skipped"}
    assert fake.updates == []
    assert fake.row.status == status


@pytest.mark.parametrize("status", ["pending", "processing"])
def test_result_terbuka_tetap_dikerjakan(run, status):
    """``processing`` = pengiriman ulang sesudah worker mati, sebelum pembersih
    menutupnya — masih boleh dicoba lagi."""
    fake, res = run(status)

    assert res == "lanjut"
    assert fake.updates[0] == "processing"
