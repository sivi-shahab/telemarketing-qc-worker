"""Worker tidak boleh menghapus row LAMA milik item yang sudah di-kill Admin.

``crud.kill_reprocess_job`` (tombol Kill, khusus role admin) menutup item
``processing`` jadi ``failed`` tanpa menunggu worker-nya. Kalau worker itu ternyata
masih hidup dan evaluasinya selesai, langkah "hapus row lama" akan membuat tiket
berakhir tanpa hasil sama sekali. Test ini memakai crud palsu — tanpa DB, MinIO,
maupun LLM.
"""
import sys
import types
from types import SimpleNamespace

import pytest


class FakeCrud:
    def __init__(self, killed_after_eval: bool):
        self.killed_after_eval = killed_after_eval
        self.evaluated = False
        self.item = SimpleNamespace(id=7, job_id="job-1", ticket_id="T1", status="pending",
                                    source_result_id="old-1", old_result_ids=["old-1"])
        self.deleted = []
        self.updates = []

    def get_reprocess_item(self, db, item_id):
        return self.item

    def get_reprocess_job(self, db, job_id):
        return SimpleNamespace(id=job_id, status="running")

    def update_reprocess_item(self, db, item_id, **fields):
        self.updates.append(fields)
        for k, v in fields.items():
            setattr(self.item, k, v)
        return self.item

    def get_result(self, db, result_id):
        if result_id == "old-1":
            return SimpleNamespace(id="old-1", campaign="Cashline", source_files=["T1_x.pdf"],
                                   num_calls=1, uploaded_by_username=None, uploaded_by_role=None)
        return SimpleNamespace(id=result_id, status="done", error_message=None)

    def create_result(self, db, **kw):
        return SimpleNamespace(id="new-1", transcript_path=None)

    def delete_results_by_ids(self, db, ids):
        self.deleted.append(list(ids))
        return len(ids)

    def finish_reprocess_job_if_complete(self, db, job_id):
        return None

    def evaluate(self, result_id):
        self.evaluated = True
        if self.killed_after_eval:
            # Admin menekan Kill selagi LLM berjalan.
            self.item.status = "failed"
            self.item.error_message = "Dihentikan paksa (kill) oleh admin1."


@pytest.fixture()
def run(monkeypatch):
    from worker.tasks import reprocess_ticket as mod

    def _run(killed_after_eval):
        fake = FakeCrud(killed_after_eval)
        monkeypatch.setattr(mod, "crud", fake)
        session = SimpleNamespace(commit=lambda: None, rollback=lambda: None,
                                  close=lambda: None, expire_all=lambda: None)
        monkeypatch.setattr(mod, "_session_factory", lambda: (lambda: session))
        monkeypatch.setattr(mod, "get_worker_settings",
                            lambda: SimpleNamespace(minio_bucket_transcripts="b"))
        monkeypatch.setattr(mod, "_copy_transcripts", lambda *a: 1)
        monkeypatch.setattr(mod, "_remove_transcripts", lambda *a: None)
        pt = types.ModuleType("worker.tasks.process_transcript")
        pt._minio_client = lambda: object()
        pt.process_transcript = fake.evaluate
        monkeypatch.setitem(sys.modules, "worker.tasks.process_transcript", pt)
        return fake, mod.reprocess_ticket.run(7)

    return _run


def test_item_yang_di_kill_tidak_menghapus_row_lama(run):
    fake, res = run(killed_after_eval=True)

    assert fake.evaluated
    assert res["status"] == "killed"
    assert ["old-1"] not in fake.deleted          # row LAMA utuh
    assert ["new-1"] in fake.deleted              # row baru dibuang
    assert fake.item.status == "failed"
    assert "Dihentikan paksa" in fake.item.error_message


def test_item_normal_tetap_menghapus_row_lama(run):
    fake, res = run(killed_after_eval=False)

    assert res["status"] == "done"
    assert ["old-1"] in fake.deleted
    assert fake.item.status == "done"
