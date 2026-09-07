"""Hitung ulang similarity verifikasi STATIK (tanggal_lahir & nama_ibu_kandung) di
Python, tidak lagi mempercayai angka dari LLM.

Kenapa perlu: angka ``similarity_percent`` menentukan tiga hal sekaligus — MATCH vs
MISMATCH, masuk-tidaknya ke zona abu-abu (minta KK/KTP), dan lewat itu AI Status
tiket. Ternyata LLM tidak selalu menghitungnya benar: pada tiket 221111rBUk pasangan
nilai yang PERSIS SAMA ("ARNIYETTI" vs "Sarieti") dilaporkan 44% pada satu run dan
56% pada run berikutnya — yang benar 56 (jarak Levenshtein 4 atas panjang 9). Selisih
12 poin seperti itu bisa memindahkan tiket melewati ambang 80 / 87,5, yaitu antara
"kesalahan agent" dan "cukup minta dokumen".

Yang dihitung di sini HANYA dua field statik, karena hanya keduanya yang punya ambang
zona abu-abu. Field dinamis & cashline dibiarkan apa adanya (aturan pencocokannya
lebih longgar: substring alamat, akumulasi digit telepon, envelope produk — bukan
sekadar Levenshtein, jadi menghitung ulang di sini justru akan salah).

Sekaligus menegakkan aturan TAHAP 2 "pakai penyebutan TERBAIK" (KB v21 / prompt v50)
secara deterministik: setiap penyebutan nasabah diadu ke acuan Ascend dan yang
similarity-nya tertinggi yang dipakai. Seri dimenangkan penyebutan PALING BARU
(elemen terakhir ``extracted_mentions``), sama dengan bunyi aturannya.

Sejak revamp 21 Agustus 2026 (prompt v56) aturan konsistensi antar-penyebutan
(>= 90%) DIHAPUS: tidak ada lagi baris yang "gugur TAHAP 1", setiap penyebutan
langsung diadu ke Ascend. Hasil LAMA — yang tidak punya cap ``static_rules_version``
— tetap dinilai dengan aturan lamanya, termasuk pengecualian TAHAP 1.

Sempat ada saringan tanggal (hanya penyebutan pada panggilan setanggal ``submit_time``
TMS yang boleh dipakai, lalu dilonggarkan menjadi prioritas berjenjang). **Dicabut
seluruhnya 21 Agustus 2026** atas konfirmasi Bank Mega: tanggal panggilan tidak menjadi
syarat pengambilan bukti verifikasi statik. Yang tersisa dari mesin itu hanya
``source_file`` per penyebutan — nama PDF tempat ucapan berada, tetap ditulis karena
berguna bagi QC untuk menelusuri bukti.
"""
import re

# Bulan Indonesia + Inggris (termasuk singkatan yang lazim muncul di transkrip).
_MONTHS = {
    "januari": 1, "januar": 1, "january": 1, "jan": 1,
    "februari": 2, "pebruari": 2, "february": 2, "feb": 2, "peb": 2,
    "maret": 3, "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "mei": 5, "may": 5,
    "juni": 6, "june": 6, "jun": 6,
    "juli": 7, "july": 7, "jul": 7,
    "agustus": 8, "august": 8, "agu": 8, "agt": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "oktober": 10, "october": 10, "okt": 10, "oct": 10,
    "november": 11, "nopember": 11, "nov": 11,
    "desember": 12, "december": 12, "des": 12, "dec": 12,
}


def levenshtein(a: str, b: str) -> int:
    """Jarak edit klasik (insert/delete/substitute), iteratif dua baris."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _ratio(a: str, b: str) -> "float | None":
    """Similarity 0-100 dari jarak Levenshtein. None bila kedua sisi kosong."""
    n = max(len(a), len(b))
    if n == 0:
        return None
    return (1 - levenshtein(a, b) / n) * 100


# Runtun huruf/angka TUNGGAL yang dipisah tanda hubung — bentuk transkrip untuk nasabah
# yang MENGEJA namanya: "N-G", "T-J-U", "S-A-R-I-D-A". Sengaja hanya mencocokkan segmen
# satu karakter, sehingga nama yang memang berhubung ("Nur-Aini") tidak ikut digabung.
_SPELLED_RUN = re.compile(r"\b[0-9A-Za-z](?:-[0-9A-Za-z])+\b")

# Tanda baca penutup ucapan yang ikut terbawa transkrip ("Zaitun.", "Farida,").
_TRAILING_PUNCT = ".,;:!?-"

# LABEL FIELD yang ikut terbawa ke dalam nilai. Terjadi ketika nasabah mengulang
# pertanyaan agent sebelum menjawabnya ("Nama ibu kandung, Wong Aiua") atau ketika
# segmen transkrip menyatukan keduanya. Label itu bukan bagian dari nama, tetapi tanpa
# pembersihan ia dihitung sebagai belasan huruf yang tidak cocok: pada tiket 021006rNU6
# "Nama ibu kandung, Wong Aiua." hanya 19% terhadap Ascend "WONG AY HWA", padahal
# namanya sendiri 64%. Hanya dibuang bila berada di AWAL ucapan.
_LEADING_LABEL = re.compile(
    r"^(?:nama\s+ibu\s+kandung|ibu\s+kandung|nama\s+ibu|nama\s+gadis\s+ibu"
    r"|tanggal\s+lahir|tgl\s+lahir)\b[\s,:.\-]*"
)

# Sapaan di depan nama ("Ibu Siumsi"). Prompt sudah lama memerintahkan mengabaikannya
# (FUZZY MATCHING: "ignore ... honorifics (Bapak/Ibu/Pak/Bu)"), tetapi normalisasi Python
# belum pernah melakukannya. ``\b`` menjaga nama yang KEBETULAN berawalan sama tetap utuh
# — "Butet", "Ibunda", "Masitoh" tidak tersentuh karena huruf sesudahnya masih huruf.
_LEADING_HONORIFIC = re.compile(r"^(?:bapak|ibu|pak|bu|mbak|mas)\b[\s,.]*")


def _strip_leading_noise(text: str) -> str:
    """Buang label field dan sapaan yang menempel di AWAL ucapan, berulang sampai habis.

    Berulang karena keduanya bisa bertumpuk: "Ibu kandung, Ibu Siumsi Boru Sihombing"
    memuat label lalu sapaan. Bila pembersihan menghabiskan seluruh teks (ucapan yang
    isinya HANYA label), yang dikembalikan teks kosong — memang tidak ada nilai untuk
    dinilai, dan pemanggil melewatkannya sebagai kandidat."""
    out = text
    while True:
        stripped = _LEADING_LABEL.sub("", out, count=1)
        stripped = _LEADING_HONORIFIC.sub("", stripped, count=1)
        if stripped == out:
            return out
        out = stripped


def _norm_name(value) -> str:
    """Normalisasi nama: huruf kecil, spasi beruntun dijadikan satu, dipangkas,
    **ejaan huruf-per-huruf digabung**, dan tanda baca penutup dibuang.

    MENGEJA ADALAH JAWABAN VERIFIKASI YANG SAH (kebijakan 21 Agustus 2026). Nasabah
    yang mengeja "N-G T-J-U L-I-E-N" sedang menjawab dengan LEBIH presisi, bukan kurang.
    Sebelum kebijakan ini tanda baca dipertahankan apa adanya, dan akibatnya jawaban
    paling presisi justru mendapat skor TERENDAH: pada tiket 020338gGlU (Ascend
    "NG TJIU LI") ejaan "N-G T-J-U L-I-E-N." hanya 50% sementara pelafalan kasar
    "Ang Tju Lien" 67% — tanda hubungnya memakan 17 poin dan titik penutupnya 6 poin
    lagi. Digabung dan dibersihkan, ejaan itu bernilai 73%.

    Dua penjagaan supaya tidak ada skor yang naik diam-diam:

    * hanya segmen SATU karakter yang digabung, jadi nama yang memang mengandung tanda
      hubung ("Nur-Aini") tidak tersentuh;
    * yang dibuang hanya tanda baca di UJUNG; tanda baca di tengah (mis. koma pada
      "S-A-R-I-D-A, Farida") dibiarkan, karena di situ nasabah memang menyebut dua
      bentuk dan masing-masing sudah menjadi kandidat tersendiri.

    Label field dan sapaan yang menempel di awal ucapan juga dibuang — lihat
    ``_strip_leading_noise``.

    Normalisasi ini dipakai pada KEDUA sisi perbandingan; acuan Ascend berupa nama polos
    huruf besar sehingga tidak terpengaruh."""
    text = _SPELLED_RUN.sub(lambda m: m.group(0).replace("-", ""), str(value or ""))
    text = re.sub(r"\s+", " ", text).strip().casefold()
    text = _strip_leading_noise(text)
    return text.strip(_TRAILING_PUNCT).strip()


def _four_digit_year(value: int) -> int:
    """Tahun 2 digit -> 4 digit dengan aturan yang sama dengan prompt: YY > 25 -> 19YY,
    selain itu 20YY."""
    if value >= 100:
        return value
    return 1900 + value if value > 25 else 2000 + value


def to_ddmmyyyy(value) -> "str | None":
    """Bentuk baku 8 digit ``DDMMYYYY`` dari sebuah tanggal lahir, atau None bila tidak
    terbaca — pemanggil membiarkan angka LLM apa adanya ketimbang menebak.

    Menerima: "23 November 1988", "6 Juni 74", "23-11-1988", "23/11/1988",
    "1988-11-23", "19881123" (YYYYMMDD, format Ascend), "23111988" (DDMMYYYY).
    """
    s = str(value or "").strip()
    if not s:
        return None

    # 1) Teks dengan nama bulan.
    m = re.search(r"(\d{1,2})\s*([A-Za-z]+)\s*(\d{2,4})", s)
    if m:
        month = _MONTHS.get(m.group(2).casefold())
        if month:
            day = int(m.group(1))
            year = _four_digit_year(int(m.group(3)))
            if 1 <= day <= 31:
                return f"{day:02d}{month:02d}{year:04d}"

    digits = re.sub(r"\D", "", s)

    # 2) Angka berpemisah: DD-MM-YYYY / YYYY-MM-DD (dan varian "/" atau ".").
    m = re.fullmatch(r"(\d{1,4})\D(\d{1,2})\D(\d{1,4})", s)
    if m:
        a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if len(m.group(1)) == 4:           # YYYY-MM-DD
            return f"{c:02d}{b:02d}{a:04d}"
        return f"{a:02d}{b:02d}{_four_digit_year(c):04d}"  # DD-MM-YY(YY)

    # 3) Rangkaian 8 digit: bedakan YYYYMMDD (Ascend) dari DDMMYYYY.
    if len(digits) == 8:
        head, tail = int(digits[:4]), int(digits[4:])
        if 1900 <= head <= 2100:
            return f"{digits[6:8]}{digits[4:6]}{digits[0:4]}"
        if 1900 <= tail <= 2100:
            return digits
    # 4) DDMMYY (6 digit).
    if len(digits) == 6:
        return f"{digits[0:2]}{digits[2:4]}{_four_digit_year(int(digits[4:6])):04d}"
    return None


def spoken_year_digits(value) -> "int | None":
    """Berapa digit tahun yang DIUCAPKAN pada sebuah penyebutan tanggal lahir.

    ``to_ddmmyyyy`` sengaja menormalkan "06 06 05" menjadi "06062005", sehingga jumlah
    digit aslinya hilang — padahal justru itu yang dinilai ATURAN TAHUN LAHIR 20XX.
    Fungsi ini membaca token tahunnya dengan tokenisasi yang SAMA dengan
    ``to_ddmmyyyy`` supaya keduanya tidak pernah berbeda pendapat tentang token mana
    yang merupakan tahun. ``None`` bila tahunnya tidak terbaca."""
    s = str(value or "").strip()
    if not s:
        return None

    m = re.search(r"(\d{1,2})\s*([A-Za-z]+)\s*(\d{2,4})", s)
    if m and _MONTHS.get(m.group(2).casefold()):
        return len(m.group(3))

    m = re.fullmatch(r"(\d{1,4})\D(\d{1,2})\D(\d{1,4})", s)
    if m:
        # YYYY-MM-DD bila token pertama 4 digit; selain itu DD-MM-YY(YY).
        return len(m.group(1)) if len(m.group(1)) == 4 else len(m.group(3))

    digits = re.sub(r"\D", "", s)
    if len(digits) == 8:
        return 4
    if len(digits) == 6:
        return 2
    return None


def four_digit_year_required(reference, mentions) -> "str | None":
    """Tahun 2 digit yang diucapkan nasabah untuk kelahiran 2000-an, atau ``None``.

    ATURAN TAHUN LAHIR 20XX WAJIB 4 DIGIT: bila tahun lahir sebenarnya >= 2000 dan
    nasabah menyebut tahunnya hanya 2 digit, verifikasi tanggal lahir GAGAL — apa pun
    kecocokan hari & bulannya. Nilai kembaliannya adalah tahun 2 digit yang diucapkan
    (untuk kalimat ``reason``); ``None`` berarti aturan ini tidak berlaku.

    Pemicunya TIDAK terpenuhi bila nasabah menyebut tahun 4 digit di penyebutan MANA
    PUN — sekali menyebut lengkap sudah cukup, penyebutan lain yang ringkas tidak
    membatalkannya. Yang dinilai hanya ucapan NASABAH; ``extracted_mentions`` memang
    hanya berisi ucapan nasabah (agent yang membacakan ulang tidak masuk).

    Tahun acuan diambil dari ``reference_value`` (Ascend). Bila acuan kosong/tak
    terbaca, dipakai tahun hasil normalisasi ucapan nasabah — sejalan dengan prompt.
    """
    rows = [m for m in (mentions or []) if isinstance(m, dict) or isinstance(m, str)]
    values = [m.get("value") if isinstance(m, dict) else m for m in rows]
    values = [str(v).strip() for v in values if v is not None and str(v).strip()]
    if not values:
        return None

    baku = to_ddmmyyyy(reference)
    if baku is None:
        for v in values:
            baku = to_ddmmyyyy(v)
            if baku is not None:
                break
    if baku is None:
        return None
    try:
        year = int(baku[4:])
    except (TypeError, ValueError):
        return None
    if year < 2000:
        return None

    two_digit = None
    for v in values:
        n = spoken_year_digits(v)
        if n == 4:
            return None          # pernah menyebut lengkap -> pemicu batal
        if n == 2 and two_digit is None:
            two_digit = f"{year % 100:02d}"
    return two_digit


def similarity_tanggal_lahir(reference, candidate) -> "float | None":
    """Similarity tanggal lahir atas bentuk baku 8 digit DDMMYYYY, TIDAK dibulatkan
    (87.5 dilaporkan 87.5, sesuai prompt). None bila salah satu sisi tidak terbaca."""
    a, b = to_ddmmyyyy(reference), to_ddmmyyyy(candidate)
    if a is None or b is None:
        return None
    return (1 - levenshtein(a, b) / 8) * 100


# --- Penyelarasan bentuk SINGKATAN Ascend (24 Agustus 2026) -----------------
# Nama di Ascend sering lebih PENDEK daripada yang diucapkan nasabah: kolomnya
# terpotong batas panjang field ("TRI WAHJOENINGSIH T"), atau nama depannya
# disingkat ("TH" untuk "Theresia"). Levenshtein bekerja huruf-per-huruf pada
# SELURUH string, sehingga sisipan yang hilang itu dihitung sebagai kesalahan:
# pada tiket 010550Vosa, "T" vs "Tirtosimono" saja menyumbang 10 dari 13 operasi
# dan menjatuhkan nilainya ke 54%. Nasabah dihukum justru karena menjawab LEBIH
# lengkap, dan tidak ada ambang yang bisa menolongnya — dengan ejaan Ascend
# diperbaiki sekalipun, singkatan itu sendiri menahan nilainya di 64%.
#
# Karena itu token transkrip DIPENDEKKAN mengikuti bentuk singkatan Ascend
# sebelum diadu. Yang berubah HANYA sisi ucapan nasabah — sejalan dengan seluruh
# normalisasi lain di modul ini (penggabungan ejaan, pembuangan label field &
# sapaan). **Acuan Ascend tetap tidak pernah disentuh.**
#
# Syaratnya sengaja ketat supaya hanya menangkap pemotongan/penyingkatan:
#   * token Ascend PALING BANYAK 3 huruf; DAN
#   * token Ascend adalah AWALAN token transkrip pasangannya.
# "RITA" vs "Sarita" TIDAK terkena — RITA akhiran, bukan awalan — sehingga nama
# depan yang memang berbeda tetap jatuh ke zona dokumen (tiket 071100bNE9).
_ABBREV_MAX_LEN = 3
_ABBREV_PAIR_MIN_RATIO = 80.0


def _align_abbreviations(reference: str, candidate: str) -> str:
    """Kembalikan ``candidate`` (sudah ternormalisasi) dengan token yang di Ascend
    tampil sebagai singkatan dipendekkan ke bentuk singkatan itu.

    Pemasangan token dilakukan berurutan dan SATU-LAWAN-SATU: satu token transkrip
    hanya boleh dipakai untuk satu token Ascend, sehingga Ascend "SITI SITI" tidak
    bisa dianggap terpenuhi oleh transkrip "siti" yang hanya punya satu kata.
    """
    ref_tokens = reference.split()
    cand_tokens = candidate.split()
    if not ref_tokens or not cand_tokens:
        return candidate
    out = list(cand_tokens)
    used = set()
    for r in ref_tokens:
        for j, c in enumerate(cand_tokens):
            if j in used:
                continue
            same = r == c
            abbrev = (len(r) <= _ABBREV_MAX_LEN and len(c) > len(r) and c.startswith(r))
            close = (_ratio(r, c) or 0) >= _ABBREV_PAIR_MIN_RATIO
            if not (same or abbrev or close):
                continue
            if abbrev:
                out[j] = r          # "tirtosimono" -> "t", "theresia" -> "th"
            used.add(j)
            break
    return " ".join(out)


def similarity_nama(reference, candidate) -> "float | None":
    """Similarity nama ibu kandung, dibulatkan ke bilangan bulat (sesuai prompt).

    Perbandingannya APA ADANYA terhadap acuan Ascend. Yang dinormalkan hanya ucapan
    nasabah (huruf kecil, ejaan huruf-per-huruf digabung, label field & sapaan di awal
    dibuang, tanda baca penutup dibuang) — semuanya lewat ``_norm_name``, dan pada nilai
    Ascend yang berupa nama polos huruf besar semua itu tidak mengubah apa pun selain
    kapitalisasi.

    **Acuan Ascend TIDAK PERNAH dimanipulasi.** Versi 21 Agustus 2026 sempat memakai
    perbandingan kedua yang membuang spasi di KEDUA sisi untuk kandidat ejaan — dengan
    alasan mengeja tidak bisa menyampaikan jeda kata. Aturan itu DICABUT: menggabungkan
    tulisan di Ascend berarti menilai terhadap acuan yang bukan lagi acuan bank. Ejaan
    tetap dibaca sebagai kata utuh (tanda hubungnya digabung), tetapi spasi pada acuan
    tetap dihitung apa adanya.

    Sejak 24 Agustus 2026 ucapan nasabah juga DISELARASKAN ke bentuk singkatan Ascend
    lebih dulu (lihat ``_align_abbreviations``): nama yang di Ascend terpotong/disingkat
    tidak lagi menghukum nasabah yang menyebutnya lengkap. Acuan Ascend tetap utuh."""
    ref = _norm_name(reference)
    raw = _norm_name(candidate)
    plain = _ratio(ref, raw)
    aligned = _ratio(ref, _align_abbreviations(ref, raw))
    # ARAHNYA SATU: penyelarasan hanya boleh MENAIKKAN nilai. Token Ascend pendek
    # kadang KEBETULAN menjadi awalan token transkrip tanpa benar-benar merupakan
    # singkatannya — pada tiket 011013QyLm, "NI" (partikel nama Bali pada
    # "NI KETUT KERTI") memenggal "Niketor" menjadi "ni" dan menjatuhkan nilainya
    # dari 71% ke 57%. Mengambil yang TERTINGGI membuat salah-tebak seperti itu
    # tidak pernah merugikan nasabah, sekaligus mempertahankan sifat lama:
    # aturan ini hanya bisa meluluskan, tidak pernah menjatuhkan.
    values = [v for v in (plain, aligned) if v is not None]
    if not values:
        return None
    return float(round(max(values)))


# field -> fungsi similarity-nya.
STATIC_SIMILARITY = {
    "tanggal_lahir": similarity_tanggal_lahir,
    "nama_ibu_kandung": similarity_nama,
}


def mention_values(mentions) -> list:
    """Nilai ucapan saja dari ``extracted_mentions``, apa pun bentuknya.

    Sejak prompt v52 tiap elemen berupa objek ``{"timestamp", "value"}`` supaya dua
    ucapan identik di menit berbeda tidak bisa digabung; hasil lama menyimpan string
    telanjang. Keduanya diterima agar tiket lama tetap terbaca."""
    out = []
    for m in mentions or []:
        if isinstance(m, dict):
            value = m.get("value")
        else:
            value = m
        if value is None or str(value).strip() == "":
            continue
        out.append(str(value).strip())
    return out


def mention_rows(mentions) -> list:
    """``[{"timestamp", "value", "source_file"}]`` yang sudah dirapikan — dipakai
    tampilan & export.

    Elemen lama (string telanjang) mendapat timestamp kosong; ``source_file`` (nama
    PDF tempat ucapan itu berada, ada sejak prompt v56) kosong untuk hasil lama."""
    out = []
    for m in mentions or []:
        if isinstance(m, dict):
            value = m.get("value")
            ts = m.get("timestamp") or ""
            src = m.get("source_file") or ""
        else:
            value, ts, src = m, "", ""
        if value is None or str(value).strip() == "":
            continue
        out.append({
            "timestamp": str(ts).strip(),
            "value": str(value).strip(),
            "source_file": str(src).strip(),
        })
    return out


def stamp_static_rules(evaluation: dict) -> dict:
    """Cap versi aturan verifikasi statik ke dalam evaluasi, SEKALI, saat tiket diproses.

    ``static_rules_version = 2`` menandai bahwa tiket ini dievaluasi dengan prompt v56 ke
    atas, sehingga seluruh pembaca memakai aturan barunya: tanpa cek konsistensi
    antar-penyebutan, dan ambang ``nama_ibu_kandung`` 80 (MATCH) / 50 (batas MISMATCH).
    Tiket lama tidak punya cap ini dan karenanya tetap dinilai dengan aturan lamanya —
    band dibaca ulang setiap kali halaman dibuka, jadi tanpa cap ini seluruh riwayat akan
    ikut dinilai ulang.

    Cap ini membekukan aturan pada versi prompt yang BENAR-BENAR dipakai, termasuk untuk
    tiket yang diproses ulang belakangan — lebih tepat daripada ambang waktu upload.

    Versi sebelumnya juga mencap ``submit_date`` per baris dan ``on_submit_date`` per
    ucapan untuk saringan tanggal; saringan itu dicabut atas konfirmasi Bank Mega, jadi
    keduanya tidak ditulis lagi. Non-destruktif.
    """
    if not isinstance(evaluation, dict):
        return evaluation
    return {**evaluation, "static_rules_version": 2}


def best_static_match(field: str, reference, candidates: list) -> "tuple | None":
    """Penyebutan TERBAIK terhadap ``reference`` beserta similarity-nya:
    ``(nilai, similarity)``. None bila tidak ada satu pun yang bisa dihitung.

    Seri dimenangkan penyebutan PALING BARU — daftar ``candidates`` urut dari yang
    paling lama, jadi perbandingannya memakai ``>=``."""
    fn = STATIC_SIMILARITY.get(field)
    if fn is None:
        return None
    best = None
    for cand in candidates:
        if cand is None or str(cand).strip() == "":
            continue
        score = fn(reference, cand)
        if score is None:
            continue
        if best is None or score >= best[1]:
            best = (cand, score)
    return best
