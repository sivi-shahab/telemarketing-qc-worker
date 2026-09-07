"""OCR + verification prompt for Cover Buku Tabungan (savings book cover).

Verifies three fields read from the document against bank reference values
(all from the cashline CSV, by ``result_id``):
  - ``nama_bank_acuan``            <- column ``nama-bank``
  - ``nama_pemilik_rekening_acuan``<- column ``nama-di-rekening``
  - ``nomor_rekening_acuan``       <- column ``nomor-rekening``
"""
from prompt._common import REQUIRED, fmt_acuan, make_props, make_schema

DOC_LABEL = "Cover Buku Tabungan"

ACUAN_KEYS = [
    "nama_bank_acuan",
    "nama_pemilik_rekening_acuan",
    "nomor_rekening_acuan",
]

# Pure-digit identifier: normalised to digits-only after the OCR, with ``match``
# recomputed from the digits (see ``prompt._common.normalize_numeric_row``).
NUMERIC_FIELDS = ["Nomor Rekening"]

PROPS = make_props()
SCHEMA = make_schema("cover_buku_tabungan_verification")

PROMPT = (
    "Anda adalah sistem OCR + verifikasi dokumen perbankan Indonesia. "
    "Anda menerima cover/halaman depan Buku Tabungan.\n\n"
    "TUGAS: baca field berikut pada dokumen (salin apa adanya, jangan menebak) lalu "
    "bandingkan masing-masing dengan nilai ACUAN dari data bank:\n"
    "1. Nama Bank        — acuan: {nama_bank_acuan}\n"
    "2. Nama Pemilik Rekening — acuan: {nama_pemilik_rekening_acuan}\n"
    "3. Nomor Rekening   — acuan: {nomor_rekening_acuan}\n\n"
    "ATURAN PERBANDINGAN:\n"
    "- Nama bank/pemilik dianggap COCOK (match=true) jika sama meskipun huruf "
    "besar/kecil, singkatan, atau spasi berbeda; nomor rekening COCOK jika digitnya "
    "sama meskipun pemisah berbeda.\n"
    "- similarity = tingkat kemiripan 0-100 untuk tiap field.\n"
    "- Jika sebuah field tidak terbaca/tidak ada: document=null, match=false.\n"
    "- Jika acuan tidak tersedia: tetap baca dokumen, set match=false dan jelaskan di reason.\n\n"
    "OUTPUT: satu objek JSON dengan key 'verifications' berisi TEPAT 3 baris, "
    "dengan field 'Nama Bank', 'Nama Pemilik Rekening', dan 'Nomor Rekening', "
    "masing-masing beserta acuan, document, similarity, match, reason."
)


def build_prompt(reference: dict) -> str:
    """Return the final prompt with the cover-buku-tabungan reference values injected."""
    reference = reference or {}
    return PROMPT.format(
        nama_bank_acuan=fmt_acuan(reference.get("nama_bank_acuan")),
        nama_pemilik_rekening_acuan=fmt_acuan(reference.get("nama_pemilik_rekening_acuan")),
        nomor_rekening_acuan=fmt_acuan(reference.get("nomor_rekening_acuan")),
    )
