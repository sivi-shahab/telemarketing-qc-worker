"""Deterministic AI score / AI status (PASS=APPROVE, FAIL=RETURN) from an
evaluation dict.

Mirrors the per-result computation in ``api/routers/stats.py`` (the Results table),
factored here so the Statistics aggregation can count Approve/Return consistently.
Input is an evaluation dict that has ALREADY had approved appeals applied; the QC
status override and the non-tolerable veto are applied by the caller.
"""


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


def max_score(evaluation: dict):
    """Skor maksimal = jumlah bobot produk yang diminati (Mega Cashline 108.75 +
    Mega Ultima Shield 41.25); fallback ke ``maximum_score``."""
    total = 0.0
    found = False
    if (evaluation.get("cashline_interest") or {}).get("status") == "INTERESTED":
        total += 108.75
        found = True
    if (evaluation.get("mus_interest") or {}).get("status") == "INTERESTED":
        total += 41.25
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
    ``item_score = 7.5`` dari bobot 15 — "satu parameter yang terverifikasi benar tetap
    sebuah keberhasilan dan tidak boleh dihukum sama beratnya dengan nol parameter".
    Sampai 31 Agustus 2026 kredit itu DIBUANG karena skor dihitung dari bobot penuh
    tiap item BELUM_SESUAI, sehingga tiket ``010714jUKH`` kehilangan 15, bukan 7,5.

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
    ``no_product_interest``)."""
    if no_product_interest(evaluation):
        return 0
    max_sc = max_score(evaluation)
    if max_sc is None:
        return None
    belum = sum(
        d for it in (evaluation.get("scorecard_result") or [])
        if (d := _item_deduction(it)) is not None
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
            "reason": (it or {}).get("reason"),
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
            "reason": (it or {}).get("reason"),
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
    passing = _numeric_or_none(evaluation.get("passing_grade"))
    if ai_score is not None and passing is not None:
        return "PASS" if ai_score >= passing else "FAIL"
    sv = evaluation.get("ai_status")
    if isinstance(sv, str) and sv.strip():
        return sv.strip().upper()
    return None
