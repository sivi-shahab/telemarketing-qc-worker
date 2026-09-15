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


# --------------------------------------------------------------------------
# NAMA ON-AIR: cocok atau tidak dengan NAME ONLINE roster (7 September 2026)
# --------------------------------------------------------------------------
# Permintaan bisnis: SC_CL_2 ("Agent menyebutkan nama agent") tidak lagi cukup dipenuhi
# dengan memperkenalkan diri — nama yang disebut harus SESUAI kolom NAME ONLINE pada
# roster sales. Ketidaksesuaian digagalkan.
#
# PEMERIKSAANNYA SENGAJA TIDAK MEMAKAI ``detect_agent_names`` SENDIRIAN. Detektor itu
# dibangun untuk menemukan pemilik panggilan, dan untuk keperluan ITU kebocorannya tidak
# berbahaya (syarat kedua menyaringnya). Dipakai sebagai penentu vonis, ia menghukum
# orang yang tidak bersalah: pengukuran atas 111 rekaman seed 7 September menemukan
# 37 rekaman yang NAME ONLINE-nya JELAS ADA di transkrip tetapi detektornya justru
# menangkap kata biasa — "PUSPA" terbaca ['kredit','notifikasi','diskon'], "AUREL"
# terbaca ['apresiasi','aplikasi','SMS'].
#
# Karena itu ada dua kesempatan sebelum sebuah tiket dinyatakan tidak cocok:
#   1. nama yang TERDETEKSI sebagai perkenalan cocok dengan NAME ONLINE; atau
#   2. NAME ONLINE muncul sebagai KATA di dalam transkrip — bukti bahwa agent
#      menyebutnya walau detektor gagal menemukannya.
# Barulah bila keduanya gagal, tiket dinyatakan tidak cocok.

# Panjang minimal NAME ONLINE untuk boleh dicocokkan secara fuzzy. Nama pendek
# ("IKE", "KIA") terlalu mudah menyerempet kata biasa — "Oke", "Ika", "jika" semuanya
# 67% terhadap "IKE" — sehingga fuzzy pada nama sependek itu menghasilkan kelolosan
# maupun kegagalan yang sama-sama tidak bisa dipertanggungjawabkan.
FUZZY_MIN_LEN = 5


def _kata_transkrip(text: str) -> set:
    return {w for w in re.findall(r"[A-Za-z'\u2019]{3,}", str(text or ""))}


def _nama_asli_cocok(kandidat: str, nama_karyawan: str) -> bool:
    """Apakah ``kandidat`` adalah nama ASLI agent (dari kolom nama karyawan roster)?

    Diperiksa dua arah karena nama on-air yang terlarang bisa muncul dalam dua bentuk:
    sebagai KATA utuh pada nama karyawan ("Zidan" pada "MUHAMMAD ZIDAN REGI PERMANA"),
    atau sebagai POTONGAN di dalam salah satu katanya ("Erina" pada "LULU SABERINA").
    Keduanya sama-sama nama asli, dan aturannya melarang keduanya.
    """
    k = str(kandidat or "").strip().casefold()
    if len(k) < 3:
        return False
    for kata in str(nama_karyawan or "").split():
        w = kata.strip().casefold()
        if not w:
            continue
        if k == w or (len(k) >= 4 and k in w) or name_similarity(k, w) >= NAME_MATCH_MIN:
            return True
    return False


def agent_name_verdict(pdf_texts: dict, name_online: str, roster_names=(),
                       nama_karyawan: str = "") -> dict:
    """Apakah agent menyebut nama on-air-nya? ``{"match", "detected", "score", "reason"}``.

    ``match`` bernilai ``None`` bila tidak bisa dinilai — NAME ONLINE kosong (agent tidak
    ada di roster) atau tidak ada transkrip. Pemanggil TIDAK boleh menjatuhkan apa pun
    pada keadaan itu: ketiadaan data roster bukan kesalahan agent.
    """
    no = str(name_online or "").strip()
    if not no or not pdf_texts:
        return {"match": None, "detected": [], "score": 0.0,
                "reason": "NAME ONLINE tidak diketahui; nama agent tidak dinilai."}

    gabung = "\n".join(pdf_texts.values())
    terdeteksi = detect_agent_names(gabung)
    skor = max((name_similarity(no, n) for n in terdeteksi), default=0.0)
    if skor >= NAME_MATCH_MIN:
        return {"match": True, "detected": terdeteksi, "score": skor,
                "reason": f"Agent memperkenalkan diri sebagai \"{terdeteksi[0]}\", "
                          f"sesuai NAME ONLINE \"{no}\"."}

    # Kesempatan kedua: NAME ONLINE muncul sebagai kata di transkrip.
    kata = _kata_transkrip(gabung)
    if any(w.casefold() == no.casefold() for w in kata):
        return {"match": True, "detected": terdeteksi, "score": 100.0,
                "reason": f"Nama \"{no}\" disebut di transkrip sesuai NAME ONLINE."}
    if len(no) >= FUZZY_MIN_LEN:
        best = max(((name_similarity(no, w), w) for w in kata), default=(0.0, ""))
        if best[0] >= NAME_MATCH_MIN:
            return {"match": True, "detected": terdeteksi, "score": best[0],
                    "reason": f"Nama \"{best[1]}\" di transkrip cocok dengan NAME ONLINE "
                              f"\"{no}\" ({best[0]:.0f}%)."}

    # KALIMATNYA HANYA MENGUTIP NAMA YANG SUNGGUHAN. ``detect_agent_names`` masih ikut
    # menangkap kata biasa yang kebetulan berdiri sebelum jangkar — pada seed 7 September
    # ia menghasilkan "mematikan", "notifikasi", "nyalain", "bertugas", "dari". Menuliskan
    # itu sebagai "agent memperkenalkan diri sebagai 'mematikan'" adalah tuduhan yang
    # salah sekaligus memalukan, dan QC yang membacanya akan berhenti mempercayai seluruh
    # laporan. Sebuah nama baru boleh dikutip bila ia PERSIS nama on-air seseorang di
    # roster — syarat yang sama dipakai penyaring pemilik panggilan untuk memutuskan
    # sebuah panggilan milik agent lain.
    # NAMA ASLI DILARANG (aturan bisnis 7 September 2026): perkenalan agent WAJIB
    # memakai nama on-air. Agent yang menyebut nama aslinya melanggar, dan itu
    # pelanggaran yang berbeda sifatnya dari sekadar "nama tidak cocok" — ia perbuatan
    # agent, bukan ketidakrapian data roster, jadi kalimatnya harus menyebutkannya.
    asli = [n for n in terdeteksi if _nama_asli_cocok(n, nama_karyawan)]
    if asli:
        return {"match": False, "detected": terdeteksi, "score": skor,
                "reason": f"Agent memperkenalkan diri dengan NAMA ASLI \"{asli[0]}\"; "
                          f"perkenalan wajib memakai nama on-air \"{no}\" sesuai "
                          f"database sales.",
                "point_of_improvement": f"Gunakan nama on-air \"{no}\" saat "
                                         f"memperkenalkan diri, bukan nama asli."}

    kenal = {str(n).strip().casefold() for n in (roster_names or ()) if str(n or "").strip()}
    nyata = [n for n in terdeteksi if n.strip().casefold() in kenal]
    if nyata:
        return {"match": False, "detected": terdeteksi, "score": skor,
                "reason": f"Agent memperkenalkan diri sebagai \"{nyata[0]}\" — nama on-air "
                          f"agent LAIN di roster, bukan NAME ONLINE \"{no}\" milik agent "
                          f"yang ditugaskan pada tiket ini.",
                "point_of_improvement": f"Gunakan nama on-air sendiri, yaitu \"{no}\", "
                                         f"bukan nama on-air agent lain."}
    return {"match": False, "detected": terdeteksi, "score": skor,
            "reason": f"NAME ONLINE \"{no}\" tidak ditemukan di transkrip; nama yang "
                      f"diperkenalkan agent tidak sesuai database sales.",
            "point_of_improvement": f"Sebutkan nama on-air \"{no}\" dengan jelas saat "
                                     f"membuka percakapan."}


# Item scorecard yang mewajibkan agent menyebut nama agent.
AGENT_NAME_ITEM = "SC_CL_2"


def apply_agent_name_verdict(evaluation: dict, verdict: dict) -> dict:
    """Turunkan ``SC_CL_2`` bila nama yang disebut agent tidak sesuai NAME ONLINE.

    Aturan bisnis 7 September 2026, dikonfirmasi Bank Mega: perkenalan agent WAJIB
    memakai nama on-air. Menyebut nama asli, nama on-air orang lain, atau tidak
    menyebutkan nama yang terdaftar sama-sama menggagalkan item ini.

    Ditegakkan di KODE, bukan lewat prompt, karena LLM tidak pernah melihat roster —
    NAME ONLINE hanya ada di database sales, dan meminta model menghafalnya berarti
    menyerahkan vonis pada tebakan.

    HANYA MENURUNKAN. ``verdict["match"] is True`` tidak pernah menaikkan status: LLM
    bisa saja menjatuhkan SC_CL_2 karena sebab lain (mis. nama disebut tetapi bukan di
    segmen pembuka), dan itu penilaian yang tidak boleh ditimpa dari sini.
    ``match is None`` (agent tidak ada di roster) juga tidak mengubah apa pun —
    ketiadaan data roster bukan kesalahan agent.

    ``point_of_improvement`` (14 September 2026) juga ditulis ulang di sini, sejalan
    dengan item lain yang gagal lewat kode (bukan lewat LLM) — tanpa ini kolom Point
    of Improvement di dashboard akan kosong padahal item-nya BELUM_SESUAI, karena
    field itu murni hasil generate LLM dan override kode ini tidak pernah lewat LLM.

    Cakupannya SELURUH rekaman yang dinilai, sesuai aturan KB untuk kategori Greeting:
    "terpenuhi di panggilan MANA PUN (cukup SATU kali) -> SESUAI".
    """
    if not evaluation or (verdict or {}).get("match") is not False:
        return evaluation
    rows = evaluation.get("scorecard_result")
    if not isinstance(rows, list):
        return evaluation
    out, ubah = [], False
    for row in rows:
        r = row if isinstance(row, dict) else {}
        if r.get("item_code") != AGENT_NAME_ITEM or r.get("status") == "BELUM_SESUAI":
            out.append(row)
            continue
        out.append({**r, "status": "BELUM_SESUAI", "item_score": 0,
                    "reason": verdict.get("reason") or
                              "Nama yang disebut agent tidak sesuai NAME ONLINE.",
                    "point_of_improvement": verdict.get("point_of_improvement") or
                              "Sebutkan nama on-air yang benar sesuai database sales."})
        ubah = True
    return {**evaluation, "scorecard_result": out} if ubah else evaluation
