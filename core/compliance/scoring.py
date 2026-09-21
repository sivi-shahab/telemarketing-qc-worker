"""Deterministic AI score / AI status (PASS=APPROVE, FAIL=RETURN) from an
evaluation dict.

Mirrors the per-result computation in ``api/routers/stats.py`` (the Results table),
factored here so the Statistics aggregation can count Approve/Return consistently.
Input is an evaluation dict that has ALREADY had approved appeals applied; the QC
status override and the non-tolerable veto are applied by the caller.
"""
import re

# ``compliance.recording_type.stamp_reason_provenance`` menempelkan kalimat asal
# rekaman ke EKOR ``reason`` tiap baris scorecard, dipisah " - " (mis. "... - Evidence
# diambil dari recording utama."). Berguna di tabel Hasil Scorecard/EvaluationView,
# tapi di kolom SCOREBOMB (Results) ia cuma kebisingan — dan pada tiket yang
# rekamannya sama untuk dua item (mis. SC_CL_8 & SC_CL_28, keduanya soal provisi,
# beda tahap) justru membuat baris SCOREBOMB-nya terbaca identik walau item_code-nya
# beda (permintaan 15 September 2026). Kalimat provenance SELALU jadi kalimat
# TERAKHIR yang ditempel, jadi aman dipotong dengan pola di akhir string.
_SCOREBOMB_PROVENANCE_TAIL_RE = re.compile(
    r"\s*-\s*Evidence (?:diambil dari|tidak disebutkan)[^.]*\.\s*$"
)


def _trim_scorebomb_reason(reason) -> str:
    """``reason`` scorecard tanpa ekor provenance rekaman — lihat komentar di atas."""
    text = str(reason or "")
    trimmed = _SCOREBOMB_PROVENANCE_TAIL_RE.sub("", text).rstrip()
    return trimmed or text


# Pasangan item "Penjelasan X" / "Final Konfirmasi X" (mis. SC_CL_8 & SC_CL_28,
# sama-sama soal provisi) sering menilai FAKTA yang sama dari SATU kutipan bukti,
# terutama pada tiket satu-rekaman (mode "full", bukan dua-tahap) — LLM lalu menulis
# ``reason`` yang nyaris identik untuk keduanya, walau ``requirement``-nya sendiri
# sudah beda tahap. Kalimat LLM TIDAK diedit/ditulis-ulang (terlalu beragam
# strukturnya untuk disisipi kata secara aman, lihat ``stamp_reason_provenance``
# untuk alasan yang sama) — cukup ditempel PENANDA TAHAP di ekornya, dari
# ``category`` baris itu sendiri (pasti ada, bukan tebakan), supaya SC_CL_8 dan
# SC_CL_28 pada tiket yang sama TIDAK PERNAH terbaca sebagai baris yang sama persis
# di kolom SCOREBOMB (permintaan 15 September 2026).
def _stamp_category_reason(reason, category) -> str:
    text = _trim_scorebomb_reason(reason)
    cat = str(category or "").strip()
    if not cat:
        return text
    return f"{text} (tahap: {cat})"


def _to_num(value):
    """Coerce a number/numeric-string to a number (int when whole), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
    else:
        try:
            n = float(value)
        except (TypeError, ValueError):
            return None
    if n != n:  # NaN
        return None
    return int(n) if n == int(n) else n


def _numeric_or_none(value):
    """Return ``value`` as a number (int when whole), or None if not numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value == int(value) else value


def no_product_interest(evaluation: dict) -> bool:
    """True bila nasabah TIDAK berminat pada Mega Cashline MAUPUN Mega Ultima Shield.

    Ini pemicu ZERO-SCORE RULE pada prompt: bila kedua minat bukan "INTERESTED"
    (sehingga ``campaign_interest`` kosong), skor dipaksa 0 dan AI Status FAIL —
    tidak peduli berapa item scorecard yang terpenuhi. Alasannya: panggilan yang
    tidak menghasilkan minat tidak layak dinilai bagus hanya karena prosedurnya
    rapi.

    Sampai 28 Agustus 2026 aturan ini HANYA hidup di prompt, sehingga hitung ulang
    deterministik di modul ini melewatkannya: ``max_score`` jatuh ke
    ``maximum_score`` lalu dikurangi bobot BELUM_SESUAI, menghasilkan skor tinggi
    dan AI Status PASS untuk tiket yang oleh LLM sudah benar dinyatakan FAIL.
    Contoh nyata: 0110505ngB -> LLM 0/FAIL, hitung ulang 101.25/PASS.

    Evaluasi lama yang belum punya blok minat sama sekali dikecualikan (return
    False): tanpa datanya, "tidak berminat" adalah tebakan, dan menebak di sini
    akan menolkan tiket yang tidak bersalah.
    """
    cashline = evaluation.get("cashline_interest")
    mus = evaluation.get("mus_interest")
    if not isinstance(cashline, dict) and not isinstance(mus, dict):
        return False
    cashline_status = (cashline or {}).get("status")
    mus_status = (mus or {}).get("status")
    return cashline_status != "INTERESTED" and mus_status != "INTERESTED"


#: Kategori MUS-Cashline "wajib" asli (3 item, 36.75 sejak 18 September 2026) — item yang
#: TERIKAT pada ``mus_wajib_tidak_dipenuhi``/``mus_exempt`` (lihat pemakaiannya di
#: ``scorecard_score``). SENGAJA dipisah dari MUS Kartu Kredit (di bawah): MUS
#: Kartu Kredit TIDAK punya kewajiban serupa (murni aditif, lihat ``max_score``),
#: jadi tidak boleh ikut kena kompensasi TIDAK_DINILAI milik base MUS.
BASE_MUS_CATEGORIES = {
    "penjelasan mega ultima shield",
    "final konfirmasi mega ultima shield",
    "legal statement mega ultima shield",
}

#: Kategori scorecard yang bobotnya milik Mega Ultima Shield (base, 3 item) PLUS
#: pendaftaran MUS pada kartu kredit non-Cashline (4 item, 13.25 — SC_CL_39..42:
#: Preposisi Penawaran/Disclaimer/Final Konfirmasi/Legal Statement MUS CC, 14
#: September 2026). Dipakai untuk pengelompokan UI/umum saja (mis. menandai "ini
#: bagian keluarga MUS"); untuk logika SKOR yang membedakan wajib vs aditif,
#: pakai ``BASE_MUS_CATEGORIES``, bukan set gabungan ini — lihat
#: ``scorecard_score``.
MUS_CATEGORIES = BASE_MUS_CATEGORIES | {
    "preposisi penawaran mus cc",
    "disclaimer mus cc",
    "final konfirmasi mus cc",
    "legal statement mus cc",
}


def is_mus_item(item) -> bool:
    """True bila item scorecard ini milik salah satu kategori Mega Ultima Shield
    (base ATAU MUS Kartu Kredit). Untuk logika skor yang butuh membedakan
    keduanya, pakai ``is_base_mus_item``."""
    return str((item or {}).get("category") or "").strip().casefold() in MUS_CATEGORIES


def is_base_mus_item(item) -> bool:
    """True bila item scorecard ini milik salah satu KATEGORI MUS-Cashline asli
    (bukan MUS Kartu Kredit) — dipakai KHUSUS oleh kompensasi TIDAK_DINILAI di
    ``scorecard_score`` karena hanya base MUS yang punya bobot "wajib" di
    ``max_score``."""
    return str((item or {}).get("category") or "").strip().casefold() in BASE_MUS_CATEGORIES


def mus_exempt(evaluation: dict) -> bool:
    """True bila tiket ini DIKECUALIKAN dari kewajiban MUS.

    Membaca ``evaluation["mus_exemption"]`` yang sudah diselesaikan lebih dulu oleh
    ``compliance.mus_exemption.resolve`` (transkrip, atau daftar tetap Bank Mega).
    Modul ini sengaja TIDAK memanggil daftar itu sendiri: hitung ulang deterministik
    harus bisa dijalankan dari satu objek ``evaluation`` saja, tanpa perlu tahu
    ``ticket_id`` maupun menyentuh berkas.
    """
    block = evaluation.get("mus_exemption")
    if not isinstance(block, dict):
        return False
    return str(block.get("status") or "").strip().upper() == "EXEMPT"


def mus_wajib_tidak_dipenuhi(evaluation: dict) -> bool:
    """True bila rekaman TIDAK VALID karena nasabah hanya tertarik Mega Cashline.

    Aturan Bank Mega 8 September 2026: rekaman baru valid bila nasabah tertarik Mega
    Cashline DAN Mega Ultima Shield. Cashline saja tidak valid, kecuali ada pengecualian
    (lihat ``compliance.mus_exemption``).

    ``NOT_STATED`` diperlakukan SAMA dengan ``NOT_INTERESTED`` (keputusan 8 September
    2026): agent yang tidak pernah menawarkan MUS justru bentuk pelanggaran yang paling
    ingin ditangkap aturan ini, jadi membebaskannya akan melubangi aturannya sendiri.

    Prasyaratnya nasabah tertarik Mega Cashline. Bila Cashline pun tidak diminati,
    yang berlaku ZERO-SCORE RULE (``no_product_interest``), bukan aturan ini — dan
    keduanya tidak boleh menghukum tiket yang sama dua kali.

    ``mus_exemption.status == "PENDING"`` (gerbang dokumen konfirmasi pengecualian
    MUS penyakit whitelist, 11 September 2026 — lihat
    ``error_codes.apply_mus_exception_document_status``) diperlakukan SAMA dengan
    ``EXEMPT``: penangguhan bukan vonis, jadi belum boleh memotong bobot MUS selagi
    tenggat H+2 masih berjalan.
    """
    if (evaluation.get("cashline_interest") or {}).get("status") != "INTERESTED":
        return False
    if (evaluation.get("mus_interest") or {}).get("status") == "INTERESTED":
        return False
    if mus_exempt(evaluation):
        return False
    block = evaluation.get("mus_exemption")
    if isinstance(block, dict) and str(block.get("status") or "").strip().upper() == "PENDING":
        return False
    return True


def max_score(evaluation: dict):
    """Skor maksimal = jumlah bobot produk yang diminati (Mega Cashline 100 +
    Mega Ultima Shield 36.75 + MUS Kartu Kredit 13.25); fallback ke ``maximum_score``.

    Revisi 18 September 2026 (``Score Card Cashline 18092026.xlsx``): Mega Ultima
    Shield naik 35,5 -> 36,75 karena SATU item baru di kategori "Final Konfirmasi
    Mega Ultima Shield" — ``SC_CL_43`` "premi yang telah dibayarkan tidak dapat
    dikembalikan apabila customer mengajukan pembatalan" (bobot 1,25, Major/Not
    tolerable). Itu SATU-SATUNYA perubahan bobot pada revisi itu; Mega Cashline
    (100) dan MUS Kartu Kredit (13,25) tidak bergerak, sehingga "Total Score" xlsx
    naik 148,75 -> 150 dan passing grade 133,875 -> 135.

    Kodenya sengaja ``SC_CL_43`` (append), BUKAN disisipkan sebagai SC_CL_35 dengan
    menggeser nomor sesudahnya: seluruh ``result_json`` tiket lama sudah menyimpan
    SC_CL_35/36 dengan arti yang berbeda, dan renumbering akan membuat riwayat itu
    salah baca. Diukur sebelum diterapkan: pada 50 tiket cashline yang sudah ``done``,
    kenaikan penyebut ini TIDAK membalik satu pun vonis PASS/FAIL (tiket lama tidak
    punya SC_CL_43 sehingga tidak kehilangan apa pun, dan skornya ikut naik 1,25
    sementara batas lulusnya hanya naik 1,125).

    Revisi scorecard v4 (14 September 2026, ``Score Card Cashline 14092026.xlsx``
    tab "cashline + mega ultima shield"): SELURUH bobot direvisi ulang, bukan cuma
    penambahan MUS Kartu Kredit — Mega Cashline turun dari 108,75 ke 100 (lihat
    weight per-item baru di ``scorecard_text`` campaign, mis. Verifikasi statik
    15->10/item, Verifikasi Dinamis 15->10 total), dan Mega Ultima Shield turun
    dari 41,25 ke 35,5 (SC_CL_19 5->3, SC_CL_21 4.5->3, SC_CL_33 3.25->2.25,
    SC_CL_35 3.5->2.25 — total -5,75). Total gabungan saat itu = 100+35.5+13.25
    = 148,75; sejak revisi 18 September 2026 di atas menjadi 100+36.75+13.25 = 150.

    Bobot MUS juga dihitung ketika MUS WAJIB tetapi tidak dipenuhi
    (``mus_wajib_tidak_dipenuhi``). Di situlah aturan 8 September 2026 menggigit:
    keringanan lama menurunkan skor maksimal sehingga tiket cashline-saja bisa
    LULUS dengan nilai penuh. Sekarang penyebutnya tetap penuh dan item MUS
    dipotong penuh, sehingga tiket semacam itu mentok di 100/136.75 = 73,1% — di
    bawah batas lulus 90%, jadi TIDAK LULUS tanpa perlu aturan veto terpisah.

    Tiket yang DIKECUALIKAN tetap memakai perhitungan lama (hanya Mega Cashline).

    MUS Kartu Kredit (SC_CL_39..42 — Preposisi Penawaran 1.5, Disclaimer MUS CC 2,
    Final Konfirmasi MUS CC 2.25, Legal Statement MUS CC 7.5, total 13.25, 14
    September 2026) TIDAK punya padanan ``mus_wajib_tidak_dipenuhi``: berbeda dari
    MUS-Cashline (wajib ditawarkan ke SEMUA nasabah cashline+MUS), MUS Kartu
    Kredit hanya relevan bila nasabah memang punya kartu kredit terpisah — fakta
    yang sistem ini belum punya datanya. Jadi murni ADITIF: bobotnya HANYA masuk
    penyebut saat ``mus_cc_interest`` benar-benar "INTERESTED" (dibahas &
    disetujui di transkrip); ``NOT_STATED``/``NOT_INTERESTED`` tidak menaikkan
    penyebut maupun memotong skor (keputusan bisnis, bukan celah).
    """
    total = 0.0
    found = False
    if (evaluation.get("cashline_interest") or {}).get("status") == "INTERESTED":
        total += 100
        found = True
    if (evaluation.get("mus_interest") or {}).get("status") == "INTERESTED":
        total += 36.75
        found = True
    elif mus_wajib_tidak_dipenuhi(evaluation):
        total += 36.75
        found = True
    if (evaluation.get("mus_cc_interest") or {}).get("status") == "INTERESTED":
        total += 13.25
        found = True
    if found:
        return int(total) if total == int(total) else total
    return _to_num(evaluation.get("maximum_score"))


def _item_deduction(item) -> "float | None":
    """Berapa poin yang HILANG dari satu item scorecard (None bila tidak mengurangi).

    Hanya item ``BELUM_SESUAI`` yang mengurangi, dan bawaannya seluruh bobot item itu.
    KREDIT PARSIAL dihormati bila ada: ``item_score`` yang bernilai di ANTARA 0 dan
    ``weight`` berarti sebagian kewajiban benar-benar dipenuhi, sehingga yang hilang
    tinggal ``weight - item_score``.

    Satu-satunya pemakainya hari ini adalah SKOR BERTINGKAT SC_CL_24 (prompt sejak
    28 Agustus 2026): verifikasi dinamis yang berhasil 1 dari 2 ditulis
    ``item_score = weight / 2`` — "satu parameter yang terverifikasi benar tetap
    sebuah keberhasilan dan tidak boleh dihukum sama beratnya dengan nol parameter".
    Sampai 31 Agustus 2026 kredit itu DIBUANG karena skor dihitung dari bobot penuh
    tiap item BELUM_SESUAI, sehingga tiket ``010714jUKH`` kehilangan bobot penuh,
    bukan separuhnya. Bobot SC_CL_24 sendiri turun dari 15 ke 10 pada revisi
    scorecard v4 (14 September 2026) — jadi ``item_score`` untuk verified_count=1
    ikut turun dari 7,5 ke 5, TANPA mengubah aturan "separuh bobot" ini sama sekali;
    lihat prompt ("SKOR SC_CL_24 BERTINGKAT") untuk angka yang berlaku saat ini.

    Yang di LUAR rentang (0, weight) sengaja jatuh ke potongan penuh:

    * ``item_score`` hilang/bukan angka -> hasil lama yang belum menuliskannya;
    * ``item_score <= 0``               -> memang tidak ada yang dipenuhi;
    * ``item_score >= weight``          -> item GAGAL tetapi diberi nilai penuh. Itu
      kontradiksi, dan pada korpus 98 tiket kekeliruan semacam itu memang ada di sisi
      sebaliknya (SC_CL_23_1/23_2 berstatus SESUAI tetapi ``item_score = 0``, 19 dan 14
      kali). Karena itu ``item_score`` TIDAK dijadikan sumber skor secara umum —
      hanya dipakai untuk MERINGANKAN item yang sudah pasti gagal.
    """
    if (item or {}).get("status") != "BELUM_SESUAI":
        return None
    weight = _to_num((item or {}).get("weight"))
    if weight is None:
        return None
    earned = _to_num((item or {}).get("item_score"))
    if earned is not None and 0 < earned < weight:
        return weight - earned
    return weight


def scorecard_score(evaluation: dict):
    """Skor scorecard = skor maksimal dikurangi potongan tiap item BELUM_SESUAI.

    Potongannya seluruh bobot item, KECUALI item yang membawa kredit parsial —
    lihat ``_item_deduction``.

    ZERO-SCORE RULE didahulukan: nasabah yang tidak berminat pada kedua produk
    mendapat 0, berapa pun item scorecard yang terpenuhi (lihat
    ``no_product_interest``).

    MUS WAJIB TAPI TIDAK DIPENUHI: item MUS yang masih ditandai ``TIDAK_DINILAI``
    dipotong PENUH. Tanpa ini ``max_score`` sudah naik ke 150 sementara 11 item MUS
    tidak dipotong apa pun, sehingga tiket cashline-saja justru mendapat 41.25 poin
    gratis — kebalikan dari maksud aturannya. Yang dipotong di sini HANYA yang
    ``TIDAK_DINILAI``; item MUS yang sudah dinilai ``BELUM_SESUAI`` sudah ditangani
    ``_item_deduction`` dan tidak boleh dipotong dua kali, dan yang ``SESUAI``
    memang benar-benar dikerjakan agent sehingga tetap dihargai.

    Cabang ini praktis hanya menyentuh hasil LAMA (prompt sebelum v80 melewati item
    MUS sebagai ``TIDAK_DINILAI``). Untuk hasil baru, prompt sudah menilai item MUS
    apa adanya, jadi cabang ini tidak menemukan apa-apa dan skornya bergradasi
    mengikuti apa yang sungguh dilakukan agent.

    Sengaja pakai ``is_base_mus_item`` (BUKAN ``is_mus_item``): item MUS Kartu
    Kredit (SC_CL_39..42) yang ``TIDAK_DINILAI`` (kasus NORMAL sejak 14 September
    2026 — lihat MUS CC SCORECARD CONDITIONAL RULE di prompt) TIDAK boleh ikut
    kompensasi ini, karena bobotnya memang tidak pernah masuk ``max_score`` untuk
    tiket semacam itu (murni aditif). Memakai set gabungan di sini akan memotong
    13,25 ekstra dari skor tiket yang sebetulnya sah tidak menawarkan MUS CC."""
    if no_product_interest(evaluation):
        return 0
    max_sc = max_score(evaluation)
    if max_sc is None:
        return None
    items = evaluation.get("scorecard_result") or []
    belum = sum(d for it in items if (d := _item_deduction(it)) is not None)
    if mus_wajib_tidak_dipenuhi(evaluation):
        belum += sum(
            w for it in items
            if is_base_mus_item(it)
            and str((it or {}).get("status") or "").strip().upper() == "TIDAK_DINILAI"
            and (w := _to_num((it or {}).get("weight"))) is not None
        )
    score = max_sc - belum
    return int(score) if score == int(score) else score


def has_blocking_intolerable_item(evaluation: dict) -> bool:
    """True bila ada item scorecard non-tolerable (tolerable=NO) yang masih
    BELUM_SESUAI — memaksa AI status RETURN (FAIL) berapapun skornya."""
    for item in evaluation.get("scorecard_result") or []:
        tol = str((item or {}).get("tolerable") or "").strip().upper()
        st = str((item or {}).get("status") or "").strip().upper()
        if tol == "NO" and st == "BELUM_SESUAI":
            return True
    return False


# Iris SCORE BOMB, sebagai pecahan dari ``maximum_score``:
#
# * item KRITIS (``CRITICAL_ITEM_CODES``: SC_CL_4, SC_CL_23_1, SC_CL_23_2, SC_CL_37,
#   SC_CL_24) -> 25% per item, sudah lama berlaku lewat
#   ``ai_score_critical_compliance_check``;
# * item NON-TOLERABLE lain yang BELUM_SESUAI -> 10% per item (31 Agustus 2026).
#
# Sebabnya: sebuah item ``tolerable = NO`` yang BELUM_SESUAI memveto tiket menjadi
# Not Qualified berapa pun skornya, tetapi potongannya selama ini hanya sebesar bobot
# itemnya sendiri — sering 1-3 dari 150. Hasilnya tiket Not Qualified yang berskor
# 149/150: vonis dan angkanya saling bertentangan. 7 dari 27 tiket Not Qualified pada
# korpus 98 tiket berada dalam keadaan itu.
NON_TOLERABLE_BOMB_RATIO = 0.10


def non_tolerable_bomb(evaluation: dict) -> float:
    """Total iris 10% untuk item NON-TOLERABLE yang BELUM_SESUAI, sebagai angka NEGATIF.

    Item KRITIS sengaja dikecualikan: kegagalannya sudah membawa iris 25% sendiri
    lewat ``ai_score_critical_compliance_check`` (lihat ``_sync_critical_compliance``),
    dan menghitungnya lagi di sini berarti mengebom satu kegagalan dua kali.

    Mengembalikan 0.0 bila tidak ada yang memenuhi syarat atau ``maximum_score``
    tidak diketahui.
    """
    from compliance.error_codes import CRITICAL_ITEM_CODES

    ms = _to_num(evaluation.get("maximum_score"))
    if not ms:
        return 0.0
    n = sum(
        1 for it in (evaluation.get("scorecard_result") or [])
        if str((it or {}).get("tolerable") or "").strip().upper() == "NO"
        and str((it or {}).get("status") or "").strip().upper() == "BELUM_SESUAI"
        and (it or {}).get("item_code") not in CRITICAL_ITEM_CODES
    )
    if not n:
        return 0.0
    return -(ms * NON_TOLERABLE_BOMB_RATIO) * n


CRITICAL_BOMB_RATIO = 0.25


def score_bomb_items(evaluation: dict) -> list:
    """SEMUA item yang mengebom skor tiket ini, satu daftar untuk ditampilkan.

    Kolom "SCOREBOMB" di daftar Results dan panel Critical Compliance Check di detail
    tiket selama ini HANYA memuat pelanggaran kritis (iris 25%), sehingga iris 10%
    untuk item non-tolerable lain memotong skor tanpa pernah terlihat di mana pun.
    Fungsi ini menyatukan keduanya supaya kedua permukaan menampilkan hal yang sama:

        [{"item_code", "requirement", "reason", "status", "ratio", "amount"}, ...]

    * ``ratio``  = 0.25 untuk item kritis, 0.10 untuk non-tolerable lainnya;
    * ``amount`` = potongannya dalam poin (angka NEGATIF), yaitu ``ratio * maximum_score``.

    Item kritis diambil dari ``critical_compliance_check.checked_items`` yang berstatus
    FAIL — sumber yang sama dengan panelnya — sehingga urutan & teksnya tidak bisa
    berbeda dari yang sudah tampil. Item non-tolerable diambil dari scorecard, dengan
    item kritis dikecualikan agar tidak muncul dua kali.
    """
    from compliance.error_codes import CRITICAL_ITEM_CODES

    ms = _to_num(evaluation.get("maximum_score")) or 0
    out = []
    ccc = evaluation.get("critical_compliance_check") or {}
    for it in (ccc.get("checked_items") or []):
        if str((it or {}).get("status") or "").strip().upper() != "FAIL":
            continue
        out.append({
            "item_code": (it or {}).get("item_code"),
            "requirement": (it or {}).get("requirement"),
            "reason": _trim_scorebomb_reason((it or {}).get("reason")),
            "status": "FAIL",
            "ratio": CRITICAL_BOMB_RATIO,
            "amount": -(ms * CRITICAL_BOMB_RATIO) if ms else 0,
        })
    for it in (evaluation.get("scorecard_result") or []):
        code = (it or {}).get("item_code")
        if code in CRITICAL_ITEM_CODES:
            continue
        if str((it or {}).get("tolerable") or "").strip().upper() != "NO":
            continue
        if str((it or {}).get("status") or "").strip().upper() != "BELUM_SESUAI":
            continue
        out.append({
            "item_code": code,
            "requirement": (it or {}).get("requirement"),
            "reason": _stamp_category_reason((it or {}).get("reason"), (it or {}).get("category")),
            "status": "BELUM_SESUAI",
            "ratio": NON_TOLERABLE_BOMB_RATIO,
            "amount": -(ms * NON_TOLERABLE_BOMB_RATIO) if ms else 0,
        })
    return out


def phase3_score(evaluation: dict):
    """SKOR AKHIR sebuah tiket — satu-satunya tempat rumusnya ditulis.

        phase3 = phase2 (scorecard) + ai_score_verification
                 + ai_score_critical_compliance_check + non_tolerable_bomb

    Sampai 31 Agustus 2026 rumus ini disalin di EMPAT tempat (``base_ai_status``,
    detail tiket, daftar Results, ekspor XLSX). Menambah suku keempat ke salinan
    yang tercecer adalah cara paling mudah membuat satu permukaan berbeda dari yang
    lain, jadi keempatnya kini memanggil fungsi ini.

    ``ai_score_verification`` tinggal 0 sejak verifikasi cashline & card holder
    dipropagasikan ke scorecard; sukunya dipertahankan supaya hasil LAMA yang masih
    membawa angka di sana tetap dihitung sama seperti dulu.

    Mengembalikan None bila tidak satu pun komponennya diketahui.
    """
    phase2 = scorecard_score(evaluation)
    verif = _to_num(evaluation.get("ai_score_verification"))
    critical = _to_num(evaluation.get("ai_score_critical_compliance_check"))
    bomb = non_tolerable_bomb(evaluation)
    if phase2 is None and verif is None and critical is None and not bomb:
        return None
    total = (phase2 or 0) + (verif or 0) + (critical or 0) + bomb
    return int(total) if total == int(total) else total


def passing_grade(evaluation: dict):
    """Batas lulus = 90% dari skor maksimal, dibulatkan 2 desimal.

    Diturunkan dari ``max_score`` dan BUKAN dibaca apa adanya dari
    ``evaluation["passing_grade"]``. Sejak aturan MUS 8 September 2026 skor maksimal
    bisa berubah setelah hasil ditulis (tiket cashline-saja naik 108.75 -> 150), dan
    angka batas lulus bawaan LLM ikut basi bersamanya. Membandingkan skor baru ke
    batas lulus lama persis mengulang cacat yang dicatat di ``no_product_interest``:
    satu sisi rumus diperbarui, sisi lain tertinggal.

    Pembulatan 2 desimal mengikuti aturan yang sama di prompt, sehingga hasilnya
    identik dengan angka yang selama ini ditulis LLM (108.75 -> 97.88; 150 -> 135.0)
    dan tidak menggeser satu pun tiket lama yang skor maksimalnya tidak berubah.
    """
    max_sc = max_score(evaluation)
    if max_sc is None:
        return _numeric_or_none(evaluation.get("passing_grade"))
    grade = round(max_sc * 0.9, 2)
    return int(grade) if grade == int(grade) else grade


def base_ai_status(evaluation: dict):
    """Base AI status 'PASS'/'FAIL' from the deterministic score vs passing grade
    (fallback to the LLM ``ai_status``), WITHOUT the QC override / non-tolerable veto.
    Returns None when it cannot be determined."""
    # ZERO-SCORE RULE: tanpa minat pada produk mana pun, vonisnya FAIL tanpa
    # membandingkan skor ke passing grade — skornya sudah dipaksa 0 di atas, tetapi
    # dinyatakan eksplisit di sini supaya tidak bergantung pada passing_grade > 0.
    if no_product_interest(evaluation):
        return "FAIL"
    ai_score = phase3_score(evaluation)
    passing = passing_grade(evaluation)
    if ai_score is not None and passing is not None:
        return "PASS" if ai_score >= passing else "FAIL"
    sv = evaluation.get("ai_status")
    if isinstance(sv, str) and sv.strip():
        return sv.strip().upper()
    return None
