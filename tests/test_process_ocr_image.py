"""Task OCR Gambar — tanpa DB/MinIO/LLM sungguhan (sesi, unduhan, dan klien palsu)."""
import uuid
from types import SimpleNamespace

import pytest

from worker.tasks import process_ocr_image as mod


class FakeClient:
    def __init__(self, content="BARIS 1\nBARIS 2", exc=None):
        self.calls, self.content, self.exc = [], content, exc
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=SimpleNamespace(prompt_tokens=265, completion_tokens=60, total_tokens=325,
                                  prompt_tokens_details=None, completion_tokens_details=None),
        )


class FakeSession:
    def __init__(self, row):
        self.row, self.commits, self.closed = row, 0, False

    def get(self, model, key):
        return self.row if self.row is not None and self.row.id == key else None

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def _row(status="pending"):
    return SimpleNamespace(id=uuid.uuid4(), status=status, object_path="ocr-images/x.png",
                           mime_type="image/png", text=None, error_message=None,
                           token_usage=None, started_at=None, finished_at=None)


@pytest.fixture()
def run(monkeypatch):
    def _run(row, client=None, download=lambda path: b"PNGDATA", image_id=None):
        session = FakeSession(row)
        client = client or FakeClient()
        monkeypatch.setattr(mod, "_session_factory", lambda: (lambda: session))
        monkeypatch.setattr(mod, "_download_object", download)
        monkeypatch.setattr(mod, "_llm_client", lambda: client)
        monkeypatch.setattr(mod, "get_worker_settings", lambda: SimpleNamespace(llm_model="gpt-test"))
        mod.process_ocr_image.run(image_id or str(row.id))
        return session, client
    return _run


def test_sukses_menyimpan_teks_dan_token(run):
    row = _row()
    session, client = run(row)
    assert row.status == "done"
    assert row.text == "BARIS 1\nBARIS 2"
    assert row.token_usage == {"input_token": 265, "output_token": 60,
                               "cached_token": None, "reasoning_token": None}
    assert row.started_at is not None and row.finished_at is not None
    assert session.closed


def test_permintaan_berbentuk_vision_tanpa_reasoning(run):
    _, client = run(_row())
    kw = client.calls[0]
    assert kw["model"] == "gpt-test"
    assert "reasoning_effort" not in kw and "extra_body" not in kw
    assert kw["messages"][0] == {"role": "system", "content": mod.OCR_SYSTEM_PROMPT}
    part = kw["messages"][1]["content"][0]
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:image/png;base64,")


def test_balasan_kosong_jadi_penanda(run):
    row = _row()
    run(row, client=FakeClient(content="   "))
    assert row.status == "done" and row.text == mod.NO_TEXT


def test_llm_gagal_jadi_failed(run):
    row = _row()
    run(row, client=FakeClient(exc=RuntimeError("429 Too Many Requests")))
    assert row.status == "failed"
    assert "429" in row.error_message
    assert row.finished_at is not None


def test_pesan_error_dipotong_1000(run):
    row = _row()
    run(row, client=FakeClient(exc=RuntimeError("x" * 5000)))
    assert len(row.error_message) == 1000


def test_unduhan_gagal_jadi_failed(run):
    row = _row()

    def boom(path):
        raise RuntimeError("NoSuchKey")
    run(row, download=boom)
    assert row.status == "failed" and "NoSuchKey" in row.error_message


def test_row_done_dilewati(run):
    row = _row(status="done")
    row.text = "LAMA"
    _, client = run(row)
    assert client.calls == [] and row.text == "LAMA"


def test_row_failed_dilewati(run):
    """``failed`` hanya kembali ke antrean lewat endpoint retry (yang me-reset ke
    ``pending``); pesan untuk row ``failed`` berarti pesan basi."""
    row = _row(status="failed")
    _, client = run(row)
    assert client.calls == [] and row.status == "failed"


def test_row_hilang_dilewati(run):
    _, client = run(_row(), image_id=str(uuid.uuid4()))
    assert client.calls == []


def test_id_bukan_uuid_dilewati(run):
    _, client = run(_row(), image_id="bukan-uuid")
    assert client.calls == []
