"""Validasi PDF + pemilihan recording utama untuk tiket MULTI-REKAMAN.

Satu ticket id bisa datang sebagai beberapa PDF. Bank Mega **tidak bisa** memberikan
penanda jenis rekaman dari hulu (dikonfirmasi 18 September 2026), jadi sistem harus
menentukan sendiri mana yang jadi acuan penilaian. Aturannya dua lapis:

    1. BUANG PDF yang tidak layak dinilai  (modul ini)
    2. Dari yang TERSISA, rekaman TERTUA = recording utama  (modul ini)

Urutan itu bukan selera. Diukur atas 4 tiket sampel (10 PDF, batch 18 September 2026),
aturan "tertua" yang dijalankan TANPA lapis pertama benar hanya **3 dari 4**: pada
``0308549YQ4`` rekaman tertua adalah panggilan 6 menit yang verifikasinya gagal lalu
ditunda agent ("nanti setelah makan siang saya coba hubungin kembali"), sementara yang
benar-benar tuntas justru rekaman kedua 23 menit. Dengan lapis pertama: **4 dari 4**.

AMBANG DURASI SAJA TIDAK CUKUP menyelamatkan kasus itu — rekaman yang keliru berdurasi
6 menit / 41 segmen, jauh di atas penggalan sungguhan (0m23s–1m51s). Yang memisahkannya
adalah **penanda penutup**: rekaman yang benar-benar menuntaskan pengajuan selalu memuat
Legal Statement, dan rekaman yang ditunda/dibatalkan/mengurus dokumen tidak pernah.

KENAPA REGEX, BUKAN LLM. Klasifikasi jenis rekaman berbasis LLM pernah dipakai dan
dicabut 16 September 2026 setelah gagal berat pada tiket ``030808fLO1`` — satu rekaman
utama 19 menit terbaca sebagai pembatalan, tiga dari empat rekaman tercoret, skor
bergerak 31,875 -> -56,44. Skrip penutup Bank Mega sangat baku (teksnya ada di
``example_phrases`` KB), jadi pencocokan deterministik bisa diaudit dan diperbaiki
dengan menambah pola — bukan dengan menaruh vonis pada tebakan model.

KATA "SETUJU" TIDAK BOLEH DIPAKAI SENDIRIAN. Tiga dari lima rekaman yang harus dibuang
di sampel justru memuatnya: ``150125GJW6`` [3] mengucapkan "Ya, setuju" untuk urusan
LAMPIRAN DOKUMEN, dan ``190324tCXV`` [2] "setuju" untuk TANDA TANGAN ULANG LINK setelah
agent lupa meminta NPWP. Karena itu penanda penutup di bawah menuntut bentuk pertanyaan
persetujuan yang baku ATAU kata setuju yang berdampingan dengan NAMA PRODUK — Legal
Statement selalu menyebut produk yang disetujui.
"""
import collections
import logging
import os
import re

from compliance.pdf_parser import (
    parse_filename_timestamp,
    parse_transcript_pdf,
    ticket_id_from_filename,
    _end_timestamp_seconds,
)
from compliance.call_ownership import fix_speaker_roles
from prompt.recording_type import (
    TAG_DITUNDA,
    TAG_LABELS,
    TAG_LAINNYA,
    TAG_PEMBATALAN,
    TAG_PERBAIKAN,
    TAG_TIDAK_TERHUBUNG,
    TAG_UTAMA,
)

logger = logging.getLogger(__name__)

#: Lantai substansi — penjaga murah untuk penggalan yang jelas bukan panggilan penilaian.
#: SENGAJA longgar: yang benar-benar memisahkan adalah penanda penutup di bawah, dan
#: ambang yang agresif justru berisiko membuang rekaman sah. Pada sampel 18 September
#: 2026 jarak antara dua kelompok sangat lebar (buang: maksimum 6m0s/41 segmen; pakai:
#: minimum 17m12s/99 segmen), jadi angka di bawah tidak menyentuh keduanya — ia hanya
#: menangkap penggalan ekstrem. Kalibrasi ulang begitu ada batch berlabel yang lebih besar.
MIN_DURASI_DETIK = 180.0
MIN_SEGMEN = 20

_PRODUK = r"mega\s*cash\s*line|mega\s*ultima?t?e?\s*shield|cashline"
_SETUJU = r"\bsetuju\b|\bbersedia\b|menyetujui"

#: Bentuk pertanyaan persetujuan yang baku ("apakah Bapak/Ibu setuju?", "saya setuju").
_SETUJU_BAKU = re.compile(
    r"(apakah|apa)\s[^.?!]{0,40}(setuju|bersedia)|menyetujui|saya setuju", re.IGNORECASE
)
#: Jarak (karakter) kata setuju ke nama produk agar dianggap Legal Statement.
_JARAK_PRODUK = 200

# Penanda alasan pembuangan. HANYA untuk LABEL yang dibaca manusia — keputusan
# pakai/buang TIDAK pernah bergantung padanya (lihat ``_alasan_tolak``).
_PENUNDAAN_CUSTOMER = re.compile(
    r"nanti\s+di(?:ulang|hubungi)|telepon\s+lagi\s+nanti|hubungi\s+(?:saya\s+)?lagi\s+nanti"
    r"|lagi\s+(?:sibuk|repot|nyetir|rapat)|belum\s+bisa\s+sekarang|nggak\s+bisa\s+(?:kalau|sekarang)"
    r"|sore\s+kali\s+ya|nanti\s+saja|besok\s+saja",
    re.IGNORECASE,
)
_PEMBATALAN_CUSTOMER = re.compile(
    r"\b(?:batal|dibatalkan|membatalkan|tidak\s+jadi|nggak\s+jadi|gak\s+jadi)\b"
    r"|tidak\s+(?:berminat|tertarik)|nggak\s+(?:minat|tertarik)",
    re.IGNORECASE,
)
_TIDAK_TERHUBUNG = re.compile(
    r"tidak\s+ada\s+jawaban|halo[,.\s]*halo[,.\s]*halo|tidak\s+terdengar|putus[- ]putus",
    re.IGNORECASE,
)
_URUSAN_DOKUMEN = re.compile(
    r"dokumen|lampir|dilampirkan|\bfoto\b|\bnpwp\b|buku\s+tabungan|upload|unggah|kirimkan\s+ulang",
    re.IGNORECASE,
)


def _teks(segments) -> str:
    return " ".join(str((s or {}).get("text") or "") for s in segments)


def punya_penanda_penutup(teks: str) -> bool:
    """True bila transkrip memuat Legal Statement — penanda rekaman yang MENUNTASKAN.

    Dua bentuk yang diterima, keduanya diukur 10/10 benar pada sampel 18 September 2026:

    * pertanyaan persetujuan baku — "apakah Bapak/Ibu setuju?", "menyetujui", "saya setuju";
    * kata setuju yang BERDAMPINGAN dengan nama produk (<= ``_JARAK_PRODUK`` karakter) —
      Legal Statement selalu menyebut produk yang disetujui.

    Diterima bila salah satu terpenuhi. Sengaja lebih longgar daripada menuntut keduanya:
    salah menolak rekaman utama jauh lebih mahal daripada salah menerima kandidat, karena
    kandidat masih disaring lagi oleh aturan "tertua" dan jaring pengaman di
    ``pilih_recording_utama``.
    """
    if not teks:
        return False
    if _SETUJU_BAKU.search(teks):
        return True
    for m in re.finditer(_SETUJU, teks, re.IGNORECASE):
        jendela = teks[max(0, m.start() - _JARAK_PRODUK) : m.end() + _JARAK_PRODUK]
        if re.search(_PRODUK, jendela, re.IGNORECASE):
            return True
    return False


def _alasan_tolak(teks: str, durasi: float, n_segmen: int) -> tuple:
    """``(tag, alasan)`` untuk rekaman yang TIDAK memenuhi syarat kandidat utama.

    Murni deskriptif — dipakai mengisi ``excluded_calls`` supaya QC tahu KENAPA sebuah
    rekaman tidak dinilai. Keputusannya sendiri sudah diambil sebelum fungsi ini dipanggil.
    """
    if _PEMBATALAN_CUSTOMER.search(teks):
        return TAG_PEMBATALAN, "Percakapan memuat pembatalan/penolakan oleh nasabah."
    if _PENUNDAAN_CUSTOMER.search(teks):
        return TAG_DITUNDA, "Nasabah menunda pembicaraan ke waktu lain."
    if _TIDAK_TERHUBUNG.search(teks):
        return TAG_TIDAK_TERHUBUNG, "Panggilan tidak terhubung dengan baik."
    if _URUSAN_DOKUMEN.search(teks) and durasi < 600:
        return TAG_LAINNYA, (
            "Panggilan singkat mengurus dokumen/kelengkapan data di luar scorecard."
        )
    return TAG_LAINNYA, (
        "Tidak memuat penutup pengajuan (Legal Statement), sehingga bukan rekaman "
        "yang menuntaskan pengajuan."
    )


def periksa_berkas(path: str, expected_ticket_id: str = None) -> dict:
    """Periksa SATU PDF. Mengembalikan ringkasan berikut vonis teknisnya.

    Kunci: ``path``, ``filename``, ``timestamp``, ``durasi``, ``n_segmen``, ``teks``,
    ``teknis_ok``, ``masalah`` (list), ``penutup`` (bool).
    """
    filename = os.path.basename(path)
    hasil = {
        "path": path, "filename": filename, "timestamp": None, "durasi": 0.0,
        "n_segmen": 0, "teks": "", "teknis_ok": False, "masalah": [], "penutup": False,
    }

    # 1.1 — bisa di-parse
    try:
        mentah = parse_transcript_pdf(path)
    except Exception as exc:  # noqa: BLE001 — PDF rusak tidak boleh menjatuhkan tiket
        hasil["masalah"].append(f"PDF tidak bisa dibaca: {exc}")
        return hasil

    segmen = fix_speaker_roles(mentah)
    hasil["n_segmen"] = len(segmen)
    hasil["teks"] = _teks(segmen)
    if segmen:
        hasil["durasi"] = _end_timestamp_seconds(segmen[-1]["timestamp"])

    # 1.2 — menghasilkan segmen
    if not segmen:
        hasil["masalah"].append("Tidak ada satu pun segmen percakapan yang terbaca.")

    # 1.3 — kedua peran hadir. Transkrip berlabel netral (SPEAKER_0/1) dilewati:
    # di sana tidak ada peran yang bisa dinilai hadir/tidak.
    label = {str((s or {}).get("speaker") or "").strip().casefold() for s in segmen}
    if label and not any(l.startswith("speaker_") for l in label):
        ada_agent = any(l.startswith("agent") for l in label)
        ada_cust = any(l.startswith(("customer", "nasabah")) for l in label)
        if not (ada_agent and ada_cust):
            hasil["masalah"].append(
                "Hanya satu peran pembicara yang terbaca (diarization gagal)."
            )

    # 1.4 — timestamp nama berkas terbaca (dibutuhkan aturan "tertua")
    ts = parse_filename_timestamp(filename)
    hasil["timestamp"] = ts
    if ts is None:
        hasil["masalah"].append("Timestamp pada nama berkas tidak bisa dibaca.")

    # 1.5 — ticket id cocok
    if expected_ticket_id:
        milik = ticket_id_from_filename(filename).split("_")[0]
        if milik.strip().casefold() != str(expected_ticket_id).strip().casefold():
            hasil["masalah"].append(
                f"Ticket id berkas ('{milik}') bukan milik tiket ini "
                f"('{expected_ticket_id}')."
            )

    hasil["teknis_ok"] = not hasil["masalah"]
    hasil["penutup"] = punya_penanda_penutup(hasil["teks"])
    return hasil


def _buang_duplikat(laporan: list) -> list:
    """Tandai berkas kembar — isi identik ATAU (timestamp, durasi, jumlah segmen) sama.

    Yang PERTAMA dipertahankan; sisanya diberi masalah sehingga gugur di gerbang teknis.
    Rekaman kembar menggandakan bobot evidence yang sama.
    """
    terlihat = {}
    for r in laporan:
        if not r["teknis_ok"]:
            continue
        kunci = (hash(r["teks"]), round(r["durasi"], 2), r["n_segmen"])
        if kunci in terlihat:
            r["masalah"].append(f"Duplikat dari {terlihat[kunci]}.")
            r["teknis_ok"] = False
        else:
            terlihat[kunci] = r["filename"]
    return laporan


def pilih_recording_utama(pdf_paths, expected_ticket_id: str = None) -> dict:
    """Tentukan recording utama + rekaman pendamping + yang dibuang.

    Mengembalikan dict:
      ``utama``      — path recording utama (selalu terisi bila ``pdf_paths`` tidak kosong)
      ``pendamping`` — path lolos validasi selain utama, urut kronologis
      ``dibuang``    — ``[{path, filename, tag, tag_label, reason}]``
      ``laporan``    — hasil ``periksa_berkas`` tiap berkas (untuk log/audit)
      ``fallback``   — ``None`` bila normal, atau alasan jaring pengaman yang menyala

    JARING PENGAMAN (semuanya wajib, lihat ``docs/csv_bank/17 September 2026/multiple_call.md``):

    1. **Tiket satu PDF tidak pernah tersentuh.** Satu-satunya berkas SELALU jadi utama,
       apa pun hasil validasi. Inilah yang mengunci janji bahwa 50 tiket single-recording
       yang sudah ada di dashboard tidak bergeser sama sekali.
    2. **Nol kandidat -> kembalikan semua.** Bila validasi menyisakan nol kandidat, seluruh
       PDF dikembalikan dan yang tertua jadi utama — persis perilaku sebelum modul ini ada.
       Lebih baik menilai berlebih daripada menerbitkan evaluasi kosong (pola yang sama
       dipakai ``split_by_recording_type`` dan ``filter_calls_by_agent``).
    """
    paths = list(pdf_paths or [])
    if not paths:
        return {"utama": None, "pendamping": [], "dibuang": [], "laporan": [], "fallback": None}

    # Jaring pengaman 1 — tiket satu rekaman tidak pernah divalidasi.
    if len(paths) == 1:
        return {
            "utama": paths[0], "pendamping": [], "dibuang": [],
            "laporan": [], "fallback": "tiket satu rekaman",
        }

    laporan = _buang_duplikat(
        [periksa_berkas(p, expected_ticket_id) for p in paths]
    )

    def urut(r):
        # Berkas tanpa timestamp didorong ke belakang; ia tidak boleh menang "tertua".
        return (r["timestamp"] is None, r["timestamp"] or 0, r["filename"])

    kandidat = [
        r for r in laporan
        if r["teknis_ok"] and r["penutup"]
        and r["durasi"] >= MIN_DURASI_DETIK and r["n_segmen"] >= MIN_SEGMEN
    ]

    # Jaring pengaman 2 — tidak ada kandidat sama sekali.
    if not kandidat:
        logger.warning(
            "tidak ada rekaman yang lolos validasi (%d berkas) — seluruhnya dikembalikan",
            len(paths),
        )
        urutan = sorted(laporan, key=urut)
        return {
            "utama": urutan[0]["path"],
            "pendamping": [r["path"] for r in urutan[1:]],
            "dibuang": [],
            "laporan": laporan,
            "fallback": "tidak ada rekaman yang lolos validasi",
        }

    kandidat.sort(key=urut)
    utama = kandidat[0]
    pendamping = [r["path"] for r in kandidat[1:]]

    dibuang = []
    for r in laporan:
        if any(r["path"] == k["path"] for k in kandidat):
            continue
        if not r["teknis_ok"]:
            tag, alasan = TAG_LAINNYA, " ".join(r["masalah"])
        else:
            tag, alasan = _alasan_tolak(r["teks"], r["durasi"], r["n_segmen"])
        dibuang.append({
            "path": r["path"], "filename": r["filename"], "tag": tag,
            "tag_label": TAG_LABELS.get(tag, tag), "reason": alasan,
        })

    return {
        "utama": utama["path"], "pendamping": pendamping, "dibuang": dibuang,
        "laporan": laporan, "fallback": None,
    }


def tags_by_file(hasil: dict) -> dict:
    """``{nama_berkas: {"tag", "reason"}}`` — bentuk yang dipakai ``stamp_evidence_tags``.

    Rekaman pendamping ditandai ``TAG_PERBAIKAN``, bukan ``TAG_UTAMA``: sejak modul ini
    ada kita memang TAHU mana yang utama, dan ``stamp_reason_provenance`` menuliskannya
    apa adanya ke kolom Reason ("Evidence diambil dari recording perbaikan."). Menandai
    keduanya "utama" akan membuat kalimat itu berbohong pada separuh barisnya.

    ``TAG_PERBAIKAN`` sengaja BUKAN anggota ``EXCLUDED_TAGS``, jadi penandaan ini tidak
    membuat rekamannya ikut tercoret — ia tetap dinilai.
    """
    out = {}
    if hasil.get("utama"):
        out[os.path.basename(hasil["utama"])] = {"tag": TAG_UTAMA, "reason": None}
    for p in hasil.get("pendamping") or []:
        out[os.path.basename(p)] = {"tag": TAG_PERBAIKAN, "reason": None}
    for d in hasil.get("dibuang") or []:
        out[d["filename"]] = {"tag": d["tag"], "reason": d["reason"]}
    return out
