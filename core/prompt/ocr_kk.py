"""OCR + verification prompt for KK (Kartu Keluarga).

Verifies the mother's maiden name read from the document against
``nama_ibu_kandung_acuan`` (bank reference: column ``CUST_MOM_NAME`` of the
ascend/custp CSV, matched by ``CUST_LOCAL_NAME`` == the cashline ``cust_name``).
"""
from prompt._common import REQUIRED, fmt_acuan, make_props, make_schema

DOC_LABEL = "KK"

ACUAN_KEYS = ["nama_ibu_kandung_acuan", "nama_anak"]

PROPS = make_props()
SCHEMA = make_schema("kk_verification")

PROMPT = (
    "Anda adalah sistem OCR + verifikasi dokumen identitas Indonesia. "
    "Anda menerima sebuah dokumen KK (Kartu Keluarga).\n\n"
    "TUGAS:\n"
    "1. Baca NAMA IBU KANDUNG dari {nama_anak} pada dokumen. Pada KK, cari anggota "
    "keluarga bernama {nama_anak}, lalu ambil NAMA IBU KANDUNG-nya (kolom 'Nama Ibu' "
    "untuk anggota tersebut, atau nama anggota keluarga berstatus hubungan 'Ibu'/"
    "'Istri' jika {nama_anak} adalah anak). Salin apa adanya, jangan menebak.\n"
    "2. Bandingkan dengan nilai ACUAN dari data bank berikut:\n"
    "   - Nama ibu kandung acuan: {nama_ibu_kandung_acuan}\n\n"
    "ATURAN PERBANDINGAN:\n"
    "- Nama dianggap COCOK (match=true) jika sama, meskipun huruf besar/kecil, "
    "gelar, atau spasi berbeda.\n"
    "- similarity = tingkat kemiripan 0-100.\n"
    "- Jika nama pada dokumen tidak terbaca/tidak ada: document=null, match=false.\n"
    "- Jika acuan tidak tersedia: tetap baca dokumen, set match=false dan jelaskan di reason.\n\n"
    "OUTPUT: satu objek JSON dengan key 'verifications' berisi TEPAT 1 baris dengan "
    "field='Nama Ibu Kandung', beserta acuan, document, similarity, match, reason."
)


def build_prompt(reference: dict) -> str:
    """Return the final prompt with the KK reference value injected."""
    reference = reference or {}
    return PROMPT.format(
        nama_anak=fmt_acuan(reference.get("nama_anak")),
        nama_ibu_kandung_acuan=fmt_acuan(reference.get("nama_ibu_kandung_acuan")),
    )
