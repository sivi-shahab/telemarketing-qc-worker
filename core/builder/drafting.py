"""Penyusun draf Builder: pemanggilan LLM + pemeriksaan deterministik atas keluarannya.

Prinsip (dari eksperimen script -> KB, November 2026):
* Kerangka item (kode, kategori, bobot, tolerable) datang dari scorecard, bukan dari LLM.
* Tiap kalimat contoh bertanda asal SCRIPT (kutipan harafiah, diverifikasi substring) atau KARANGAN.
* Aturan keras yang dituning pada panggilan nyata TIDAK bisa diturunkan dari script; item penting ditandai agar
  ditinjau manual atau dipakai ulang dari pustaka KB campaign yang sudah teruji.
"""
import base64
import json
import re
from difflib import SequenceMatcher

# Item dengan bobot sekurang-kurangnya ini ditandai "penting": tidak boleh disetujui massal.
CRITICAL_WEIGHT = 4

STOPWORDS = frozenset(
    "agent menyebutkan menanyakan menyampaikan menjelaskan nasabah atau dan yang dari ke di pada untuk dengan "
    "sudah bila hanya dalam secara kepada oleh agar adalah akan juga".split()
)

# Modul fitur campaign yang dijawab LLM dari script. "ada" dan "bukti" wajib; bukti harus kutipan harafiah.
MODULES = {
    "kartu_tambahan": "nasabah dapat menambah kartu tambahan (supplement)",
    "asuransi_mus": "ada penawaran asuransi Mega Ultima Shield / PFI Mega Life dengan Health Declaration",
    "verifikasi_data_acuan": "agent memverifikasi identitas nasabah terhadap data yang sudah tercatat di bank "
                             "(mis. tempat lahir, tanggal lahir, nama ibu kandung, pertanyaan dinamis)",
    "konfirmasi_data_tercatat": "agent membacakan data yang sudah tercatat lalu nasabah membenarkan atau mengoreksi",
    "pengisian_data_baru": "agent mengisi data aplikasi baru dari nol (nasabah baru)",
    "persetujuan_sharing_data": "ada permintaan persetujuan sharing/penggunaan data pribadi",
}


# --------------------------------------------------------------------------------------------- utilitas teks

def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (s or "").lower())).strip()


def content_words(s: str) -> set:
    return {w for w in re.findall(r"[a-z]+", (s or "").lower()) if len(w) > 3 and w not in STOPWORDS}


def keyword_overlap(requirement: str, phrase: str) -> bool:
    """True bila kalimat berbagi kata isi (awalan 5 huruf) dengan requirement. Kalimat yang tidak berbagi
    kata apa pun dengan requirement dicurigai salah makna (flag "perlu ditinjau", bukan penolakan)."""
    req = content_words(requirement)
    ph = {w[:5] for w in content_words(phrase)}
    return any(r[:5] in ph for r in req)


def is_critical(item: dict) -> bool:
    return float(item.get("weight") or 0) >= CRITICAL_WEIGHT


def evidence_rule_for(category: str) -> dict:
    """Aturan pemilihan evidence standar menurut kategori (pola yang sama di KB NTB/Reinstate/Cashline)."""
    c = (category or "").lower()
    if c.startswith("final konfirmasi"):
        return {"phase_scope": "FINAL_CONFIRMATION", "prefer": "MENTION_IN_FINAL_CONFIRMATION", "fallback": "NONE"}
    if c.startswith("closing"):
        return {"phase_scope": "CLOSING", "prefer": "MENTION_IN_CLOSING", "fallback": "NONE"}
    return {"phase_scope": "ANY", "prefer": "LATEST_QUALIFYING_MENTION", "fallback": "NONE"}


# --------------------------------------------------------------------------------------------- panggilan LLM

def chat_json(client, model: str, prompt: str) -> tuple:
    r = client.chat.completions.create(
        model=model, response_format={"type": "json_object"}, messages=[{"role": "user", "content": prompt}]
    )
    return json.loads(r.choices[0].message.content), (r.usage.prompt_tokens, r.usage.completion_tokens)


def transcribe_image(client, model: str, png: bytes) -> tuple:
    r = client.chat.completions.create(model=model, messages=[{"role": "user", "content": [
        {"type": "text", "text": "Salin isi gambar ke teks apa adanya. Tabel: satu baris per baris tabel, kolom dipisah "
                                 "' | ', sertakan header; jangan menafsir atau menambah. Infografis: tulis semua teks "
                                 "yang terlihat."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
    ]}])
    return r.choices[0].message.content, (r.usage.prompt_tokens, r.usage.completion_tokens)


def script_digest(blocks: list, limit: int = 450, image_limit: int = 1800) -> str:
    """Ringkasan script untuk prompt LLM: tiap blok dipotong, transkrip gambar ikut ditempel."""
    out = []
    for b in blocks:
        syarat = (b.get("syarat") or "").splitlines()[0][:40] if b.get("syarat") else "-"
        out.append(f"[{b['fase']} / {(b['bagian'] or '-')[:60]}] SYARAT: {syarat}\n{(b['script'] or '')[:limit]}")
        for g in b.get("gambar") or []:
            if g.get("transkrip"):
                out.append(f"  (gambar di baris {g['baris']}):\n{g['transkrip'][:image_limit]}")
    return "\n\n".join(out)


def script_corpus(blocks: list) -> str:
    """Seluruh teks script (SCRIPT + KETERANGAN + transkrip gambar), ternormalisasi, untuk uji substring."""
    parts = []
    for b in blocks:
        parts += [b.get("script") or "", b.get("keterangan") or ""]
        parts += [g.get("transkrip") or "" for g in b.get("gambar") or []]
    return norm(" ".join(parts))


_ASK_MODULES = """Dari SCRIPT campaign Bank Mega di bawah, jawab modul fitur campaign. Untuk tiap modul beri "ada" (true/false)
dan "bukti": kutipan HARAFIAH singkat dari script (null bila tidak ada). Jangan menebak: bila script tidak menyebutnya,
ada=false. Tambahkan "maks_nama" (angka atau null) pada kartu_tambahan.
Modul:
%s
Kembalikan HANYA JSON: {"modul": {"<nama>": {"ada": bool, "bukti": str|null}}}

SCRIPT:
%s
"""


def suggest_modules(client, model: str, digest: str, corpus: str) -> tuple:
    mods, usage = chat_json(client, model, _ASK_MODULES % ("\n".join(f"- {k}: {v}" for k, v in MODULES.items()), digest))
    out = {}
    for name in MODULES:
        m = (mods.get("modul") or {}).get(name) or {}
        bukti = m.get("bukti")
        # bukti yang bukan kutipan harafiah script tidak dipercaya
        verified = bool(bukti) and norm(bukti.strip('"')) in corpus
        out[name] = {"ada": bool(m.get("ada")), "bukti": bukti, "bukti_terverifikasi": verified,
                     "maks_nama": m.get("maks_nama")}
    return out, usage


_ASK_ITEMS = """Dari SCRIPT campaign Bank Mega di bawah, usulkan item scorecard QC telemarketing: satu item per langkah yang
agent WAJIB lakukan (menanyakan, menjelaskan, mengonfirmasi, meminta persetujuan). Kategori mengikuti fase script.
Requirement berbentuk kalimat "Agent ...". bobot: 1-5 (tebakan; manusia akan mengoreksi). tolerable: "YES"/"NO".
Kembalikan HANYA JSON: {"items": [{"category": "...", "requirement": "...", "weight": n, "tolerable": "YES|NO"}]}

SCRIPT:
%s
"""


def propose_items_from_script(client, model: str, digest: str) -> tuple:
    res, usage = chat_json(client, model, _ASK_ITEMS % digest)
    return res.get("items") or [], usage


_ASK_SPLIT = """Dari satu bagian SCRIPT campaign Bank Mega di bawah, pecah menjadi item scorecard QC yang terpisah: SATU item per
pertanyaan, konfirmasi, atau informasi yang agent WAJIB sampaikan (jangan menggabung beberapa pertanyaan dalam satu item,
jangan menambah yang tidak ada di script). Requirement berbentuk kalimat "Agent ...". "kutipan" = potongan HARAFIAH dari
script yang menjadi dasar item itu. Urutkan sesuai urutan di script.
Kembalikan HANYA JSON: {"items": [{"requirement": "...", "kutipan": "..."}]}

BAGIAN SCRIPT:
%s
"""


def split_item_from_script(client, model: str, text: str) -> tuple:
    """Pecah satu bagian script menjadi item per pertanyaan. Kutipan diverifikasi harafiah (spasi diabaikan)."""
    res, usage = chat_json(client, model, _ASK_SPLIT % text)
    flat = re.sub(r"\s+", " ", text).lower()
    out = []
    for it in res.get("items") or []:
        req = str(it.get("requirement") or "").strip()
        q = str(it.get("kutipan") or "").strip()
        if req:
            out.append({"requirement": req, "kutipan": q, "kutipan_terverifikasi": bool(q) and re.sub(r"\s+", " ", q).lower() in flat})
    return out, usage


_ASK_GROUP = """Kamu menyusun Knowledge Base penilaian QC telemarketing Bank Mega dari SCRIPT campaign.
Untuk SETIAP item scorecard di bawah, hasilkan:
- requirement: kalimat "Agent ..." yang jelas (perbaiki dari label scorecard bila perlu, jangan mengubah maksudnya).
- example_phrases: kalimat contoh yang menandakan agent memenuhi requirement. Beri "source": "SCRIPT" HANYA bila kalimat
  adalah kutipan HARAFIAH (boleh potongan) dari script; selain itu "KARANGAN". Utamakan SCRIPT; jangan memalsukan kutipan;
  jangan mengutip instruksi untuk agent (mis. "hitung dari ...", "lanjutkan ke ...") sebagai ucapan agent.
- scoring_rule: kapan SESUAI dan BELUM_SESUAI, berdasar teks script dan kolom SYARAT.
- assumptions: hal yang Anda tebak karena script tidak menyatakannya (kosong bila tidak ada).
Kembalikan HANYA JSON: {"items":[{"item_code":...,"requirement":...,"example_phrases":[{"text":...,"source":...}],"scoring_rule":...,"assumptions":[...]}]}

ITEM SCORECARD:
%s

SCRIPT (fase / bagian / syarat / isi):
%s
"""

_ASK_VERIFY = """Kamu auditor QC telemarketing. Untuk tiap kalimat, nilai apakah kalimat itu SAJA, bila diucapkan agent, sudah
memenuhi requirement item. Tolak bila kalimat hanya terkait topik tetapi tidak melakukan hal yang diminta requirement
(mis. terima kasih bukan salam; ajakan melanjutkan bukan permintaan waktu). Placeholder seperti (nama agent) atau XXX boleh.
Kembalikan HANYA JSON: {"verdicts":[{"item_code":...,"index":...,"memenuhi":true|false,"alasan":"..."}]}

ITEM DAN KALIMAT:
%s
"""


def generate_group(client, model: str, items: list, digest: str, corpus: str) -> tuple:
    """Draf KB untuk satu kelompok item (satu kategori). Mengembalikan ({item_code: draf}, token)."""
    want = json.dumps([{k: i[k] for k in ("item_code", "requirement", "weight", "tolerable")} for i in items],
                      ensure_ascii=False, indent=1)
    res, (pi, po) = chat_json(client, model, _ASK_GROUP % (want, digest))
    drafts = {g["item_code"]: g for g in res.get("items") or [] if g.get("item_code")}
    # verifikasi kedua: tiap kalimat dinilai terhadap requirement-nya saja (tanpa aturan KB, agar tidak bias)
    payload = [{"item_code": c, "requirement": (d.get("requirement") or "").strip() or
                next(i["requirement"] for i in items if i["item_code"] == c),
                "kalimat": [{"index": n, "text": p["text"]} for n, p in enumerate(d.get("example_phrases") or [])]}
               for c, d in drafts.items() if d.get("example_phrases")]
    verdicts = {}
    if payload:
        v, (vi, vo) = chat_json(client, model, _ASK_VERIFY % json.dumps(payload, ensure_ascii=False, indent=1))
        pi, po = pi + vi, po + vo
        verdicts = {(x["item_code"], x["index"]): x for x in v.get("verdicts") or []}
    out = {}
    for c, d in drafts.items():
        req = (d.get("requirement") or "").strip() or next(i["requirement"] for i in items if i["item_code"] == c)
        phrases = []
        for n, p in enumerate(d.get("example_phrases") or []):
            text = (p.get("text") or "").strip()
            if not text:
                continue
            quoted = norm(text) in corpus
            vd = verdicts.get((c, n)) or {}
            flags = []
            if p.get("source") == "SCRIPT" and not quoted:
                flags.append("bukan_kutipan_script")
            if vd and not vd.get("memenuhi"):
                flags.append("ditolak_verifikasi_llm")
            if not keyword_overlap(req, text):
                flags.append("tanpa_kata_kunci_requirement")
            if not quoted:
                flags.append("karangan")
            phrases.append({"text": text, "source": "SCRIPT" if quoted else "KARANGAN", "flags": flags,
                            "alasan_verifikasi": vd.get("alasan")})
        out[c] = {"requirement": req, "example_phrases": phrases,
                  "scoring_rule": (d.get("scoring_rule") or "").strip(), "assumptions": d.get("assumptions") or []}
    return out, (pi, po)


# --------------------------------------------------------------------------------------------- pustaka KB

_NUM = re.compile(r"\d+(?:[.,]\d+)?\s*%?")
_NAMES = re.compile(r"Mega Ultima Shield|Mega Life|PFI|Cashline|Reinstate|NTB|MUS|RIPLAY|Ascend|microsite|Transmart|Hartono",
                    re.I)


def _wrap_phases(text: str) -> str:
    """KB Cashline: ``[ "conversation_phases": {...}, {item}, ...]`` (kunci langsung di dalam array). Bungkus objek
    fase dengan kurung kurawal supaya jadi JSON; teks lain tidak diubah."""
    m = re.match(r'\s*\[\s*"conversation_phases"\s*:\s*', text)
    if not m:
        return text
    try:
        value, end = json.JSONDecoder(strict=False).raw_decode(text, m.end())
    except ValueError:
        return text
    return '[{"conversation_phases": ' + json.dumps(value, ensure_ascii=False) + "}" + text[end:]


def parse_kb_lenient(text: str):
    """KB sebagai daftar item; None bila tidak terbaca. Toleran pada dua cacat yang ada di KB Cashline v40: objek
    fase tanpa pembungkus dan tanda kutip nyasar (`""`) di akhir daftar kalimat contoh."""
    fixed = _wrap_phases(text)
    fixed2 = re.sub(r'""(\s*[\],])', r'"\1', fixed)  # `""` nyasar sebelum `]` atau `,`
    for candidate in (text, fixed, fixed2):
        try:
            data = json.loads(candidate, strict=False)  # strict=False: baris baru/tab mentah di dalam string
            return data if isinstance(data, list) else None
        except (ValueError, TypeError):
            continue
    return None


def build_library(campaigns: list) -> tuple:
    """campaigns: [(nama, kb_text, scorecard_text)] -> (daftar item pustaka, daftar peringatan)."""
    lib, warn = [], []
    for name, kb_text, sc_text in campaigns:
        kb = parse_kb_lenient(kb_text or "")
        if not kb:
            if kb_text:
                warn.append(f"KB campaign '{name}' tidak bisa dibaca sebagai JSON; dilewati")
            continue
        try:
            sc = {i.get("kb_reference"): i for i in json.loads(sc_text or "[]")}
        except (ValueError, TypeError):
            sc = {}
        for k in kb:
            if not isinstance(k, dict) or "kb_code" not in k:
                continue
            s = sc.get(k["kb_code"], {})
            phrases = k.get("example_phrases") or k.get("agent_example_phrases") or []
            lib.append({
                "campaign": name, "kb_code": k["kb_code"], "category": k.get("category") or "",
                "requirement": k.get("requirement") or "", "example_phrases": phrases,
                "scoring_rule": k.get("scoring_rule") or "", "evidence_selection_rule": k.get("evidence_selection_rule"),
                "weight": s.get("weight"), "tolerable": s.get("tolerable"), "applies_to": k.get("applies_to"),
            })
    return lib, warn


def similarity(a: str, b: str) -> float:
    wa, wb = content_words(a), content_words(b)
    jac = len(wa & wb) / len(wa | wb) if wa | wb else 0.0
    return (jac + SequenceMatcher(None, norm(a), norm(b), autojunk=False).ratio()) / 2


def library_candidates(item: dict, library: list, limit: int = 3, floor: float = 0.4) -> list:
    scored = []
    for lib in library:
        s = similarity(item["requirement"], lib["requirement"])
        if norm(item.get("category")) == norm(lib["category"]):
            s += 0.1
        if s >= floor:
            scored.append((s, lib))
    scored.sort(key=lambda x: (-x[0], x[1]["campaign"], x[1]["kb_code"]))
    out, seen = [], set()
    for s, l in scored:
        key = (norm(l["requirement"]), norm(l["scoring_rule"]))
        if key in seen:  # salinan campaign lain dengan isi sama (mis. campaign uji): cukup sekali
            continue
        seen.add(key)
        out.append({"skor": round(s, 2), **l})
    return out[:limit]


def fact_flags(texts: list, corpus: str) -> list:
    """Angka dan nama produk dalam item pustaka yang TIDAK ada di script campaign baru (fakta campaign lain)."""
    found = set()
    for t in texts:
        found |= {m.group(0).strip() for m in _NUM.finditer(t or "")} | {m.group(0) for m in _NAMES.finditer(t or "")}
    missing = sorted(f for f in found if norm(f) and norm(f) not in corpus and len(norm(f)) > 1)
    return missing
