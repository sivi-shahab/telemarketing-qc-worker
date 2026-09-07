"""RIPLAY (Ringkasan Informasi Produk dan Layanan) ingestion for campaign upload.

RIPLAY is the product fact sheet Bank Mega publishes for a product; it is the
**ground truth** for every product value the agent must quote on the call
(tenor, bunga, provisi, biaya admin, limit, premi asuransi, ...). Bank Mega can
revise it at any time, so the QC knowledge base must follow the RIPLAY instead
of being hand-edited each time.

Flow (driven by ``POST /upload_detail_campaign``):

1. ``pdf_to_png_pages`` renders the uploaded RIPLAY PDF to page images.
2. ``extract_riplay`` sends those images to the (vision-capable) LLM and gets a
   strict JSON extraction back.
3. ``product_name_similarity`` gates the upload: the product name printed on the
   RIPLAY must match the campaign name being uploaded (default >= 50%).
4. ``apply_riplay_to_kb`` overlays the extraction onto the uploaded KB text.

The overlay only ever rewrites the ``details`` and ``example_phrases`` of KB
entries that ALREADY exist (see ``ASPECTS``) — it never adds, removes or
renumbers a ``kb_code``. Where the KB disagrees with the RIPLAY, the RIPLAY
wins. Where they agree, the KB block is left byte-identical.

The KB file is hand-maintained pseudo-JSON (tabs, occasional missing commas,
literal newlines inside strings), so the overlay is deliberately *textual*:
it brace-matches the value of a single key and swaps it in place, leaving every
other byte of the file untouched.
"""
import base64
import io
import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable, Optional

from compliance.evaluator import _parse_llm_json_object

# --- PDF -> page images ------------------------------------------------------

DEFAULT_MAX_PAGES = 20
DEFAULT_RENDER_SCALE = 2.0


def pdf_to_png_pages(
    pdf_bytes: bytes,
    max_pages: int = DEFAULT_MAX_PAGES,
    scale: float = DEFAULT_RENDER_SCALE,
) -> list[bytes]:
    """Render a PDF to a list of PNG page images (first ``max_pages`` pages).

    ``scale`` 2.0 renders at ~144 dpi, enough for the small print in a RIPLAY
    fee table while keeping the base64 payload reasonable.
    """
    if not pdf_bytes:
        raise ValueError("RIPLAY PDF kosong")

    import pypdfium2 as pdfium  # local import: only needed on the upload path

    try:
        doc = pdfium.PdfDocument(pdf_bytes)
    except Exception as exc:
        # A corrupt or mislabelled upload is the caller's problem, not the LLM's —
        # surface it as a validation error rather than a provider failure.
        raise ValueError(f"file bukan PDF yang bisa dibaca ({exc})")

    pages: list[bytes] = []
    try:
        for index in range(min(len(doc), max_pages)):
            image = doc[index].render(scale=scale).to_pil()
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="PNG")
            pages.append(buffer.getvalue())
    finally:
        doc.close()

    if not pages:
        raise ValueError("RIPLAY PDF tidak memiliki halaman yang bisa dirender")
    return pages


# A RIPLAY is a single dense A4 sheet: fee tables, footnotes and rendered formulas
# all at small point sizes. Sent whole, the page is downscaled by the vision API
# and the fine print stops being legible (a mis-read fraction bar turns the
# instalment formula into a mathematically wrong one). So each page is sent as the
# full view *plus* overlapping horizontal bands at full resolution.
BAND_HEIGHT = 1200
BAND_OVERLAP = 220


def _horizontal_bands(image, band_height: int = BAND_HEIGHT, overlap: int = BAND_OVERLAP):
    """Slice a tall page image into overlapping top-to-bottom bands."""
    width, height = image.size
    if height <= band_height:
        return [image]
    step = max(1, band_height - overlap)
    bands = []
    top = 0
    while top < height:
        bands.append(image.crop((0, top, width, min(top + band_height, height))))
        if top + band_height >= height:
            break
        top += step
    return bands


def pdf_to_llm_images(
    pdf_bytes: bytes,
    max_pages: int = DEFAULT_MAX_PAGES,
    scale: float = DEFAULT_RENDER_SCALE,
) -> list[tuple[str, bytes]]:
    """Render a PDF into ``(label, png)`` views for the LLM: whole pages + bands.

    Blank bands (a mostly-empty trailing page) are dropped so they don't spend
    tokens or invite the model to invent content for them.
    """
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(pdf_bytes)
    except Exception as exc:
        raise ValueError(f"file bukan PDF yang bisa dibaca ({exc})")

    views: list[tuple[str, bytes]] = []
    try:
        total = min(len(doc), max_pages)
        for index in range(total):
            page = doc[index].render(scale=scale).to_pil().convert("RGB")
            views.append((f"Halaman {index + 1} dari {total} — tampilan penuh", _png(page)))

            bands = _horizontal_bands(page)
            if len(bands) == 1:
                continue
            for position, band in enumerate(bands, start=1):
                if _is_blank(band):
                    continue
                views.append(
                    (
                        f"Halaman {index + 1} — potongan {position} dari {len(bands)} "
                        "(resolusi penuh, untuk membaca tulisan kecil)",
                        _png(band),
                    )
                )
    finally:
        doc.close()

    if not views:
        raise ValueError("RIPLAY PDF tidak memiliki halaman yang bisa dirender")
    return views


def _png(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _is_blank(image, threshold: int = 250) -> bool:
    """True when a crop is effectively empty paper (nothing worth sending)."""
    grey = image.convert("L")
    histogram = grey.histogram()
    dark = sum(histogram[:threshold])
    return dark < (grey.size[0] * grey.size[1]) * 0.001


# --- Extraction --------------------------------------------------------------

# Shape of the extraction. Kept flat and string-valued on purpose: the values are
# copied straight into the KB `details`, which the evaluator LLM reads as prose.
RIPLAY_SCHEMA: dict = {
    "nama_produk": "string — nama produk utama persis seperti tertulis di RIPLAY",
    "jenis_produk": "string|null",
    "penerbit": "string|null — nama bank/penerbit",
    "tanggal_berlaku": "string|null — tanggal berlaku/terbit dokumen",
    "limit_pencairan": {
        "minimum": "string|null — mis. '2 juta Rupiah'",
        "maksimum": "string|null — mis. '200 juta Rupiah'",
        "ketentuan": "string|null — syarat/ketentuan pemberian limit",
    },
    "tenor_cicilan": {
        "pilihan_bulan": "array of integer — mis. [12, 24, 36]",
        "keterangan": "string|null",
    },
    "suku_bunga": {
        "cicilan": "string|null — mis. '1,75% - 2,2% flat per bulan'",
        "revolving": "string|null — mis. '0,1%/hari'",
        "keterangan": "string|null",
    },
    "biaya_provisi": (
        "string|null — besaran provisi DAN periode pengenaannya bila tertulis "
        "(mis. 'dikenakan per tahun', 'mulai tahun ke-2'); periode boleh diambil dari "
        "label barisnya, ini pengecualian dari aturan 'buang label'"
    ),
    "biaya_admin": {
        "tiering": "array of {rentang_pencairan: string, biaya: string}",
        "keterangan": "string|null — dipakai bila biaya admin tidak bertingkat",
    },
    "nominal_cicilan": {
        "rumus": "string|null — cara hitung cicilan bulanan",
        "keterangan": "string|null",
    },
    "pelunasan_dipercepat": "string|null — biaya/penalti pelunasan dipercepat",
    "tanggal_tagihan": {
        "tanggal_cetak": "string|null",
        "tanggal_jatuh_tempo": "string|null",
    },
    "biaya_keterlambatan": "string|null",
    "asuransi": {
        "nama_produk": "string|null",
        "premi": "string|null",
        "manfaat": "array of string — daftar manfaat/benefit",
        "informasi_klaim": "string|null — tata cara & batas waktu klaim",
    },
}

RIPLAY_PROMPT = """Kamu adalah ekstraktor dokumen RIPLAY (Ringkasan Informasi Produk dan Layanan) milik Bank Mega.

Kamu menerima dokumen RIPLAY sebagai GAMBAR. Untuk tiap halaman diberikan dua jenis tampilan:
"tampilan penuh" (untuk memahami tata letak) dan beberapa "potongan" beresolusi penuh yang saling
bertumpang tindih (untuk membaca tulisan kecil, tabel biaya, catatan kaki, dan rumus).
Potongan-potongan itu adalah bagian dari halaman yang SAMA — jangan menghitungnya sebagai halaman
baru dan jangan menganggap data yang muncul di dua potongan sebagai dua data berbeda.
Utamakan potongan resolusi penuh saat membaca angka.

TUGAS: keluarkan SATU objek JSON dengan struktur PERSIS seperti berikut (nilai di bawah adalah
deskripsi tipe, bukan contoh jawaban):

{schema}

ATURAN:
1. Salin nilai APA ADANYA dari dokumen (angka, satuan, tanda persen, mata uang). JANGAN membulatkan,
   menghitung ulang, atau menambah informasi yang tidak tertulis.
2. Gunakan null (atau array kosong) bila informasi tidak ada di dokumen. JANGAN mengarang.
   **null jauh lebih baik daripada tebakan** — nilai hasil tebakan akan menimpa aturan QC yang benar.
3. Buang LABEL/judul kolomnya, TAPI JANGAN PERNAH membuang angka atau satuannya. Sel
   "Biaya Provisi (Dikenakan per tahun) | 2% (dua persen) dari limit kredit" -> nilainya
   "2% (dua persen) dari limit kredit". Bila sebuah sel hanya berisi label tanpa isi -> null.
   Setiap nilai biaya/bunga/limit WAJIB memuat angkanya; kalau angkanya tidak terbaca -> null.
4. Bagian **Simulasi/Ilustrasi/contoh perhitungan BUKAN ketentuan produk**. Jangan mengambil
   angka/tanggal dari sana sebagai nilai field mana pun.
5. Rumus matematika dirender sebagai GAMBAR dengan garis pecahan. Garis pecahan hanya mencakup
   suku yang berada TEPAT di atas/bawahnya — suku yang ditulis setelah tanda "+" DI LUAR garis
   pecahan TIDAK ikut dibagi. Telusuri panjang garis pecahan sebelum menulis ulang, lalu tulis
   sebagai "(pembilang / penyebut) + (suku di luar)". Salah menempatkan tanda kurung membuat
   rumusnya salah secara matematis.
5b. Ambil nilai HANYA dari baris/sel milik field itu sendiri. Baris tabel di sebelah atau di
   bawahnya membahas hal lain — jangan dipakai (mis. "Jenis Agunan" bukan keterangan tenor).
6. `nama_produk` diambil dari baris "Nama Produk" / judul produk utama, bukan nama penerbit.
7. Pertahankan format penulisan angka Indonesia (koma desimal), mis. "1,75%" bukan "1.75%".
8. Untuk `biaya_admin.tiering`, satu entri per baris tabel, mis.
   {{"rentang_pencairan": "<= Rp 20.000.000", "biaya": "Rp 150.000"}}.
9. `limit_pencairan.ketentuan` hanya diisi bila ada KALIMAT syarat/ketentuan pemberian limit.
   Label tabel seperti "Limit Kredit" BUKAN ketentuan -> null.
10. `tanggal_tagihan` hanya diisi bila dokumen menyatakan tanggal cetak tagihan / jatuh tempo
    sebagai KETENTUAN produk. Bila hanya muncul di contoh simulasi -> null.
11. `keterangan` diisi hanya untuk syarat tambahan yang BELUM termuat di field utamanya. Jangan
    mengulang isi field utama, dan jangan menyalin catatan kaki yang sama lebih dari satu kali.
12. Untuk `asuransi`, ambil dari bagian asuransi/proteksi yang menempel pada produk. Bila dokumen
    ini tidak membahas asuransi sama sekali -> semua sub-fieldnya null / array kosong.
13. Balas HANYA objek JSON, tanpa penjelasan, tanpa markdown fence."""


def extract_riplay(
    pdf_bytes: bytes,
    llm_client,
    model: str,
    max_pages: int = DEFAULT_MAX_PAGES,
    scale: float = DEFAULT_RENDER_SCALE,
    temperature: float = 1.0,
    max_retries: int = 2,
) -> dict:
    """Render the RIPLAY PDF to images, send them to the LLM, return parsed JSON.

    Raises ``ValueError`` if the model never returns a parseable JSON object.
    """
    views = pdf_to_llm_images(pdf_bytes, max_pages=max_pages, scale=scale)
    page_count = sum(1 for label, _ in views if "tampilan penuh" in label)

    content: list[dict] = [
        {
            "type": "text",
            "text": RIPLAY_PROMPT.format(
                schema=json.dumps(RIPLAY_SCHEMA, indent=2, ensure_ascii=False)
            ),
        }
    ]
    for label, png in views:
        content.append({"type": "text", "text": f"--- {label} ---"})
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")
                },
            }
        )

    last_error: Optional[Exception] = None
    for _ in range(max_retries + 1):
        response = llm_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            temperature=temperature,
        )
        try:
            extraction = _parse_llm_json_object(response.choices[0].message.content)
        except ValueError as exc:
            last_error = exc
            continue
        extraction["_pages"] = page_count
        return extraction

    raise ValueError(f"LLM tidak mengembalikan JSON RIPLAY yang valid: {last_error}")


# --- Product-name gate -------------------------------------------------------

# Words that carry no identity: document boilerplate, the legal entity, and the
# "Mega" brand every Bank Mega product shares. Dropped before comparing, so
# "Mega Cashline" vs "Mega Ultima Shield" can't score a match on the brand alone.
_GENERIC_TOKENS = {
    "pt", "tbk", "bank", "mega", "produk", "layanan", "riplay", "ringkasan",
    "informasi", "dan", "the", "program", "fasilitas",
}


# Character-ratio floor below which the two names are treated as unrelated rather
# than partially similar (see product_name_similarity).
_SAME_STRING_RATIO = 0.7


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _tokens(value: str, drop_generic: bool = True) -> list[str]:
    parts = _normalize_name(value).split()
    if not drop_generic:
        return parts
    return [t for t in parts if t not in _GENERIC_TOKENS]


def product_name_similarity(riplay_name: str, campaign_name: str) -> float:
    """Similarity (0-100) between the RIPLAY product name and the campaign name.

    Primary signal: the share of the shorter token set that also appears in the
    longer one, matched fuzzily so typos survive. Either string may be the longer
    one, so campaign "Cashline" vs RIPLAY "Mega Cashline" scores 100 rather than
    being penalised for the extra word.

    A character-level ratio is used as a second view, but only once it is high
    enough to mean "the same string written differently" (``Cash Line`` vs
    ``Cashline``). Below that it just counts scattered shared letters — enough to
    push two unrelated products ("Cashline" vs "Ultima Shield") over the gate.
    """
    left, right = _tokens(riplay_name), _tokens(campaign_name)
    if not left or not right:
        # A name made up entirely of generic words (a campaign literally called
        # "Mega") would otherwise always score 0 — compare the raw words instead.
        left, right = _tokens(riplay_name, False), _tokens(campaign_name, False)
    if not left or not right:
        return 0.0

    short, long = (left, right) if len(left) <= len(right) else (right, left)
    covered = sum(
        1 for t in short if any(SequenceMatcher(None, t, o).ratio() >= 0.85 for o in long)
    )
    token_ratio = covered / len(short)

    char_ratio = SequenceMatcher(None, "".join(left), "".join(right)).ratio()
    if char_ratio < _SAME_STRING_RATIO:
        char_ratio = 0.0

    return round(max(char_ratio, token_ratio) * 100, 2)


# --- Lenient JSON for the hand-maintained KB ---------------------------------

def _repair_missing_commas(snippet: str) -> str:
    """Insert the commas the hand-edited KB blocks sometimes omit between entries."""
    return re.sub(r'(["\]\}])(\s*\n\s*)(")', r"\1,\2\3", snippet)


def _loads_lenient(snippet: str) -> Optional[dict]:
    for candidate in (snippet, _repair_missing_commas(snippet)):
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _loads_lenient_list(snippet: str) -> Optional[list]:
    for candidate in (snippet, _repair_missing_commas(snippet)):
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, list):
            return value
    return None


def _render(value, indent_level: int = 2) -> str:
    """Serialise a value in the KB's tab-indented style at ``indent_level`` tabs.

    Number lists stay on one line to match how the KB already writes them
    (``"tenor_cicilan": [12, 24, 36]``).
    """
    if isinstance(value, list) and value and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in value
    ):
        return json.dumps(value, ensure_ascii=False)

    text = json.dumps(value, indent="\t", ensure_ascii=False)
    pad = "\t" * indent_level
    lines = text.split("\n")
    out = [lines[0]]
    for line in lines[1:]:
        # A nested number list is emitted expanded by json.dumps; fold it back.
        out.append("\n" + pad + line)
    rendered = "".join(out)
    return re.sub(
        r"\[\s*((?:-?\d+(?:\.\d+)?\s*,\s*)*-?\d+(?:\.\d+)?)\s*\]",
        lambda m: "[" + ", ".join(p.strip() for p in m.group(1).split(",")) + "]",
        rendered,
    )


def _value_span(text: str, key: str, start: int = 0) -> Optional[tuple[int, int, int]]:
    """Locate ``"key": <object|array>`` and return (key_start, value_start, value_end).

    Brace/bracket matching is string-aware, which matters because the KB stores
    example phrases containing literal newlines and braces.
    """
    match = re.compile(r'"%s"\s*:\s*' % re.escape(key)).search(text, start)
    if not match:
        return None
    value_start = match.end()
    if value_start >= len(text) or text[value_start] not in "{[":
        return None

    opener = text[value_start]
    closer = {"{": "}", "[": "]"}[opener]
    depth = 0
    in_string = False
    escaped = False
    for i in range(value_start, len(text)):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return match.start(), value_start, i + 1
    return None


# --- Which KB entries overlap the RIPLAY -------------------------------------

@dataclass(frozen=True)
class _Aspect:
    """One product fact that appears both in the RIPLAY and in the KB.

    ``pinned`` names detail keys where the KB value stays authoritative even when
    the RIPLAY says something different, because the KB carries an operating rule
    the RIPLAY does not state. The RIPLAY wording is still recorded, under
    ``pinned_note_key``, so the divergence is visible rather than lost. Everything
    not listed here follows the RIPLAY.

    Currently pinned (decided 5 Aug 2026, see RIPLAY_KB_MAPPING.md):

    - ``suku_bunga_cicilan`` — the RIPLAY quotes a floor ("mulai dari 1,75%",
      risk-based pricing) with no ceiling; the KB's "1,75% - 2,2%" range is what
      catches an agent quoting 3%.
    - ``biaya_provisi`` — the RIPLAY says "dikenakan per tahun", but the KB (and
      its untouchable ``scoring_rule``) requires the mention to say "mulai tahun
      ke-2". Same 2%, so no number is in conflict.
    """

    key: str
    label: str
    kb_codes: tuple[str, ...]
    build_details: Callable[[dict], Optional[dict]]
    build_phrases: Callable[[dict, str, dict], list[str]]
    pinned: tuple[str, ...] = ()
    pinned_note_key: str = ""


def _get(extraction: dict, *path, default=None):
    node = extraction
    for part in path:
        if not isinstance(node, dict):
            return default
        node = node.get(part)
    if node is None or node == "" or node == []:
        return default
    return node


def _join_id(values: list) -> str:
    """'12, 24 dan 36' — how the agent says a list out loud."""
    items = [str(v) for v in values]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " dan " + items[-1]


# -- tenor (KB_CL_6 penjelasan, KB_CL_26 final konfirmasi)

def _details_tenor(e: dict) -> Optional[dict]:
    months = _get(e, "tenor_cicilan", "pilihan_bulan")
    if not isinstance(months, list) or not months:
        return None
    details: dict = {"tenor_cicilan": months}
    keterangan = _get(e, "tenor_cicilan", "keterangan")
    if keterangan:
        details["keterangan_tenor"] = keterangan
    return details


def _phrases_tenor(e: dict, kb_code: str, d: dict) -> list[str]:
    spoken = _join_id(_get(e, "tenor_cicilan", "pilihan_bulan", default=[]))
    if kb_code == "KB_CL_26":
        return [
            f"pengajuan mega cashline sebesar Rp xxx dengan tenor xx bulan (pilihan tenor: {spoken} bulan)",
            "untuk dana kita cairkan dengan nominal Rp xxx dengan tenor xx bulan",
        ]
    return [
        f"Tenor cicilan ada {spoken} bulan",
        f"Bapak/Ibu bisa ambil tenor {spoken} bulan",
        "dari limit yang tersedia mau ambil tenor yang mana?",
    ]


# -- bunga (KB_CL_7, KB_CL_27)

def _details_bunga(e: dict) -> Optional[dict]:
    cicilan = _get(e, "suku_bunga", "cicilan")
    revolving = _get(e, "suku_bunga", "revolving")
    if not cicilan and not revolving:
        return None
    details: dict = {}
    if cicilan:
        details["suku_bunga_cicilan"] = cicilan
    if revolving:
        details["suku_bunga_revolving"] = revolving
    keterangan = _get(e, "suku_bunga", "keterangan")
    if keterangan:
        details["keterangan_bunga"] = keterangan
    return details


def _phrases_bunga(e: dict, kb_code: str, d: dict) -> list[str]:
    # Read the value actually written to details — for bunga that is the pinned KB
    # range, not the RIPLAY floor. The KB requires the phrase to end in "effective
    # rate" (KB_CL_7 scoring_rule), so drop a trailing "(effective rate)"/"(Flat per
    # bulan)" style suffix to avoid saying it twice. details keeps it verbatim.
    cicilan = str(d.get("suku_bunga_cicilan") or "xx% per bulan")
    cicilan = re.sub(r"\s*\((?:[^)]*(?:effective|efektif)\s*rate[^)]*)\)\s*$", "", cicilan).strip()
    return [
        f"Bunganya {cicilan} dihitung berdasarkan effective rate",
        f"Dengan bunga {cicilan} berdasarkan efektif rate",
        "Bunga promonya di xx% per bulan dihitung berdasarkan effective rate",
    ]


# -- provisi (KB_CL_8, KB_CL_28)

def _details_provisi(e: dict) -> Optional[dict]:
    provisi = _get(e, "biaya_provisi")
    return {"biaya_provisi": provisi} if provisi else None


def _phrases_provisi(e: dict, kb_code: str, d: dict) -> list[str]:
    provisi = d.get("biaya_provisi") or ""
    return [f"biaya provisi Mega Cashline sebesar {provisi}"]


# -- nominal cicilan (KB_CL_9, KB_CL_29)

def _details_cicilan(e: dict) -> Optional[dict]:
    # Only the formula. `nominal_cicilan.keterangan` reliably picks up the header of
    # the simulation block ("Suku Bunga: 1,75% Fixed Installment"), which reads as a
    # fixed rate and would undercut the bunga range the KB pins.
    rumus = _get(e, "nominal_cicilan", "rumus")
    return {"cicilan_bulanan": rumus} if rumus else None


def _phrases_cicilan(e: dict, kb_code: str, d: dict) -> list[str]:
    if kb_code == "KB_CL_29":
        return [
            "Sehingga Cicilan per bulan untuk Dana Tunai Mega Cashline : Rp xxx",
            "untuk angsuran per bulannya Rp xxx",
        ]
    return [
        "Jika Bapak/Ibu memilih tenor xx bulan, maka nominal cicilan bulanan sebesar xx Rupiah",
        "Nominal cicilan yang dibayarkan sebesar xx Rupiah",
    ]


# -- limit pencairan (KB_CL_10, KB_CL_25)

def _details_limit(e: dict) -> Optional[dict]:
    minimum = _get(e, "limit_pencairan", "minimum")
    maksimum = _get(e, "limit_pencairan", "maksimum")
    if not minimum and not maksimum:
        return None
    details: dict = {}
    if minimum:
        details["limit_pencairan_minimal"] = minimum
    if maksimum:
        details["limit_pencairan_maksimal"] = maksimum
    return details


def _phrases_limit(e: dict, kb_code: str, d: dict) -> list[str]:
    minimum = _get(e, "limit_pencairan", "minimum", default="xx")
    maksimum = _get(e, "limit_pencairan", "maksimum", default="xx")
    if kb_code == "KB_CL_25":
        return [
            "untuk pengajuan mega cashline dan mega ultima shield dengan detail sebagai berikut : Mega cashline sebesar Rp xxx",
            "untuk dana kita cairkan sebesar Rp xxx",
        ]
    return [
        "Dana yang dapat kami cairkan yaitu sebesar Rp xxx (sebutkan nominal dana) dari limit yang tersedia",
        f"Dana tunai dengan limit {minimum} hingga {maksimum}",
    ]


# -- ketentuan limit (KB_CL_31)

def _details_ketentuan_limit(e: dict) -> Optional[dict]:
    ketentuan = _get(e, "limit_pencairan", "ketentuan")
    return {"ketentuan_limit_pencairan": ketentuan} if ketentuan else None


def _phrases_ketentuan_limit(e: dict, kb_code: str, d: dict) -> list[str]:
    ketentuan = _get(e, "limit_pencairan", "ketentuan", default="")
    return [
        "Diinformasikan kembali ya Pak/Bu bahwa limit terundang merupakan pengajuan, dan pemberian limit tergantung pada analis yang dapat bertambah atau berkurang.",
        f"Ketentuan limit: {ketentuan}",
    ]


# -- biaya admin (KB_CL_11, KB_CL_30)

def _details_admin(e: dict) -> Optional[dict]:
    tiering = _get(e, "biaya_admin", "tiering")
    keterangan = _get(e, "biaya_admin", "keterangan")
    if isinstance(tiering, list) and tiering:
        table = {}
        for row in tiering:
            if not isinstance(row, dict):
                continue
            rentang = row.get("rentang_pencairan")
            biaya = row.get("biaya")
            if rentang and biaya:
                table[str(rentang)] = str(biaya)
        if table:
            return {"biaya_admin": table}
    if keterangan:
        return {"biaya_admin": keterangan}
    return None


def _phrases_admin(e: dict, kb_code: str, d: dict) -> list[str]:
    if kb_code == "KB_CL_30":
        return [
            "Admin fee sekali di awal aja Rp xxx tidak memotong dana yang diterima",
            "biaya admin sesuai tiering Rp xxx",
        ]
    return [
        "Biaya Administrasi Mega Cashline sebesar xxx Rupiah (sesuai tiering)",
        "biaya admin sebesar xxx Rupiah",
        "dikenakan admin sebesar xxx Rupiah",
    ]


# -- pelunasan dipercepat (KB_CL_15, KB_CL_32)

def _details_pelunasan(e: dict) -> Optional[dict]:
    value = _get(e, "pelunasan_dipercepat")
    return {"biaya_pelunasan_dipercepat": value} if value else None


def _phrases_pelunasan(e: dict, kb_code: str, d: dict) -> list[str]:
    value = _get(e, "pelunasan_dipercepat", default="")
    return [
        f"Apabila Bapak/Ibu ingin melakukan pelunasan di awal sebelum berakhir tenor, dikenakan {value}",
        f"pelunasan dipercepat dikenakan {value}",
    ]


# -- tanggal cetak / jatuh tempo (KB_CL_16)

def _details_tagihan(e: dict) -> Optional[dict]:
    cetak = _get(e, "tanggal_tagihan", "tanggal_cetak")
    jatuh_tempo = _get(e, "tanggal_tagihan", "tanggal_jatuh_tempo")
    if not cetak and not jatuh_tempo:
        return None
    details: dict = {}
    if cetak:
        details["tanggal_cetak_tagihan"] = cetak
    if jatuh_tempo:
        details["tanggal_jatuh_tempo"] = jatuh_tempo
    return details


def _phrases_tagihan(e: dict, kb_code: str, d: dict) -> list[str]:
    cetak = _get(e, "tanggal_tagihan", "tanggal_cetak")
    jatuh_tempo = _get(e, "tanggal_tagihan", "tanggal_jatuh_tempo")
    phrases = []
    if jatuh_tempo:
        phrases.append(f"jatuh temponya {jatuh_tempo}")
        phrases.append(f"pembayaran maksimal {jatuh_tempo}")
    if cetak:
        phrases.append(f"tagihan tercetak {cetak}")
        phrases.append(f"tanggal cetak {cetak}")
    return phrases


# -- premi asuransi (KB_CL_17)

def _details_premi(e: dict) -> Optional[dict]:
    premi = _get(e, "asuransi", "premi")
    return {"premi": premi} if premi else None


def _phrases_premi(e: dict, kb_code: str, d: dict) -> list[str]:
    premi = _get(e, "asuransi", "premi", default="")
    return [f"Preminya relatif kecil, hanya {premi}", f"preminya {premi}"]


# -- benefit asuransi (KB_CL_21)

def _details_benefit(e: dict) -> Optional[dict]:
    manfaat = _get(e, "asuransi", "manfaat")
    if not isinstance(manfaat, list) or not manfaat:
        return None
    return {"benefit_asuransi": [str(m) for m in manfaat]}


def _phrases_benefit(e: dict, kb_code: str, d: dict) -> list[str]:
    manfaat = _get(e, "asuransi", "manfaat", default=[])
    return [str(m) for m in manfaat]


# -- informasi klaim (KB_CL_35)

def _details_klaim(e: dict) -> Optional[dict]:
    klaim = _get(e, "asuransi", "informasi_klaim")
    return {"informasi_klaim": klaim} if klaim else None


def _phrases_klaim(e: dict, kb_code: str, d: dict) -> list[str]:
    klaim = _get(e, "asuransi", "informasi_klaim", default="")
    return [
        "Sebagai penutup, terdapat beberapa informasi penting yang perlu kami sampaikan agar proses klaim dapat berjalan dengan baik, yaitu: "
        + str(klaim)
    ]


ASPECTS: tuple[_Aspect, ...] = (
    _Aspect("tenor", "Tenor cicilan", ("KB_CL_6", "KB_CL_26"), _details_tenor, _phrases_tenor),
    _Aspect(
        "bunga", "Suku bunga", ("KB_CL_7", "KB_CL_27"), _details_bunga, _phrases_bunga,
        pinned=("suku_bunga_cicilan",), pinned_note_key="keterangan_bunga",
    ),
    _Aspect(
        "provisi", "Biaya provisi", ("KB_CL_8", "KB_CL_28"), _details_provisi, _phrases_provisi,
        pinned=("biaya_provisi",), pinned_note_key="keterangan_provisi_riplay",
    ),
    _Aspect("cicilan", "Nominal cicilan", ("KB_CL_9", "KB_CL_29"), _details_cicilan, _phrases_cicilan),
    _Aspect("limit", "Limit pencairan", ("KB_CL_10", "KB_CL_25"), _details_limit, _phrases_limit),
    _Aspect("ketentuan_limit", "Ketentuan limit pencairan", ("KB_CL_31",), _details_ketentuan_limit, _phrases_ketentuan_limit),
    _Aspect("biaya_admin", "Biaya admin", ("KB_CL_11", "KB_CL_30"), _details_admin, _phrases_admin),
    _Aspect("pelunasan", "Pelunasan dipercepat", ("KB_CL_15", "KB_CL_32"), _details_pelunasan, _phrases_pelunasan),
    _Aspect("tagihan", "Tanggal cetak / jatuh tempo", ("KB_CL_16",), _details_tagihan, _phrases_tagihan),
    _Aspect("premi", "Premi asuransi", ("KB_CL_17",), _details_premi, _phrases_premi),
    _Aspect("benefit", "Benefit asuransi", ("KB_CL_21",), _details_benefit, _phrases_benefit),
    _Aspect("klaim", "Informasi klaim", ("KB_CL_35",), _details_klaim, _phrases_klaim),
)

#: kb_code -> aspect label, for the "which KB entries does RIPLAY govern" view.
RIPLAY_KB_COVERAGE: dict[str, str] = {
    code: aspect.label for aspect in ASPECTS for code in aspect.kb_codes
}


# --- TnC Product reference (cashline_data_verification) -----------------------

#: The fields compared in ``cashline_data_verification``, in output order.
TNC_PRODUCT_FIELDS: tuple[str, ...] = (
    "nominal_pencairan",
    "nama_bank",
    "tenor_dalam_bulan",
    "nominal_cicilan_per_bulan",
    "biaya_admin",
    "nomor_rekening",
    "nama_pemilik_rekening",
    "bunga",
    "provisi",
    "penalti_pelunasan_dipercepat",
)

#: Fields the RIPLAY says nothing about — they describe this customer's own
#: disbursement, not the product, so TMS stays their only ground truth.
TNC_TMS_ONLY_FIELDS: frozenset = frozenset(
    {"nama_bank", "nomor_rekening", "nama_pemilik_rekening"}
)


def _num(value) -> Optional[float]:
    """Parse an amount/percentage into a float, tolerating both notations.

    Reference data mixes conventions: TMS/RIPLAY amounts are Indonesian
    ("Rp 2.000.000", "1,75%") while ``compute_bunga`` emits a dot decimal
    ("2.09%"). A lone dot is read as a decimal point unless it separates a group
    of exactly three digits, which makes it a thousands separator.
    """
    if value is None:
        return None
    text = re.sub(r"[^\d.,-]", "", str(value))
    if not text:
        return None
    if "," in text:
        # Indonesian: dots group thousands, the comma is the decimal point.
        text = text.replace(".", "").replace(",", ".")
    elif "." in text:
        if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", text):
            text = text.replace(".", "")
    try:
        return float(text)
    except ValueError:
        return None


def _rupiah(value: float) -> str:
    return "Rp " + f"{round(value):,}".replace(",", ".")


def _percent_of(text) -> Optional[float]:
    """First percentage figure in a string: '2% (dua persen) dari ...' -> 2.0."""
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", str(text or ""))
    return _num(match.group(1)) if match else None


def _instantiate_installment(rumus: Optional[str], cashline_ref: Optional[dict]) -> Optional[str]:
    """Append the worked arithmetic to the RIPLAY instalment formula.

    "(Pokok / Tenor) + (Pokok x bunga)" becomes
    "... = (Rp 12.000.000 / 12) + (Rp 12.000.000 x 2,09%) = Rp 1.000.000 + Rp 250.800
    = Rp 1.250.800", so the row shows where the monthly figure comes from.
    """
    if not rumus:
        return None
    if not cashline_ref:
        return rumus

    pokok = _num(cashline_ref.get("nominal_pencairan"))
    tenor = _num(cashline_ref.get("tenor_dalam_bulan"))
    bunga = _percent_of(cashline_ref.get("bunga"))
    if not pokok or not tenor or bunga is None:
        return rumus

    pokok_part = pokok / tenor
    bunga_part = pokok * bunga / 100
    bunga_txt = f"{bunga}".rstrip("0").rstrip(".").replace(".", ",")
    return (
        f"{rumus} = ({_rupiah(pokok)} / {round(tenor)}) + ({_rupiah(pokok)} x {bunga_txt}%)"
        f" = {_rupiah(pokok_part)} + {_rupiah(bunga_part)} = {_rupiah(pokok_part + bunga_part)}"
    )


def build_tnc_product_reference(
    extraction: Optional[dict], cashline_ref: Optional[dict] = None
) -> dict:
    """Map a RIPLAY extraction onto the ``cashline_data_verification`` fields.

    Returns every field in ``TNC_PRODUCT_FIELDS``; a field the RIPLAY does not
    cover is ``None``. These are product-level terms (an envelope: a range, a set
    of allowed tenors, a fee table), not this ticket's values — TMS remains the
    per-ticket ground truth wherever it has a column.

    ``cashline_ref`` (this ticket's TMS values) is used to instantiate the
    instalment formula with real numbers, so the row shows the arithmetic and its
    result rather than an abstract formula.
    """
    fields: dict = {name: None for name in TNC_PRODUCT_FIELDS}
    if not extraction:
        return fields

    minimum = _get(extraction, "limit_pencairan", "minimum")
    maksimum = _get(extraction, "limit_pencairan", "maksimum")
    if minimum and maksimum:
        fields["nominal_pencairan"] = f"{minimum} - {maksimum}"
    elif minimum or maksimum:
        fields["nominal_pencairan"] = minimum or maksimum

    months = _get(extraction, "tenor_cicilan", "pilihan_bulan")
    if isinstance(months, list) and months:
        fields["tenor_dalam_bulan"] = " / ".join(str(m) for m in months) + " bulan"

    rumus = _get(extraction, "nominal_cicilan", "rumus")
    fields["nominal_cicilan_per_bulan"] = _instantiate_installment(rumus, cashline_ref)
    fields["bunga"] = _get(extraction, "suku_bunga", "cicilan")
    fields["provisi"] = _get(extraction, "biaya_provisi")
    fields["penalti_pelunasan_dipercepat"] = _get(extraction, "pelunasan_dipercepat")

    # The admin fee is tiered by disbursement amount; hand over the whole table so
    # the tier matching this ticket's nominal can be picked at evaluation time.
    tiering = _get(extraction, "biaya_admin", "tiering")
    if isinstance(tiering, list) and tiering:
        rows = [
            f"{row.get('rentang_pencairan')}: {row.get('biaya')}"
            for row in tiering
            if isinstance(row, dict) and row.get("rentang_pencairan") and row.get("biaya")
        ]
        if rows:
            fields["biaya_admin"] = "; ".join(rows)
    if not fields["biaya_admin"]:
        fields["biaya_admin"] = _get(extraction, "biaya_admin", "keterangan")

    return fields


# --- TMS vs TnC Product: agent data-entry check ------------------------------

#: Derived from the other TMS columns (``compute_bunga`` inverts the instalment
#: formula), so checking it against that same formula is a tautology — excluded
#: from the divergence check while still shown with its worked arithmetic.
_DERIVED_FIELDS: frozenset = frozenset({"nominal_cicilan_per_bulan"})


def _tier_fee_for(tnc_admin: str, pokok: Optional[float]) -> Optional[float]:
    """Fee from the tier table that covers ``pokok``.

    Tiers look like "<= 20.000.000: Rp 150.000; > 20.000.000 - <= 50.000.000:
    Rp 300.000; > 50.000.000: Rp 500.000".
    """
    if pokok is None:
        return None
    for part in str(tnc_admin).split(";"):
        if ":" not in part:
            continue
        bounds, fee = part.rsplit(":", 1)
        numbers = [_num(n) for n in re.findall(r"[\d.,]+", bounds)]
        numbers = [n for n in numbers if n is not None]
        if not numbers:
            continue
        has_lower = ">" in bounds
        has_upper = "<" in bounds
        if has_lower and has_upper and len(numbers) >= 2:
            ok = numbers[0] < pokok <= numbers[1]
        elif has_upper:
            ok = pokok <= numbers[0]
        elif has_lower:
            ok = pokok > numbers[0]
        else:
            continue
        if ok:
            return _num(fee)
    return None


def check_tms_against_tnc(cashline_ref: dict, tnc_ref: dict) -> dict:
    """Compare each TMS value against the product terms from the RIPLAY.

    A divergence means the value keyed into TMS is not one the product allows —
    i.e. the agent entered the deal wrongly, independent of what they said on the
    call. Returns ``{field: reason}`` for diverging fields only.

    Fields describing the customer's own account (``TNC_TMS_ONLY_FIELDS``) have no
    product term and are never checked, and neither are derived fields.
    """
    findings: dict = {}
    if not cashline_ref or not tnc_ref:
        return findings

    pokok = _num(cashline_ref.get("nominal_pencairan"))

    for field in TNC_PRODUCT_FIELDS:
        if field in TNC_TMS_ONLY_FIELDS or field in _DERIVED_FIELDS:
            continue
        tms, tnc = cashline_ref.get(field), tnc_ref.get(field)
        if tms in (None, "") or tnc in (None, ""):
            continue
        tms_text, tnc_text = str(tms), str(tnc)

        if field == "nominal_pencairan":
            bounds = [_num(n) for n in re.findall(r"[\d.][\d.,]*", tnc_text)]
            bounds = [b for b in bounds if b is not None]
            value = _num(tms_text)
            if value is not None and len(bounds) >= 2 and not (bounds[0] <= value <= bounds[1]):
                findings[field] = (
                    f"Nominal pencairan pada TMS {_rupiah(value)} berada di luar limit produk "
                    f"pada TnC Product ({tnc_text})"
                )

        elif field == "tenor_dalam_bulan":
            allowed = {int(n) for n in re.findall(r"\d+", tnc_text)}
            value = _num(tms_text)
            if value is not None and allowed and int(value) not in allowed:
                findings[field] = (
                    f"Tenor pada TMS {int(value)} bulan tidak termasuk pilihan tenor produk "
                    f"pada TnC Product ({tnc_text})"
                )

        elif field == "biaya_admin":
            expected = _tier_fee_for(tnc_text, pokok)
            value = _num(tms_text)
            if expected is not None and value is not None and abs(expected - value) > 0.5:
                findings[field] = (
                    f"Biaya admin pada TMS {_rupiah(value)} tidak sesuai tier TnC Product untuk "
                    f"pencairan {_rupiah(pokok)} (seharusnya {_rupiah(expected)})"
                )

        elif field == "bunga":
            floor = _percent_of(tnc_text)
            value = _percent_of(tms_text)
            # The RIPLAY quotes a floor ("mulai dari 1,75%") under risk-based
            # pricing, so only a rate BELOW it is impossible for the product.
            if floor is not None and value is not None and value < floor - 0.001:
                findings[field] = (
                    f"Bunga pada TMS {tms_text} lebih rendah dari bunga minimum produk pada "
                    f"TnC Product ({tnc_text})"
                )

        else:  # provisi, penalti_pelunasan_dipercepat — compare the percentage
            expected, value = _percent_of(tnc_text), _percent_of(tms_text)
            if expected is not None and value is not None and abs(expected - value) > 0.001:
                label = "Provisi" if field == "provisi" else "Penalti pelunasan dipercepat"
                findings[field] = (
                    f"{label} pada TMS {tms_text} berbeda dari TnC Product ({tnc_text})"
                )

    return findings


# --- Overlay -----------------------------------------------------------------

def _kb_block_bounds(kb_text: str) -> dict[str, tuple[int, int]]:
    """Map each ``kb_code`` to the [start, end) slice of text that describes it.

    A block runs from its own ``"kb_code"`` key up to the next one (or EOF), which
    is enough to scope a key lookup without needing the file to be valid JSON.
    """
    matches = list(re.finditer(r'"kb_code"\s*:\s*"([^"]+)"', kb_text))
    bounds: dict[str, tuple[int, int]] = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(kb_text)
        bounds[match.group(1)] = (match.start(), end)
    return bounds


def _has_digit(text: str) -> bool:
    return any(char.isdigit() for char in text)


def _merge_details(old: Optional[dict], new: dict) -> dict:
    """RIPLAY values win; keys the RIPLAY says nothing about are preserved."""
    if not old:
        return dict(new)
    merged = dict(old)
    merged.update(new)
    return merged


def _merge_phrases(old: Optional[list], generated: list[str]) -> list[str]:
    """RIPLAY-derived phrases first, then the hand-written ones that can't be stale.

    A hand-written phrase that carries a number may be quoting a superseded value
    (e.g. "provisi 2%" after the RIPLAY moved to 3%), so it is dropped; purely
    templated phrases ("biaya admin sebesar xxx Rupiah") are kept.
    """
    result: list[str] = []
    seen: set[str] = set()

    def add(phrase, *, skip_numeric: bool) -> None:
        text = str(phrase).strip()
        if not text or (skip_numeric and _has_digit(text)):
            return
        # Dedupe on a normalised key so spacing/case slips ("Pak/Bu" vs "Pak/ Bu")
        # don't leave two copies of the same sentence.
        key = re.sub(r"\s+", " ", text.lower()).strip()
        if key in seen:
            return
        seen.add(key)
        result.append(text)

    for phrase in generated:
        add(phrase, skip_numeric=False)
    for phrase in old or []:
        add(phrase, skip_numeric=True)
    return result


def _existing_detail(kb_text: str, bounds: dict, kb_codes, key: str):
    """First value the KB currently gives ``key`` across an aspect's entries.

    An aspect writes the same details to every entry it owns, but only some of
    them have a details block today (the Final Konfirmasi twins often have none),
    so the pinned value is resolved once per aspect rather than per entry.
    """
    for kb_code in kb_codes:
        if kb_code not in bounds:
            continue
        start, end = bounds[kb_code]
        span = _value_span(kb_text[start:end], "details")
        if not span:
            continue
        details = _loads_lenient(kb_text[start:end][span[1]:span[2]])
        if details and details.get(key):
            return details[key]
    return None


def _apply_pins(aspect, new_details: dict, kb_text: str, bounds: dict, extraction: dict) -> dict:
    """Let the KB keep ``aspect.pinned`` keys, recording the RIPLAY wording instead.

    Used where the RIPLAY is authoritative but less specific than the operating
    rule the KB encodes — see the ``_Aspect`` docstring. When the KB has no value
    to pin, or the two already agree, the RIPLAY value is used as normal.
    """
    if not aspect.pinned:
        return new_details

    result = dict(new_details)
    notes: list[str] = []
    for key in aspect.pinned:
        riplay_value = result.get(key)
        kb_value = _existing_detail(kb_text, bounds, aspect.kb_codes, key)
        if not riplay_value or not kb_value:
            continue
        if str(riplay_value).strip() == str(kb_value).strip():
            continue
        result[key] = kb_value
        notes.append(str(riplay_value).strip())

    if notes:
        tanggal = _get(extraction, "tanggal_berlaku")
        prefix = f"RIPLAY {tanggal}: " if tanggal else "RIPLAY: "
        existing_note = result.get(aspect.pinned_note_key)
        if existing_note:
            notes.append(str(existing_note).strip())
        result[aspect.pinned_note_key] = prefix + " ".join(notes)
    return result


def apply_riplay_to_kb(kb_text: str, extraction: dict) -> tuple[str, list[dict]]:
    """Overlay a RIPLAY extraction onto ``kb_text``.

    Returns ``(new_kb_text, applied)`` where ``applied`` is one audit record per
    KB entry that actually changed::

        {"kb_code", "aspect", "details_before", "details_after", "phrases_before",
         "phrases_after"}

    Only the ``details`` and ``example_phrases`` of existing entries are touched;
    entries whose values already agree with the RIPLAY are left untouched, so
    re-uploading an unchanged RIPLAY is a no-op.
    """
    if not extraction:
        return kb_text, []

    bounds = _kb_block_bounds(kb_text)
    edits: list[tuple[int, int, str]] = []  # (start, end, replacement) in kb_text
    applied: list[dict] = []

    for aspect in ASPECTS:
        new_details = aspect.build_details(extraction)
        if not new_details:
            continue

        new_details = _apply_pins(aspect, new_details, kb_text, bounds, extraction)

        for kb_code in aspect.kb_codes:
            if kb_code not in bounds:
                continue
            block_start, block_end = bounds[kb_code]
            block = kb_text[block_start:block_end]

            details_span = _value_span(block, "details")
            phrases_span = _value_span(block, "example_phrases")
            if phrases_span is None:
                continue  # every real entry has one; skip anything malformed

            old_details = (
                _loads_lenient(block[details_span[1]:details_span[2]]) if details_span else None
            )
            old_phrases = _loads_lenient_list(block[phrases_span[1]:phrases_span[2]]) or []

            merged_details = _merge_details(old_details, new_details)
            if old_details == merged_details:
                continue  # KB already matches the RIPLAY — leave it byte-identical

            merged_phrases = _merge_phrases(
                old_phrases, aspect.build_phrases(extraction, kb_code, merged_details)
            )

            block_edits: list[tuple[int, int, str]] = [
                (phrases_span[1], phrases_span[2], _render(merged_phrases))
            ]
            if details_span:
                block_edits.append((details_span[1], details_span[2], _render(merged_details)))
            else:
                # No details block yet: insert one just before example_phrases.
                block_edits.append(
                    (
                        phrases_span[0],
                        phrases_span[0],
                        '"details": ' + _render(merged_details) + ",\n\t\t",
                    )
                )

            for start, end, replacement in block_edits:
                edits.append((block_start + start, block_start + end, replacement))

            applied.append(
                {
                    "kb_code": kb_code,
                    "aspect": aspect.label,
                    "details_before": old_details,
                    "details_after": merged_details,
                    "phrases_before": old_phrases,
                    "phrases_after": merged_phrases,
                }
            )

    if not edits:
        return kb_text, []

    result = kb_text
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        result = result[:start] + replacement + result[end:]
    return result, applied
