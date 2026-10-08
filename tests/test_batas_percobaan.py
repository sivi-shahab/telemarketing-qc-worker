"""Batas percobaan ``process_transcript`` setelah proses worker mati mendadak.

Kasus 8 Oktober 2026 (worker kube): proses anak di-``SIGKILL`` (OOM) di tengah task,
``task_reject_on_worker_lost`` mengembalikan pesannya, dan tiket dimulai lagi dari nol
tanpa akhir — tidak pernah ``done``/``failed``. Setelah ``TASK_MAX_ATTEMPTS`` kali mulai
tanpa berakhir normal, row ditutup ``failed`` dengan pesan yang menjelaskan sebabnya.

Kematian mendadak disimulasikan dengan memanggil ``attempts.mulai`` tanpa
``attempts.selesai`` — persis jejak yang ditinggalkan proses yang di-SIGKILL.
Tanpa Redis/DB/MinIO/LLM — semuanya palsu.
"""
from types import SimpleNamespace

import pytest

from worker import attempts


class _Stop(Exception):
    pass


class FakeRedis:
    def __init__(self):
        self.data = {}

    def pipeline(self):
        return _FakePipe(self)

    def delete(self, key):
        self.data.pop(key, None)


class _FakePipe:
    def __init__(self, r):
        self.r, self.ops = r, []

    def incr(self, key):
        self.ops.append(key)

    def expire(self, key, ttl):
        pass

    def execute(self):
        out = []
        for key in self.ops:
            self.r.data[key] = self.r.data.get(key, 0) + 1
            out.append(self.r.data[key])
        return [*out, True]


class FakeCrud:
    def __init__(self):
        self.row = SimpleNamespace(id="r1", status="processing", campaign="Cashline",
                                   current_stage="unduh_pdf")
        self.updates = []

    def get_result(self, db, result_id):
        return self.row

    def update_result_status(self, db, result_id, status, **kw):
        self.updates.append((status, kw.get("error_message")))
        self.row.status = status
        return self.row

    def __getattr__(self, name):
        return lambda *a, **k: None


@pytest.fixture()
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(attempts, "_client", lambda: fake)
    monkeypatch.setattr(attempts, "MAX_ATTEMPTS", 3)
    return fake


@pytest.fixture()
def run(monkeypatch, redis):
    from worker.tasks import process_transcript as mod

    def _run(download=None, status="processing"):
        fake = FakeCrud()
        fake.row.status = status
        monkeypatch.setattr(mod, "crud", fake)
        session = SimpleNamespace(close=lambda: None, commit=lambda: None,
                                  rollback=lambda: None)
        monkeypatch.setattr(mod, "_session_factory", lambda: (lambda: session))
        monkeypatch.setattr(mod, "get_worker_settings", lambda: SimpleNamespace())
        calls = []

        def _download(result_id):
            calls.append(result_id)
            raise download or _Stop()
        monkeypatch.setattr(mod, "_download_transcripts", _download)
        try:
            res = mod.process_transcript.run("r1")
        except _Stop:
            res = "lanjut"
        except Exception as exc:  # gagal biasa — dilempar ulang oleh task
            res = exc
        return fake, res, calls

    return _run


KEY = "qc:attempts:process_transcript:r1"


def _mati_mendadak(n):
    for _ in range(n):
        attempts.mulai("process_transcript", "r1")


def test_mati_tiga_kali_lalu_ditutup_failed(run, redis):
    _mati_mendadak(3)

    fake, res, calls = run()

    assert res == {"result_id": "r1", "status": "failed", "reason": "max_attempts"}
    assert calls == []                     # tidak diunduh/dikerjakan lagi
    (status, pesan), = fake.updates
    assert status == "failed"
    assert "3 kali" in pesan and "'unduh_pdf'" in pesan and "OOM" in pesan
    assert KEY not in redis.data           # hitungan dibersihkan


def test_di_bawah_batas_tetap_dikerjakan(run, redis):
    _mati_mendadak(2)                      # percobaan ke-3 masih boleh

    fake, res, calls = run()

    assert calls == ["r1"]
    assert fake.updates[0][0] == "processing"


def test_gagal_biasa_mereset_hitungan(run, redis):
    """Exception biasa = berakhir normal: tiket yang di-reprocess nanti mulai dari 1."""
    _mati_mendadak(2)

    fake, res, calls = run(download=ValueError("pdf rusak"))

    assert isinstance(res, ValueError)
    assert fake.updates[-1] == ("failed", "pdf rusak")
    assert KEY not in redis.data


def test_result_tertutup_tidak_dihitung(run, redis):
    """Pesan basi untuk row done/failed dilewati sebelum penghitung disentuh."""
    _mati_mendadak(5)

    fake, res, calls = run(status="done")

    assert res == {"result_id": "r1", "status": "skipped"}
    assert fake.updates == [] and calls == []


def test_redis_mati_tidak_membatasi(monkeypatch):
    def _rusak():
        raise ConnectionError("redis down")
    monkeypatch.setattr(attempts, "_client", _rusak)

    assert attempts.mulai("process_transcript", "r1") == 1
    attempts.selesai("process_transcript", "r1")   # tidak melempar
