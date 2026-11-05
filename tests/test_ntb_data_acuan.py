"""Data acuan campaign New To Bank Eksternal (5 November 2026).

NTB hanya punya baris TMS NTB di App A (``/campaign/new-to-bank-eksternal/{id}``) —
tanpa TMS Cashline dan tanpa card holder Ascend. Test ini menjaga tiga kabel:
deteksi campaign (``NTB_CAMPAIGNS``), klien endpoint NTB, dan keputusan retry data
acuan untuk rencana NTB. Tanpa DB/Redis/DWH.
"""
import json
from datetime import datetime

import pytest
import requests

NOW = datetime(2026, 11, 5, 3, 30)


# --- deteksi campaign --------------------------------------------------------------

def test_is_ntb_bawaan_new_to_bank_eksternal(monkeypatch):
    from compliance.campaign_kind import is_ntb

    monkeypatch.delenv("NTB_CAMPAIGNS", raising=False)
    assert is_ntb("New To Bank Eksternal")
    assert is_ntb("  new to bank eksternal ")
    assert not is_ntb("Cashline")
    assert not is_ntb(None)


def test_is_ntb_env_kosong_mematikan(monkeypatch):
    from compliance.campaign_kind import is_ntb

    monkeypatch.setenv("NTB_CAMPAIGNS", "")
    assert not is_ntb("New To Bank Eksternal")


# --- klien endpoint NTB ------------------------------------------------------------

class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text
        self.ok = 200 <= status < 300

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _get(monkeypatch, resp):
    from services import data_dwh

    seen = []

    def fake(url, timeout):
        seen.append(url)
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(data_dwh.requests, "get", fake)
    return data_dwh, seen


def test_fetch_ntb_200(monkeypatch):
    row = {"result_id": "040845DQJG", "agent_id": "fikri801"}
    data_dwh, seen = _get(monkeypatch, _Resp(200, {"campaign": row}))

    assert data_dwh.fetch_ntb_checked(" 040845DQJG ") == (row, True)
    assert seen[0].endswith("/campaign/new-to-bank-eksternal/040845DQJG")


def test_fetch_ntb_404_berarti_pasti_kosong(monkeypatch):
    data_dwh, _ = _get(monkeypatch, _Resp(404, {"detail": "x"}))
    assert data_dwh.fetch_ntb_checked("0101073k1C") == (None, True)


@pytest.mark.parametrize("resp", [
    _Resp(500, text="boom"),
    _Resp(200, ValueError("bukan json")),
    requests.ConnectionError("down"),
])
def test_fetch_ntb_gagal_bukan_bukti_kosong(monkeypatch, resp):
    data_dwh, _ = _get(monkeypatch, resp)
    assert data_dwh.fetch_ntb_checked("x") == (None, False)


def test_fetch_ntb_id_kosong_tanpa_http(monkeypatch):
    data_dwh, seen = _get(monkeypatch, _Resp(200, {"campaign": {}}))
    assert data_dwh.fetch_ntb_checked("  ") == (None, True)
    assert seen == []


# --- retry data acuan --------------------------------------------------------------

def _plan(tid, ntb):
    return {"ticket_id": tid, "campaign": "New To Bank Eksternal" if ntb else "Cashline",
            "source_result_id": "r-" + tid, "old_result_ids": ["r-" + tid],
            "gaps": ("tms",) if ntb else ("tms", "ascend"), "ntb": ntb}


def test_retry_ntb_memakai_endpoint_ntb(monkeypatch):
    from worker.tasks import reference_retry as mod

    jobs = []
    monkeypatch.setattr(mod.crud, "running_reprocess_job", lambda db, scope=None: None)
    monkeypatch.setattr(mod.crud, "reference_gap_retry_plan", lambda db, since: [
        _plan("ntb_ada", True), _plan("ntb_belum", True), _plan("ntb_mati", True),
        _plan("cl_kosong", False)])
    ntb_rows = {"ntb_ada": ({"agent_id": "a"}, True), "ntb_belum": (None, True),
                "ntb_mati": (None, False)}
    monkeypatch.setattr(mod.data_dwh, "fetch_ntb_checked", lambda tid: ntb_rows[tid])

    def cashline(tid):
        assert tid == "cl_kosong", "tiket NTB tidak boleh ditanyakan ke endpoint Cashline"
        return {"cashline": None, "customer": None}, True

    monkeypatch.setattr(mod.data_dwh, "fetch_bundle_checked", cashline)

    def create(db, campaigns, plan, username, scope):
        jobs.append((plan[0]["ticket_id"], campaigns))
        return type("Job", (), {"id": len(jobs)})

    monkeypatch.setattr(mod.crud, "create_reprocess_job", create)
    monkeypatch.setattr(mod.crud, "reprocess_job_items",
                        lambda db, jid: [type("Item", (), {"id": 100 + jid})])
    monkeypatch.setattr(mod.celery_app, "send_task", lambda name, args: None)
    saved = {}
    monkeypatch.setattr(mod.crud, "set_app_setting",
                        lambda db, key, value, user: saved.update({key: json.loads(value)}))

    summary = mod.run_once(object(), now=NOW)

    assert jobs == [("ntb_ada", ["New To Bank Eksternal"])]
    assert summary["requeued"] == 1
    assert summary["still_missing"] == 2  # ntb_belum + cl_kosong
    assert summary["dwh_unreachable"] == 1


# --- data_gap saat BACA ------------------------------------------------------------

def _gap(monkeypatch, snapshot, sales=("fikri801",)):
    import compliance.stats_aggregate as sa

    monkeypatch.setattr(sa.crud, "reference_snapshot_index", lambda db, cids: {"T1": snapshot})
    monkeypatch.setattr(sa, "active_sales_map", lambda db: {s: {} for s in sales})
    r = type("R", (), {"id": "r1", "source_files": ["T1_20261105.pdf"]})
    return sa.data_gap_map(object(), [r]).get("r1")


def test_data_gap_ntb_tanpa_tms_hanya_tms(monkeypatch):
    from compliance.stats_aggregate import DATA_GAP_TMS

    snap = {"cashline": False, "customer": False, "agent_id": None, "ntb": False}
    assert _gap(monkeypatch, snap) == (DATA_GAP_TMS,)


def test_data_gap_ntb_lengkap_tidak_menagih_ascend(monkeypatch):
    snap = {"cashline": False, "customer": False, "agent_id": "fikri801", "ntb": True}
    assert _gap(monkeypatch, snap) is None


def test_data_gap_ntb_agent_tidak_terpetakan(monkeypatch):
    from compliance.stats_aggregate import DATA_GAP_AGENT

    snap = {"cashline": False, "customer": False, "agent_id": "x999", "ntb": True}
    assert _gap(monkeypatch, snap) == (DATA_GAP_AGENT,)


def test_data_gap_cashline_tidak_berubah(monkeypatch):
    from compliance.stats_aggregate import DATA_GAP_ASCEND, DATA_GAP_TMS

    snap = {"cashline": False, "customer": False, "agent_id": None, "ntb": None}
    assert _gap(monkeypatch, snap) == (DATA_GAP_TMS, DATA_GAP_ASCEND)
