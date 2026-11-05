"""Perakitan artefak Builder: scorecard, KB, dan prompt dari draf, plus validasi sebelum diunggah."""
import json
import re
from collections import Counter

from builder.drafting import evidence_rule_for, is_critical

# Bagian prompt keluarga NTB/Reinstate yang berisi slot spesifik campaign (hasil `split_prompt`, November 2026).
# Bagian ini TIDAK dibangkitkan otomatis: Builder hanya menggantikan nama, sisanya wajib ditinjau.
SLOT_SECTIONS = [
    "CAMPAIGN CONTEXT", "EVALUATION RULES", "ERROR CODE CLASSIFICATION RULE", "CRITICAL COMPLIANCE CHECK RULE",
    "GLOBAL EVIDENCE ORDERING", "EVIDENCE EXTRACTION RULES", "SCORECARD REASON RULE", "CUSTOMER INTEREST RULE",
    "VARIANT (DITENTUKAN DARI MINAT)", "VARIANT CONDITIONAL RULE", "SCORING RULE (MAXIMUM SCORE, PASSING GRADE",
    "STRUCTURED DATA EXTRACTION", "OUTPUT FORMAT (STRICT JSON ONLY)",
]

_GENERIC_NOTE = ("Pindai SEMUA panggilan (langkah script sering terulang setelah telepon terputus). SESUAI bila langkah "
                 "ini terpenuhi di panggilan MANA PUN; evidence = kemunculan yang MEMENUHI dengan posisi "
                 "(call_index, timestamp) TERBESAR.")


def _variant_mentions(category: str) -> tuple:
    c = (category or "").lower()
    basic = "basic" in c
    supl = "supplement" in c or "suplemen" in c or "kartu tambahan" in c
    mus = "ultima" in c or "mus" in c.split() or "health" in c
    return basic, supl, mus


_FIELD_ROW = re.compile(r"^\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(Wajib|Optional|Mandatory)\s*\|\s*(.*?)\s*$", re.I)


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip().lower()


def parse_field_tables(blocks: list) -> list:
    """Tabel field berbentuk 'NO | Field | Wajib/Optional | Keterangan' di transkrip gambar script (bisa terbagi di beberapa
    gambar). Hasil: [{category, fields:[{no, name, wajib, otomatis, ket}]}] per blok. Field Wajib yang keterangannya
    'otomatis terisi' diisi sistem, jadi tidak dinilai per field (otomatis=True)."""
    out = []
    for b in blocks:
        fields = []
        for g in b.get("gambar") or []:
            for line in str(g.get("transkrip") or "").splitlines():
                m = _FIELD_ROW.match(line)
                if m:
                    ket = m.group(4)
                    fields.append({"no": int(m.group(1)), "name": re.sub(r"\s*\(new\)\s*$", "", m.group(2), flags=re.I).strip(),
                                   "wajib": m.group(3).lower() != "optional", "otomatis": "otomatis" in ket.lower(), "ket": ket})
        if len(fields) >= 5:
            out.append({"category": f"{b.get('fase') or ''} / {b.get('bagian') or ''}", "fields": fields})
    return out


def field_table_for(blocks: list, category: str):
    cat = _norm(category)
    for t in parse_field_tables(blocks):
        if _norm(t["category"]) == cat or cat.startswith(_norm(t["category"])) or _norm(t["category"]).startswith(cat):
            return t
    return None


def items_from_fields(fields: list, category: str, prefix: str, start: int) -> list:
    """Satu item per field Wajib yang diisi agent (bukan yang otomatis). Bobot/tolerable awal hanya tebakan."""
    items = []
    for f in [f for f in fields if f["wajib"] and not f["otomatis"]]:
        req = f"Agent menanyakan atau mengonfirmasi {f['name']} nasabah"
        items.append({
            "item_code": f"SC_{prefix}_{start + len(items)}", "category": category, "label": req, "requirement": req, "weight": 1,
            "tolerable": "NO", "applies_to": applies_to_for(category), "source": "BELUM", "reuse_from": None, "kb": None,
            "flags": ["bobot_dan_tolerable_tebakan"], "decision": "pending", "field": {"no": f["no"], "ket": f["ket"]},
        })
    return items


def applies_to_for(category: str):
    """Varian item dari nama kategori. Kategori gabungan (menyebut lebih dari satu varian) tidak ditebak: None."""
    basic, supl, mus = _variant_mentions(category)
    if basic or (supl and mus):
        return None
    return "SUPL" if supl else "MUS" if mus else None


def variant_unclear(category: str) -> bool:
    """True bila kategori menyebut beberapa varian sekaligus: varian per item harus ditentukan manusia."""
    basic, supl, mus = _variant_mentions(category)
    return (supl or mus) and (basic or (supl and mus))


def items_from_scorecard(parsed_items: list, prefix: str) -> tuple:
    """Item scorecard Bank Mega -> item draf. Item berskor kosong/0 (terisi otomatis) tidak dinilai: dikembalikan terpisah."""
    items, skipped, n = [], [], 0
    for it in parsed_items:
        if not it.get("score"):
            skipped.append({"category": it["category"], "label": it["label"], "note": it.get("note")})
            continue
        n += 1
        flags = [] if it.get("tolerable") else ["tolerable_tidak_terbaca"]
        if variant_unclear(it["category"]):
            flags.append("varian_tidak_jelas")
        items.append({
            "item_code": f"SC_{prefix}_{n}", "category": it["category"], "label": it["label"],
            "requirement": it["label"], "weight": it["score"], "tolerable": it.get("tolerable") or "NO",
            "applies_to": applies_to_for(it["category"]), "source": "BELUM", "reuse_from": None, "kb": None,
            "flags": flags, "decision": "pending",
        })
    return items, skipped


def items_from_proposal(proposed: list, prefix: str) -> list:
    items = []
    for n, it in enumerate(proposed, 1):
        items.append({
            "item_code": f"SC_{prefix}_{n}", "category": it.get("category") or "Lainnya", "label": it.get("requirement") or "",
            "requirement": it.get("requirement") or "", "weight": it.get("weight") or 1,
            "tolerable": it.get("tolerable") if it.get("tolerable") in ("YES", "NO") else "NO",
            "applies_to": applies_to_for(it.get("category")), "source": "BELUM", "reuse_from": None, "kb": None,
            "flags": ["bobot_dan_tolerable_tebakan"], "decision": "pending",
        })
    return items


def refresh_flags(item: dict) -> dict:
    """Hitung ulang flag tingkat item dari isi KB-nya (dipanggil tiap item berubah)."""
    keep = [f for f in item.get("flags", []) if f in ("bobot_dan_tolerable_tebakan", "tolerable_tidak_terbaca", "varian_tidak_jelas")]
    if "varian_tidak_jelas" in keep and item.get("varian_dikonfirmasi"):
        keep.remove("varian_tidak_jelas")
    kb = item.get("kb") or {}
    phrases = [p for p in kb.get("example_phrases", []) if not p.get("dibuang")]
    if is_critical(item):
        keep.append("penting")
    if item["source"] == "BARU" and phrases and not any(p["source"] == "SCRIPT" for p in phrases):
        keep.append("tanpa_kalimat_script")
    if item["source"] == "BARU" and kb.get("assumptions"):
        keep.append("ada_asumsi")
    if item["source"] == "BARU" and any("ditolak_verifikasi_llm" in p["flags"] or "tanpa_kata_kunci_requirement" in p["flags"]
                                          for p in phrases):
        keep.append("kalimat_perlu_ditinjau")
    if item["source"] == "PAKAI_ULANG" and (item.get("fakta_tidak_ada_di_script") or []):
        keep.append("fakta_campaign_lain")
    if item["source"] == "BELUM":
        keep.append("belum_diisi")
    item["flags"] = sorted(set(keep))
    return item


def build_scorecard(draft: dict) -> str:
    out = []
    for i in draft["items"]:
        if i["decision"] == "rejected":
            continue
        row = {"category": i["category"], "item_code": i["item_code"], "requirement": i["requirement"],
               "kb_reference": i["item_code"].replace("SC_", "KB_", 1), "weight": i["weight"], "tolerable": i["tolerable"]}
        if i.get("applies_to"):
            row["applies_to"] = i["applies_to"]
        out.append(row)
    return json.dumps(out, ensure_ascii=False, indent="\t")


def build_kb(draft: dict) -> str:
    kept = [i for i in draft["items"] if i["decision"] != "rejected"]
    cats = list(dict.fromkeys(i["category"] for i in kept))
    desc = draft.get("phase_descriptions") or {}
    kb = [{"conversation_phases": {c: {"description": desc.get(c) or
           f"{c}: " + "; ".join(i["requirement"] for i in kept if i["category"] == c)[:300]} for c in cats}}]
    for i in kept:
        k = i.get("kb") or {}
        rule = k.get("evidence_selection_rule") or {**evidence_rule_for(i["category"]), "note": _GENERIC_NOTE}
        row = {"kb_code": i["item_code"].replace("SC_", "KB_", 1), "category": i["category"], "requirement": i["requirement"],
               "evidence_selection_rule": rule,
               "example_phrases": [p["text"] for p in k.get("example_phrases", []) if not p.get("dibuang")]}
        if k.get("scoring_rule"):
            row["scoring_rule"] = k["scoring_rule"]
        if i.get("applies_to"):
            row["applies_to"] = i["applies_to"]
        kb.append(row)
    return json.dumps(kb, ensure_ascii=False, indent="\t")


def build_prompt(base_text: str, base_name: str, prefix: str, label: str) -> tuple:
    """Prompt campaign baru = prompt campaign dasar dengan nama diganti. Mengembalikan (teks, catatan tinjauan)."""
    text, notes = base_text, []
    counts = Counter(re.findall(r"\bSC_([A-Z0-9]+)_\d", text))
    old = counts.most_common(1)[0][0] if counts else None
    if old and old != prefix:
        text = text.replace(f"SC_{old}_", f"SC_{prefix}_").replace(f"KB_{old}_", f"KB_{prefix}_")
        if old == "NTB":
            text = text.replace("NTB VARIANT", f"{label.upper()} VARIANT")
            text = re.sub(r"\bNTB\b(?! \()", label, text)
        elif old == "RI":
            text = text.replace("Reinstate", label).replace("REINSTATE", label.upper())
        else:
            notes.append(f"Awalan kode basis '{old}' tidak dikenal: label campaign tidak diganti, periksa manual.")
    elif not old:
        notes.append("Prompt basis tidak memuat kode item SC_*: pastikan campaign dasar yang dipilih benar.")
    refs = sorted(set(re.findall(rf"SC_{prefix}_\d+(?:-\d+)?", text)), key=lambda s: int(re.search(r"\d+", s.split("_")[-1]).group()))
    if refs:
        notes.append(f"{len(refs)} rujukan ke nomor item SPESIFIK basis ({', '.join(refs[:12])}{'...' if len(refs) > 12 else ''}) "
                     "menunjuk item scorecard BARU dengan nomor yang sama, belum tentu item yang dimaksud: periksa satu per satu.")
    present = [s for s in SLOT_SECTIONS if s in text]
    notes.append(f"{len(present)} bagian spesifik campaign wajib ditinjau (konteks, aturan minat, varian, skor, ekstraksi, "
                 f"skema keluaran): {'; '.join(present)}.")
    return text, notes


def validate(draft: dict, scorecard_text: str, kb_text: str, prompt_text: str = "") -> dict:
    """Pemeriksaan sebelum terbit. ``errors`` memblokir terbit; ``warnings`` hanya tampil."""
    errors, warnings = [], []
    try:
        sc = json.loads(scorecard_text)
        kb = json.loads(kb_text)
    except ValueError as exc:
        return {"errors": [f"JSON tidak valid: {exc}"], "warnings": [], "stats": {}}
    kb_codes = {k["kb_code"] for k in kb if isinstance(k, dict) and "kb_code" in k}
    codes = [s["item_code"] for s in sc]
    if len(codes) != len(set(codes)):
        errors.append("item_code scorecard ada yang dobel")
    for s in sc:
        if s["kb_reference"] not in kb_codes:
            errors.append(f"{s['item_code']}: kb_reference {s['kb_reference']} tidak ada di KB")
    pending = [i["item_code"] for i in draft["items"] if i["decision"] == "pending"]
    blank = [i["item_code"] for i in draft["items"] if i["decision"] != "rejected" and i["source"] == "BELUM"]
    if blank:
        errors.append(f"{len(blank)} item belum diisi (pilih Pakai ulang, Baru, atau tolak): {', '.join(blank[:8])}")
    if pending:
        warnings.append(f"{len(pending)} item belum disetujui atau ditolak")
    crit = [i["item_code"] for i in draft["items"] if i["decision"] == "accepted" and "penting" in i["flags"]
            and not i.get("ditinjau_manual")]
    if crit:
        errors.append(f"{len(crit)} item penting (bobot >= 4) disetujui tanpa tinjauan manual: {', '.join(crit[:8])}")
    total = sum(float(s["weight"]) for s in sc)
    return {"errors": errors, "warnings": warnings,
            "stats": {"item": len(sc), "kb_item": len(kb_codes), "total_bobot": total, "passing_90_persen": round(total * 0.9, 2),
                      "karakter": {"scorecard": len(scorecard_text), "kb": len(kb_text), "prompt": len(prompt_text)}}}
