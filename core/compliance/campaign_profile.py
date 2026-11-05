"""Profil perilaku per campaign (NTB vs Cashline): penanda penutup, profil skor, score bomb.

Gerbang data acuan TIDAK diatur di sini: production menagih baris TMS NTB dari DWH
(``data_dwh.fetch_ntb_checked``, snapshot ``reference_data.ntb``) untuk NTB dan TMS
Cashline + Ascend untuk campaign lain (lihat ``worker.tasks.process_transcript``).
"""

from compliance.campaign_kind import is_ntb

# Pola nama produk yang berdampingan dengan kata "setuju" pada Legal Statement —
# dipakai ``recording_validation`` untuk mengenali rekaman yang MENUNTASKAN pengajuan.
# Cashline menyebut produknya (Mega Cashline / Mega Ultima Shield); Legal Statement NTB
# menyebut "kartu kredit" / "kartu utama" (basic) atau kartu tambahan / MUS.
_CASHLINE_CLOSING_PRODUCT = r"mega\s*cash\s*line|mega\s*ultima?t?e?\s*shield|cashline"
_CLOSING_PRODUCT = {
    "cashline": _CASHLINE_CLOSING_PRODUCT,
    "ntb": (
        r"kartu\s+kredit|kartu\s+(?:utama|basic|tambahan|suplemen|supplement)"
        r"|mega\s*ultima?t?e?\s*shield"
    ),
}


def closing_product_pattern(campaign) -> str:
    """Regex nama produk untuk penanda penutup. Campaign tak dikenal -> pola Cashline
    (perilaku semula)."""
    key = "ntb" if is_ntb_campaign(campaign) else (campaign or "").strip().casefold()
    return _CLOSING_PRODUCT.get(key, _CASHLINE_CLOSING_PRODUCT)


# ---------------------------------------------------------------------------
# Profil skor per campaign, dibaca dari ISI evaluasi (bukan dari nama campaign),
# supaya fungsi hitung ulang di ``scoring`` / ``error_codes`` / ``two_pass`` —
# yang menerima satu dict ``evaluation`` saja — tidak perlu berubah tanda tangan.
# Evaluasi NTB selalu membawa kunci ``ntb_interest`` (prompt NTB; kunci minat NTB
# sengaja berbeda dari ``cashline_interest``/``mus_interest``). Evaluasi lain
# (termasuk semua hasil Cashline yang sudah ada) memakai profil Cashline, yaitu
# perilaku semula.
# ---------------------------------------------------------------------------

CASHLINE_CRITICAL_ITEM_CODES = ("SC_CL_4", "SC_CL_23_1", "SC_CL_23_2", "SC_CL_37", "SC_CL_24")

#: SCORE BOMB NTB DINONAKTIFKAN SEMENTARA (keputusan 7 Oktober 2026): score bomb (item
#: kritis 25% dan non-tolerable 10%) berasal dari scorecard Cashline dan perlakuannya
#: akan direvisi untuk NTB. Sampai itu, NTB tidak punya item kritis (tanpa bom 25%) dan
#: bom non-tolerable 0%. Veto non-tolerable (vonis FAIL) TIDAK ikut dimatikan.
NTB_CRITICAL_ITEM_CODES = ()

#: Daftar yang diusulkan saat Fase 1 (asumsi, belum dikonfirmasi Bank Mega; CHANGELOG_ntb
#: Q3): waktu, tiga Legal Statement, dua PDP. Disimpan sebagai bahan revisi nanti.
NTB_CRITICAL_ITEM_CODES_DRAFT = (
    "SC_NTB_4", "SC_NTB_71", "SC_NTB_46", "SC_NTB_72", "SC_NTB_50", "SC_NTB_73",
)

#: Iris SCORE BOMB item non-tolerable BELUM_SESUAI, sebagai pecahan skor maksimal.
#: Cashline 10%. NTB 0: hampir semua item NTB ber-tolerable=NO (70 dari 75), jadi iris
#: 10% per item membuat skor selalu 0. Vonis NTB tetap lewat veto non-tolerable;
#: nilainya menunggu konfirmasi Bank Mega.
CASHLINE_NON_TOLERABLE_BOMB_RATIO = 0.10
NTB_NON_TOLERABLE_BOMB_RATIO = 0.0  # NTB tidak memakai iris 10% per item; lihat NTB_BOMB_GROUPS

#: SCORE BOMB NTB (30 Okt 2026, kesepakatan Bank Mega): SATU iris 25% skor maksimal PER KELOMPOK yang gagal,
#: tanpa syarat skor >= passing grade. Maksimal 4 iris (KTP, Legal Statement, PDP, Final Confirmation).
#: Item non-tolerable di luar kelompok ini tidak memotong skor (hanya memveto vonis).
#: Kelompok item: gagal bila salah satu item BELUM_SESUAI. KTP: lihat ``NTB_KTP_BOMB_KEY``.
NTB_VETO_BOMB_RATIO = 0.25
NTB_BOMB_GROUPS = (
    ("Legal Statement", ("SC_NTB_71", "SC_NTB_72", "SC_NTB_73")),
    ("PDP", ("SC_NTB_46", "SC_NTB_50", "SC_NTB_40")),  # SC_NTB_40 (sharing data) = PDP, keputusan user 3 Nov 2026
    ("Final Confirmation", tuple(f"SC_NTB_{n}" for n in range(54, 71))),
)
#: Kelompok KTP: kunci evaluasi berisi kode item yang datanya SALAH menurut KTP (baris NTB Data Verification
#: MISMATCH dengan nilai KTP/Microsite hasil OCR), diisi saat tiket diproses. Satu atau lebih = satu iris.
NTB_KTP_BOMB_KEY = "ntb_ktp_mismatch"

#: Bobot aditif NTB (scorecard 05102026): NTB 100 + Supplement 25 + MUS 50.
NTB_BASIC_WEIGHT = 100
NTB_SUPPLEMENT_WEIGHT = 25
NTB_MUS_WEIGHT = 50


#: Item scorecard "Agent menyebutkan nama agent" (nama on-air harus sama dengan NAME ONLINE
#: roster; aturan Bank Mega 7 September 2026) tiap campaign. Pembeda B12/B29 dan alasan
#: deterministiknya berlaku untuk semuanya.
AGENT_NAME_ITEM_CODES = ("SC_CL_2", "SC_NTB_2", "SC_RI_2")


def is_ntb_campaign(campaign) -> bool:
    """True untuk campaign NTB. Production: daftar nama di ``NTB_CAMPAIGNS``
    (bawaan "New To Bank Eksternal"), lihat ``campaign_kind.is_ntb``."""
    return is_ntb(campaign)


def agent_from_ntb_tms(campaign) -> bool:
    """True bila agent tiket campaign ini dibaca dari baris TMS NTB (DWH ``reference_data.ntb``), bukan TMS Cashline."""
    return is_ntb_campaign(campaign)


def is_ntb_evaluation(evaluation) -> bool:
    return isinstance(evaluation, dict) and "ntb_interest" in evaluation


#: Awalan kode item scorecard campaign Reinstate (``SC_RI_*``). Evaluasi Reinstate memakai kunci minat
#: ``ntb_*`` (supaya skor aditif dan dashboard NTB mengenalinya), jadi pembedanya adalah kode item.
REINSTATE_ITEM_PREFIX = "SC_RI_"


def is_reinstate_evaluation(evaluation) -> bool:
    """True bila evaluasi ini milik campaign Reinstate (ada item ``SC_RI_*`` di scorecard_result).

    Tabel NTB Data Verification / Supplement Verification / Campaign Interest Verification
    membandingkan ucapan dengan ``tms_ntb`` dan item ``SC_NTB_*``; keduanya tidak berlaku
    untuk Reinstate, jadi tabel itu tidak dibangun."""
    if not isinstance(evaluation, dict):
        return False
    return any(
        str((it or {}).get("item_code") or "").startswith(REINSTATE_ITEM_PREFIX)
        for it in (evaluation.get("scorecard_result") or [])
    )


def _status(evaluation, key):
    block = evaluation.get(key)
    return str((block or {}).get("status") or "").strip().upper() if isinstance(block, dict) else ""


def critical_bomb_enabled(evaluation=None) -> bool:
    """False bila bom item kritis tidak berlaku (NTB, sementara). Nilai
    ``ai_score_critical_compliance_check`` bawaan LLM diabaikan, termasuk pada hasil lama."""
    return not is_ntb_evaluation(evaluation)


def critical_item_codes(evaluation=None) -> tuple:
    return NTB_CRITICAL_ITEM_CODES if is_ntb_evaluation(evaluation) else CASHLINE_CRITICAL_ITEM_CODES


def non_tolerable_bomb_ratio(evaluation=None) -> float:
    return NTB_NON_TOLERABLE_BOMB_RATIO if is_ntb_evaluation(evaluation) else CASHLINE_NON_TOLERABLE_BOMB_RATIO


def ntb_mus_in_scope(evaluation) -> bool:
    """MUS ikut dinilai bila nasabah mengikutinya DAN tidak terhenti Health Declaration."""
    return (
        _status(evaluation, "ntb_mus_interest") == "INTERESTED"
        and _status(evaluation, "ntb_mus_eligibility") != "NOT_ELIGIBLE"
    )


def ntb_product_breakdown(evaluation) -> list:
    """``[(label, bobot), ...]`` produk NTB yang berlaku untuk percakapan ini.

    Kosong bila nasabah tidak menyetujui kartu utama (tidak ada skor maksimal yang bisa
    diturunkan; pemanggil jatuh ke ``maximum_score``)."""
    if _status(evaluation, "ntb_interest") != "INTERESTED":
        return []
    out = [("NTB", NTB_BASIC_WEIGHT)]
    if _status(evaluation, "ntb_supplement_interest") == "INTERESTED":
        out.append(("Supplement", NTB_SUPPLEMENT_WEIGHT))
    if ntb_mus_in_scope(evaluation):
        out.append(("MUS", NTB_MUS_WEIGHT))
    return out
