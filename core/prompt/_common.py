"""Shared building blocks for the OCR + verification prompt modules.

The OCR endpoint is Mistral Document AI: it takes the PDF plus a strict JSON
``document_annotation_format`` (schema) and a ``document_annotation_prompt``, and
returns a single JSON object. We standardise every document type on the same
output shape — a ``verifications`` list with one row per field compared against
its reference ("acuan") value — so the dashboard can render a uniform table
(field | acuan | document | similarity | match | reason).
"""
import re

# Per-row schema: one verified field compared against its reference value.
ITEM_PROPS = {
    "field": {"type": "string", "description": "Nama field yang diverifikasi"},
    "acuan": {
        "type": ["string", "null"],
        "description": "Nilai acuan dari data bank (disalin dari instruksi)",
    },
    "document": {
        "type": ["string", "null"],
        "description": "Nilai yang terbaca dari dokumen, apa adanya (null bila tidak terbaca)",
    },
    "similarity": {
        "type": "integer",
        "description": "Tingkat kemiripan acuan vs dokumen, skala 0-100",
    },
    "match": {
        "type": "boolean",
        "description": "true bila nilai dokumen dianggap cocok dengan acuan",
    },
    "reason": {
        "type": "string",
        "description": "Alasan singkat mengapa match bernilai true atau false",
    },
}
ITEM_REQUIRED = ["field", "acuan", "document", "similarity", "match", "reason"]

REQUIRED = ["verifications"]


# Jenis dokumen yang BENAR-BENAR terbaca, terlepas dari slot mana ia diunggah.
# Tanpa ini, mengunggah KTP ke slot NPWP hanya menghasilkan ``document=null,
# match=false`` — tidak bisa dibedakan dari "NPWP-nya ada tetapi nomornya salah",
# padahal keduanya kesalahan yang berbeda (sheet QC: C03 vs B02/B03/B05).
# Nilainya dibatasi supaya bisa dibandingkan langsung dengan slot yang diminta;
# "LAINNYA" untuk dokumen di luar keempat jenis, "TIDAK_JELAS" bila memang tidak
# terbaca — yang kedua sengaja TIDAK dianggap salah jenis (lihat
# ``compliance.documents.wrong_document_type``).
DOC_KIND_VALUES = ["KTP", "KK", "NPWP", "COVER_BUKU_TABUNGAN", "LAINNYA", "TIDAK_JELAS"]
DOC_KIND_KEY = "jenis_dokumen"

DOC_KIND_PROP = {
    "type": "string",
    "enum": DOC_KIND_VALUES,
    "description": (
        "Jenis dokumen yang benar-benar terlihat pada berkas ini, apa adanya — "
        "JANGAN mengikuti jenis yang diminta pada instruksi."
    ),
}

# Ditempelkan ke setiap prompt OCR agar instruksinya seragam antar jenis dokumen.
DOC_KIND_INSTRUCTION = (
    "\n\nIDENTIFIKASI JENIS DOKUMEN (WAJIB):\n"
    "- Selain tugas di atas, tentukan jenis dokumen yang BENAR-BENAR terlihat pada "
    "berkas ini dan tulis pada field '" + DOC_KIND_KEY + "'.\n"
    "- Nilai yang boleh dipakai: " + ", ".join(DOC_KIND_VALUES) + ".\n"
    "- Nilai ini menggambarkan APA YANG ANDA LIHAT, bukan jenis yang diminta di atas. "
    "Bila berkasnya ternyata dokumen lain, sebutkan jenis aslinya (atau LAINNYA); "
    "jangan menyesuaikannya dengan permintaan.\n"
    "- Pakai TIDAK_JELAS hanya bila berkasnya tidak terbaca sama sekali.\n"
    "- Bila jenisnya bukan yang diminta, tetap isi verifications: document=null, "
    "match=false, dan jelaskan di reason bahwa dokumennya bukan jenis yang diminta."
)


def make_props() -> dict:
    """Top-level schema properties: a ``verifications`` array of comparison rows,
    plus the detected document kind."""
    return {
        "verifications": {
            "type": "array",
            "description": "Satu baris per field yang diverifikasi.",
            "items": {
                "type": "object",
                "properties": ITEM_PROPS,
                "required": ITEM_REQUIRED,
                "additionalProperties": False,
            },
        },
        DOC_KIND_KEY: DOC_KIND_PROP,
    }


def make_schema(name: str) -> dict:
    """Build the Mistral ``document_annotation_format`` (strict JSON schema)."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "schema": {
                "type": "object",
                "properties": make_props(),
                # ``strict`` menuntut setiap properti ada di ``required``; tanpa itu
                # jenis_dokumen boleh dihilangkan model dan deteksi C03 mati diam-diam.
                "required": REQUIRED + [DOC_KIND_KEY],
                "additionalProperties": False,
            },
            "strict": True,
        },
    }


def fmt_acuan(value) -> str:
    """Render a reference value for inclusion in the prompt text."""
    if value is None or str(value).strip() == "":
        return "(tidak tersedia)"
    return str(value).strip()


# --------------------------------------------------------------------------
# Numeric-identifier normalisation (NPWP / NIK / nomor rekening / ...).
# --------------------------------------------------------------------------
# The bank side (TMS/Ascend CSV) stores these identifiers as bare digits
# ("070563382036000"), but the document prints them formatted
# ("07.056.338.2-036.000") and the OCR copies that formatting verbatim — so the
# stored ``document`` value never lines up with the acuan column it is compared
# against, and the dashboard shows two strings that look unrelated even when the
# digits are identical. The prompt already tells the model to ignore separators
# when deciding ``match``, but that is a request, not a guarantee.
#
# So for the fields listed in a prompt module's ``NUMERIC_FIELDS`` we strip every
# non-digit from BOTH sides after the OCR returns, and — since a pure-digit
# identifier admits exactly one comparison — decide ``match`` in code instead of
# trusting the model's. Applied by ``compliance.documents.normalize_ocr_json``.

_NON_DIGIT = re.compile(r"\D+")


def digits_only(value) -> str | None:
    """``"07.056.338.2-036.000" -> "070563382036000"``.

    Returns ``None`` when ``value`` is empty or carries no digit at all (e.g.
    "tidak terbaca"), so callers can keep the original text in that case.
    """
    if value is None:
        return None
    return _NON_DIGIT.sub("", str(value)) or None


def normalize_numeric_row(row: dict) -> dict:
    """Digits-only ``acuan``/``document`` for one verification row, with ``match``
    recomputed from the normalised digits.

    A side without any digit is left untouched (nothing to normalise, and the
    text may explain why the field is missing); ``match`` is only recomputed when
    BOTH sides normalised to digits.
    """
    out = dict(row)
    acuan = digits_only(row.get("acuan"))
    document = digits_only(row.get("document"))
    if acuan is not None:
        out["acuan"] = acuan
    if document is not None:
        out["document"] = document
    if acuan is None or document is None:
        return out

    match = acuan == document
    if match:
        out["match"] = True
        out["similarity"] = 100
        if not row.get("match"):
            out["reason"] = (
                "Digit nomor pada dokumen sama dengan acuan setelah pemisah "
                "(titik/strip/spasi) diabaikan."
            )
    else:
        out["match"] = False
        # A mismatch cannot be 100% similar; keep the model's figure otherwise.
        similarity = row.get("similarity")
        if not isinstance(similarity, int) or isinstance(similarity, bool):
            out["similarity"] = 0
        elif similarity >= 100:
            out["similarity"] = 99
        if row.get("match"):
            out["reason"] = "Digit nomor pada dokumen berbeda dengan acuan."
    return out
