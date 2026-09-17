"""Statistik menu Stats untuk campaign Collection — agregasi MURNI.

Masukannya pasangan (result, result_json) yang sudah dipersempit ke cakupan login
(``api.qc_scope.collection_view_scope``). Setiap laporan dibaca ulang lewat
``normalize_stored_report`` supaya angka Stats sepakat dengan daftar & detail
Collection Results, dan verdict yang tidak terbaca (TIDAK_TERSEDIA) tidak pernah
dihitung sebagai PASS/FAIL.
"""
import re
from collections import Counter, OrderedDict
from datetime import timedelta

from compliance.collection_report import is_collection_result_json, normalize_stored_report

COMMITMENT_KEYS = ("COMMITTED_TO_PAY", "PARTIAL_COMMITMENT", "DISPUTE", "REFUSED", "NOT_STATED")
TOP_INDICATORS = 10
NO_AGENT = "Tidak disebut"
_WIB = timedelta(hours=7)


def _pct(num, den):
    return round(num / den * 100, 1) if den else None


def _avg(values):
    return round(sum(values) / len(values), 1) if values else None


def _ticket_date(result):
    if getattr(result, "generated_at", None) is not None:
        return result.generated_at.date().isoformat()
    if getattr(result, "uploaded_at", None) is not None:
        return (result.uploaded_at + _WIB).date().isoformat()
    return None


def _score_percent(report):
    maximum = report.get("maximum_score") or 0
    return report.get("ai_score_phase_2", 0) / maximum * 100 if maximum > 0 else None


def _agent_display(raw):
    return re.sub(r"\s+", " ", raw).strip() if isinstance(raw, str) else ""


def aggregate_collection_stats(rows) -> dict:
    kpi = Counter()
    daily = {}
    categories = OrderedDict()
    indicators = {}
    critical = Counter()
    critical_items = {}
    codes = OrderedDict()
    agents = {}
    commitment = Counter({k: 0 for k in COMMITMENT_KEYS})
    scores = []

    for result, result_json in rows:
        kpi["total"] += 1
        status = getattr(result, "status", None)
        if status == "failed":
            kpi["failed"] += 1
            continue
        if status != "done":
            # pending/processing — dan status lain yang tak dikenal — dihitung
            # "Diproses" supaya done + in_progress + failed selalu sama dengan total.
            kpi["in_progress"] += 1
            continue
        kpi["done"] += 1
        if not is_collection_result_json(result_json):
            kpi["without_report"] += 1
            continue

        report = normalize_stored_report(result_json.get("evaluation"))
        kpi["with_report"] += 1
        verdict = report.get("ai_status")
        is_pass = verdict == "PASS"
        kpi["pass" if is_pass else "fail"] += 1
        percent = _score_percent(report)
        if percent is not None:
            scores.append(percent)

        day = _ticket_date(result)
        if day:
            bucket = daily.setdefault(day, {"date": day, "pass": 0, "fail": 0})
            bucket["pass" if is_pass else "fail"] += 1

        for cat in report.get("category_summary") or []:
            c = categories.setdefault(cat["category"], {"reports": 0, "percents": [], "fail": 0, "unavailable": 0})
            c["reports"] += 1
            if (cat.get("total_weight") or 0) > 0:
                c["percents"].append(cat.get("earned_score", 0) / cat["total_weight"] * 100)
            if cat.get("category_result") == "FAIL":
                c["fail"] += 1
            elif cat.get("category_result") == "TIDAK_TERSEDIA":
                c["unavailable"] += 1

        for item in report.get("scorecard_result") or []:
            code = (item.get("item_code") or "").strip()
            if item.get("status") != "BELUM_SESUAI" or code in ("", "-"):
                continue
            ind = indicators.setdefault(code, {"item_code": code, "requirement": item.get("requirement", ""),
                                               "category": item.get("category", ""), "belum_sesuai": 0})
            ind["belum_sesuai"] += 1

        check = report.get("critical_compliance_check") or {}
        crit_status = check.get("status")
        critical["pass" if crit_status == "PASS" else "fail" if crit_status == "FAIL" else "unavailable"] += 1
        for it in check.get("checked_items") or []:
            code = (it.get("item_code") or "").strip()
            if it.get("status") != "FAIL" or code in ("", "-"):
                continue
            ci = critical_items.setdefault(code,
                                           {"item_code": code,
                                            "requirement": it.get("requirement", ""), "fail": 0})
            ci["fail"] += 1

        for err in report.get("error_codes") or []:
            code = (err.get("error_code") or "").strip()
            if code in ("", "-"):
                continue
            e = codes.setdefault(code, {"error_code": code, "count": 0, "example": ""})
            e["count"] += 1
            if not e["example"] and (err.get("details_error") or "").strip():
                e["example"] = err["details_error"].strip()

        display = _agent_display(report.get("agent_name"))
        key = display.casefold() if display else None
        a = agents.setdefault(key, {"names": Counter(), "tickets": 0, "pass": 0, "scores": []})
        a["names"][display or NO_AGENT] += 1
        a["tickets"] += 1
        a["pass"] += 1 if is_pass else 0
        if percent is not None:
            a["scores"].append(percent)

        commit = (report.get("commitment_status") or {}).get("status")
        commitment[commit if commit in COMMITMENT_KEYS else "NOT_STATED"] += 1

    with_report = kpi["with_report"]
    agent_rows = []
    for key, a in agents.items():
        name = NO_AGENT if key is None else sorted(a["names"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        agent_rows.append({"agent": name, "tickets": a["tickets"], "pass": a["pass"],
                           "fail": a["tickets"] - a["pass"], "pass_rate": _pct(a["pass"], a["tickets"]),
                           "avg_score_percent": _avg(a["scores"])})
    # "Tidak disebut" selalu di akhir; sisanya tiket terbanyak lalu nama.
    agent_rows.sort(key=lambda r: (r["agent"] == NO_AGENT, -r["tickets"], r["agent"]))

    return {
        "kpi": {
            "total": kpi["total"], "done": kpi["done"], "in_progress": kpi["in_progress"],
            "failed": kpi["failed"], "with_report": with_report, "without_report": kpi["without_report"],
            "pass": kpi["pass"], "fail": kpi["fail"], "pass_rate": _pct(kpi["pass"], with_report),
            "avg_score_percent": _avg(scores),
        },
        "daily": [daily[d] for d in sorted(daily)],
        "categories": [
            {"category": name, "reports": c["reports"], "avg_percent": _avg(c["percents"]),
             "fail": c["fail"], "fail_rate": _pct(c["fail"], c["reports"]), "unavailable": c["unavailable"]}
            for name, c in categories.items()
        ],
        "top_failed_indicators": [
            {**ind, "rate": _pct(ind["belum_sesuai"], with_report)}
            for ind in sorted(indicators.values(), key=lambda i: (-i["belum_sesuai"], i["item_code"]))[:TOP_INDICATORS]
        ],
        "critical": {
            "pass": critical["pass"], "fail": critical["fail"], "unavailable": critical["unavailable"],
            "items": sorted(critical_items.values(), key=lambda i: (-i["fail"], i["item_code"])),
        },
        "error_codes": sorted(codes.values(), key=lambda e: (-e["count"], e["error_code"])),
        "agents": agent_rows,
        "commitment": {k: commitment[k] for k in COMMITMENT_KEYS},
    }
