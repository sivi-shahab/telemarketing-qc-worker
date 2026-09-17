"""Laporan audit BERBOBOT campaign Collection (POJK 22/2023).

Port Python dari ``qc-collection/src/server/weightedReport.ts``. Keluaran LLM
hanya dijamin JSON yang bisa di-parse, tidak pernah berbentuk tertentu — modul ini
memaksanya menjadi ``WeightedAuditReport`` yang aman dirender dashboard.

Dua aturan:
  1. Setiap field yang dibaca dashboard ada sesudahnya, dengan tipe yang benar.
  2. Tidak ada verdict yang dikarang. Bila model tidak memberi PASS/FAIL — atau
     memberinya dalam bentuk yang polaritasnya harus ditebak — hasilnya
     ``TIDAK_TERSEDIA``. Menyatakan panggilan penagihan compliant tanpa bukti lebih
     berbahaya daripada menampilkan celah.

Jalur Collection sengaja TIDAK memakai TMS maupun Ascend: tidak ada nilai acuan,
sehingga ``collection_data_verification`` hanya bisa berisi nilai yang disebut
dalam panggilan.
"""
import json
import math

REPORT_TYPE = "collection_weighted"

# Ambang lulus adalah kebijakan, bukan angka yang boleh berubah tiap panggilan
# model (pernah keluar 87 dari 150 dan 120.6 dari 134 untuk prompt yang sama).
PASSING_GRADE_RATIO = 0.9

UNAVAILABLE = "TIDAK_TERSEDIA"
_PASS_FAIL = {"PASS", "FAIL"}
_COMMITMENT = {"COMMITTED_TO_PAY", "PARTIAL_COMMITMENT", "DISPUTE", "REFUSED", "NOT_STATED"}
_MATCH = {"MATCH", "MISMATCH", "SKIPPED_NULL"}
_SCORECARD = {"SESUAI", "BELUM_SESUAI", "TIDAK_DINILAI"}


def _num(value):
    """Angka hingga (bool bukan angka), selain itu None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _str(value, fallback=""):
    return value if isinstance(value, str) else fallback


def _scalar(value):
    if isinstance(value, bool):
        return "YA" if value else "TIDAK"
    if _num(value) is not None or isinstance(value, str):
        return value
    return None


def _round1(n):
    return round(n * 10) / 10


def _evidence(value):
    # String di sini adalah uraian, bukan kutipan transkrip — tidak boleh sampai ke
    # ``quote``, yang dirender miring dalam tanda kutip seolah diucapkan nasabah.
    if not isinstance(value, dict):
        return {"timestamp": None, "quote": None}
    return {
        "timestamp": value["timestamp"] if isinstance(value.get("timestamp"), str) else None,
        "quote": value["quote"] if isinstance(value.get("quote"), str) else None,
    }


def _commitment(value):
    if not isinstance(value, dict):
        return {"status": "NOT_STATED", "reason": "", "evidence": _evidence(None)}
    declared = _str(value.get("status")).upper()
    if declared in _COMMITMENT:
        status = declared
    elif value.get("ada_kesepakatan") is True:
        status = "COMMITTED_TO_PAY"
    else:
        status = "NOT_STATED"
    reason = value.get("reason") if value.get("reason") is not None else value.get("ringkasan")
    return {"status": status, "reason": _str(reason), "evidence": _evidence(value.get("evidence"))}


def _critical(value):
    unavailable = {"status": UNAVAILABLE, "checked_items": []}
    if not isinstance(value, dict):
        return unavailable
    declared = _str(value.get("status")).upper()
    items = []
    for it in value.get("checked_items") if isinstance(value.get("checked_items"), list) else []:
        if not isinstance(it, dict):
            continue
        st = _str(it.get("status")).upper()
        items.append({
            "item_code": _str(it.get("item_code"), "-"),
            "requirement": _str(it.get("requirement")),
            "status": st if st in _PASS_FAIL else UNAVAILABLE,
        })
    # Bentuk datar {kunci: bool} sengaja TIDAK diterjemahkan: polaritas kuncinya
    # campur (false = syarat terlewat di satu kunci, pelanggaran terhindar di kunci
    # lain). Menebak berarti bisa mencetak PASS di atas kebocoran data sungguhan.
    if declared not in _PASS_FAIL and not items:
        return unavailable
    return {"status": declared if declared in _PASS_FAIL else UNAVAILABLE, "checked_items": items}


def _verification_row(field, source, extracted):
    match = _str(source.get("match")).upper()
    return {
        "field": field,
        "reference_value": _scalar(source.get("reference_value")),
        "extracted_value": _scalar(extracted),
        "match": match if match in _MATCH else UNAVAILABLE,
        "similarity_percent": _num(source.get("similarity_percent")),
        "reason": _str(source.get("reason")),
        "item_score": _num(source.get("item_score")),
    }


def _verification(value):
    if isinstance(value, list):
        return [_verification_row(_str(r.get("field"), "-"), r, r.get("extracted_value"))
                for r in value if isinstance(r, dict)]
    if isinstance(value, dict):
        # Bentuk datar: hanya nilai yang disebut agent, tanpa acuan -> tanpa verdict.
        return [_verification_row(k, {}, v) for k, v in value.items()]
    return []


def _categories(value):
    if not isinstance(value, list):
        return []
    out = []
    for c in value:
        if not isinstance(c, dict):
            continue
        res = _str(c.get("category_result")).upper()
        total = c.get("total_weight") if c.get("total_weight") is not None else c.get("maximum_score")
        earned = c.get("earned_score") if c.get("earned_score") is not None else c.get("score")
        out.append({
            "category": _str(c.get("category"), "-"),
            "total_weight": _num(total) or 0,
            "earned_score": _num(earned) or 0,
            "category_result": res if res in _PASS_FAIL else UNAVAILABLE,
            "fail_reason": c["fail_reason"] if isinstance(c.get("fail_reason"), str) else None,
        })
    return out


def _scorecard_status(raw):
    s = _str(raw).upper()
    if s in _SCORECARD:
        return s
    if s in ("PASS", "YA"):
        return "SESUAI"
    if s in ("FAIL", "TIDAK"):
        return "BELUM_SESUAI"
    return "TIDAK_DINILAI"


def _scorecard(value):
    if not isinstance(value, list):
        return []
    out = []
    for it in value:
        if not isinstance(it, dict):
            continue
        status = _scorecard_status(it.get("status"))
        weight = _num(it.get("weight")) or 0
        declared = _num(it.get("item_score"))
        if declared is not None:
            score = declared
        elif status == "SESUAI":
            score = weight
        elif status == "BELUM_SESUAI":
            score = 0
        else:
            score = None
        prose = it["evidence"] if isinstance(it.get("evidence"), str) else ""
        out.append({
            "category": _str(it.get("category"), "-"),
            "item_code": _str(it.get("item_code"), "-"),
            "requirement": _str(it.get("requirement")),
            "kb_reference": _str(it.get("kb_reference")),
            "tolerable": _str(it.get("tolerable"), "YES"),
            "weight": weight,
            "status": status,
            "item_score": score,
            "reason": _str(it.get("reason")) or prose,
            "evidence": _evidence(it.get("evidence")),
        })
    return out


def _error_codes(value):
    if not isinstance(value, list):
        return []
    out = []
    for e in value:
        if isinstance(e, str):
            if e.strip():
                out.append({"error_code": e.strip(), "details_error": "",
                            "trigger_source": {"reason": "", "evidence": _evidence(None)}})
            continue
        if not isinstance(e, dict):
            continue
        src = e.get("trigger_source") if isinstance(e.get("trigger_source"), dict) else {}
        out.append({
            "error_code": _str(e.get("error_code"), "-"),
            "details_error": _str(e.get("details_error")),
            "trigger_source": {"reason": _str(src.get("reason")),
                               "evidence": _evidence(src.get("evidence"))},
        })
    return out


def scorecard_maximum(scorecard_text):
    """Jumlah kolom ``weight`` scorecard campaign (JSON array), atau None."""
    try:
        parsed = json.loads(scorecard_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    total = sum(_num(i.get("weight")) or 0 for i in parsed if isinstance(i, dict))
    return total if total > 0 else None


def normalize_weighted_report(value, configured_maximum=None):
    """Satu gerbang untuk setiap laporan berbobot — saat keluar dari model DAN saat
    dibaca ulang dari ``result_data``, sehingga catatan lama ikut tersembuhkan."""
    raw = value if isinstance(value, dict) else {}
    scorecard = _scorecard(raw.get("scorecard_result"))

    # Skor adalah aritmetika atas scorecard, bukan bacaan dari model: pernah keluar
    # 116/116 PASS padahal 26 item-nya sendiri berjumlah 89 dari 150.
    items_max = sum(i["weight"] for i in scorecard)
    maximum = configured_maximum if configured_maximum and configured_maximum > 0 else items_max
    earned = _round1(sum(i["item_score"] or 0 for i in scorecard))
    passing = _round1(maximum * PASSING_GRADE_RATIO)

    report = {
        "call_id": _str(raw.get("call_id"), "-"),
        "consumer_full_name": raw["consumer_full_name"] if isinstance(raw.get("consumer_full_name"), str) else None,
        "agent_name": raw["agent_name"] if isinstance(raw.get("agent_name"), str) else None,
        "product_type": raw["product_type"] if isinstance(raw.get("product_type"), str) else None,
        "agunan_discussion_status": "INITIATED" if raw.get("agunan_discussion_status") == "INITIATED" else "NOT_INITIATED",
        "commitment_status": _commitment(raw.get("commitment_status")),
        "maximum_score": maximum,
        "passing_grade": passing,
        "ai_score_phase_2": earned,
        # Laporan tanpa apa pun untuk dinilai bukan kelulusan.
        "ai_status": "PASS" if maximum > 0 and earned >= passing else "FAIL",
        "scorecard_result": scorecard,
        "category_summary": _categories(raw.get("category_summary")),
        "critical_compliance_check": _critical(raw.get("critical_compliance_check")),
        "collection_data_verification": _verification(raw.get("collection_data_verification")),
        "error_codes": _error_codes(raw.get("error_codes")),
        "ai_summary": _str(raw.get("ai_summary")),
    }
    for key in ("audio_filename", "generated_at", "banner_warning"):
        if isinstance(raw.get(key), str):
            report[key] = raw[key]
    return report
