"""Retry otomatis data acuan TMS/Ascend (19 Oktober 2026) — kabel worker-nya:
jadwal beat tiap 30 menit dan keputusan per tiket. Pemilihan tiketnya
(``crud.reference_gap_retry_plan``) diuji terhadap DB di repo api
(``tests/test_reference_gap_retry.py``). Tanpa DB/Redis/DWH.
"""
import json
from datetime import datetime

TASK = "worker.tasks.reference_retry.retry_reference_gaps"
NOW = datetime(2026, 10, 19, 3, 30)


def _plan(tid):
    return {"ticket_id": tid, "campaign": "Cashline", "source_result_id": "r-" + tid,
            "old_result_ids": ["r-" + tid], "gaps": ("tms", "ascend")}


def test_beat_tiap_30_menit():
    from worker.celery_app import celery_app
    from worker.tasks import reference_retry as mod

    (entry,) = [e for e in celery_app.conf.beat_schedule.values() if e["task"] == TASK]
    assert entry["schedule"].minute == {0, 30}
    assert mod.INTERVAL_MIN == 30
    assert "worker.tasks.reference_retry" in celery_app.conf.include


def _patch(monkeypatch, plans, bundles, bulk=None):
    from worker.tasks import reference_retry as mod

    jobs, sent, saved = [], [], {}
    monkeypatch.setattr(mod.crud, "running_reprocess_job", lambda db, scope=None: bulk)
    monkeypatch.setattr(mod.crud, "reference_gap_retry_plan", lambda db, since: plans)
    monkeypatch.setattr(mod.data_dwh, "fetch_bundle_checked", lambda tid: bundles[tid])

    def create(db, campaigns, plan, username, scope):
        jobs.append((plan[0]["ticket_id"], username, scope))
        return type("Job", (), {"id": len(jobs)})

    monkeypatch.setattr(mod.crud, "create_reprocess_job", create)
    monkeypatch.setattr(mod.crud, "reprocess_job_items",
                        lambda db, jid: [type("Item", (), {"id": 100 + jid})])
    monkeypatch.setattr(mod.celery_app, "send_task", lambda name, args: sent.append((name, args)))
    monkeypatch.setattr(mod.crud, "set_app_setting",
                        lambda db, key, value, user: saved.update({key: json.loads(value)}))
    return mod, jobs, sent, saved


def test_hanya_tiket_yang_kini_lengkap_yang_direproses(monkeypatch):
    full = {"cashline": {"a": 1}, "customer": {"b": 2}}
    mod, jobs, sent, saved = _patch(
        monkeypatch,
        [_plan("lengkap"), _plan("tms_saja"), _plan("kosong"), _plan("dwh_mati")],
        {"lengkap": (full, True),
         "tms_saja": ({"cashline": {"a": 1}, "customer": None}, True),
         "kosong": ({"cashline": None, "customer": None}, True),
         "dwh_mati": ({"cashline": None, "customer": None}, False)},
    )

    summary = mod.run_once(object(), now=NOW)

    assert jobs == [("lengkap", mod.SYSTEM_USERNAME, "ticket")]
    assert sent == [("worker.tasks.reprocess_ticket.reprocess_ticket", [101])]
    assert summary["requeued"] == 1
    assert summary["still_missing"] == 2
    assert summary["dwh_unreachable"] == 1
    stored = saved["reference_retry_last_run"]
    assert stored["last_run_at"] == "2026-10-19T03:30:00+00:00"
    assert stored["interval_min"] == 30 and stored["max_age_days"] == 7


def test_job_massal_berjalan_putaran_dilewati(monkeypatch):
    mod, jobs, _, saved = _patch(monkeypatch, [_plan("x")],
                                 {"x": ({"cashline": {}, "customer": {}}, True)}, bulk=object())

    summary = mod.run_once(object(), now=NOW)

    assert jobs == []
    assert summary["skipped_bulk_job"] is True
    assert "reference_retry_last_run" in saved


def test_rem_per_putaran(monkeypatch):
    full = ({"cashline": {"a": 1}, "customer": {"b": 2}}, True)
    plans = [_plan(f"t{i}") for i in range(5)]
    mod, jobs, _, _ = _patch(monkeypatch, plans, {p["ticket_id"]: full for p in plans})
    monkeypatch.setattr(mod, "MAX_PER_RUN", 2)

    mod.run_once(object(), now=NOW)

    assert len(jobs) == 2


def test_saklar_mati(monkeypatch):
    from worker.tasks import reference_retry as mod

    monkeypatch.setenv("REFERENCE_RETRY_ENABLED", "0")
    assert mod.retry_reference_gaps.run() == {"disabled": True}
