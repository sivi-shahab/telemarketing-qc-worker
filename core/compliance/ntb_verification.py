"""NTB Data Verification: data TMS NTB, nilai yang diucapkan di transkrip, dan item scorecard.

Di Cashline setiap field acuan TMS terhubung ke item scorecard tertentu. Untuk NTB,
tiap field di bawah menunjuk SATU item ``SC_NTB_n`` yang menilai apakah agent
menanyakan/mengonfirmasi field itu (``Pengisian Data Basic`` = ``SC_NTB_8``..``41``,
``Pengisian Data Supplement`` = ``SC_NTB_42``..``45``).

Tiap baris membawa Match (MATCH / MISMATCH / SKIPPED_NULL) dan Similarity (%) seperti
"Cashline Data Verification", dihitung DETERMINISTIK di kode menurut tipe field (nama, tanggal
lahir, digit, RT/RW, teks/alamat, kategori, penghasilan). Nama dan tanggal lahir memakai fungsi
similarity yang sama dengan verifikasi statik Cashline (``static_similarity``).

TAMPILAN SAJA: match/similarity di sini TIDAK mengubah skor, status item scorecard, maupun
error code. Data TMS-nya ada di tabel ``tms_ntb`` (migration 0062).
"""

import csv
import functools
import os
from compliance.campaign_profile import NTB_KTP_BOMB_KEY

# Baris gabungan alamat (penanda pada kolom NTB_FIELDS): dibandingkan dari komponen major saja.
ALAMAT_RUMAH = "__alamat_rumah__"
ALAMAT_KANTOR = "__alamat_kantor__"
ALAMAT_AMBANG = 60.0  # alamat rumah/kantor gabungan (diturunkan dari 80 atas keputusan user, 26 Okt 2026)
ALAMAT_TUJUAN_AMBANG = 80.0  # alamat lengkap pengiriman kartu/penagihan vs alamat rumah/kantor (tidak berubah)

# Komponen alamat gabungan: (kolom TMS, kunci ekstraksi transkrip). Major (non-tolerable) menentukan
# lulus/gagal; minor (tolerable) tidak dibandingkan, ikut lulus/gagal bersama major.
_ALAMAT_RUMAH_MAJOR = (("alamat-rumah-1-new", "alamat_rumah"), ("rumah-kelurahan-new", "kelurahan"), ("rumah-kecamatan-new", "kecamatan"))
_ALAMAT_RUMAH_MINOR = ("alamat-rumah-2-new", "rumah-rt-new", "rumah-rw-new")
_ALAMAT_KANTOR_MAJOR = (("nama-perusahaan", "nama_perusahaan"), ("alamat-kantor-1-new", "alamat_kantor"), ("kantor-kelurahan-new", "kelurahan_kantor"), ("kantor-kecamatan-new", "kecamatan_kantor"))
_ALAMAT_KANTOR_MINOR = ("alamat-kantor-2-new",)

# Komponen yang dibandingkan di baris gabungan (26 Okt 2026): SEMUA komponen, major dan minor.
# (kolom TMS, kunci ekstraksi transkrip atau None). Sisi ucapan memakai ``alamat_rumah`` /
# ``alamat_kantor`` yang sudah memuat baris 2; ``rt``/``rw`` ucapan ditambahkan bila terekstrak
# sendiri. Alamat baris 2 kantor sering berisi jalannya (baris 1 hanya kelurahan).
_ALAMAT_RUMAH_BANDING = (("alamat-rumah-1-new", "alamat_rumah"), ("alamat-rumah-2-new", None), ("rumah-rt-new", "rt"),
                         ("rumah-rw-new", "rw"), ("rumah-kelurahan-new", "kelurahan"), ("rumah-kecamatan-new", "kecamatan"))
_ALAMAT_KANTOR_BANDING = (("nama-perusahaan", "nama_perusahaan"), ("alamat-kantor-1-new", "alamat_kantor"),
                          ("alamat-kantor-2-new", None), ("kantor-kelurahan-new", "kelurahan_kantor"),
                          ("kantor-kecamatan-new", "kecamatan_kantor"))

# (kunci ekstraksi transkrip, label, kolom TMS NTB, kode item scorecard)
# Kunci ekstraksi = ``evaluation["ntb_application_extraction"]`` (lihat prompt NTB).
NTB_FIELDS = (
    ("jenis_kartu", "Jenis kartu", "kartu-kredit-yang-dikehendaki", "SC_NTB_8"),
    ("nama_lengkap_ktp", "Nama lengkap sesuai KTP", "nama-lengkap-sesuai-ktp", "SC_NTB_9"),
    ("no_ktp", "Nomor KTP/KITAS", "no-ktpkitas", "SC_NTB_10"),
    ("kewarganegaraan", "Kewarganegaraan", "kewarganegaraan", "SC_NTB_11"),
    ("jenis_kelamin", "Jenis kelamin", "jenis-kelamin", "SC_NTB_12"),
    ("status_perkawinan", "Status perkawinan", "status-perkawinan", "SC_NTB_13"),
    ("tempat_lahir", "Tempat lahir", "tempat-lahir", "SC_NTB_14"),
    ("tanggal_lahir", "Tanggal lahir", "tanggal-lahir", "SC_NTB_15"),
    (ALAMAT_RUMAH, "Alamat rumah (gabungan)", ALAMAT_RUMAH, "SC_NTB_16"),
    ("handphone", "Nomor handphone", "handphone", "SC_NTB_22"),
    ("email", "Email", "email-address", "SC_NTB_23"),
    ("nama_keluarga_dekat", "Nama keluarga dekat", "nama-keluarga-dekat", "SC_NTB_24"),
    ("handphone_keluarga_dekat", "Handphone keluarga dekat", "handphone-keluarga-dekat", "SC_NTB_25"),
    ("nama_gadis_ibu_kandung", "Nama gadis ibu kandung", "nama-gadis-ibu-kandung", "SC_NTB_26"),
    ("jabatan", "Jabatan", "jabatan", "SC_NTB_27"),
    ("bidang_usaha", "Bidang usaha", "bidang-usaha", "SC_NTB_28"),
    ("pekerjaan", "Pekerjaan", "pekerjaan", "SC_NTB_29"),
    (ALAMAT_KANTOR, "Nama perusahaan + alamat kantor (gabungan)", ALAMAT_KANTOR, "SC_NTB_31"),
    ("telepon_kantor", "Telepon kantor", "telepon-kantor", "SC_NTB_35"),
    ("lama_bekerja", "Lama bekerja", "lama-bekerjausaha", "SC_NTB_76"),
    ("penghasilan_kotor_perbulan", "Penghasilan kotor per bulan", "penghasilan-kotor-perbulan", "SC_NTB_36"),
    ("alamat_pengiriman_kartu", "Alamat pengiriman kartu", "alamat-pengiriman-kartu", "SC_NTB_37"),
    ("alamat_tagihan_bulanan", "Alamat penagihan bulanan", "alamat-tagihan-bulanan", "SC_NTB_38"),
    (None, "Source code", "source-code", "SC_NTB_39"),
    ("persetujuan_sharing_data", "Persetujuan data nasabah (sharing data)", "persetujuan-data-nasabah", "SC_NTB_40"),
    ("estatement", "E-statement", "estatement", "SC_NTB_41"),
)

# Microsite (di dashboard disebut Microsite): daftar nilai yang diizinkan dari Bank Mega (berkas CSV di ``ntb_source_data/``, satu per
# field; kolom ``campaign,value,keterangan``). Field di sini dibandingkan TMS vs Microsite, bukan
# dengan ucapan. Berkas baru = satu entri di peta ini + tipe ``source_data`` di ``_TYPE``.
SOURCE_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ntb_source_data")
SOURCE_DATA_CAMPAIGN = "New To Bank Eksternal"  # nama campaign NTB di berkas Microsite Bank Mega
SOURCE_DATA_FILES = {"source-code": "source_code.csv"}  # kolom TMS -> berkas


@functools.lru_cache(maxsize=None)
def load_source_data(column):
    """Daftar ``(nilai, keterangan)`` yang diizinkan untuk kolom TMS ``column`` pada campaign NTB
    (kosong bila kolom tak punya Microsite atau berkasnya tidak ada)."""
    nama = SOURCE_DATA_FILES.get(column)
    if not nama:
        return ()
    try:
        with open(os.path.join(SOURCE_DATA_DIR, nama), encoding="utf-8-sig", newline="") as f:
            return tuple((r["value"].strip(), (r.get("keterangan") or "").strip())
                         for r in csv.DictReader(f) if r.get("campaign", "").strip() == SOURCE_DATA_CAMPAIGN and (r.get("value") or "").strip())
    except OSError:
        return ()


def source_data_values(column):
    """Nilai Microsite (tanpa keterangan) untuk ``column``, atau None bila field tak memakainya."""
    return [v for v, _ in load_source_data(column)] if column in SOURCE_DATA_FILES else None


# Placeholder yang dipakai TMS untuk isian kosong.
_EMPTY = {"", "0", ".", "-", "null", "none"}


def _clean(value):
    if value is None:
        return None
    s = str(value).strip()
    return None if s.casefold() in _EMPTY else s


def _extracted(extraction, key):
    if not key or not isinstance(extraction, dict):
        return None
    v = extraction.get(key)
    if v is None or v == "":
        return None
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) or None
    return str(v)


NAMA_AMBANG = 80.0  # ambang similarity nama KTP untuk tiap pasangan (OCR, TMS, transkrip)


# Nilai KTP hasil OCR (``ktp_ocr_values``) -> kunci; kolom TMS NTB -> kunci KTP (Microsite untuk voting).
_KTP_OCR_KEYS = {"nama": "nama_ktp", "tanggal_lahir": "tanggal_lahir", "tempat_lahir": "tempat_lahir",
                 "jenis_kelamin": "jenis_kelamin", "status_perkawinan": "status_perkawinan_ktp",
                 "pekerjaan": "pekerjaan_ktp", "alamat": "alamat_ktp", "rt": "rt_ktp", "rw": "rw_ktp",
                 "kelurahan": "kelurahan_ktp", "kecamatan": "kecamatan_ktp"}
_KTP_KOLOM = {"no-ktpkitas": "nik", "kewarganegaraan": "kewarganegaraan", "tanggal-lahir": "tanggal_lahir",
              "tempat-lahir": "tempat_lahir", "jenis-kelamin": "jenis_kelamin",
              "status-perkawinan": "status_perkawinan", "pekerjaan": "pekerjaan"}


# Item Data Verification KTP yang ikut score bomb (kesepakatan Bank Mega): hanya NIK, nama, jenis kelamin,
# tanggal lahir, kewarganegaraan. Field KTP lain (tempat lahir, status, alamat, pekerjaan) tidak ikut.
_KTP_ITEM_CODES = {"SC_NTB_9", "SC_NTB_10", "SC_NTB_11", "SC_NTB_12", "SC_NTB_15"}  # nama, NIK, WNI/WNA, jenis kelamin, tanggal lahir


def ktp_ocr_values(documents):
    """Semua nilai KTP hasil OCR dari ``[(doc_type, ocr_json), ...]`` (dokumen ``done``) sebagai dict
    (``nama``, ``nik``, ``kewarganegaraan``, ``tanggal_lahir``, ``tempat_lahir``, ``jenis_kelamin``,
    ``status_perkawinan``, ``pekerjaan``, ``alamat``, ``rt``, ``rw``, ``kelurahan``, ``kecamatan``);
    hanya yang terbaca. Per field, KTP terakhir yang terbaca menang. NIK/kewarganegaraan dari baris
    ``verifications``; field lain butuh OCR baru (KTP yang di-OCR sebelum 30 Okt 2026 hanya punya nama,
    tanggal lahir, NIK, kewarganegaraan)."""
    out = {}
    for doc_type, ocr in documents or ():
        if doc_type != "ktp" or not isinstance(ocr, dict):
            continue
        for key, src in _KTP_OCR_KEYS.items():
            v = _clean(ocr.get(src))
            if v:
                out[key] = v
        for row in ocr.get("verifications") or []:
            kunci = {"NIK": "nik", "KEWARGANEGARAAN": "kewarganegaraan"}.get(str((row or {}).get("field") or "").strip().upper())
            v = _clean((row or {}).get("document"))
            if kunci and v:
                out[kunci] = v
    return out


def ktp_ocr_name(documents):
    """Nama pada KTP hasil OCR (``nama_ktp``); None bila tak ada."""
    return ktp_ocr_values(documents).get("nama")


# Microsite: satu CSV per field di ``ntb_source_data/microsite/<kolom TMS>.csv`` (kolom ``ticket_id,value``),
# dipakai sebagai sumber ketiga voting (Microsite, TMS, transkrip). Tanpa berkas / tanpa baris tiket =
# perilaku lama (TMS vs transkrip). Berkas dibaca ulang bila berubah (mtime).
MICROSITE_DIR = os.path.join(SOURCE_DATA_DIR, "microsite")


@functools.lru_cache(maxsize=None)
def _load_microsite(column, mtime):
    try:
        with open(os.path.join(MICROSITE_DIR, f"{column}.csv"), encoding="utf-8-sig", newline="") as f:
            return {(r.get("ticket_id") or "").strip(): (r.get("value") or "").strip()
                    for r in csv.DictReader(f) if (r.get("ticket_id") or "").strip() and (r.get("value") or "").strip()}
    except OSError:
        return {}


def microsite_value(column, ticket_id):
    """Nilai Microsite untuk ``ticket_id`` pada kolom TMS ``column`` (None bila tak ada)."""
    if not ticket_id:
        return None
    try:
        mtime = os.path.getmtime(os.path.join(MICROSITE_DIR, f"{column}.csv"))
    except OSError:
        return None
    return _load_microsite(column, mtime).get(str(ticket_id).strip()) or None


# Tipe pembanding yang ikut voting. Tidak ikut (aturan khusus): source_data, jenis_kartu, estatement,
# microsite_menyusul, telepon kantor (digit), handphone nasabah, alamat pengiriman/penagihan. Alamat rumah gabungan
# memakai voting sendiri di ``_alamat_row`` (KTP sebagai sumber ketiga).
_VOTING_TIPE = {"nama", "tanggal", "digit_1", "email", "lama_kerja", "bidang_usaha", "pekerjaan", "angka",
                "kategori", "penghasilan", "teks"}
_VOTING_KECUALI = {"alamat-pengiriman-kartu", "alamat-tagihan-bulanan", "handphone", "telepon-kantor"}
_VOTING_NOTE = ["Voting 3 sumber: Microsite, TMS, dan transkrip; valid (MATCH) bila minimal 2 sumber sepakat (ada pasangan yang cocok)",
                "Sumber yang kosong dilewati: hanya 2 sumber terisi = 1 perbandingan yang harus cocok"]


def vote_field(column, tms_value, micro_value, extracted):
    """Voting 2 sumber sepakat dari TMS, Microsite, dan transkrip dengan pembanding ``compare_field`` milik
    field itu. ``(match, similarity, reason)``; sumber kosong dilewati, kurang dari 2 sumber = SKIPPED_NULL."""
    hasil = []
    for label, a, b in (("Microsite vs TMS", micro_value, tms_value), ("TMS vs transkrip", tms_value, extracted),
                        ("Microsite vs transkrip", micro_value, extracted)):
        if a is None or b is None:
            continue
        m, sim, _r, _t = compare_field(column, a, b)
        if m != "SKIPPED_NULL":
            hasil.append((label, m, sim))
    if not hasil:
        return "SKIPPED_NULL", None, "Kurang dari 2 sumber yang dapat dibandingkan (Microsite, TMS, transkrip)."
    ok = any(m == "MATCH" for _, m, _ in hasil)
    sims = [x for _, _, x in hasil if x is not None]
    rinci = "; ".join(f"{l} {m}" + (f" {_fmt(x)}%" if x is not None else "") for l, m, x in hasil)
    return ("MATCH" if ok else "MISMATCH"), (float(max(sims)) if sims else None), (
        f"{'Minimal 2 sumber sepakat' if ok else 'Tidak ada 2 sumber yang sepakat'}: {rinci}.")


def _nama_tiga_arah(tms_value, ocr_nama, extracted):
    """Nama KTP: voting antara Microsite (OCR KTP), TMS, dan transkrip. Valid bila minimal 2 sumber sepakat,
    yaitu ada pasangan (Microsite vs TMS, TMS vs transkrip, Microsite vs transkrip) dengan similarity
    >= ``NAMA_AMBANG``. Sumber yang kosong dilewati: hanya 2 sumber terisi = 1 perbandingan yang harus cocok;
    kurang dari 2 sumber = SKIPPED_NULL. ``(match, similarity, reason)``: similarity = tertinggi."""
    pasangan = []
    for label, a, b in (("Microsite vs TMS", ocr_nama, tms_value), ("TMS vs transkrip", tms_value, extracted),
                        ("Microsite vs transkrip", ocr_nama, extracted)):
        if a and b:
            pasangan.append((label, similarity_nama(a, b)))
    pasangan = [(l, x) for l, x in pasangan if x is not None]
    if not pasangan:
        return "SKIPPED_NULL", None, "Nama tidak dapat dibandingkan (Microsite, TMS, dan transkrip tidak berpasangan)."
    cocok = [x for _, x in pasangan if x >= NAMA_AMBANG]
    ok = bool(cocok)
    rinci = "; ".join(f"{l} {_fmt(x)}%" for l, x in pasangan)
    return ("MATCH" if ok else "MISMATCH"), float(max(x for _, x in pasangan)), (
        f"{'Minimal 2 sumber sepakat' if ok else 'Tidak ada 2 sumber yang sepakat'} (ambang {int(NAMA_AMBANG)}%): {rinci}.")


def build_ntb_data_verification(evaluation, tms_row, ktp=None) -> list:
    """Baris tampilan NTB Data Verification, satu per field TMS yang ditautkan ke item SC.

    ``tms_row`` = dict baris ``tms_ntb`` (atau None bila belum ada; kolom TMS tampil kosong).
    Tiap baris: ``field``, ``label``, ``tms_column``, ``tms_value``, ``extracted_value``,
    ``item_code``, ``item_status`` (status item di scorecard_result, atau None)."""
    ev = evaluation if isinstance(evaluation, dict) else {}
    ktp = {"nama": ktp} if isinstance(ktp, str) else dict(ktp or {})  # nilai KTP hasil OCR (``ktp_ocr_values``)
    ktp_nama = ktp.get("nama")
    status_by_code = {
        (it or {}).get("item_code"): (it or {}).get("status")
        for it in (ev.get("scorecard_result") or [])
    }
    extraction = ev.get("ntb_application_extraction")
    tms = tms_row or {}
    rows = []
    # Nomor handphone nasabah (transkrip dan TMS): nomor keluarga dekat tidak boleh sama dengannya.
    nasabah_phones = {
        d for d in (_digits(_extracted(extraction, "handphone"), True), _digits(_clean(tms.get("handphone")), True)) if d
    }
    for key, label, column, code in NTB_FIELDS:
        if column in (ALAMAT_RUMAH, ALAMAT_KANTOR):
            rows.append(_alamat_row(column, label, code, extraction, tms, status_by_code, ktp))
            continue
        tms_value = _clean(tms.get(column))
        extracted = _extracted(extraction, key)
        if column == "jenis-kelamin":
            # Jenis kelamin dihitung dari NIK digit 7-12 (NIK transkrip; bila tak terbaca, NIK TMS).
            nik, from_nik = None, None
            for kandidat in (_extracted(extraction, "no_ktp"), _clean(tms.get("no-ktpkitas"))):
                from_nik = gender_from_nik(kandidat)
                if from_nik:
                    nik = kandidat
                    break
            if from_nik:
                extracted = from_nik
        match, similarity, reason, tipe = compare_field(column, tms_value, extracted)
        if (column in ("alamat-pengiriman-kartu", "alamat-tagihan-bulanan") and match == "MISMATCH"
                and _canon_kategori(column, extracted) is None):
            # Ucapan berupa alamat lengkap (bukan kata "rumah"/"kantor"/"email"): cocokkan dengan alamat
            # rumah / kantor, lalu bandingkan kodenya (H/C) dengan kode TMS.
            prefer = {"RUMAH": "H", "KANTOR": "C"}.get(_canon_kategori(column, tms_value))
            kode, sim_alamat = _alamat_tujuan_dari_ucapan(extracted, extraction, tms, prefer)
            if kode:
                ok = ("RUMAH" if kode == "H" else "KANTOR") == _canon_kategori(column, tms_value)
                match, similarity = ("MATCH" if ok else "MISMATCH"), float(round(sim_alamat))
                nama = "rumah" if kode == "H" else "kantor"
                reason = (f"Alamat yang disebut sama dengan atau bagian dari alamat {nama} ({_fmt(sim_alamat)}%), kode {kode}; "
                          + ("sesuai kode TMS." if ok else f"TMS {tms_value}."))
        catatan, source_value = None, None
        if column == "estatement":
            # E-statement mengikuti alamat penagihan bulanan (keputusan pemilik, 27 Okt 2026): kode E -> harus Y,
            # selain E -> harus N. TMS vs TMS, tidak dibandingkan dengan transkrip.
            tagihan, est = _clean(tms.get("alamat-tagihan-bulanan")), _canon_kategori("estatement", tms_value)
            extracted, tipe = None, "estatement_tagihan"
            if tagihan is None or est is None:
                match, similarity, reason = "SKIPPED_NULL", None, "Alamat penagihan bulanan atau e-statement TMS kosong."
            else:
                harus = "Y" if _canon_kategori("alamat-tagihan-bulanan", tagihan) == "EMAIL" else "T"
                ok = est == harus
                match, similarity = ("MATCH" if ok else "MISMATCH"), (100.0 if ok else 0.0)
                reason = (f"Alamat penagihan bulanan TMS {tagihan} → e-statement harus {'Y' if harus == 'Y' else 'N'}; "
                          f"TMS {tms_value}.")
                source_value = "Y" if harus == "Y" else "N"
        if column == "alamat-tagihan-bulanan" and extracted is None and tms_value is not None:
            # Pertanyaan alamat penagihan tidak ada di transkrip: cukup cek kecocokan dengan e-statement TMS.
            est = _canon_kategori("estatement", _clean(tms.get("estatement")))
            if est is not None:
                ok = (_canon_kategori(column, tms_value) == "EMAIL") == (est == "Y")
                match, similarity = ("MATCH" if ok else "MISMATCH"), (100.0 if ok else 0.0)
                reason = (f"Tidak disebut di transkrip; dicek terhadap e-statement TMS: alamat penagihan {tms_value}, "
                          f"e-statement {_clean(tms.get('estatement'))} → {'sesuai' if ok else 'tidak sesuai'}.")
                catatan = _THRESHOLD_NOTES["tagihan_tanpa_ucapan"]
        if ((tipe in _VOTING_TIPE or column == "no-ktpkitas") and column not in _VOTING_KECUALI
                and column != "nama-lengkap-sesuai-ktp"):
            # Microsite = CSV per field bila ada, selain itu hasil OCR KTP (Microsite) untuk field yang ada di KTP.
            micro = microsite_value(column, tms.get("result_id")) or ktp.get(_KTP_KOLOM.get(column))
            if micro is not None:
                match, similarity, reason = vote_field(column, tms_value, micro, extracted)
                source_value = micro
                catatan = _THRESHOLD_NOTES["gender_nik" if column == "jenis-kelamin" else tipe] + _VOTING_NOTE
        if column == "nama-lengkap-sesuai-ktp" and ktp_nama:
            # Microsite = hasil OCR KTP yang diunggah: OCR, TMS, dan transkrip harus saling cocok.
            match, similarity, reason = _nama_tiga_arah(tms_value, ktp_nama, extracted)
            tipe, source_value = "nama_tiga_arah", ktp_nama
        if column == "jenis-kelamin":
            tipe = "gender_nik"
            if from_nik and match != "SKIPPED_NULL":
                reason = f"{reason.replace(', ucapan ', ', NIK ')} Dihitung dari NIK digit 7-12 ({re.sub(chr(92) + 'D', '', str(nik))[6:12]})."
        if column == "persetujuan-data-nasabah":
            # Persetujuan sharing data ke mitra dicek dari Microsite (menunggu contoh data), bukan dari transkrip.
            extracted = None
        if column == "handphone":
            # Handphone nasabah dibandingkan TMS vs Microsite, bukan dengan transkrip (keputusan pemilik, 27 Okt 2026;
            # menggantikan aturan "wajib diucapkan lengkap" 22 Okt). Microsite belum diterima: sementara SKIPPED_NULL,
            # status SC_NTB_22 mengikuti penilaian LLM.
            extracted, tipe = None, "source_data_menyusul"
            match, similarity = "SKIPPED_NULL", None
            reason = "Nilai TMS kosong." if tms_value is None else "Microsite handphone belum tersedia; tidak dibandingkan dengan transkrip."
            catatan = _THRESHOLD_NOTES["source_data_menyusul"] + [_ATURAN_HP_BEDA_KELUARGA]
            # Nomor nasabah tidak boleh sama dengan nomor keluarga dekat (TMS maupun ucapan).
            keluarga = {d for d in (_digits(_clean(tms.get("handphone-keluarga-dekat")), True),
                                    _digits(_extracted(extraction, "handphone_keluarga_dekat"), True)) if d}
            sama = keluarga & {d for d in (_digits(tms_value, True), _digits(_extracted(extraction, "handphone"), True)) if d}
            if sama:
                match, similarity = "MISMATCH", 0.0
                reason = "Nomor handphone nasabah sama dengan nomor handphone keluarga dekat."
        if column == "telepon-kantor":
            # Telepon kantor kosong tidak apa-apa (SKIPPED_NULL), tetapi bila terisi dan berawalan
            # +628/08xx (nomor seluler) SELALU salah, di sisi TMS maupun ucapan, apa pun hasil pembandingan.
            salah = [nama for nama, v in (("TMS", tms_value), ("ucapan", extracted)) if is_nomor_seluler(v)]
            if salah:
                match, similarity = "MISMATCH", 0.0
                reason = f"Telepon kantor berformat nomor seluler (+628/08xx) pada {' dan '.join(salah)}: tidak diperbolehkan."
        if tipe == "digit_1":
            own = {d for d in (_digits(tms_value, True), _digits(extracted, True)) if d}
            if own & nasabah_phones:
                match, reason = "MISMATCH", "Nomor keluarga dekat sama dengan handphone nasabah."
        if tipe == "lama_kerja":
            ref = lama_kerja_tms_months(tms_value)
            if ref is not None:
                tms_value = format_lama_kerja(ref)
        rows.append({
            "field": key or column,
            "label": label,
            "tms_column": column,
            "tms_value": tms_value,
            "extracted_value": extracted,
            "source_value": source_value or (", ".join(source_data_values(column)) if column in SOURCE_DATA_FILES else None),
            "item_code": code,
            "item_status": status_by_code.get(code),
            "match": match,
            "similarity_percent": similarity,
            "reason": reason,
            "thresholds": catatan or _THRESHOLD_NOTES[tipe],
        })
    return rows


# NTB Supplement Verification: data kartu tambahan (maksimal 4 slot) TMS vs ucapan agent/nasabah.
# (kunci ekstraksi ``kartu_tambahan[]``, label, kolom TMS per slot, kolom pembanding, kode item scorecard)
SUPPLEMENT_SLOTS = 4
_SUPPLEMENT_FIELDS = (
    ("nama", "Nama", "full-name-supplektp-{n}-new", "nama-lengkap-sesuai-ktp", "SC_NTB_42"),
    ("jenis_kelamin", "Jenis kelamin", "jenis-kelamin-{n}-new", "jenis-kelamin", "SC_NTB_43"),
    ("hubungan", "Hubungan kekerabatan", "hub-status-keluarga-{n}-new", "hub-status-keluarga", "SC_NTB_44"),
)
SUPPLEMENT_ALAMAT_ITEM = "SC_NTB_45"


def _supplement_pairs(tms_cards, spoken_cards):
    """Pasangkan kartu TMS dengan kartu di ucapan: nama paling mirip dulu, sisanya menurut urutan.
    Mengembalikan daftar ``(slot_tms | None, kartu_tms, kartu_ucapan)``."""
    kandidat = sorted(
        ((similarity_nama(t.get("nama"), s.get("nama")) or 0.0, ti, si)
         for ti, (_n, t) in enumerate(tms_cards) if t.get("nama")
         for si, s in enumerate(spoken_cards) if s.get("nama")),
        key=lambda x: -x[0])
    pakai_t, pakai_s, pasang = set(), set(), {}
    for sim, ti, si in kandidat:
        if ti not in pakai_t and si not in pakai_s and sim >= THRESHOLD:
            pakai_t.add(ti)
            pakai_s.add(si)
            pasang[ti] = si
    sisa_s = [si for si in range(len(spoken_cards)) if si not in pakai_s]
    out = []
    for ti, (slot, tcard) in enumerate(tms_cards):
        si = pasang.get(ti)
        if si is None and sisa_s:
            si = sisa_s.pop(0)
        out.append((slot, tcard, spoken_cards[si] if si is not None else {}))
        if si is not None:
            pakai_s.add(si)
    for si, scard in enumerate(spoken_cards):
        if si not in pakai_s:
            out.append((None, {}, scard))
    return out


def build_ntb_supplement_verification(evaluation, tms_row) -> list:
    """Baris tampilan NTB Supplement Verification: nama, jenis kelamin, dan hubungan tiap kartu tambahan
    (TMS ``*-supplektp-N-new``, ``jenis-kelamin-N-new``, ``hub-status-keluarga-N-new`` vs
    ``ntb_application_extraction.kartu_tambahan[]``), ditambah satu baris alamat pengiriman kartu tambahan
    (kartu tambahan dikirim ke alamat kartu utama, jadi mengikuti hasil baris alamat pengiriman kartu utama).
    Daftar kosong bila TMS maupun transkrip tidak memuat kartu tambahan. Tampilan saja: tidak mengubah
    status item scorecard, skor, maupun error code. Kolom Microsite (CSV per kolom TMS slot) menjadi sumber
    ketiga voting 2 dari 3 seperti Data Verification."""
    ev = evaluation if isinstance(evaluation, dict) else {}
    tms = tms_row or {}
    extraction = ev.get("ntb_application_extraction")
    status_by_code = {
        (it or {}).get("item_code"): (it or {}).get("status")
        for it in (ev.get("scorecard_result") or [])
    }
    tms_cards = []
    for n in range(1, SUPPLEMENT_SLOTS + 1):
        kartu = {key: _clean(tms.get(kolom.format(n=n))) for key, _l, kolom, _c, _i in _SUPPLEMENT_FIELDS}
        if any(kartu.values()):
            tms_cards.append((n, kartu))
    raw = extraction.get("kartu_tambahan") if isinstance(extraction, dict) else None
    spoken_cards = []
    for k in raw if isinstance(raw, list) else []:
        if isinstance(k, dict):
            kartu = {key: _extracted(k, key) for key, *_r in _SUPPLEMENT_FIELDS}
            if any(kartu.values()):
                spoken_cards.append(kartu)
    if not tms_cards and not spoken_cards:
        return []

    rows = []
    pasangan = _supplement_pairs(tms_cards, spoken_cards)
    for urut, (slot, tcard, scard) in enumerate(pasangan, start=1):
        for key, label, kolom, pembanding, code in _SUPPLEMENT_FIELDS:
            tms_value, extracted = tcard.get(key), scard.get(key)
            match, similarity, reason, tipe = compare_field(pembanding, tms_value, extracted)
            kolom_slot = kolom.format(n=slot) if slot else None
            catatan, source_value = None, None
            # Microsite = CSV per field (``microsite/<kolom TMS slot>.csv``, mis. ``jenis-kelamin-1-new``), sumber
            # ketiga voting seperti NTB Data Verification: valid bila minimal 2 sumber sepakat.
            micro = microsite_value(kolom_slot, tms.get("result_id")) if kolom_slot else None
            if micro is not None:
                if key == "nama":
                    match, similarity, reason = _nama_tiga_arah(tms_value, micro, extracted)
                    tipe, catatan = "nama_tiga_arah", None
                else:
                    match, similarity, reason = vote_field(pembanding, tms_value, micro, extracted)
                    catatan = _THRESHOLD_NOTES[tipe] + _VOTING_NOTE
                source_value = micro
            rows.append({
                "field": f"kartu_tambahan_{urut}_{key}",
                "card_no": urut,
                "tms_slot": slot,
                "label": f"Kartu tambahan {urut} - {label}",
                "tms_column": kolom.format(n=slot) if slot else kolom.format(n="N"),
                "tms_value": tms_value,
                "extracted_value": extracted,
                "source_value": source_value,
                "item_code": code,
                "item_status": status_by_code.get(code),
                "match": match,
                "similarity_percent": similarity,
                "reason": reason,
                "thresholds": catatan or _THRESHOLD_NOTES[tipe],
            })
    # Alamat pengiriman: kartu tambahan dikirim ke alamat kartu utama (script), jadi hasilnya sama
    # dengan baris "Alamat pengiriman kartu" di NTB Data Verification.
    utama = next((r for r in build_ntb_data_verification(ev, tms_row)
                  if r.get("tms_column") == "alamat-pengiriman-kartu"), None)
    if utama is not None:
        rows.append({
            "field": "kartu_tambahan_alamat_pengiriman",
            "card_no": None,
            "tms_slot": None,
            "label": "Alamat pengiriman kartu tambahan (sama dengan kartu utama)",
            "tms_column": "alamat-pengiriman-kartu",
            "tms_value": utama["tms_value"],
            "extracted_value": utama["extracted_value"],
            "source_value": None,
            "item_code": SUPPLEMENT_ALAMAT_ITEM,
            "item_status": status_by_code.get(SUPPLEMENT_ALAMAT_ITEM),
            "match": utama["match"],
            "similarity_percent": utama["similarity_percent"],
            "reason": f"Mengikuti alamat pengiriman kartu utama. {utama['reason'] or ''}".strip(),
            "thresholds": _THRESHOLD_NOTES["kategori"],
        })
    return rows


# NTB Phone Number Verification: nomor nasabah, keluarga dekat, dan Supplement 1-4 dibandingkan satu sama lain
# (TMS vs TMS) untuk mendeteksi nomor yang nyaris sama (kemungkinan dipermainkan). Pakai jarak edit ABSOLUT,
# bukan persentase Levenshtein (beda 1 digit di 12 digit sudah 91,7%, tetapi di nomor pendek jauh lebih kecil).
PHONE_MIRIP_JARAK = 2  # sementara: jarak Damerau-Levenshtein <= ini (tetapi bukan identik) = MIRIP
_PHONE_NAMA_SAMA = 90.0  # keluarga dekat = pemegang Supplement bila similarity nama >= ini (nomor identik wajar)


def _phone_digits(value):
    s = _clean(value)
    d = "".join(c for c in (s or "") if c.isdigit())
    if d.startswith("62"):
        d = "0" + d[2:]
    return d if d.strip("0") else None  # "0" / "." (placeholder TMS) = kosong


def _damerau(a, b):
    """Jarak Damerau-Levenshtein (sisip/hapus/ganti/tukar dua digit bersebelahan = 1)."""
    n, m = len(a), len(b)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[n][m]


def build_ntb_phone_verification(tms_row) -> list:
    """Baris tampilan NTB Phone Number Verification: satu baris per nomor (nasabah, keluarga dekat, Supplement 1-4)
    dengan nomor lain yang paling mirip. Hasil: ``AMAN``, ``MIRIP`` (jarak <= ``PHONE_MIRIP_JARAK``, tidak identik),
    ``IDENTIK`` (nomor sama dengan nomor lain), ``KOSONG``. Keluarga dekat = pemegang Supplement dengan nama sama
    (nomor identik wajar) tidak ditandai. Tampilan saja: tidak mengubah status item, skor, maupun error code.
    Daftar kosong bila tidak ada data TMS."""
    tms = tms_row or {}
    if not tms:
        return []
    fields = [
        ("nasabah", "Nomor handphone nasabah", "handphone", None),
        ("keluarga", "Nomor handphone keluarga dekat", "handphone-keluarga-dekat", _clean(tms.get("nama-keluarga-dekat"))),
    ] + [
        (f"supplement_{n}", f"Nomor handphone kartu tambahan {n}", f"mobile-phone-supple-{n}-new",
         _clean(tms.get(f"full-name-supplektp-{n}-new")))
        for n in range(1, SUPPLEMENT_SLOTS + 1)
    ]
    entries = [{"key": k, "label": lb, "column": col, "name": nm,
                "raw": _clean(tms.get(col)), "digits": _phone_digits(tms.get(col))}
               for k, lb, col, nm in fields]
    rows = []
    for e in entries:
        row = {"field": f"phone_{e['key']}", "label": e["label"], "tms_column": e["column"],
               "tms_value": e["raw"], "nearest_label": None, "nearest_value": None, "distance": None,
               "result": "KOSONG", "reason": "Nomor tidak ada di TMS.", "thresholds": _PHONE_THRESHOLD_NOTES}
        if e["digits"]:
            best = None
            for o in entries:
                if o is e or not o["digits"]:
                    continue
                jarak = _damerau(e["digits"], o["digits"])
                if best is None or jarak < best[0]:
                    best = (jarak, o)
            row["result"], row["reason"] = "AMAN", "Tidak ada nomor lain yang identik atau mirip."
            if best is not None:
                jarak, o = best
                row["nearest_label"], row["nearest_value"], row["distance"] = o["label"], o["raw"], jarak
                if jarak == 0:
                    sama_orang = (e["name"] and o["name"]
                                  and (similarity_nama(e["name"], o["name"]) or 0.0) >= _PHONE_NAMA_SAMA)
                    if sama_orang:
                        row["reason"] = f"Identik dengan {o['label'].lower()}, tetapi nama sama ({e['name']}): wajar."
                    else:
                        row["result"] = "IDENTIK"
                        row["reason"] = f"Nomor sama persis dengan {o['label'].lower()}."
                elif jarak <= PHONE_MIRIP_JARAK:
                    row["result"] = "MIRIP"
                    row["reason"] = f"Hanya berbeda {jarak} digit dari {o['label'].lower()}."
        rows.append(row)
    return rows


_PHONE_THRESHOLD_NOTES = [
    "Nomor dinormalisasi (hanya digit; awalan 62 menjadi 0); nilai kosong atau 0 dilewati",
    f"Jarak Damerau-Levenshtein absolut (bukan persen); jarak 1..{PHONE_MIRIP_JARAK} dan tidak identik → MIRIP",
    "Jarak 0 → IDENTIK, kecuali keluarga dekat dan pemegang Supplement bernama sama (wajar)",
    "Ambang sementara; tampilan saja, tidak memengaruhi skor atau status item",
]


# Kelompok item alamat gabungan: lulus/gagal concat komponen major berlaku untuk seluruh item kelompok.
_KELOMPOK_ALAMAT = {
    "SC_NTB_16": {"major": ("SC_NTB_16", "SC_NTB_20", "SC_NTB_21"), "minor": ("SC_NTB_17", "SC_NTB_18", "SC_NTB_19")},
    "SC_NTB_31": {"major": ("SC_NTB_30", "SC_NTB_31", "SC_NTB_33", "SC_NTB_34"), "minor": ("SC_NTB_32",)},
}


def _set_item(item, status, reason):
    item["status"] = status
    item["reason"] = reason
    w = item.get("weight")
    try:
        item["item_score"] = float(w) if status == "SESUAI" and w is not None else 0
        if status == "SESUAI" and item["item_score"] == int(item["item_score"]):
            item["item_score"] = int(item["item_score"])
    except (TypeError, ValueError):
        item["item_score"] = 0


def apply_verification_verdicts(evaluation, tms_row, ktp=None):
    """Status item scorecard NTB mengikuti Match/Mismatch Data Verification, seperti Cashline (12 Okt 2026).

    * ``MATCH`` -> ``SESUAI``; ``MISMATCH`` -> ``BELUM_SESUAI`` dengan alasan pembandingan di ``reason``.
    * ``SKIPPED_NULL`` (salah satu sisi kosong / tak dibandingkan) -> status dari LLM ("menanyakan atau tidak")
      dibiarkan.
    * Alamat rumah dan kantor (+ nama perusahaan): hasil concat komponen major berlaku untuk SEMUA item
      kelompok (major dan minor); ``quote`` evidence item major diganti hasil concat, evidence minor
      dibiarkan. Tanpa item di scorecard -> dilewati."""
    if not isinstance(evaluation, dict):
        return evaluation
    by_code = {(it or {}).get("item_code"): it for it in (evaluation.get("scorecard_result") or []) if it}
    if not by_code:
        return evaluation
    for row in build_ntb_data_verification(evaluation, tms_row, ktp):
        code, match = row.get("item_code"), row.get("match")
        if not code or match not in ("MATCH", "MISMATCH"):
            continue
        sim = row.get("similarity_percent")
        sim_txt = f" ({_fmt(sim)}%)" if sim is not None else ""
        ringkas = f"TMS: {row.get('tms_value') or '-'} | ucapan: {row.get('extracted_value') or '-'}"
        if row.get("source_value"):
            ringkas = f"TMS: {row.get('tms_value') or '-'} | Microsite: {row['source_value']}"
            if row.get("extracted_value"):
                ringkas += f" | ucapan: {row['extracted_value']}"
        status = "SESUAI" if match == "MATCH" else "BELUM_SESUAI"
        if code in _KELOMPOK_ALAMAT:
            grup = _KELOMPOK_ALAMAT[code]
            for kode in grup["major"] + grup["minor"]:
                it = by_code.get(kode)
                if it is None:
                    continue
                if match == "MATCH":
                    alasan = f"Alamat gabungan cocok dengan TMS{sim_txt}; item {'major' if kode in grup['major'] else 'minor'} ikut lulus."
                else:
                    alasan = f"Alamat gabungan tidak cocok dengan TMS{sim_txt}: {row.get('reason')}"
                _set_item(it, status, alasan)
                if kode in grup["major"]:
                    ev = it.get("evidence") if isinstance(it.get("evidence"), dict) else {}
                    it["evidence"] = {**ev, "quote": f"{ringkas} | similarity {_fmt(sim) if sim is not None else '-'}%"}
            continue
        it = by_code.get(code)
        if it is None or code == LAMA_BEKERJA_ITEM:
            continue
        label = "cocok dengan" if match == "MATCH" else "TIDAK cocok dengan"
        _set_item(it, status, f"{row.get('label')} {label} data TMS{sim_txt}. {row.get('reason') or ''} {ringkas}".strip())
    # Score bomb KTP: data yang SALAH menurut KTP (baris MISMATCH yang membawa nilai KTP hasil OCR).
    evaluation[NTB_KTP_BOMB_KEY] = [
        r["item_code"] for r in build_ntb_data_verification(evaluation, tms_row, ktp)
        if r.get("match") == "MISMATCH" and r.get("source_value") and r.get("item_code") in _KTP_ITEM_CODES]
    return evaluation


SUPPLEMENT_NAMA_ITEM = "SC_NTB_42"
SUPPLEMENT_NAMA_SAMA = 90.0  # similarity nama >= ini = orang yang sama


def _duplicate_names(names) -> list:
    """Pasangan nama yang sama (similarity >= ``SUPPLEMENT_NAMA_SAMA``) di antara ``names``."""
    pairs = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            sim = similarity_nama(a, b)
            if sim is not None and sim >= SUPPLEMENT_NAMA_SAMA:
                pairs.append((a, b))
    return pairs


def _apply_supplement_unique_names(evaluation, by_code, tms_row):
    """Nama kartu tambahan HARUS orang yang berbeda: nama yang sama dipakai untuk dua kartu
    tambahan atau lebih (di TMS ``full-name-supplektp-N-new`` ATAU di ucapan) membuat
    ``SC_NTB_42`` BELUM_SESUAI, walau tiap nama cocok dengan pasangannya. Item
    ``TIDAK_DINILAI`` (Supplement tidak diikuti) tidak dihidupkan kembali."""
    item = by_code.get(SUPPLEMENT_NAMA_ITEM)
    if item is None or str(item.get("status") or "").upper() in ("", "TIDAK_DINILAI"):
        return
    tms = tms_row or {}
    tms_names = [n for n in (_clean(tms.get(f"full-name-supplektp-{i}-new")) for i in range(1, SUPPLEMENT_SLOTS + 1)) if n]
    extraction = evaluation.get("ntb_application_extraction")
    raw = extraction.get("kartu_tambahan") if isinstance(extraction, dict) else None
    spoken_names = [n for n in (_extracted(k, "nama") for k in raw if isinstance(k, dict)) if n] if isinstance(raw, list) else []
    found = [("TMS", a, b) for a, b in _duplicate_names(tms_names)] + [
        ("ucapan", a, b) for a, b in _duplicate_names(spoken_names)]
    if found:
        rincian = "; ".join(f"{src}: {a} = {b}" for src, a, b in found)
        _set_item(item, "BELUM_SESUAI", f"Nama kartu tambahan tidak boleh sama antar kartu. {rincian}")


def apply_supplement_verdicts(evaluation, tms_row):
    """Status item Pengisian Data Supplement (``SC_NTB_42``..``44``) mengikuti NTB Supplement Verification,
    seperti Data Verification Basic (``apply_verification_verdicts``): dua arah, SEBELUM skor dihitung.

    * Ada kartu tambahan ``MISMATCH`` -> ``BELUM_SESUAI`` (alasan memuat kartu dan nilainya).
    * SEMUA kartu ``MATCH`` -> ``SESUAI``.
    * Selain itu (``SKIPPED_NULL`` pada sebagian kartu) -> status LLM dibiarkan.
    * Item ``TIDAK_DINILAI`` (Supplement tidak diikuti nasabah) TIDAK dihidupkan kembali.
    * ``SC_NTB_45`` (alamat kirim) sengaja tidak ditimpa: barisnya hanya mengulang hasil alamat pengiriman
      kartu utama (``SC_NTB_37``), bukan ucapan agent tentang kartu tambahan, jadi MATCH di situ tidak
      membuktikan agent menyampaikannya.
    Tanpa item di scorecard -> dilewati."""
    if not isinstance(evaluation, dict):
        return evaluation
    by_code = {(it or {}).get("item_code"): it for it in (evaluation.get("scorecard_result") or []) if it}
    if not by_code:
        return evaluation
    per_item = {}
    for row in build_ntb_supplement_verification(evaluation, tms_row):
        code = row.get("item_code")
        if code and code != SUPPLEMENT_ALAMAT_ITEM:
            per_item.setdefault(code, []).append(row)
    for code, rows in per_item.items():
        item = by_code.get(code)
        if item is None or str(item.get("status") or "").upper() in ("", "TIDAK_DINILAI"):
            continue
        salah = [r for r in rows if r.get("match") == "MISMATCH"]
        if salah:
            rincian = "; ".join(
                f"{r['label']}: TMS {r.get('tms_value') or '-'} | ucapan {r.get('extracted_value') or '-'}"
                + (f" ({_fmt(r['similarity_percent'])}%)" if r.get("similarity_percent") is not None else "")
                for r in salah)
            _set_item(item, "BELUM_SESUAI", f"Data kartu tambahan TIDAK cocok dengan data TMS. {rincian}")
        elif all(r.get("match") == "MATCH" for r in rows):
            _set_item(item, "SESUAI", "Data kartu tambahan cocok dengan data TMS: "
                      + "; ".join(f"{r['label']} {r.get('tms_value') or '-'}" for r in rows))
    _apply_supplement_unique_names(evaluation, by_code, tms_row)
    return evaluation


HP_KELUARGA_ITEM = "SC_NTB_25"


def apply_hp_keluarga_verdict(evaluation, tms_row):
    """Handphone keluarga dekat (``SC_NTB_25``): MISMATCH di Data Verification -> ``BELUM_SESUAI``.

    MISMATCH = beda lebih dari 1 digit antara ucapan dan TMS, ATAU nomor sama dengan handphone nasabah
    (di transkrip maupun TMS). Alasannya ditulis ke ``reason`` item. MATCH/SKIPPED -> status LLM
    dibiarkan. Tanpa item itu di scorecard -> tidak berbuat apa-apa."""
    if not isinstance(evaluation, dict):
        return evaluation
    item = next((it for it in (evaluation.get("scorecard_result") or [])
                 if (it or {}).get("item_code") == HP_KELUARGA_ITEM), None)
    if item is None:
        return evaluation
    row = next((r for r in build_ntb_data_verification(evaluation, tms_row)
                if r.get("tms_column") == "handphone-keluarga-dekat"), None)
    if row and row.get("match") == "MISMATCH":
        item["status"] = "BELUM_SESUAI"
        item["item_score"] = 0
        item["reason"] = (
            f"Handphone keluarga dekat tidak sesuai: {row.get('reason')} "
            f"(TMS: {row.get('tms_value') or '-'}, ucapan: {row.get('extracted_value') or '-'})")
    return evaluation


HANDPHONE_ITEM = "SC_NTB_22"


def _belum_dinilai(evaluation, tms_row, item_code, tms_column, alasan):
    """Item yang menunggu data Microsite/Microsite: baris Data Verification ``SKIPPED_NULL`` -> ``TIDAK_DINILAI``
    (tanpa potongan skor dan tanpa veto), apa pun status dari LLM. ``MATCH``/``MISMATCH`` tidak disentuh."""
    if not isinstance(evaluation, dict):
        return evaluation
    item = next((it for it in (evaluation.get("scorecard_result") or [])
                 if (it or {}).get("item_code") == item_code), None)
    if item is None:
        return evaluation
    row = next((r for r in build_ntb_data_verification(evaluation, tms_row)
                if r.get("tms_column") == tms_column), None)
    if row and row.get("match") == "SKIPPED_NULL":
        item["status"] = "TIDAK_DINILAI"
        item["item_score"] = 0
        item["reason"] = alasan
        item["point_of_improvement"] = None
    return evaluation


def apply_handphone_belum_dinilai(evaluation, tms_row):
    """Handphone nasabah (``SC_NTB_22``) TIDAK DINILAI selama Microsite handphone belum ada (keputusan pemilik, 27 Okt 2026).

    Nomor nasabah dibandingkan TMS vs Microsite, bukan dengan transkrip, jadi "agent menanyakan nomor?" dari LLM
    tidak boleh menentukan statusnya. ``MISMATCH`` (mis. nomor sama dengan keluarga dekat) dan ``MATCH`` tidak
    disentuh. Begitu pembanding Microsite ditulis, baris ini tidak lagi ``SKIPPED_NULL`` dan fungsi ini otomatis
    tidak berbuat apa-apa. Tanpa item itu di scorecard -> tidak berbuat apa-apa."""
    return _belum_dinilai(evaluation, tms_row, HANDPHONE_ITEM, "handphone",
                          "Nomor handphone nasabah dibandingkan TMS dengan Microsite (bukan transkrip); "
                          "Microsite handphone belum tersedia sehingga item belum dinilai.")


PERSETUJUAN_DATA_ITEM = "SC_NTB_40"


def apply_persetujuan_data_belum_dinilai(evaluation, tms_row):
    """Persetujuan sharing data ke mitra (``SC_NTB_40``) dicek dari Microsite (30 Okt 2026), bukan dari transkrip.

    Contoh data Microsite belum ada, jadi item ``TIDAK_DINILAI`` (tanpa potongan skor dan tanpa veto), apa pun
    penilaian LLM dari percakapan. Begitu pembanding Microsite ditulis, baris Data Verification tidak lagi
    ``SKIPPED_NULL`` dan fungsi ini otomatis tidak berbuat apa-apa."""
    return _belum_dinilai(evaluation, tms_row, PERSETUJUAN_DATA_ITEM, "persetujuan-data-nasabah",
                          "Persetujuan sharing data ke mitra dicek dari Microsite (bukan transkrip); "
                          "data Microsite belum tersedia sehingga item belum dinilai.")


PDP_SUPPLEMENT_ITEM = "SC_NTB_46"


def apply_pdp_supplement_belum_dinilai(evaluation):
    """PDP kartu tambahan (``SC_NTB_46``) dicek dari Microsite (keputusan user 3 Nov 2026), bukan dari transkrip.

    Contoh data Microsite untuk Supplement belum ada, jadi item ``TIDAK_DINILAI`` (tanpa potongan skor, tanpa veto,
    tanpa bom PDP), apa pun penilaian LLM dari percakapan. Hapus fungsi ini saat pembaca Microsite ditulis.
    Tanpa item itu di scorecard -> tidak berbuat apa-apa."""
    if not isinstance(evaluation, dict):
        return evaluation
    item = next((it for it in (evaluation.get("scorecard_result") or [])
                 if (it or {}).get("item_code") == PDP_SUPPLEMENT_ITEM), None)
    if item is None:
        return evaluation
    if str(item.get("status") or "").upper() != "TIDAK_DINILAI":
        item["status"] = "TIDAK_DINILAI"
        item["item_score"] = 0
        item["reason"] = ("PDP kartu tambahan dicek dari Microsite (bukan transkrip); "
                          "data Microsite belum tersedia sehingga item belum dinilai.")
        item["point_of_improvement"] = None
    return evaluation


TELEPON_KANTOR_ITEM = "SC_NTB_35"


def apply_telepon_kantor_verdict(evaluation, tms_row):
    """Telepon kantor (``SC_NTB_35``) diputuskan SEBELUM skor dihitung.

    * TMS maupun ucapan berawalan +628/628/08xx (seluler) -> ``BELUM_SESUAI`` (tidak diizinkan; item
      ``tolerable = NO`` jadi memveto tiket), apa pun status dari LLM.
    * Kosong di KEDUA sisi -> boleh dan tetap DINILAI: status ``SESUAI`` dengan skor penuh (bila LLM menilai
      ``BELUM_SESUAI`` atau ``TIDAK_DINILAI``).
    * Selain itu status LLM dibiarkan. Tanpa item itu di scorecard -> tidak berbuat apa-apa."""
    if not isinstance(evaluation, dict):
        return evaluation
    item = next((it for it in (evaluation.get("scorecard_result") or [])
                 if (it or {}).get("item_code") == TELEPON_KANTOR_ITEM), None)
    if item is None:
        return evaluation
    tms_value = _clean((tms_row or {}).get("telepon-kantor"))
    spoken = _extracted(evaluation.get("ntb_application_extraction"), "telepon_kantor")
    salah = [nama for nama, v in (("TMS", tms_value), ("ucapan", spoken)) if is_nomor_seluler(v)]
    if salah:
        item["status"] = "BELUM_SESUAI"
        item["item_score"] = 0
        item["reason"] = f"Telepon kantor berformat nomor seluler (+628/08xx) pada {' dan '.join(salah)}: tidak diperbolehkan."
    elif tms_value is None and spoken is None and str(item.get("status") or "").upper() != "SESUAI":
        _set_item(item, "SESUAI", "Telepon kantor tidak ada di TMS maupun ucapan: boleh kosong, dinilai SESUAI.")
    return evaluation


LAMA_BEKERJA_ITEM = "SC_NTB_76"  # tanpa bobot, non-tolerable (scorecard v2 sesudah 12 Okt 2026)


def apply_lama_bekerja_verdict(evaluation, tms_row):
    """Tandai ``SC_NTB_76`` (Lama bekerja) dari perbandingan TMS vs ucapan, SEBELUM skor dihitung.

    Item tanpa bobot tetapi ``tolerable = NO``: salah (MISMATCH) -> ``BELUM_SESUAI``, yang
    memveto tiket (RETURN). MATCH -> ``SESUAI``. SKIPPED (TMS kosong / tak disebut / tak terbaca)
    -> status dari LLM dibiarkan. Tidak ada item itu di scorecard (versi lama) -> tidak berbuat apa-apa."""
    if not isinstance(evaluation, dict):
        return evaluation
    items = evaluation.get("scorecard_result") or []
    item = next((it for it in items if (it or {}).get("item_code") == LAMA_BEKERJA_ITEM), None)
    if item is None:
        return evaluation
    tms_value = _clean((tms_row or {}).get("lama-bekerjausaha"))
    spoken = _extracted(evaluation.get("ntb_application_extraction"), "lama_bekerja")
    match, _sim, reason, _tipe = compare_field("lama-bekerjausaha", tms_value, spoken)
    if match == "MISMATCH":
        item["status"] = "BELUM_SESUAI"
        item["item_score"] = 0
        item["reason"] = f"Lama bekerja tidak sama dengan data TMS. {reason}"
    elif match == "MATCH":
        item["status"] = "SESUAI"
        item["reason"] = f"Lama bekerja sama dengan data TMS. {reason}"
    return evaluation


def ensure_scorecard_items(evaluation, scorecard_text):
    """Tambahkan item scorecard yang TIDAK dikembalikan LLM sebagai ``TIDAK_DINILAI`` (12 Okt 2026).

    LLM kadang menghilangkan item (mis. telepon kantor yang "boleh tidak ada"), padahal aturan sistem
    (Match/Mismatch, telepon kantor kosong, dst.) perlu barisnya untuk menimpa status. ``TIDAK_DINILAI``
    tidak mengurangi skor dan tidak memveto, jadi menambahkannya tidak mengubah tiket sebelum aturan
    sistem dijalankan. Teks scorecard tak terbaca -> tidak berbuat apa-apa."""
    import json as _json
    if not isinstance(evaluation, dict):
        return evaluation
    try:
        master = _json.loads(scorecard_text or "[]")
    except (TypeError, ValueError):
        return evaluation
    if not isinstance(master, list):
        return evaluation
    rows = evaluation.setdefault("scorecard_result", [])
    ada = {(r or {}).get("item_code") for r in rows}
    for it in master:
        code = (it or {}).get("item_code") if isinstance(it, dict) else None
        if not code or code in ada:
            continue
        rows.append({
            "category": it.get("category"), "item_code": code, "requirement": it.get("requirement"),
            "kb_reference": it.get("kb_reference"), "weight": it.get("weight"), "tolerable": it.get("tolerable"),
            "status": "TIDAK_DINILAI", "item_score": 0, "evidence": {},
            "reason": "Item tidak dikembalikan oleh penilaian; ditambahkan otomatis sebagai TIDAK_DINILAI.",
        })
    return evaluation


def _join(values):
    parts = [v for v in values if v]
    return " ".join(parts) if parts else None


def _ibu_rumah_tangga(extraction, tms):
    """True bila pekerjaan (transkrip atau TMS) ibu rumah tangga: alamat kantor = alamat rumah."""
    for v in (_extracted(extraction, "pekerjaan"), _clean(tms.get("pekerjaan"))):
        if v and re.search(r"\b(irt|ibu rumah tangga|rumah tangga)\b", str(v), re.I):
            return True
    return False


def _alamat_tokens(text):
    """Token alamat untuk perbandingan gabungan. Kata angka dijadikan digit ("tujuh tiga" -> "73",
    "enam belas" -> "16", "dua puluh" -> "20"), lalu huruf tunggal + angka digabung ("G 121" -> "g121")."""
    toks = [_ALAMAT_SINGKATAN.get(t, t) for t in _tokens(text)]
    out = []
    for t in toks:
        if t in _NUMBER_WORDS:
            out.append(str(_NUMBER_WORDS[t]))
        elif t == "belas" and out and out[-1].isdigit():
            out[-1] = str(10 + int(out[-1]))
        elif t == "puluh" and out and out[-1].isdigit():
            out[-1] = str(int(out[-1]) * 10)
        else:
            out.append(t)
    merged = []
    for t in out:
        prev = merged[-1] if merged else None
        if prev and t.isdigit() and prev.isdigit() and len(t) == 1 and (len(prev) == 1 or prev.endswith("0") and len(prev) == 2):
            merged[-1] = prev[:-1] + t if prev.endswith("0") and len(prev) == 2 else prev + t
        else:
            merged.append(t)
    final = []
    for t in merged:
        if final and len(final[-1]) == 1 and final[-1].isalpha() and t.isdigit():
            final[-1] += t
        else:
            final.append(t)
    # Nol depan angka murni dibuang: RT/RW "02" (TMS) = "2" (ucapan).
    return [(x.lstrip("0") or "0") if x.isdigit() and len(x) > 1 and x.startswith("0") else x for x in final]


def _alamat_similarity(ref, spoken):
    """Similarity (0-100) alamat ``ref`` terhadap ucapan ``spoken``: yang tertinggi antara kemiripan huruf
    dan recall token ``ref`` di ``spoken`` (spasi diabaikan). ``None`` bila salah satu kosong."""
    ta, tb = _alamat_tokens(ref), _alamat_tokens(spoken)
    if not ta or not tb:
        return None
    letter = _ratio(" ".join(ta), " ".join(tb))
    rapat = "".join(tb)
    recall = sum(1 for t in set(ta) if t in rapat) / len(set(ta)) * 100
    return max(v for v in (letter, recall) if v is not None)


# Kata pengisi di ucapan alamat tujuan ("Ke Majalaya aja") yang dibuang sebelum uji subset.
_TUJUAN_PENGISI = {"ke", "aja", "saja", "ya", "iya", "di", "dikirim", "kirim", "kirimkan", "alamat", "yang", "nya", "dong",
                   "deh", "sih", "pak", "bu", "ibu", "bapak", "tolong", "untuk", "sama", "dengan", "itu", "tadi", "tuh",
                   "kartu", "kartunya", "lewat", "via"}


def _alamat_subset(spoken, refs):
    """True bila SEMUA token bermakna ucapan (sesudah singkatan dinormalkan dan kata pengisi dibuang)
    termuat di token ``refs`` (daftar alamat), mis. "Ke Majalaya aja" di dalam alamat rumah yang memuat
    kecamatan Majalaya. Ucapan tanpa token huruf bermakna -> False."""
    toks = [t for t in _alamat_tokens(spoken) if t not in _TUJUAN_PENGISI]
    if not any(t.isalpha() and len(t) >= 3 for t in toks):
        return False
    pool = {t for r in refs if r for t in _alamat_tokens(r)}
    return all(t in pool for t in toks)


def _alamat_tujuan_dari_ucapan(spoken, extraction, tms, prefer=None):
    """Ucapan berupa ALAMAT LENGKAP untuk kolom kode tujuan (pengiriman kartu / penagihan): "H" bila
    sama dengan alamat rumah, "C" bila sama dengan alamat kantor. Pembanding = alamat rumah/kantor
    di TMS dan di ucapan (major saja). Mengembalikan ``(kode | None, similarity | None)``."""
    def gabung(daftar, sumber):
        return _join([sumber(k) for k in daftar])
    rumah = [gabung([c for c, _k in _ALAMAT_RUMAH_MAJOR], lambda c: _clean(tms.get(c))),
             gabung([k for _c, k in _ALAMAT_RUMAH_MAJOR], lambda k: _extracted(extraction, k))]
    kantor = [gabung([c for c, _k in _ALAMAT_KANTOR_MAJOR[1:]], lambda c: _clean(tms.get(c))),
              gabung([k for _c, k in _ALAMAT_KANTOR_MAJOR[1:]], lambda k: _extracted(extraction, k))]
    # Ucapan yang hanya sebagian (subset) dari alamat rumah/kantor, mis. "Majalaya" = kecamatan rumah:
    # dianggap alamat itu (100%). Bila subset keduanya, kode yang diminta TMS (``prefer``) dipilih.
    subset = [k for k, refs in (("H", rumah), ("C", kantor)) if _alamat_subset(spoken, refs)]
    if subset:
        return (prefer if prefer in subset else subset[0]), 100.0
    best = {}
    for kode, refs in (("H", rumah), ("C", kantor)):
        sims = [v for v in (_alamat_similarity(r, spoken) for r in refs if r) if v is not None]
        if sims:
            best[kode] = max(sims)
    if not best:
        return None, None
    kode = max(best, key=best.get)
    return (kode, best[kode]) if best[kode] >= ALAMAT_TUJUAN_AMBANG else (None, best[kode])


def _alamat_row(marker, label, code, extraction, tms, status_by_code, ktp=None):
    """Satu baris alamat gabungan (rumah atau kantor+nama perusahaan).

    Perbandingan dari SEMUA komponen (major dan minor: alamat baris 1 dan 2, RT, RW, kelurahan,
    kecamatan; kantor + nama perusahaan), TMS di-concat vs ucapan di-concat, nol depan angka diabaikan.
    Lulus (similarity >= 60%) -> bobot major + minor dihitung penuh; gagal -> gagal semua. Ibu rumah tangga: alamat kantor TMS dibandingkan dengan
    alamat rumah di ucapan. Tampilan saja; status item tidak diubah."""
    rumah = marker == ALAMAT_RUMAH
    banding = _ALAMAT_RUMAH_BANDING if rumah else _ALAMAT_KANTOR_BANDING
    irt = (not rumah) and _ibu_rumah_tangga(extraction, tms)
    irt0 = irt
    ktp_alamat = None
    tms_value = _join([_clean(tms.get(col)) for col, _k in banding])
    spoken_parts = _ALAMAT_RUMAH_BANDING if (rumah or irt) else _ALAMAT_KANTOR_BANDING
    extracted = _join([_extracted(extraction, k) for _c, k in spoken_parts if k])
    if tms_value is None and irt:
        # Ibu rumah tangga tanpa alamat kantor di TMS: alamat kantor dianggap sama dengan alamat rumah.
        match, sim, reason = "MATCH", 100.0, "Ibu rumah tangga: alamat kantor tidak ada di TMS, dianggap sama dengan alamat rumah."
        irt = False
    elif tms_value is None:
        match, sim, reason = "SKIPPED_NULL", None, "Nilai TMS kosong."
    elif extracted is None:
        match, sim, reason = "SKIPPED_NULL", None, "Tidak disebut di transkrip."
    elif _extracted(extraction, "alamat_rumah" if (rumah or irt0) else "alamat_kantor") is None:
        # Alamat baris 1 tidak terekstrak (mis. hanya nama perusahaan/kelurahan): perbandingan parsial akan
        # menggagalkan item secara keliru, jadi dianggap tidak terbaca dan status LLM dipakai.
        match, sim, reason = "SKIPPED_NULL", None, "Alamat (baris 1) tidak terekstrak dari transkrip; tidak dibandingkan."
    else:
        def _sim(x, y):
            ta, tb = _alamat_tokens(x), _alamat_tokens(y)
            letter = _ratio(" ".join(ta), " ".join(tb))
            # Spasi diabaikan: "KALIBARU" (TMS) = "Kali Baru" (ucapan). Recall per token dihitung pada
            # string rapat, jadi token TMS yang terpecah di ucapan tetap terhitung.
            rapat = "".join(tb)
            recall = (sum(1 for t in set(ta) if t in rapat) / len(set(ta)) * 100) if ta else None
            cands = [v for v in (letter, recall) if v is not None]
            return max(cands) if cands else None

        sim = _sim(tms_value, extracted)
        if rumah and ktp:
            ktp_alamat = _join([ktp.get(k) for k in ("alamat", "rt", "rw", "kelurahan", "kecamatan")]) if ktp.get("alamat") else None
        if sim is None:
            match, reason = "SKIPPED_NULL", "Kedua sisi kosong sesudah normalisasi."
        elif ktp_alamat:
            # Voting 3 sumber (KTP/Microsite, TMS, transkrip): valid bila minimal 2 sumber sepakat.
            pair = [("TMS vs transkrip", sim), ("KTP vs TMS", _sim(ktp_alamat, tms_value)), ("KTP vs transkrip", _sim(ktp_alamat, extracted))]
            pair = [(l, x) for l, x in pair if x is not None]
            ok = any(x >= ALAMAT_AMBANG for _, x in pair)
            match = "MATCH" if ok else "MISMATCH"
            sim = max(x for _, x in pair)
            reason = (f"{'Minimal 2 sumber sepakat' if ok else 'Tidak ada 2 sumber yang sepakat'} (ambang {int(ALAMAT_AMBANG)}%): "
                      + "; ".join(f"{l} {_fmt(x)}%" for l, x in pair) + ".")
            sim = float(round(sim))
        else:
            ok = sim >= ALAMAT_AMBANG
            match = "MATCH" if ok else "MISMATCH"
            bobot = "bobot penuh (major + minor)" if ok else "gagal semua (major + minor)"
            reason = f"Similarity {_fmt(sim)}% {'≥' if ok else '<'} ambang {int(ALAMAT_AMBANG)}%: {bobot}."
            sim = float(round(sim))
    if irt:
        reason += " Ibu rumah tangga: alamat kantor TMS dibandingkan dengan alamat rumah."
    return {
        "field": "alamat_rumah" if rumah else "alamat_kantor",
        "label": label,
        "tms_column": "+".join(c for c, _k in banding),
        "tms_value": tms_value,
        "extracted_value": extracted,
        "item_code": code,
        "item_status": status_by_code.get(code),
        "match": match,
        "similarity_percent": sim,
        "reason": reason,
        "source_value": ktp_alamat if rumah else None,
        "thresholds": _THRESHOLD_NOTES["alamat_gabungan_rumah" if rumah else "alamat_gabungan_kantor"] + (_VOTING_NOTE if ktp_alamat else []),
    }


def _interest_row(field, label, column_label, tms_value, tms_interested, status, ket_ya, ket_tidak, tms_row, thresholds=None):
    """Satu baris Campaign Interest Verification: TMS menunjukkan tertarik atau tidak, dibandingkan
    dengan status minat dari transkrip (INTERESTED / bukan). Baris TMS tidak ada -> SKIPPED_NULL."""
    interested = str(status or "").upper() == "INTERESTED"
    extracted = "INTERESTED" if interested else (str(status).upper() if status else "NOT_STATED")
    if tms_row is None:
        match, sim, reason = "SKIPPED_NULL", None, "Baris TMS NTB tidak ditemukan."
    else:
        ok = tms_interested == interested
        match, sim = ("MATCH" if ok else "MISMATCH"), (100.0 if ok else 0.0)
        reason = (f"TMS dan transkrip sama-sama menunjukkan nasabah {ket_ya if tms_interested else ket_tidak}."
                  if ok else
                  f"TMS menunjukkan nasabah {ket_ya if tms_interested else ket_tidak}, transkrip {extracted}.")
    return {
        "field": field, "label": label, "tms_column": column_label,
        "tms_value": tms_value, "extracted_value": extracted, "item_code": None, "item_status": None,
        "match": match, "similarity_percent": sim, "reason": reason, "thresholds": thresholds or _THRESHOLD_NOTES["interest"],
    }


SOURCE_CODE_BASIC = "TPIREGRFCNET"      # Pengajuan Basic Only
SOURCE_CODE_BUNDLING = "TPIBDLREGNET"    # Pengajuan Basic Bundling Supplement
SUPPLEMENT_CHK_COLUMNS = tuple(f"supplement-{i}-chk" for i in range(1, SUPPLEMENT_SLOTS + 1))


def _source_code_interest_row(tms_row, supp_interested):
    """Source code TMS dibandingkan dengan minat Supplement di TMS (kolom ``supplement-1..4-chk``):
    ada kartu tambahan -> ``TPIBDLREGNET`` (Basic Bundling Supplement), tidak ada -> ``TPIREGRFCNET`` (Basic Only).
    Tampilan saja; SC_NTB_39 tetap mengikuti baris Data Verification (kode ada di daftar Microsite)."""
    code = _clean((tms_row or {}).get("source-code"))
    expected = SOURCE_CODE_BUNDLING if supp_interested else SOURCE_CODE_BASIC
    if tms_row is None:
        match, sim, reason = "SKIPPED_NULL", None, "Baris TMS NTB tidak ditemukan."
    elif code is None:
        match, sim, reason = "SKIPPED_NULL", None, "Source code TMS kosong."
    else:
        ok = code.upper() == expected
        match, sim = ("MATCH" if ok else "MISMATCH"), (100.0 if ok else 0.0)
        reason = (f"Source code {code} sesuai minat TMS ({'ada' if supp_interested else 'tanpa'} kartu tambahan)." if ok else
                  f"Minat TMS {'ada' if supp_interested else 'tanpa'} kartu tambahan seharusnya {expected}, source code TMS {code}.")
    return {
        "field": "ntb_source_code", "label": "Source code vs minat Supplement", "tms_column": "source-code",
        "tms_value": code, "extracted_value": None, "item_code": None, "item_status": None,
        "match": match, "similarity_percent": sim, "reason": reason, "thresholds": _THRESHOLD_NOTES["source_code_minat"],
    }


def build_ntb_campaign_interest_verification(evaluation, tms_row) -> list:
    """Campaign Interest Verification NTB (seperti Cashline), satu baris per campaign:
    NTB (kartu utama), Supplement, dan MUS. TMS dibandingkan dengan status minat dari transkrip.
    Tampilan saja."""
    ev = evaluation if isinstance(evaluation, dict) else {}
    tms = tms_row or {}
    kartu = _clean(tms.get("kartu-kredit-yang-dikehendaki"))
    # Minat Supplement di TMS = salah satu kolom supplement-1..4-chk terisi (tidak ada kolom identifier khusus).
    supp_chk = [f"{c}: {v}" for c in SUPPLEMENT_CHK_COLUMNS if (v := _clean(tms.get(c)))]
    mus = _clean(tms.get("pendaftaran-credit-shield"))
    return [
        _interest_row("ntb_interest", "NTB (kartu utama)", "kartu-kredit-yang-dikehendaki", kartu,
                      bool(kartu), (ev.get("ntb_interest") or {}).get("status"),
                      "mengajukan kartu utama", "tidak mengajukan kartu utama", tms_row),
        _interest_row("ntb_supplement_interest", "Supplement (kartu tambahan)", "supplement-1..4-chk",
                      (", ".join(supp_chk) if supp_chk else None), bool(supp_chk),
                      (ev.get("ntb_supplement_interest") or {}).get("status"),
                      "mengajukan kartu tambahan", "tidak mengajukan kartu tambahan", tms_row,
                      _THRESHOLD_NOTES["interest_supplement"]),
        _interest_row("ntb_mus_interest", "MUS (4-ULTIMA)", "pendaftaran-credit-shield", mus,
                      bool(mus and "4-ultima" in mus.casefold()), (ev.get("ntb_mus_interest") or {}).get("status"),
                      "tertarik MUS", "tidak tertarik MUS", tms_row),
        _source_code_interest_row(tms_row, bool(supp_chk)),
    ]


def _mus_interest_row(ev, tms_row):
    """Interest MUS: ``pendaftaran-credit-shield`` TMS (memuat "4-ULTIMA" = tertarik MUS)
    dibandingkan dengan ``ntb_mus_interest`` dari transkrip, seperti Campaign Interest
    Verification Cashline untuk Mega Ultima Shield."""
    column = "pendaftaran-credit-shield"
    tms_value = _clean((tms_row or {}).get(column))
    interested = str(((ev.get("ntb_mus_interest") or {}).get("status")) or "").upper() == "INTERESTED"
    extracted = "INTERESTED" if interested else "NOT_INTERESTED"
    if tms_row is None:
        match, sim, reason = "SKIPPED_NULL", None, "Baris TMS NTB tidak ditemukan."
    else:
        ref = bool(tms_value and "4-ultima" in tms_value.casefold())
        ok = ref == interested
        match, sim = ("MATCH" if ok else "MISMATCH"), (100.0 if ok else 0.0)
        reason = ("TMS dan transkrip sama-sama menunjukkan nasabah " + ("tertarik" if ref else "tidak tertarik") + " MUS."
                  if ok else
                  f"TMS menunjukkan {'tertarik' if ref else 'tidak tertarik'} MUS, transkrip {'INTERESTED' if interested else 'NOT_INTERESTED'}.")
    return {
        "field": "ntb_mus_interest", "label": "Interest MUS (4-ULTIMA)", "tms_column": column,
        "tms_value": tms_value, "extracted_value": extracted, "item_code": None, "item_status": None,
        "match": match, "similarity_percent": sim, "reason": reason, "thresholds": _THRESHOLD_NOTES["interest"],
    }


# ---------------------------------------------------------------------------
# Match & similarity per tipe field
# ---------------------------------------------------------------------------
import re

from compliance.static_similarity import (
    _ratio,
    levenshtein,
    similarity_nama,
    similarity_tanggal_lahir,
)

THRESHOLD = 80.0  # ambang umum, sama dengan GENERIC_THRESHOLD di dashboard

# kunci field (``NTB_FIELDS`` kolom TMS) -> tipe pembanding
_TYPE = {
    "nama-lengkap-sesuai-ktp": "nama",
    "nama-gadis-ibu-kandung": "nama",
    "nama-keluarga-dekat": "nama",
    "tanggal-lahir": "tanggal",
    "no-ktpkitas": "digit",
    "handphone": "digit",
    "email-address": "email",
    "handphone-keluarga-dekat": "digit_1",
    "lama-bekerjausaha": "lama_kerja",
    "pendaftaran-credit-shield": "interest",
    "bidang-usaha": "bidang_usaha",
    "pekerjaan": "pekerjaan",
    "telepon-kantor": "digit",
    "rumah-rt-new": "angka",
    "rumah-rw-new": "angka",
    "jenis-kelamin": "kategori",
    "status-perkawinan": "kategori",
    "kewarganegaraan": "kategori",
    "estatement": "kategori",
    "persetujuan-data-nasabah": "microsite_menyusul",
    "penghasilan-kotor-perbulan": "penghasilan",
    "alamat-pengiriman-kartu": "kategori",
    "alamat-tagihan-bulanan": "kategori",
    "source-code": "source_data",
    "kartu-kredit-yang-dikehendaki": "jenis_kartu",
    "hub-status-keluarga": "hubungan",
}  # selain di atas: "teks"

_THRESHOLD_NOTES = {
    "nama": ["≥ 80% → MATCH", "< 80% → MISMATCH", "Nama dibandingkan dengan normalisasi yang sama seperti verifikasi statik Cashline"],
    "tanggal": ["≥ 80% → MATCH", "< 80% → MISMATCH", "Dibandingkan sebagai DDMMYYYY"],
    "digit": ["Digit sama persis (sesudah nol depan / awalan 62 dibuang) → MATCH", "Beda 1 digit pun → MISMATCH, berapa pun similarity-nya", "Telepon kantor: kosong → SKIPPED_NULL; berawalan +628 / 08xx (seluler) → MISMATCH apa pun hasilnya"],
    "digit_1": ["Digit sama persis, ATAU beda maksimal 1 digit (sesudah nol depan / awalan 62 dibuang) → MATCH", "Beda lebih dari 1 digit → MISMATCH", "Nomor yang sama dengan handphone nasabah → MISMATCH (nomor keluarga dekat tidak boleh sama dengan nomor nasabah)"],
    "lama_kerja": ["TMS dibaca YYMM (306 = 3 tahun 6 bulan, 600 = 6 tahun, 3000 = 30 tahun); total bulan sama dengan ucapan → MATCH", "Beda → MISMATCH"],
    "gender_nik": ["Jenis kelamin TMS dibandingkan dengan hitungan dari NIK digit 7-12 (DDMMYY): tanggal lahir + 40 → perempuan, selain itu laki-laki", "Beda → MISMATCH"],
    "interest": ["TMS (`pendaftaran-credit-shield`) memuat \"4-ULTIMA\" ⇔ transkrip menunjukkan nasabah INTERESTED MUS → MATCH", "Berbeda → MISMATCH", "Seperti Campaign Interest Verification Cashline (Mega Ultima Shield)"],
    "bidang_usaha": ["≥ 80% → MATCH", "< 80% → MISMATCH", "Kata umum \"sektor\", \"bidang\", \"usaha\" diabaikan sebelum dibandingkan (\"Jasa\" = \"SEKTOR JASA\")"],
    "pekerjaan": ["Pekerjaan sepadan → MATCH (100%): \"Karyawan\" = \"KARYAWAN SWASTA\", \"wirausaha\" = \"wiraswasta\", TMS \"A/B\" cukup salah satu; satu sisi memuat sisi lain juga MATCH", "Beda → MISMATCH (mis. karyawan BUMN vs karyawan swasta)"],
    "email": ["Email dinormalkan dulu: \"at\" = @, \"dot\"/\"titik\" = ., \"underscore\" = _, \"dash\"/\"strip\" = -, spasi dan huruf besar diabaikan (\"Hidayat Rizki75 at gmail dot com\" = hidayatrizki75@gmail.com)", "Angka pada email harus sama persis; alamat utuh dinilai sebagai satu entitas; similarity ≥ 90% → MATCH (salah eja huruf ringan dari ASR, mis. rizky/rizki, masih lolos)", "Similarity < 90% → MISMATCH"],
    "angka": ["Angka sama (nol depan diabaikan) → MATCH", "Beda → MISMATCH"],
    "teks": ["≥ 80% → MATCH", "< 80% → MISMATCH", "Similarity tertinggi antara kemiripan huruf dan kecocokan kata"],
    "kategori": ["Sepadan sesudah disamakan (mis. F = Perempuan, kawin = menikah, Y = YA; kode alamat H = rumah, C = kantor, E = email) → MATCH", "Beda → MISMATCH", "Alamat pengiriman/penagihan yang disebut sebagian saja dari alamat rumah/kantor (mis. kecamatan \"Majalaya\") dianggap alamat itu: TMS H + bagian alamat rumah → MATCH"],
    "penghasilan": ["TMS di dalam rentang yang disebut → MATCH (100%)", "Nilai tunggal → rasio nilai kecil/besar, ambang 80%"],
    "alamat_gabungan_rumah": ["Semua komponen dibandingkan: alamat baris 1 dan 2, RT, RW, kelurahan, kecamatan (TMS di-concat vs ucapan; nol depan RT/RW diabaikan)", "Similarity ≥ 60% → MATCH: bobot item gabungan penuh (major + minor: alamat baris 2, RT, RW)", "< 60% → MISMATCH: gagal semua", "Item minor ikut lulus/gagal bersama major"],
    "alamat_gabungan_kantor": ["Semua komponen dibandingkan: nama perusahaan, alamat baris 1 dan 2, kelurahan, kecamatan (TMS di-concat vs ucapan)", "Similarity ≥ 60% → MATCH: bobot item gabungan penuh (major + minor: alamat baris 2)", "< 60% → MISMATCH: gagal semua", "Ibu rumah tangga: alamat kantor TMS dibandingkan dengan alamat rumah di ucapan"],
    "estatement_tagihan": ["E-statement mengikuti alamat penagihan bulanan TMS: kode E → e-statement harus Y; selain E → harus N", "TMS vs TMS, tidak dibandingkan dengan transkrip; sesuai → MATCH, tidak sesuai → MISMATCH", "Salah satu kosong → SKIPPED_NULL"],
    "tagihan_tanpa_ucapan": ["Alamat penagihan tidak disebut di transkrip: cukup dicek terhadap e-statement TMS", "Kode E ⇔ e-statement Y (selain E ⇔ N) → MATCH; tidak sesuai → MISMATCH"],
    "nama_tiga_arah": ["Nama KTP: voting antara Microsite (OCR KTP), TMS, dan transkrip, memakai fungsi similarity nama yang sama", "Valid (MATCH) bila minimal 2 dari 3 sumber sepakat: ada pasangan (Microsite vs TMS, TMS vs transkrip, Microsite vs transkrip) dengan similarity ≥ 80%", "Sumber yang kosong dilewati: hanya 2 sumber terisi = 1 perbandingan yang harus cocok", "Tidak ada pasangan cocok → MISMATCH; kurang dari 2 sumber terisi → SKIPPED_NULL"],
    "source_data_menyusul": ["Handphone nasabah dibandingkan TMS vs Microsite, tidak dengan transkrip", "Microsite belum tersedia → SKIPPED_NULL, status item mengikuti penilaian LLM"],
    "source_data": ["Field diisi sistem, tidak diucapkan agent: TMS dibandingkan dengan Microsite (daftar nilai dari Bank Mega), bukan dengan transkrip", "Nilai TMS ada di Microsite → MATCH (100%); tidak ada → MISMATCH", "TMS kosong → SKIPPED_NULL, status item mengikuti penilaian LLM"],
    "interest_supplement": ["Minat Supplement di TMS: salah satu kolom supplement-1-chk .. supplement-4-chk terisi → tertarik", "Dibandingkan dengan minat Supplement dari transkrip (INTERESTED / bukan)", "Sama → MATCH; berbeda → MISMATCH"],
    "source_code_minat": ["Source code TMS dibandingkan dengan minat Supplement di TMS (kolom supplement-1..4-chk)", "Ada kartu tambahan → TPIBDLREGNET (Basic Bundling Supplement); tanpa kartu tambahan → TPIREGRFCNET (Basic Only)", "Sesuai → MATCH; tidak sesuai → MISMATCH; source code kosong → SKIPPED_NULL", "Hanya perbandingan TMS vs TMS; tidak memengaruhi status SC_NTB_39"],
    "tidak_diucapkan": ["Field diisi sistem, tidak diucapkan agent → tidak dibandingkan"],
    "microsite_menyusul": ["Persetujuan sharing data ke mitra dicek dari Microsite (persetujuan yang diberikan nasabah di microsite), bukan dari transkrip", "Contoh data Microsite belum tersedia → SKIPPED_NULL, item belum dinilai (TIDAK_DINILAI, tanpa potongan skor dan tanpa veto)"],
    "jenis_kartu": ["Jenis kartu TMS tidak disebut di transkrip → MATCH (TMS sudah valid)", "Hanya disebut \"kartu kredit\" tanpa nama jenis → MATCH (dianggap sesuai TMS)", "Nama jenis disebut (mis. Visa Metro, Gold): semua kata nama harus ada di jenis kartu TMS (salah eja ringan ≥ 80% per kata) → MATCH; selain itu MISMATCH", "TMS kosong → SKIPPED_NULL"],
    "hubungan": ["Disamakan ke kelompok: istri, suami, anak, saudara (kakak/adik, laki-laki/perempuan tidak dibedakan), orang tua (ibu/bapak/ayah/mama/papa) → kelompok sama = MATCH", "Kelompok berbeda → MISMATCH", "Kata di luar kelompok: similarity teks ≥ 80% → MATCH"],
}

_ATURAN_HP_BEDA_KELUARGA = "Nomor yang sama dengan handphone keluarga dekat (TMS atau ucapan) → MISMATCH (nomor nasabah tidak boleh sama dengan nomor keluarga dekat)"

_BIDANG_UMUM = {"sektor", "bidang", "usaha"}

_SYNONYMS = {
    "jenis-kelamin": (("perempuan", "wanita", "female", "cewek", "p", "f"), "P", ("laki", "pria", "male", "cowok", "l", "m"), "L"),
    "kewarganegaraan": (("wni", "indonesia"), "WNI", ("wna", "asing"), "WNA"),
    "estatement": (("y", "ya", "iya", "setuju", "yes", "bersedia"), "Y", ("t", "n", "tidak", "no", "nggak", "gak"), "T"),
    "persetujuan-data-nasabah": (("y", "ya", "iya", "setuju", "yes", "bersedia"), "Y", ("t", "n", "tidak", "no", "nggak", "gak"), "T"),
}
_ADDRESS_ABBR = {"jl": "jalan", "jln": "jalan", "gg": "gang", "no": "nomor", "kel": "", "kelurahan": "", "kec": "", "kecamatan": "", "kota": "", "kab": "", "kabupaten": ""}
# Singkatan khusus alamat gabungan (hanya dipakai ``_alamat_tokens``, dua sisi sama-sama dinormalkan).
# Kel/Kec/Kelurahan/Kecamatan sudah dibuang di ``_ADDRESS_ABBR``, jadi otomatis setara.
_ALAMAT_SINGKATAN = {"kp": "kampung", "tm": "timur"}
_NUMBER_WORDS = {
    "nol": 0, "satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5, "enam": 6, "tujuh": 7,
    "delapan": 8, "sembilan": 9, "sepuluh": 10, "sebelas": 11,
}


def _tokens(text):
    s = re.sub(r"[^0-9a-z ]+", " ", str(text or "").casefold().replace(".", " "))
    out = []
    for t in s.split():
        t = _ADDRESS_ABBR.get(t, t)
        if t:
            out.append(t)
    return out


def _digits(value, phone=False):
    d = re.sub(r"\D", "", str(value or ""))
    if phone:
        if d.startswith("62"):
            d = d[2:]
        d = d.lstrip("0")
    return d


def lama_kerja_tms_months(value):
    """Kolom TMS ``lama-bekerjausaha`` berformat YYMM: 306 = 3 tahun 6 bulan, 600 = 6 tahun,
    3000 = 30 tahun. Dua digit terakhir = bulan, sisanya = tahun. ``None`` bila bukan angka."""
    d = re.sub(r"\D", "", str(value or ""))
    if not d:
        return None
    return int(d[:-2] or 0) * 12 + int(d[-2:])


def format_lama_kerja(months):
    if months is None:
        return None
    y, m = divmod(months, 12)
    parts = ([f"{y} tahun"] if y else []) + ([f"{m} bulan"] if m else [])
    return " ".join(parts) or "0 bulan"


def _lama_kerja_spoken_months(text):
    """Total bulan dari ucapan: "3 tahun 6 bulan", "enam tahun", "18 bulan", "3,5 tahun",
    "tiga setengah tahun"; ``None`` bila tidak terbaca."""
    s = str(text or "").casefold().replace(",", ".")
    s = re.sub(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", lambda m: str(_NUMBER_WORDS[m.group(1)]), s)
    s = re.sub(r"(\d+)\s*setengah", lambda m: f"{m.group(1)}.5", s)
    s = s.replace("setengah", "0.5")
    years = re.search(r"(\d+(?:\.\d+)?)\s*(?:tahun|thn|th)\b", s)
    months = re.search(r"(\d+(?:\.\d+)?)\s*(?:bulan|bln)\b", s)
    if not years and not months:
        return None
    total = (float(years.group(1)) * 12 if years else 0) + (float(months.group(1)) if months else 0)
    return int(round(total))


_SELULER = re.compile(r"^(?:\+?628|08)\d")


def is_nomor_seluler(value):
    """True bila nomor berawalan +628 / 628 / 08xx (seluler). Telepon kantor tidak boleh begitu."""
    return bool(value) and bool(_SELULER.match(re.sub(r"[\s\-.()]", "", str(value))))


def gender_from_nik(nik):
    """Jenis kelamin dari NIK digit 7-12 (DDMMYY): hari lahir perempuan ditambah 40
    (28-09-1987: laki-laki 280987, perempuan 680987). ``None`` bila NIK tak terbaca."""
    d = re.sub(r"\D", "", str(nik or ""))
    if len(d) < 12:
        return None
    day, month = int(d[6:8]), int(d[8:10])
    if not 1 <= month <= 12:
        return None
    if 1 <= day <= 31:
        return "Laki-laki"
    if 41 <= day <= 71:
        return "Perempuan"
    return None


# Satu kelompok pekerjaan: wirausaha = wiraswasta = pedagang = pengusaha (permintaan pemilik, 12 Okt 2026).
_WIRASWASTA = {"wirausaha", "wiraswasta", "pedagang", "pengusaha", "wirausahawan", "wiraswastawan"}


def _pekerjaan_alternatif(text):
    """Daftar alternatif pekerjaan (token): "WIRASWASTA/PEDAGANG" -> dua alternatif. "wirausaha"
    disamakan dengan "wiraswasta"; "karyawan" saja berarti "karyawan swasta"."""
    out = []
    for part in re.split(r"[/;,]|\bdan\b", str(text or "").casefold()):
        toks = ["wiraswasta" if t in _WIRASWASTA else t for t in _tokens(part)]
        if toks in (["usaha"], ["pemilik", "usaha"], ["berusaha"], ["wiraswasta", "wiraswasta"]):
            toks = ["wiraswasta"]
        if toks == ["karyawan"]:
            toks = ["karyawan", "swasta"]
        if toks:
            out.append(toks)
    return out


def _canon_kategori(column, value):
    s = " ".join(_tokens(value))
    if column in ("alamat-pengiriman-kartu", "alamat-tagihan-bulanan"):
        # TMS menyimpan KODE tujuan, bukan alamat: H = rumah (Home), C = kantor (Company),
        # E = email (penagihan elektronik). Ucapan agent: "alamat rumah", "ke kantor", "via email".
        raw = str(value or "").strip()
        if "@" in raw or re.search(r"\b(email|e mail|surel)\b", s) or s == "e":
            return "EMAIL"
        if re.search(r"\b(kantor|perusahaan|company|tempat kerja|tempat bekerja|usaha)\b", s) or s == "c":
            return "KANTOR"
        if re.search(r"\b(rumah|home|domisili|tinggal)\b", s) or s == "h":
            return "RUMAH"
        return None
    if column == "status-perkawinan":
        if re.search(r"\b(belum|lajang|single)\b", s):
            return "BELUM"
        if re.search(r"\b(cerai|duda|janda)\b", s):
            return "CERAI"
        if re.search(r"\b(kawin|menikah|married|nikah|sudah)\b", s):
            return "KAWIN"
        return None
    syn = _SYNONYMS.get(column)
    if not syn:
        return None
    first, first_c, second, second_c = syn
    toks = set(s.split())
    if toks & set(first):
        return first_c
    if toks & set(second):
        return second_c
    return None


_HUBUNGAN = (  # urutan penting: "suami"/"istri" sebelum "anak"
    ("SUAMI", ("suami",)),
    ("ISTRI", ("istri", "isteri")),
    ("ANAK", ("anak",)),
    ("SAUDARA", ("saudara", "sodara", "kakak", "adik", "abang", "kaka")),
    ("ORANGTUA", ("orangtua", "ortu", "ibu", "bapak", "ayah", "mama", "mami", "papa", "papi", "bunda", "ibunda")),
)


def _canon_hubungan(value):
    """Kelompok hubungan kekerabatan: TMS ``Istri``, ``Suami``, ``Anak``, ``SaudaraLaki-laki``,
    ``Saudaraperempuan``, ``OrangTua``; ucapan "istri saya", "kakak", "ibu". ``None`` bila di luar kelompok."""
    s = re.sub(r"[^a-z]", "", str(value or "").casefold())
    for kanon, kata in _HUBUNGAN:
        if any(k in s for k in kata):
            return kanon
    return None


def _spoken_number(token):
    if token.isdigit():
        return float(token)
    return float(_NUMBER_WORDS[token]) if token in _NUMBER_WORDS else None


def _spoken_amounts(text):
    """Nilai rupiah yang diucapkan: "6 sampai 7 jutaan" -> (6e6, 7e6), "tujuh juta" -> (7e6,)."""
    s = str(text or "").casefold().replace(",", ".")
    nums = []
    for m in re.finditer(r"(\d+(?:\.\d+)?|[a-z]+)\s*(juta|jt|ribu|rb)?", s):
        word, unit = m.group(1), m.group(2)
        v = float(word) if re.fullmatch(r"\d+(?:\.\d+)?", word) else _spoken_number(word)
        if v is None:
            continue
        if unit in ("juta", "jt"):
            v *= 1_000_000
        elif unit in ("ribu", "rb"):
            v *= 1_000
        elif v < 1000:
            continue  # angka telanjang kecil: bukan nominal
        nums.append(v)
    return nums


def _fmt(sim):
    return int(round(sim)) if float(sim).is_integer() or abs(sim - round(sim)) < 0.05 else round(sim, 1)


# Kata generik pada sebutan jenis kartu: bila hanya ini yang disebut, dianggap "kartu kredit" saja.
_KARTU_UMUM = {"kartu", "kredit", "card", "credit", "cc", "bank", "mega", "nya", "ini", "itu", "yang", "ya"}

EMAIL_AMBANG = 90.0
from compliance.spoken_normalize import normalize_email  # noqa: E402  (dipakai bersama Cashline)


def compare_field(column, tms_value, extracted_value):
    """``(match, similarity_percent | None, reason, tipe)`` untuk satu field.

    ``match``: ``MATCH`` / ``MISMATCH`` / ``SKIPPED_NULL`` (salah satu sisi kosong, atau field
    tidak diucapkan). Sisi kosong tidak pernah dihukum: ``SKIPPED_NULL``, seperti di Cashline."""
    tipe = _TYPE.get(column, "teks")
    if tipe == "tidak_diucapkan":
        return "SKIPPED_NULL", None, "Field diisi sistem, tidak diucapkan agent.", tipe
    if tipe == "source_data":
        if tms_value is None:
            return "SKIPPED_NULL", None, "Nilai TMS kosong.", tipe
        allowed = source_data_values(column)
        if not allowed:
            return "SKIPPED_NULL", None, "Microsite tidak tersedia untuk field ini.", tipe
        if tms_value.strip().upper() in {a.upper() for a in allowed}:
            return "MATCH", 100.0, "Nilai TMS ada di Microsite; tidak dibandingkan dengan ucapan.", tipe
        return "MISMATCH", 0.0, f"Nilai TMS tidak ada di Microsite ({', '.join(allowed)}).", tipe
    if tipe == "microsite_menyusul":
        return "SKIPPED_NULL", None, "Persetujuan sharing data dicek dari Microsite; data Microsite belum tersedia.", tipe
    if tms_value is None:
        return "SKIPPED_NULL", None, "Nilai TMS kosong.", tipe
    if tipe == "jenis_kartu":
        if extracted_value is None:
            return "MATCH", 100.0, "Jenis kartu tidak disebut di transkrip; TMS terisi dianggap valid.", tipe
        nama = [t for t in _tokens(extracted_value) if t not in _KARTU_UMUM]
        if not nama:
            return "MATCH", 100.0, "Hanya disebut \"kartu kredit\"; dianggap sesuai TMS.", tipe
        ref = _tokens(tms_value)
        hilang = [t for t in nama if not any(t == r or (_ratio(t, r) or 0) >= THRESHOLD for r in ref)]
        if not hilang:
            return "MATCH", 100.0, "Jenis kartu yang disebut ada di jenis kartu TMS.", tipe
        return "MISMATCH", float(round(_ratio(" ".join(nama), " ".join(ref)) or 0)), (
            f"Jenis kartu yang disebut ({extracted_value}) berbeda dari TMS ({tms_value})."), tipe
    if extracted_value is None:
        return "SKIPPED_NULL", None, "Tidak disebut di transkrip.", tipe

    sim = None
    if tipe == "nama":
        sim = similarity_nama(tms_value, extracted_value)
    elif tipe == "tanggal":
        sim = similarity_tanggal_lahir(tms_value, extracted_value)
        if sim is None:
            return "SKIPPED_NULL", None, "Tanggal tidak terbaca pada salah satu sisi.", tipe
    elif tipe == "digit":
        a, b = _digits(tms_value, True), _digits(extracted_value, True)
        if not a or not b:
            return "SKIPPED_NULL", None, "Tidak ada digit pada salah satu sisi.", tipe
        sim = 100.0 if a == b else float(round(_ratio(a, b) or 0))
        ok = a == b
        return ("MATCH" if ok else "MISMATCH"), sim, ("Digit sama persis." if ok else "Digit berbeda."), tipe
    elif tipe == "digit_1":
        a, b = _digits(tms_value, True), _digits(extracted_value, True)
        if not a or not b:
            return "SKIPPED_NULL", None, "Tidak ada digit pada salah satu sisi.", tipe
        dist = levenshtein(a, b)
        sim = 100.0 if dist == 0 else float(round(_ratio(a, b) or 0))
        if dist == 0:
            return "MATCH", sim, "Digit sama persis.", tipe
        if dist == 1:
            return "MATCH", sim, "Beda 1 digit (batas toleransi).", tipe
        return "MISMATCH", sim, "Beda lebih dari 1 digit.", tipe
    elif tipe == "email":
        a, b = normalize_email(tms_value), normalize_email(extracted_value)
        if not a or not b:
            return "SKIPPED_NULL", None, "Email kosong sesudah normalisasi.", tipe
        sim = _ratio(a, b)
        if sim is None:
            return "SKIPPED_NULL", None, "Email tidak terbaca.", tipe
        if re.sub(r"\D", "", a) != re.sub(r"\D", "", b):
            return "MISMATCH", float(round(sim)), f"Angka pada email berbeda (setelah normalisasi: {b}).", tipe
        # alamat utuh dinilai sebagai satu entitas (local + domain tidak dipisah)
        ok = sim >= EMAIL_AMBANG
        return ("MATCH" if ok else "MISMATCH"), float(round(sim)), (
            f"Similarity email utuh {_fmt(sim)}% {'≥' if ok else '<'} ambang {int(EMAIL_AMBANG)}% "
            f"(setelah normalisasi: {b})."), tipe
    elif tipe == "lama_kerja":
        ref, spoken = lama_kerja_tms_months(tms_value), _lama_kerja_spoken_months(extracted_value)
        if ref is None or spoken is None:
            return "SKIPPED_NULL", None, "Lama bekerja tidak terbaca pada salah satu sisi.", tipe
        ok = ref == spoken
        return ("MATCH" if ok else "MISMATCH"), (100.0 if ok else 0.0), (
            "Lama bekerja sama." if ok else f"Berbeda: TMS {format_lama_kerja(ref)}, ucapan {format_lama_kerja(spoken)}."), tipe
    elif tipe == "pekerjaan":
        ok = any(set(a) <= set(b) or set(b) <= set(a)
                 for a in _pekerjaan_alternatif(tms_value) for b in _pekerjaan_alternatif(extracted_value))
        if ok:
            return "MATCH", 100.0, "Pekerjaan sepadan.", tipe
        letter = _ratio(" ".join(_tokens(tms_value)), " ".join(_tokens(extracted_value)))
        return "MISMATCH", float(round(letter or 0)), "Pekerjaan berbeda.", tipe
    elif tipe == "hubungan":
        a, b = _canon_hubungan(tms_value), _canon_hubungan(extracted_value)
        if a is not None and b is not None:
            ok = a == b
            return ("MATCH" if ok else "MISMATCH"), (100.0 if ok else 0.0), (
                "Hubungan sepadan." if ok else f"Berbeda: TMS {a}, ucapan {b}."), tipe
        sim = _ratio(" ".join(_tokens(tms_value)), " ".join(_tokens(extracted_value)))
    elif tipe == "angka":
        a, b = _digits(tms_value).lstrip("0") or "0", _digits(extracted_value).lstrip("0") or "0"
        ok = a == b
        return ("MATCH" if ok else "MISMATCH"), (100.0 if ok else 0.0), ("Angka sama." if ok else "Angka berbeda."), tipe
    elif tipe == "kategori":
        a, b = _canon_kategori(column, tms_value), _canon_kategori(column, extracted_value)
        if a is None or b is None:
            sim = _ratio(" ".join(_tokens(tms_value)), " ".join(_tokens(extracted_value)))
        else:
            ok = a == b
            return ("MATCH" if ok else "MISMATCH"), (100.0 if ok else 0.0), (
                "Sepadan sesudah disamakan." if ok else f"Berbeda: TMS {a}, ucapan {b}."), tipe
    elif tipe == "penghasilan":
        amounts = _spoken_amounts(extracted_value)
        try:
            ref = float(re.sub(r"\D", "", str(tms_value)))
        except ValueError:
            ref = None
        if not amounts or ref is None:
            return "SKIPPED_NULL", None, "Nominal tidak terbaca pada salah satu sisi.", tipe
        lo, hi = min(amounts), max(amounts)
        if len(amounts) >= 2 and lo <= ref <= hi:
            return "MATCH", 100.0, "TMS berada di dalam rentang yang disebut.", tipe
        spoken = hi if len(amounts) == 1 else min(amounts, key=lambda x: abs(x - ref))
        sim = (min(ref, spoken) / max(ref, spoken) * 100) if max(ref, spoken) else None
    else:  # teks
        ta, tb = _tokens(tms_value), _tokens(extracted_value)
        if tipe == "bidang_usaha":
            # "Jasa" di transkrip dan "SEKTOR JASA" di TMS dianggap sama: kata umum dibuang.
            ta = [t for t in ta if t not in _BIDANG_UMUM] or ta
            tb = [t for t in tb if t not in _BIDANG_UMUM] or tb
        letter = _ratio(" ".join(ta), " ".join(tb))
        recall = (len(set(ta) & set(tb)) / len(set(ta)) * 100) if ta else None
        cands = [v for v in (letter, recall) if v is not None]
        sim = max(cands) if cands else None

    if sim is None:
        return "SKIPPED_NULL", None, "Kedua sisi kosong sesudah normalisasi.", tipe
    ok = sim >= THRESHOLD
    return ("MATCH" if ok else "MISMATCH"), float(round(sim)) if tipe != "tanggal" else float(sim), (
        f"Similarity {_fmt(sim)}% {'≥' if ok else '<'} ambang {int(THRESHOLD)}%."), tipe
