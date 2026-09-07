"""OCR + verification prompt for KTP (Kartu Tanda Penduduk).

Extracts and verifies three fields read from the document against bank reference
values (from the cashline CSV, by ``result_id``):
  - ``nama_acuan``          <- column ``cust_name``
  - ``nik_acuan``           <- column ``nik-new``
  - ``alamat_rumah_acuan``  <- home-address columns (``alamat-rumah-1-new``,
    ``rumah-rt-new``, ``rumah-rw-new``, ``rumah-kelurahan-new``, ``rumah-kecamatan-new``,
    ``rumah-kabupatenkota-new``, ``rumah-provinsi-new``, ``rumah-kode-pos-new``) joined
    with a single space.
"""
from prompt._common import REQUIRED, fmt_acuan, make_props, make_schema

DOC_LABEL = "KTP"

ACUAN_KEYS = ["nama_acuan", "nik_acuan", "alamat_rumah_acuan"]

# Pure-digit identifier: normalised to digits-only after the OCR, with ``match``
# recomputed from the digits (see ``prompt._common.normalize_numeric_row``).
NUMERIC_FIELDS = ["NIK"]

PROPS = make_props()
SCHEMA = make_schema("ktp_verification")

PROMPT = (
    "Anda adalah sistem OCR + verifikasi dokumen identitas Indonesia. "
    "Anda menerima sebuah dokumen KTP (Kartu Tanda Penduduk).\n\n"
    "TUGAS: baca field berikut pada KTP (salin apa adanya, jangan menebak) lalu "
    "bandingkan masing-masing dengan nilai ACUAN dari data bank:\n"
    "1. Nama          — acuan: {nama_acuan}\n"
    "2. NIK           — acuan: {nik_acuan}\n"
    "3. Alamat Rumah  — gabungkan Alamat, RT/RW, Kel/Desa, Kecamatan, Kabupaten/Kota, "
    "Provinsi, dan Kode Pos bila tertera. Acuan: {alamat_rumah_acuan}\n\n"
    "ATURAN PERBANDINGAN:\n"
    "- Nama dianggap COCOK (match=true) jika sama meskipun huruf besar/kecil, gelar, "
    "singkatan, atau spasi berbeda.\n"
    "- NIK COCOK jika 16 digitnya sama persis (abaikan spasi/pemisah).\n"
    "- Alamat COCOK jika komponen utamanya sama (nama jalan, nomor, RT/RW, kelurahan, "
    "kecamatan, kota, provinsi, kode pos), meskipun urutan, singkatan (mis. "
    "'JL'/'JALAN', 'KEL'/'KELURAHAN'), tanda baca, atau huruf besar/kecil berbeda.\n"
    "- similarity = tingkat kemiripan acuan vs dokumen, skala 0-100 untuk tiap field.\n"
    "- Jika sebuah field tidak terbaca/tidak ada pada dokumen: document=null, match=false.\n"
    "- Jika acuan tidak tersedia: tetap baca dokumen, set match=false dan jelaskan di reason.\n\n"
    "OUTPUT: satu objek JSON dengan key 'verifications' berisi TEPAT 3 baris, dengan "
    "field 'Nama', 'NIK', dan 'Alamat Rumah', masing-masing beserta acuan, document, "
    "similarity, match, reason."
)


def build_prompt(reference: dict) -> str:
    """Return the final prompt with the KTP reference values injected."""
    reference = reference or {}
    return PROMPT.format(
        nama_acuan=fmt_acuan(reference.get("nama_acuan")),
        nik_acuan=fmt_acuan(reference.get("nik_acuan")),
        alamat_rumah_acuan=fmt_acuan(reference.get("alamat_rumah_acuan")),
    )
