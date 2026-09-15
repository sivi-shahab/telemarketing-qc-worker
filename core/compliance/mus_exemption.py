"""Daftar pengecualian Mega Ultima Shield (MUS) dari Bank Mega.

Aturan Bank Mega (8 September 2026): sebuah rekaman baru dianggap VALID bila nasabah
tertarik pada Mega Cashline **dan** Mega Ultima Shield. Tertarik Mega Cashline saja
TIDAK valid — kecuali ada alasan yang membuat MUS memang tidak bisa diajukan.

Status pengecualian punya SATU sumber penentu:

1. **Transkrip** — satu-satunya sumber yang menentukan status akhir. Terbaca oleh LLM
   dan ditulis ke ``evaluation["mus_exemption"]``. Dua kategori lahir di sini, keduanya
   dari pernyataan kesehatan yang dibacakan agent: ``SAKIT`` dan ``HAMIL``. Contoh yang
   jadi acuan, tiket ``030808fLO1``: agent membacakan pernyataan kesehatan, nasabah
   menjawab "Iya, saya dalam perawatan ... perawatan jantung" lalu "Ginjal juga ada",
   dan agent menutup di rekaman perbaikan dengan "Mega Ultima Shield-nya tidak kita
   proseskan ya Pak karena ada riwayat tadi jantung dan juga ginjalnya". Pola itu —
   pernyataan kesehatan dijawab "tidak benar" lalu agent membatalkan MUS — adalah
   jangkar yang dipakai prompt.

2. **Daftar tetap di modul ini** (``mus_exemption.json``) — HANYA pembanding, tidak
   pernah dipakai untuk menentukan status. Daftar resmi Bank Mega, 175 tiket Feb
   2025–Juli 2026, dicatat berdampingan di ``mus_exemption.daftar_bank_mega`` supaya
   ketidaksepakatan antara daftar dan bacaan transkrip tetap terlihat (mis. daftar
   berkata EXEMPT sementara transkrip tidak menangkap alasannya) — tapi skor dan status
   tiket tetap dihitung dari transkrip saja. Daftar ini datang BULANAN dari Bank Mega
   sehingga selalu tertinggal dari tiket yang baru masuk; menjadikannya penentu akan
   membuat tiket lama berubah status begitu daftar baru tiba, padahal transkripnya
   sendiri tidak berubah.

Kategori ``XSELL`` ("Xsell tidak muncul" / "Xsell tidak eligible") ADA di daftar (63 dari
175) tetapi **sengaja tidak ditangani secara khusus** (keputusan 8 September 2026): pada
kasus itu tiket MUS-nya memang tidak terbentuk, sehingga tiketnya tidak pernah masuk
antrean QC. Entrinya tetap disimpan supaya daftarnya utuh dan apa adanya seperti kiriman
Bank Mega.

JSON-nya diturunkan sekali dari berkas Bank Mega "Not Eligible MUS (Health Declaration
& Tidak Tampil Xsell).xlsx" (17 sheet bulanan, Feb 2025–Juli 2026). Berkas asalnya ada
di ``docs/csv_bank/`` yang masuk ``.gitignore``, jadi JSON inilah satu-satunya salinan
yang ikut ter-commit. Bank Mega memperbarui daftar itu tiap bulan — setiap kiriman baru
harus dikonversi ulang dan menimpa JSON ini.
"""
import json
import os

_PATH = os.path.join(os.path.dirname(__file__), "mus_exemption.json")

# Kategori pengecualian. SAKIT & HAMIL terbaca dari transkrip; XSELL hanya dari daftar.
KATEGORI_SAKIT = "SAKIT"
KATEGORI_HAMIL = "HAMIL"
KATEGORI_XSELL = "XSELL"

KATEGORI_LABELS = {
    KATEGORI_SAKIT: "Pernyataan kesehatan — tidak eligible MUS",
    KATEGORI_HAMIL: "Mengandung lebih dari 7 bulan",
    KATEGORI_XSELL: "Xsell MUS tidak muncul / tidak eligible di sistem",
}

# Whitelist kategori penyakit "boleh MUS Exception" — dikompilasi dari 93 entri
# KATEGORI_SAKIT di ``mus_exemption.json`` (kasus yang sudah pernah disetujui Bank
# Mega), bukan daftar resmi/medis dari Bank Mega. Dipakai untuk mengisi bagian
# reference pada prompt MUS EXEMPTION DETECTION supaya LLM bisa mengklasifikasikan
# apakah penyakit yang disebut nasabah termasuk kategori yang lazim disetujui
# (``mus_exemption.disease_listed``, 11 September 2026) — lihat
# ``docs/csv_bank/11 September 2026/MUS_logic_update.md``.
#
# Penyakit di LUAR daftar ini TIDAK membebaskan (flow normal: harus interest
# Cashline + MUS agar valid) — daftar ini bukan sekadar referensi, ia jadi
# penggerbang rule dokumen konfirmasi baru (lihat error_codes.apply_mus_exception_document_status).
DISEASE_WHITELIST = [
    "Diabetes / kencing manis",
    "Hipertensi / darah tinggi",
    "Penyakit jantung (termasuk pasca operasi jantung, pasang ring jantung)",
    "Kanker (termasuk kemoterapi, tumor)",
    "Gangguan ginjal (gagal ginjal, batu ginjal, cuci darah)",
    "Cedera/patah tulang, kecelakaan lalu lintas yang masih dalam perawatan",
    "GERD / tukak lambung",
    "Penyakit auto imun",
    "Kelainan darah (termasuk idiopatik trombositopenia)",
    "Cedera lutut / cedera tendon",
    "Gangguan tiroid",
    "Stroke",
    "Syaraf kejepit",
    "Vertigo",
    "Operasi (termasuk operasi gigi) yang masih dalam masa pemulihan",
    "Hamil lebih dari 7 bulan (kategori HAMIL, bukan SAKIT)",
]


def disease_whitelist_prompt_block() -> str:
    """Whitelist di atas, diformat sebagai bullet list untuk disisipkan ke prompt."""
    return "\n".join(f"- {d}" for d in DISEASE_WHITELIST)

try:
    with open(_PATH, encoding="utf-8") as _f:
        _RAW = json.load(_f)
except (OSError, ValueError):
    _RAW = {}

#: ticket_id (huruf kecil) -> entri daftar.
REGISTER = {
    str(e.get("ticket_id") or "").strip().casefold(): e
    for e in (_RAW.get("entries") or [])
    if str(e.get("ticket_id") or "").strip()
}


def register_entry(ticket_id) -> "dict | None":
    """Entri daftar tetap untuk ``ticket_id``, atau None bila tidak terdaftar.

    Pencocokan case-insensitive dan mengabaikan spasi di tepi — beberapa baris di
    berkas Bank Mega memang membawa spasi ekor (mis. ``"1901428fE3 "``).
    """
    key = str(ticket_id or "").strip().casefold()
    if not key:
        return None
    return REGISTER.get(key)


def _llm_exemption(evaluation: dict) -> "dict | None":
    """Pengecualian yang dibaca LLM dari transkrip, bila memang menyatakan EXEMPT."""
    block = evaluation.get("mus_exemption")
    if not isinstance(block, dict):
        return None
    if str(block.get("status") or "").strip().upper() != "EXEMPT":
        return None
    return block


def _pembanding_daftar(ticket_id) -> "dict | None":
    """Entri daftar Bank Mega untuk ``ticket_id``, dibentuk sebagai field pembanding.

    HANYA untuk audit/visibilitas — tidak pernah dipakai untuk menentukan status.
    """
    entry = register_entry(ticket_id)
    if not entry:
        return None
    return {
        "status": "EXEMPT",
        "kategori": entry.get("kategori"),
        "reason": entry.get("reason"),
        "periode": entry.get("periode"),
    }


def resolve(evaluation: dict, ticket_id=None) -> "dict | None":
    """Pengecualian MUS yang berlaku untuk satu tiket, atau None bila tidak ada.

    Transkrip adalah SATU-SATUNYA sumber yang menentukan status. Daftar tetap Bank
    Mega dicatat berdampingan sebagai ``daftar_bank_mega`` (plus flag ``sepakat``)
    murni untuk pembanding/audit — kalaupun daftar berkata EXEMPT, status akhir tetap
    mengikuti bacaan transkrip. Ini disengaja: daftar datang BULANAN dan selalu
    tertinggal dari tiket baru, jadi menjadikannya penentu akan membuat status sebuah
    tiket berubah-ubah tiap kali daftar diperbarui, padahal transkripnya sendiri tidak
    berubah.
    """
    llm = _llm_exemption(evaluation)
    pembanding = _pembanding_daftar(ticket_id)

    if llm:
        out = dict(llm)
        out["sumber"] = "transkrip"
        if pembanding is not None:
            out["daftar_bank_mega"] = pembanding
            out["sepakat"] = True
        return out

    if pembanding is None:
        return None

    # Daftar menyatakan EXEMPT tetapi transkrip tidak menangkap alasannya — dicatat
    # sebagai pembanding saja. Status akhir TETAP NOT_EXEMPT karena transkrip yang
    # memutuskan, bukan daftar.
    existing = evaluation.get("mus_exemption")
    return {
        "status": "NOT_EXEMPT",
        "kategori": None,
        "reason": (existing or {}).get("reason") if isinstance(existing, dict) else None,
        "sumber": "transkrip",
        "daftar_bank_mega": pembanding,
        "sepakat": False,
    }


def stamp(evaluation: dict, ticket_id=None) -> dict:
    """Kembalikan salinan ``evaluation`` dengan ``mus_exemption`` yang sudah final.

    Dipanggil worker SEBELUM ``two_pass.resync_scores``, karena skor maksimal tiket
    bergantung pada ada-tidaknya pengecualian. Setelah langkah ini, seluruh sisi
    pembaca cukup membaca ``evaluation["mus_exemption"]`` dan tidak perlu tahu
    ``ticket_id`` maupun membuka daftar.

    Bila tidak ada pengecualian dari transkrip, field-nya tetap DITULIS dengan
    ``status = "NOT_EXEMPT"``. Membiarkannya kosong akan membuat hasil baru tidak bisa
    dibedakan dari hasil lama yang memang belum mengenal aturan ini.
    """
    out = dict(evaluation)
    resolved = resolve(evaluation, ticket_id)
    if resolved:
        out["mus_exemption"] = resolved
        return out
    existing = evaluation.get("mus_exemption")
    out["mus_exemption"] = {
        "status": "NOT_EXEMPT",
        "kategori": None,
        "reason": (existing or {}).get("reason") if isinstance(existing, dict) else None,
        "sumber": "transkrip",
    }
    return out
