"""OCR + verification prompt for NPWP (Nomor Pokok Wajib Pajak).

Verifies the NPWP number read from the document against ``nomor_npwp_acuan``
(bank reference: column ``no-npwp-new`` of the cashline CSV, by ``result_id``).
"""
from prompt._common import REQUIRED, fmt_acuan, make_props, make_schema

DOC_LABEL = "NPWP"

# Reference ("acuan") keys this document type expects from build_document_reference.
ACUAN_KEYS = ["nomor_npwp_acuan"]

# Pure-digit identifier: normalised to digits-only after the OCR, with ``match``
# recomputed from the digits (see ``prompt._common.normalize_numeric_row``).
NUMERIC_FIELDS = ["Nomor NPWP"]

PROPS = make_props()
SCHEMA = make_schema("npwp_verification")

PROMPT = (
    "Anda adalah sistem OCR + verifikasi dokumen perpajakan Indonesia. "
    "Anda menerima sebuah dokumen NPWP (Nomor Pokok Wajib Pajak).\n\n"
    "TUGAS:\n"
    "1. Baca nomor NPWP yang tertera pada dokumen. Jangan menebak digit yang tidak "
    "terbaca.\n"
    "   Tulis hasilnya HANYA berupa angka, tanpa titik, strip, atau spasi "
    "(contoh: '07.056.338.2-036.000' ditulis '070563382036000'), agar formatnya "
    "sama dengan data bank.\n"
    "2. Bandingkan dengan nilai ACUAN dari data bank berikut:\n"
    "   - Nomor NPWP acuan: {nomor_npwp_acuan}\n\n"
    "ATURAN PERBANDINGAN:\n"
    "- Dua nomor dianggap COCOK (match=true) jika digit-digitnya sama, meskipun "
    "format penulisan (titik, strip, atau spasi) berbeda.\n"
    "- similarity = tingkat kemiripan 0-100.\n"
    "- Jika nomor pada dokumen tidak terbaca/tidak ada: document=null, match=false.\n"
    "- Jika acuan tidak tersedia: tetap baca dokumen, set match=false dan jelaskan di reason.\n\n"
    "OUTPUT: satu objek JSON dengan key 'verifications' berisi TEPAT 1 baris dengan "
    "field='Nomor NPWP', beserta acuan, document, similarity, match, reason."
)


def build_prompt(reference: dict) -> str:
    """Return the final prompt with the NPWP reference value injected."""
    reference = reference or {}
    return PROMPT.format(nomor_npwp_acuan=fmt_acuan(reference.get("nomor_npwp_acuan")))
