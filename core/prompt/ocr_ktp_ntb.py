"""OCR + verification prompt for KTP pada campaign NTB.

Berbeda dari ``ocr_ktp`` (Cashline: nama, NIK, alamat), NTB membandingkan DUA field
dengan data TMS ``tms_ntb``:
  - ``NIK``             <- kolom ``no-ktpkitas``
  - ``Kewarganegaraan`` <- kolom ``kewarganegaraan`` (WNI/WNA)

Tanggal lahir TIDAK dibandingkan; ia dibaca apa adanya (``tanggal_lahir``, YYYY-MM-DD)
untuk aturan usia NTB: di bawah 21 atau di atas 65 tahun = tiket ditolak
(``compliance.ntb_ktp``). Bentuk tabelnya sama dengan KTP Cashline.
"""
from prompt._common import REQUIRED, DOC_KIND_KEY, fmt_acuan, make_props, make_schema

DOC_LABEL = "KTP"

ACUAN_KEYS = ["nik_acuan", "kewarganegaraan_acuan"]

NUMERIC_FIELDS = ["NIK"]

BIRTH_KEY = "tanggal_lahir"

NAMA_KEY = "nama_ktp"

# Field KTP lain yang dibaca apa adanya (tanpa acuan) untuk voting NTB Data Verification
# (Microsite = KTP): key -> deskripsi.
EXTRA_KEYS = {
    "tempat_lahir": "Tempat lahir pada KTP (kota/kabupaten saja, tanpa tanggal)",
    "jenis_kelamin": "Jenis kelamin pada KTP: LAKI-LAKI atau PEREMPUAN",
    "alamat_ktp": "Alamat pada KTP (jalan/dusun, tanpa RT/RW, kelurahan, kecamatan)",
    "rt_ktp": "RT pada KTP (angka saja)",
    "rw_ktp": "RW pada KTP (angka saja)",
    "kelurahan_ktp": "Kel/Desa pada KTP",
    "kecamatan_ktp": "Kecamatan pada KTP",
    "status_perkawinan_ktp": "Status perkawinan pada KTP, apa adanya (BELUM KAWIN, KAWIN, CERAI HIDUP, CERAI MATI)",
    "pekerjaan_ktp": "Pekerjaan pada KTP, apa adanya",
}

PROPS = make_props()
PROPS[NAMA_KEY] = {
    "type": ["string", "null"],
    "description": "Nama lengkap pada KTP, disalin apa adanya (null bila tidak terbaca)",
}
PROPS[BIRTH_KEY] = {
    "type": ["string", "null"],
    "description": "Tanggal lahir pada KTP, format YYYY-MM-DD (null bila tidak terbaca)",
}
for _k, _d in EXTRA_KEYS.items():
    PROPS[_k] = {"type": ["string", "null"], "description": _d + " (null bila tidak terbaca)"}
SCHEMA = make_schema("ktp_ntb_verification")
SCHEMA["json_schema"]["schema"]["properties"] = PROPS
SCHEMA["json_schema"]["schema"]["required"] = REQUIRED + [DOC_KIND_KEY, BIRTH_KEY, NAMA_KEY] + list(EXTRA_KEYS)

PROMPT = (
    "Anda adalah sistem OCR + verifikasi dokumen identitas Indonesia. "
    "Anda menerima sebuah dokumen KTP (Kartu Tanda Penduduk).\n\n"
    "TUGAS: baca field berikut pada KTP (salin apa adanya, jangan menebak) lalu "
    "bandingkan dengan nilai ACUAN dari data bank:\n"
    "1. NIK             — acuan: {nik_acuan}\n"
    "2. Kewarganegaraan — acuan: {kewarganegaraan_acuan}\n"
    "Selain itu baca TANGGAL LAHIR pada KTP dan tulis pada field 'tanggal_lahir' dengan "
    "format YYYY-MM-DD (tanggal lahir TIDAK dibandingkan dengan acuan apa pun). "
    "Baca juga NAMA LENGKAP pada KTP, salin apa adanya (tanpa gelar yang tidak tercetak, tanpa menebak), "
    "dan tulis pada field 'nama_ktp' (nama TIDAK dibandingkan di sini; bukan salah satu baris verifications). "
    "Baca juga field KTP berikut, salin apa adanya tanpa menebak (null bila tidak terbaca; tidak dibandingkan di sini): "
    "tempat_lahir (kota saja), jenis_kelamin, alamat_ktp (jalan/dusun saja), rt_ktp, rw_ktp (angka saja), "
    "kelurahan_ktp (Kel/Desa), kecamatan_ktp, status_perkawinan_ktp, pekerjaan_ktp.\n\n"
    "ATURAN PERBANDINGAN:\n"
    "- NIK COCOK jika 16 digitnya sama persis (abaikan spasi/pemisah).\n"
    "- Kewarganegaraan COCOK jika sama (WNI = Warga Negara Indonesia, WNA = Warga Negara Asing); "
    "KTP Indonesia yang tidak mencantumkan kewarganegaraan dibaca WNI.\n"
    "- similarity = tingkat kemiripan acuan vs dokumen, skala 0-100 untuk tiap field.\n"
    "- Jika sebuah field tidak terbaca/tidak ada pada dokumen: document=null, match=false.\n"
    "- Jika acuan tidak tersedia: tetap baca dokumen, set match=false dan jelaskan di reason.\n\n"
    "OUTPUT: satu objek JSON dengan key 'verifications' berisi TEPAT 2 baris, dengan "
    "field 'NIK' dan 'Kewarganegaraan', masing-masing beserta acuan, document, "
    "similarity, match, reason; ditambah 'tanggal_lahir', 'nama_ktp', dan field KTP lain di atas."
)


def build_prompt(reference: dict) -> str:
    """Return the final prompt with the KTP NTB reference values injected."""
    reference = reference or {}
    return PROMPT.format(
        nik_acuan=fmt_acuan(reference.get("nik_acuan")),
        kewarganegaraan_acuan=fmt_acuan(reference.get("kewarganegaraan_acuan")),
    )
