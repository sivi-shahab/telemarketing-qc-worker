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

# Scorecard v01 (Okt 2026) membagi item ke dua kasus. Etika adalah gerbang: satu
# item etika BELUM_SESUAI menolkan skor akhir, berapa pun poin item lainnya.
CASE_STANDARD = "standard_penagihan"
CASE_ETIKA = "etika_penagihan"

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
        weight = max(_num(it.get("weight")) or 0, 0)
        declared = _num(it.get("item_score"))
        if declared is not None:
            # Dijepit ke [0, bobot]: model pernah memberi skor di atas bobot item,
            # dan satu item tidak boleh menyumbang lebih dari bobotnya ke total.
            score = min(max(declared, 0), weight)
        elif status == "SESUAI":
            score = weight
        elif status == "BELUM_SESUAI":
            score = 0
        else:
            score = None
        prose = it["evidence"] if isinstance(it.get("evidence"), str) else ""
        row = {
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
        }
        # Kasus & sifat opsional ditempel apply_configured_weights dari scorecard;
        # disimpan di item supaya pembacaan ulang bisa menghitung ulang ringkasan kasus.
        if isinstance(it.get("case"), str) and it["case"].strip():
            row["case"] = it["case"].strip()
        if isinstance(it.get("optional"), bool):
            row["optional"] = it["optional"]
        out.append(row)
    return out


def _case_summary(scorecard):
    """Ringkasan per kasus, dihitung dari item — ``case_summary`` model tidak dibaca.

    Item opsional adalah bonus: masuk ``optional_earned`` hanya bila SESUAI, dan
    tidak masuk ``mandatory_weight``. Etika FAIL bila ada item BELUM_SESUAI; standard
    FAIL bila ada item wajib non-tolerable yang BELUM_SESUAI."""
    cases = {}
    for it in scorecard:
        case = it.get("case")
        if not case:
            continue
        c = cases.setdefault(case, {"case": case, "max_points": 0, "mandatory_weight": 0,
                                    "mandatory_earned": 0, "optional_earned": 0,
                                    "case_points": 0, "case_result": "PASS"})
        optional = it.get("optional") is True
        score = it["item_score"] or 0
        c["max_points"] += it["weight"]
        if optional:
            c["optional_earned"] += score if it["status"] == "SESUAI" else 0
        else:
            c["mandatory_weight"] += it["weight"]
            c["mandatory_earned"] += score
        failed = it["status"] == "BELUM_SESUAI"
        if failed and (case == CASE_ETIKA or (not optional and it["tolerable"].upper() == "NO")):
            c["case_result"] = "FAIL"
    for c in cases.values():
        for k in ("max_points", "mandatory_weight", "mandatory_earned", "optional_earned"):
            c[k] = _round1(c[k])
        c["case_points"] = _round1(c["mandatory_earned"] + c["optional_earned"])
    return list(cases.values())


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


def _configured_items(scorecard_text):
    """Item scorecard campaign sebagai list dict, atau None bila tidak terbaca.

    Dua bentuk diterima: array datar ``[{item_code, weight}]`` (sebelum v01) dan
    bentuk berkasus v01 ``[{"standard_penagihan": {"items": [...]}, ...}]`` — tiap
    item bentuk kedua diberi ``case`` sesuai kunci kasusnya."""
    try:
        parsed = json.loads(scorecard_text)
    except (TypeError, ValueError):
        return None
    flat_allowed = isinstance(parsed, list)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return None
    items = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        if "item_code" in entry or "weight" in entry:
            if not flat_allowed:
                continue
            items.append(entry)
            continue
        for case, block in entry.items():
            if isinstance(block, dict) and isinstance(block.get("items"), list):
                items.extend({**i, "case": case} for i in block["items"] if isinstance(i, dict))
    return items


def scorecard_maximum(scorecard_text):
    """Jumlah kolom ``weight`` scorecard campaign, atau None."""
    items = _configured_items(scorecard_text)
    if not items:
        return None
    total = sum(_num(i.get("weight")) or 0 for i in items)
    return total if total > 0 else None


def apply_configured_weights(raw, scorecard_text):
    """Salinan ``raw`` dengan bobot tiap item ``scorecard_result`` diambil dari
    scorecard campaign bila item_code-nya ada — begitu pula ``case`` dan
    ``optional`` bila scorecard memuatnya.

    Bobot adalah konfigurasi, bukan keluaran model: tanpa ini model bisa menaikkan
    bobot item yang ia nilai SESUAI. Item yang tidak ada di konfigurasi, atau
    konfigurasi yang tidak terbaca, dibiarkan apa adanya. Masukan tidak diubah."""
    if not isinstance(raw, dict) or not isinstance(raw.get("scorecard_result"), list):
        return raw
    config = {}
    for item in _configured_items(scorecard_text) or []:
        if not isinstance(item.get("item_code"), str):
            continue
        fields = {}
        w = _num(item.get("weight"))
        if w is not None:
            fields["weight"] = w
        if isinstance(item.get("case"), str):
            fields["case"] = item["case"]
        if isinstance(item.get("optional"), bool):
            fields["optional"] = item["optional"]
        if fields:
            config[item["item_code"].strip()] = fields
    if not config:
        return raw
    items = []
    for it in raw["scorecard_result"]:
        code = it.get("item_code") if isinstance(it, dict) else None
        if isinstance(code, str) and code.strip() in config:
            it = {**it, **config[code.strip()]}
        items.append(it)
    return {**raw, "scorecard_result": items}


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

    # Scorecard berkasus (v01): item opsional adalah bonus, jadi ambang lulus diukur
    # dari bobot item wajib saja; satu pelanggaran etika menolkan skor akhir.
    cases = _case_summary(scorecard)
    base_maximum = None
    etika_failed = False
    if cases:
        base_maximum = _round1(sum(c["mandatory_weight"] for c in cases))
        passing = _round1(base_maximum * PASSING_GRADE_RATIO)
        etika_failed = any(c["case"] == CASE_ETIKA and c["case_result"] == "FAIL" for c in cases)
        if etika_failed:
            earned = 0

    report = {
        "call_id": _str(raw.get("call_id"), "-"),
        "consumer_full_name": raw["consumer_full_name"] if isinstance(raw.get("consumer_full_name"), str) else None,
        "agent_name": raw["agent_name"] if isinstance(raw.get("agent_name"), str) else None,
        "product_type": raw["product_type"] if isinstance(raw.get("product_type"), str) else None,
        "agunan_discussion_status": "INITIATED" if raw.get("agunan_discussion_status") == "INITIATED" else "NOT_INITIATED",
        "commitment_status": _commitment(raw.get("commitment_status")),
        "maximum_score": maximum,
        "base_maximum_score": base_maximum,
        "passing_grade": passing,
        "ai_score_phase_2": earned,
        # Laporan tanpa apa pun untuk dinilai bukan kelulusan.
        "ai_status": "PASS" if maximum > 0 and not etika_failed and earned >= passing else "FAIL",
        "scorecard_result": scorecard,
        "case_summary": cases,
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


def normalize_stored_report(evaluation):
    """Normalisasi ulang laporan yang DIBACA dari ``result_data``.

    Maksimum yang tersimpan (``maximum_score``, hasil konfigurasi scorecard saat
    ditulis worker) dipakai lagi sebagai ``configured_maximum``. Tanpanya balasan
    terpotong dinilai ulang terhadap jumlah bobot item yang dijawab saja, sehingga
    FAIL bisa berubah PASS hanya karena dibaca ulang."""
    stored = evaluation.get("maximum_score") if isinstance(evaluation, dict) else None
    maximum = _num(stored)
    return normalize_weighted_report(
        evaluation, configured_maximum=maximum if maximum is not None and maximum > 0 else None)


def build_collection_result_json(*, result_id, campaign, source_files, report,
                                 processed_at, processing_sec, audio_duration=None):
    """``result_json`` tiket Collection. Sengaja TANPA ``reference_data``,
    ``assigned_agent``, ``recording_types`` dsb.: semua itu turunan TMS/Ascend dan
    jalur ini tidak menyentuhnya. ``report_type`` membuat pembaca tidak pernah
    salah mengira isinya format Cashline."""
    return {
        "report_type": REPORT_TYPE,
        "result_id": str(result_id),
        "campaign": campaign,
        "source_files": list(source_files),
        "num_calls": len(source_files),
        "audio_duration": audio_duration,
        "processed_at": processed_at,
        "processing_sec": round(processing_sec, 2),
        "evaluation": report,
    }


def is_collection_result_json(result_json) -> bool:
    return isinstance(result_json, dict) and result_json.get("report_type") == REPORT_TYPE


def _ticket_id(source_files):
    first = (source_files or [None])[0]
    return first.split("_", 1)[0] if isinstance(first, str) and first else None


def collection_list_row(result, result_json):
    """Satu baris tabel menu Collection. Laporan dibaca ULANG lewat normalizer
    supaya baris lama yang tersimpan sebelum aturan berubah ikut konsisten."""
    report = (normalize_stored_report(result_json.get("evaluation"))
              if is_collection_result_json(result_json) else None)
    iso = lambda dt: dt.isoformat() if dt is not None else None  # noqa: E731
    return {
        "result_id": str(result.id),
        "campaign": result.campaign,
        "ticket_id": _ticket_id(result.source_files),
        "source_files": list(result.source_files or []),
        "status": result.status,
        "uploaded_at": iso(result.uploaded_at),
        "completed_at": iso(result.completed_at),
        "agent_name": report["agent_name"] if report else None,
        "consumer_full_name": report["consumer_full_name"] if report else None,
        "product_type": report["product_type"] if report else None,
        "score": report["ai_score_phase_2"] if report else None,
        "maximum_score": report["maximum_score"] if report else None,
        "passing_grade": report["passing_grade"] if report else None,
        "ai_status": report["ai_status"] if report else None,
        "critical_status": report["critical_compliance_check"]["status"] if report else None,
        "commitment_status": report["commitment_status"]["status"] if report else None,
        "error_code_count": len(report["error_codes"]) if report else 0,
        **_case_columns(report["case_summary"] if report else []),
    }


def _case_columns(cases):
    """Kolom Standard/Etika tabel; None untuk laporan scorecard datar (pra-v01)."""
    by_case = {c["case"]: c for c in cases}
    out = {}
    for prefix, case in (("standard", CASE_STANDARD), ("etika", CASE_ETIKA)):
        c = by_case.get(case)
        out[f"{prefix}_score"] = c["case_points"] if c else None
        out[f"{prefix}_maximum"] = c["max_points"] if c else None
        out[f"{prefix}_status"] = c["case_result"] if c else None
    return out
