"""Kepemilikan panggilan: satu tiket bisa berisi panggilan dari BEBERAPA agent.

Sebuah ticket id dikirim ke QC sebagai kumpulan PDF, dan sampai 31 Agustus 2026
kelimanya digabung menjadi SATU percakapan tanpa memeriksa siapa agent-nya. Padahal
TMS hanya meng-assign tiket itu kepada SATU agent (``tms_cashline.agent_id``), dan
scorecard-nya menilai agent tersebut. Akibatnya evidence bisa diambil dari panggilan
agent LAIN — pada tiket ``180107uT48`` empat item scorecard dinilai dari panggilan
"Desi" (18 Juli), padahal tiketnya milik "ELVIN" (``nurha801``, submit 22 Juli).

Modul ini menentukan panggilan mana yang boleh ikut dinilai:

* Agent Bank Mega SELALU memperkenalkan diri di pembuka ("saya Alvin dari Bank Mega",
  "ini dengan Desi dari Pusat Informasi Bank Mega"). Nama itulah penanda pemiliknya —
  label diarization TIDAK bisa dipakai: isinya hanya "Agent"/"Customer", dan pada tiket
  ``180107uT48`` label itu bahkan TERTUKAR di 4 dari 5 file.
* Nama yang terdeteksi diadu ke kolom **NAME ONLINE** agent TMS (nama panggilan
  on-air, bukan nama lengkap karyawan) secara fuzzy — transkrip menulis "Alvin" untuk
  NAME ONLINE "ELVIN" (80%), dan salah dengar seperti itu tidak boleh membuang
  panggilan yang sah.

DUA SYARAT, keduanya WAJIB, sebelum sebuah panggilan dibuang:

1. **Nama yang di-assign HARUS muncul di tiket ini.** Minimal satu panggilan
   memperkenalkan nama yang mirip NAME ONLINE agent TMS (>= ``NAME_MATCH_MIN``).
   Tanpa itu kita tidak tahu panggilan mana yang miliknya, jadi TIDAK ADA yang
   dibuang. Ini yang melindungi kasus terbanyak di lapangan: NAME ONLINE di roster
   sering BUKAN nama yang benar-benar dipakai agent on-air — ``nena801`` terdaftar
   "LAILA" tapi memperkenalkan diri "Nadia" di seluruh panggilannya, ``gamu729``
   terdaftar "VADLI" tapi menyebut "Yusuf", ``yunur729`` terdaftar "KIA" tapi
   menyebut "Nisa". Tiket seperti itu BUKAN tiket dua agent.
2. **Nama pada panggilan itu harus nama agent SUNGGUHAN.** Yaitu persis (100%) sama
   dengan NAME ONLINE orang lain di roster. Detektor nama masih ikut menangkap kata
   biasa yang kebetulan berdiri sebelum jangkar ("informasi", "SMS", "fasilitas",
   "produk"); pengukuran atas 98 tiket menemukan 114 dari 140 nama terdeteksi TIDAK
   cocok dengan siapa pun di roster. Mensyaratkan kecocokan roster membuang seluruh
   kebisingan itu sekaligus melindungi salah-dengar nama agent sendiri (transkrip
   "Alvin"/"Alia"/"Delvina" untuk NAME ONLINE ELVIN/ALYA/DAVINA tidak persis sama
   dengan nama siapa pun, jadi tidak pernah dianggap orang lain).

Tanpa syarat (1) aturan ini menyentuh 74 dari 98 tiket — hampir semuanya keliru.
Dengan keduanya, tersisa 4 tiket yang kutipan perkenalannya sudah diperiksa manual
dan memang berisi dua agent berbeda.

JARING PENGAMAN: bila penyaringan menyisakan NOL panggilan, seluruh panggilan
dikembalikan. Tiket tanpa transkrip pasti salah, dan lebih baik menilai berlebih
daripada menerbitkan evaluasi kosong.
"""
import collections
import os
import re

from compliance.static_similarity import levenshtein

# Ambang kemiripan nama (persen) untuk mengenali agent yang di-assign pada sebuah
# panggilan. Diturunkan ke 75 setelah pengukuran 98 tiket: transkrip menulis "Alvin"
# untuk NAME ONLINE ELVIN (80%), "Alia" untuk ALYA (75%), "Dika" untuk DIKTA (80%).
# Pada 75 ketiganya dikenali. Ambang ini hanya MENGIZINKAN penyaringan berjalan —
# yang benar-benar menjatuhkan sebuah panggilan adalah syarat kedua (nama itu persis
# nama on-air agent lain di roster), jadi menurunkannya tidak membuat sistem lebih
# gampang membuang panggilan.
NAME_MATCH_MIN = 75.0

# Jangkar perkenalan: "... dari Bank Mega" / "... dari Pusat Informasi Bank Mega".
# Nama agent adalah 1-2 kata TEPAT SEBELUM jangkar itu.
_INTRO_ANCHOR = re.compile(
    r"([A-Za-z'’.\- ]{2,40}?)\s*,?\s+dari\s+(?:pusat\s+informasi\s+)?bank\s*mega",
    re.IGNORECASE,
)
# Pola cadangan tanpa jangkar "dari Bank Mega": "nama saya X", "saya X di sini".
_INTRO_NAME_SAYA = re.compile(r"\bnama\s+saya\s+([A-Za-z'’.\-]{3,20})", re.IGNORECASE)

# Kata pembuka perkenalan. Nama yang DIDAHULUI salah satu kata ini adalah perkenalan
# sungguhan ("saya Alvin", "ini dengan Desi", "Bapak Alvin"); tanpa itu, nama yang
# terbaca bisa saja kata biasa yang kebetulan berdiri sebelum jangkar ("informasi
# terkait dari fasilitas dari Bank Mega" -> "fasilitas"). Bila ada perkenalan sungguhan
# di sebuah panggilan, HANYA itu yang dipakai — sisanya kebisingan.
_INTRO_CUES = {
    "saya", "dengan", "ini", "bersama", "nama", "namanya",
    "bapak", "ibu", "pak", "bu", "mbak", "mas", "kak",
}

# Kata yang mendahului nama dan JELAS bukan nama — dibuang dari ekor jangkar.
_STOPWORDS = {
    "saya", "dengan", "ini", "itu", "kembali", "bersama", "adalah", "yaitu",
    "bapak", "ibu", "pak", "bu", "mbak", "mas", "kak", "halo", "hallo", "iya", "ya",
    "selamat", "pagi", "siang", "sore", "malam", "dan", "atau", "di", "ke", "nya",
    "betul", "benar", "baik", "maaf", "izin", "ijin", "mohon", "nama", "eh", "ee",
    "sini", "gimana", "bagaimana", "apa", "yang", "untuk", "atas", "kami", "kita",
}


def _clean(token: str) -> str:
    return token.strip(" ,.;:!?-'’\"").strip()


def detect_agent_names(text: str) -> list:
    """Nama agent yang diperkenalkan di dalam teks satu panggilan, terbanyak dulu.

    Ekor sebelum jangkar "dari Bank Mega" dipangkas dari belakang: kata terakhir yang
    BUKAN stopword diambil sebagai nama. "Iya. Bapak, saya Alvin dari Bank Mega
    Jakarta" -> ``["Alvin"]``; "Halo Bapak, ini dengan Desi dari Pusat Informasi Bank
    Mega" -> ``["Desi"]``.
    """
    if not text:
        return []
    strong, weak = collections.Counter(), collections.Counter()
    for m in _INTRO_ANCHOR.finditer(text):
        tokens = [_clean(t) for t in m.group(1).split()]
        for i in range(len(tokens) - 1, -1, -1):
            tok = tokens[i]
            if not tok or not tok.isalpha() or len(tok) < 3:
                continue
            if tok.casefold() in _STOPWORDS:
                continue
            before = tokens[i - 1].casefold() if i > 0 else ""
            (strong if before in _INTRO_CUES else weak)[tok] += 1
            break
    for m in _INTRO_NAME_SAYA.finditer(text):
        tok = _clean(m.group(1))
        if tok.isalpha() and tok.casefold() not in _STOPWORDS:
            strong[tok] += 1
    counts = strong or weak
    return [name for name, _ in counts.most_common()]


def name_similarity(a: str, b: str) -> float:
    """Kemiripan dua nama dalam persen (0-100), case-insensitive."""
    x, y = (a or "").strip().casefold(), (b or "").strip().casefold()
    if not x or not y:
        return 0.0
    if x == y:
        return 100.0
    return round((1 - levenshtein(x, y) / max(len(x), len(y))) * 100, 1)


def classify_call(text: str, name_online: str) -> tuple:
    """``(verdict, detected_names, best_score)`` untuk satu panggilan, TANPA melihat
    tiketnya secara utuh.

    ``verdict``: ``"owned"`` (ada nama mirip agent yang di-assign) | ``"other"`` (ada
    nama, tak satu pun mirip) | ``"unknown"`` (tidak ada nama terbaca). ``"other"``
    BUKAN vonis: penentuan dibuang atau tidak ada di ``filter_calls_by_agent``, yang
    juga menimbang seluruh panggilan tiket dan roster.
    """
    names = detect_agent_names(text)
    if not names:
        return "unknown", [], 0.0
    if not (name_online or "").strip():
        return "unknown", names, 0.0
    best = max(name_similarity(n, name_online) for n in names)
    return ("owned" if best >= NAME_MATCH_MIN else "other"), names, best


def _matches_other_agent(name: str, name_online: str, roster_names) -> str:
    """NAME ONLINE orang lain yang PERSIS sama dengan ``name`` (None bila tidak ada).

    Kecocokan harus 100%: nama agent yang salah dengar sedikit pun tidak boleh
    dianggap sebagai orang lain.
    """
    mine = (name_online or "").strip().casefold()
    for other in roster_names or ():
        o = (other or "").strip()
        if not o or o.casefold() == mine:
            continue
        if name_similarity(name, o) >= 100.0:
            return o
    return None


def filter_calls_by_agent(pdf_texts: dict, name_online: str, roster_names=()) -> tuple:
    """Pilih panggilan milik ``name_online`` dari ``{path -> teks transkrip}``.

    ``roster_names`` = seluruh NAME ONLINE pada database sales aktif; dipakai untuk
    memastikan nama yang terbaca benar-benar nama agent (syarat 2 di docstring modul).

    Mengembalikan ``(kept_paths, dropped)`` dengan ``dropped`` =
    ``[{"path", "filename", "names", "score", "matched_agent"}]``. ``kept_paths``
    mempertahankan urutan ``pdf_texts``.
    """
    verdicts = {}
    for path, text in pdf_texts.items():
        verdicts[path] = classify_call(text, name_online)

    # SYARAT 1: nama agent yang di-assign harus muncul di salah satu panggilan.
    if not any(v[0] == "owned" for v in verdicts.values()):
        return list(pdf_texts.keys()), []

    kept, dropped = [], []
    for path, (verdict, names, score) in verdicts.items():
        matched = None
        if verdict == "other":
            # SYARAT 2: namanya harus persis nama on-air agent LAIN di roster.
            for n in names:
                matched = _matches_other_agent(n, name_online, roster_names)
                if matched:
                    break
        if matched:
            dropped.append({
                "path": path,
                "filename": os.path.basename(path),
                "names": names,
                "score": score,
                "matched_agent": matched,
            })
        else:
            kept.append(path)
    if not kept:
        return list(pdf_texts.keys()), []
    return kept, dropped


# --------------------------------------------------------------------------
# PERAN PEMBICARA YANG TERTUKAR (31 Agustus 2026)
# --------------------------------------------------------------------------
# Diarization hulu kadang MENUKAR label "Agent" dan "Customer": ucapan agent
# tercatat sebagai Customer dan sebaliknya. Pengukuran atas seluruh transkrip yang
# ada — 217 PDF pada 98 tiket — menemukan **31 PDF** dalam keadaan itu.
#
# Akibatnya tidak sepele. Verifikasi STATIK card holder menilai UCAPAN NASABAH:
# prompt mewajibkan setiap penyebutan nasabah masuk ke "extracted_mentions", lalu
# ``normalize_static_verification`` menghitung similarity DARI array itu. Pada berkas
# yang tertukar, jawaban nasabah berlabel "Agent", sehingga LLM menyimpulkan nasabah
# tidak pernah menyebutkannya: "extracted_mentions" kosong, similarity 0, MISMATCH,
# dan SC_CL_23_1/SC_CL_23_2 — keduanya item KRITIS — jatuh BELUM_SESUAI dengan iris
# 25% masing-masing plus veto Not Qualified. Tiket ``0210052AQd`` adalah contohnya:
# transkrip jelas memuat "28 Juli 1980" dan "Hajah Aminah" (acuan Ascend 19800728 /
# AMINAH), keduanya diucapkan pihak yang dilabeli "Agent".
#
# Kontradiksinya terlihat di layar: kolom Transkrip baris verifikasi kosong, sementara
# item scorecard pasangannya membawa "evidence.quote" yang MEMUAT jawaban itu — sebab
# evidence tidak peduli siapa yang bicara, extracted_mentions peduli.
#
# Modul ini sejak awal menolak mempercayai label diarization untuk menentukan pemilik
# panggilan (lihat docstring modul). Fungsi di bawah melangkah lebih jauh: label yang
# salah DIPERBAIKI sebelum transkrip dikirim ke LLM.

# Jangkar perkenalan agent. Dicari HANYA di pembukaan panggilan — di tengah
# percakapan "Bank Mega" disebut siapa saja ("yang dari Bank Mega itu"), sedangkan
# perkenalan hanya terjadi di awal.
_ROLE_INTRO = re.compile(
    r"dari\s+(?:pusat\s+informasi\s+|call\s*cent(?:er|re)\s+|bagian\s+[\w\s]{0,20}?)?bank\s*mega"
    r"|\bnama\s+saya\b|\bperkenalkan\b",
    re.IGNORECASE,
)
# Berapa segmen pembuka yang diperiksa. 8 sudah cukup pada seluruh 31 berkas yang
# tertukar; memperlebarnya hanya menambah kebisingan tengah panggilan.
_ROLE_OPENING_SEGMENTS = 8


def _role_kind(label) -> "str | None":
    """``"agent"`` / ``"customer"`` / None untuk sebuah label pembicara.

    Menerima ragam label hulu: "Agent", "AGENT_BM", "Customer", "NASABAH". Label
    netral ("SPEAKER_0/1") sengaja tidak dikenali — di sana tidak ada peran yang bisa
    tertukar, jadi tidak ada yang perlu diperbaiki."""
    text = str(label or "").strip().casefold()
    if text.startswith("agent"):
        return "agent"
    if text.startswith(("customer", "nasabah")):
        return "customer"
    return None


def fix_speaker_roles(segments) -> list:
    """Tukar balik label pembicara satu panggilan bila diarization menukarnya.

    Pihak yang MEMPERKENALKAN DIRI dari Bank Mega di pembukaan adalah agent. Bila
    perkenalan itu justru ada pada label bersifat nasabah, kedua label ditukar.

    SANGAT KONSERVATIF — tidak menyentuh apa pun kecuali seluruh syarat ini terpenuhi:

    * tepat DUA label, satu bersifat agent dan satu bersifat nasabah (lihat
      ``_role_kind``); transkrip berlabel netral dilewati;
    * ada perkenalan terbaca di ``_ROLE_OPENING_SEGMENTS`` segmen pertama;
    * satu pihak saja yang membawanya — seri berarti tidak bisa dipastikan, dan
      panggilan itu dibiarkan apa adanya.

    Pada 217 PDF yang ada, aturan ini menandai 31 berkas, TANPA satu pun kasus seri;
    ke-31-nya diperiksa manual dan memang tertukar. 23 berkas lain tidak punya
    perkenalan yang terbaca dan dibiarkan — seluruhnya memang sudah berlabel benar.

    Non-destruktif: mengembalikan daftar ASLI bila tidak ada yang diubah.
    """
    if not segments:
        return segments
    labels = []
    for seg in segments:
        lab = (seg or {}).get("speaker")
        if lab not in labels:
            labels.append(lab)
    if len(labels) != 2:
        return segments
    kinds = {lab: _role_kind(lab) for lab in labels}
    if sorted(k for k in kinds.values() if k) != ["agent", "customer"]:
        return segments
    hits = collections.Counter()
    for seg in segments[:_ROLE_OPENING_SEGMENTS]:
        if _ROLE_INTRO.search(str((seg or {}).get("text") or "")):
            hits[(seg or {}).get("speaker")] += 1
    if not hits:
        return segments
    ranked = hits.most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return segments             # seri -> tidak bisa dipastikan
    if kinds.get(ranked[0][0]) == "agent":
        return segments             # label sudah benar
    swap = {labels[0]: labels[1], labels[1]: labels[0]}
    return [{**seg, "speaker": swap.get(seg.get("speaker"), seg.get("speaker"))}
            for seg in segments]
