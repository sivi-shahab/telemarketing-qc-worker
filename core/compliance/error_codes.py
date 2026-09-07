"""Single source of truth for QA error codes.

Error codes come from distinct sources; this module groups them so the mapping is
easy to understand and is consumed by BOTH the dashboard (via the ``/result`` API,
which injects ``evaluation.error_code_table``) and the XLSX export.

Sources:
  - Scorecard            : items BELUM_SESUAI (derived B10/B12/B18) + LLM ``error_codes``
  - Card Holder Verif.   : per-field MISMATCH -> B17 (SKIPPED_NULL carries no error;
                           baris dinamis yang salah satu sisi pembandingnya kosong
                           diturunkan ke SKIPPED_NULL — normalize_dynamic_verification)
  - Cashline Data Verif. : per-field MISMATCH -> B02/B03/B05 risk-graded (SKIPPED_NULL none)
  - Dokumen pendukung    : B09 (tenggat H+2 lewat tanpa dokumen) & C03 (jenis
                           dokumen salah) — lihat ``document_error_code_rows``
"""

import re
from datetime import datetime

from compliance.riplay import check_tms_against_tnc

# --- Source groups ----------------------------------------------------------
SOURCE_SCORECARD = "scorecard"
SOURCE_CARD_HOLDER = "card_holder"
SOURCE_CASHLINE = "cashline_data"
# Dokumen pendukung (KTP/KK/NPWP/buku tabungan): B09 saat tenggat H+2 lewat tanpa
# dokumen, C03 saat jenisnya keliru. Berdiri sendiri dari scorecard karena pemicunya
# bukan transkrip melainkan berkas yang diunggah — atau tidak diunggah.
SOURCE_DOCUMENT = "document"
# "others" is a QC-add-only source (see appeal_kind='add'): an error code that does
# not belong to any of the structured sources. It is display-only — it never has an
# evaluation item/field to attach to, so it does not change the score.
SOURCE_OTHERS = "others"

SOURCE_LABELS = {
    SOURCE_SCORECARD: "Scorecard",
    SOURCE_CARD_HOLDER: "Card Holder Verification",
    SOURCE_CASHLINE: "Cashline Data Verification",
    SOURCE_DOCUMENT: "Dokumen Pendukung",
    SOURCE_OTHERS: "Others",
}

# --- Catalog: code -> {desc, error_type, error_category, source, trigger, risk_base} ---
#
# ``desc`` / ``error_type`` / ``error_category`` / ``risk_base`` DISALIN APA ADANYA dari
# sheet resmi QC — "Error Reason - Telemarketing QC_05082025.xlsx", kolom Details Error /
# Error Type / Error Categories / Risk Base (lihat ``csv_bank/``). Sengaja tidak
# ditulis ulang dengan kalimat sendiri (permintaan 14 Agustus 2026): sheet itulah
# kosakata yang dipakai QC sehari-hari, dan wording tandingan membuat dua pihak
# menyebut kesalahan yang sama dengan nama berbeda. Kalau sheet-nya diperbarui,
# perbarui blok ini — jangan mengarang padanan baru.
#
# ``source`` dan ``trigger`` TIDAK ada di sheet: keduanya milik sistem ini, yaitu dari
# blok evaluasi mana sebuah kode terbit dan pada kondisi apa.
ERROR_CODES = {
    "B02": {
        "desc": "Salah input data dengan risiko low yang berakibat pada kesalahan proses transaksi",
        "error_type": "Error - Human",
        "error_category": "Data Input",
        "source": SOURCE_CASHLINE,
        "trigger": "Field cashline_data_verification MISMATCH/SKIPPED_NULL — risiko rendah",
        "risk_base": "L",
    },
    "B03": {
        "desc": "Salah input data dengan risiko medium yang berakibat pada kesalahan proses transaksi",
        "error_type": "Error - Human",
        "error_category": "Data Input",
        "source": SOURCE_CASHLINE,
        "trigger": "Field cashline_data_verification MISMATCH/SKIPPED_NULL — risiko menengah (data finansial)",
        "risk_base": "M",
    },
    "B05": {
        "desc": "Salah input data dengan risiko tinggi yang dapat berakibat financial loss",
        "error_type": "Error - Human",
        "error_category": "Data Input",
        "source": SOURCE_CASHLINE,
        "trigger": "Field cashline_data_verification MISMATCH/SKIPPED_NULL — risiko tinggi",
        "risk_base": "H",
    },
    # Tidak diterbitkan sistem: salah jenis dokumen memakai C03 (lihat di bawah).
    # Tetap dikatalogkan karena B08 ada di sheet — daftar ini menggambarkan kosakata
    # QC, bukan hanya kode yang kebetulan terbit.
    "B08": {
        "desc": "Type File Doc. terlampir kurang/tidak sesuai",
        "error_type": "Error - Human",
        "error_category": "Dokumen Pendukung",
        "source": SOURCE_DOCUMENT,
        "trigger": "(tidak diterbitkan sistem — lihat C03)",
        "risk_base": "L",
    },
    # Tenggat H+2 lewat tanpa dokumen. Dibebankan ke AGENT (Error - Human, Risk Base
    # M) meski berkasnya sendiri datang dari nasabah: yang dinilai bukan siapa yang
    # membuat berkas, melainkan kelalaian menagih kelengkapan sampai tenggatnya
    # habis. Bandingkan dengan C03 — di sana berkasnya DATANG, hanya keliru jenis,
    # dan keliru memilih berkas memang pekerjaan nasabah.
    "B09": {
        "desc": "Doc Unclear/ Buram / Tidak Jelas/ Expired/Tidak Melampirkan Doc pendukung",
        "error_type": "Error - Human",
        "error_category": "Dokumen Pendukung",
        "source": SOURCE_DOCUMENT,
        "trigger": "Tenggat H+2 lewat dan dokumen pendukung yang diminta belum diunggah",
        "risk_base": "M",
    },
    "B10": {
        "desc": "Inaccurate Product Feature/Script/Fee",
        "error_type": "Error - Human",
        "error_category": "TnC Product",
        "source": SOURCE_SCORECARD,
        "trigger": ("Item BELUM_SESUAI di kategori Penjelasan Mega Cashline, "
                    "Final Konfirmasi Mega Cashline, atau Final Konfirmasi Mega Ultima Shield"),
        "risk_base": "M",
    },
    "B11": {
        "desc": "Ketentuan Pembelian Product (Subscription Requirement)",
        "error_type": "Error - Human",
        "error_category": "TnC Product",
        "source": SOURCE_SCORECARD,
        "trigger": "Pelanggaran aturan produk pada transkrip",
        "risk_base": "M",
    },
    "B12": {
        "desc": ("Agent tidak menyebutkan nama, tidak menyebutkan dari Bank mega atau "
                 "hal lain yang berhubungan dengan standard script"),
        "error_type": "Error - Human",
        "error_category": "Probbing",
        "source": SOURCE_SCORECARD,
        "trigger": "Item BELUM_SESUAI di kategori Greeting (SC_CL_1/2/3)",
        "risk_base": "L",
    },
    "B13": {
        "desc": "Voice Mistake",
        "error_type": "Error - Human",
        "error_category": "Voice Mistake",
        "source": SOURCE_SCORECARD,
        "trigger": "Pengungkapan data verifikasi dinamis sebelum nasabah",
        "risk_base": "M",
    },
    "B15": {
        "desc": "Open data yang termasuk data statik namun belum verifikasi",
        "error_type": "Error - Human",
        "error_category": "Open Data",
        "source": SOURCE_SCORECARD,
        "trigger": "Agen mengungkap data statik (SC_CL_23_1/23_2) sebelum nasabah",
        "risk_base": "H",
    },
    "B16": {
        "desc": "Verfikasi Statik Berhasil, Verifikasi Dinamik kurang/tidak sesuai",
        "error_type": "Error - Human",
        "error_category": "Verification",
        "source": SOURCE_SCORECARD,
        "trigger": "SC_CL_23_1 & 23_2 SESUAI tetapi SC_CL_24 BELUM_SESUAI",
        "risk_base": "M",
    },
    "B17": {
        "desc": "Tidak ada Verifikasi/ verifikasi statik kurang/tidak berhasil",
        "error_type": "Error - Human",
        "error_category": "Verification",
        "source": SOURCE_CARD_HOLDER,
        "trigger": "Field card_holder_verification MISMATCH/SKIPPED_NULL, atau SC_CL_23 BELUM_SESUAI",
        "risk_base": "H",
    },
    "B18": {
        "desc": "Tidak Ada Legal Statement",
        "error_type": "Error - Human",
        "error_category": "Legal Statement",
        "source": SOURCE_SCORECARD,
        "trigger": "Item BELUM_SESUAI di kategori Legal Statement (SC_CL_37/38)",
        "risk_base": "H",
    },
    "B19": {
        "desc": "Menjanjikan hal-hal diluar kewenangan",
        "error_type": "Error - Human",
        "error_category": "Over Promise",
        "source": SOURCE_SCORECARD,
        "trigger": "Janji di luar kewenangan pada transkrip",
        "risk_base": "M",
    },
    "B20": {
        "desc": "Offering bukan kepada CH",
        "error_type": "Error - Human",
        "error_category": "Offering bukan kepada CH",
        "source": SOURCE_SCORECARD,
        "trigger": "Penawaran tidak ditujukan kepada pemegang kartu utama",
        "risk_base": "H",
    },
    "B24": {
        "desc": "Customer Cancel Supplement (crosselling), namun sudah tersubmit",
        "error_type": "Error - Human",
        "error_category": "TnC Product",
        "source": SOURCE_SCORECARD,
        "trigger": "Pembatalan kartu suplemen tetapi proses tetap dilanjutkan",
        "risk_base": "L",
    },
    "B26": {
        "desc": "Legal Statement Bersyarat",
        "error_type": "Error - Human",
        "error_category": "Legal Statement",
        "source": SOURCE_SCORECARD,
        "trigger": "Nasabah mengajukan syarat/kondisi untuk menerima penawaran",
        "risk_base": "M",
    },
    # B27 & B28 — dua kode baru pada sheet Error Reason Bank Mega
    # (Error Reason - Telemarketing QC_28082026.xlsx). desc / error_type /
    # error_category disalin APA ADANYA dari sheet, sesuai aturan berkas ini.
    #
    # Bedanya B27 dengan B20: B20 soal penawaran yang tidak ditujukan kepada PEMEGANG
    # KARTU UTAMA (mis. jatuh ke pemegang suplemen atau keluarganya) — orangnya tetap
    # ada di daftar undangan. B27 soal orang yang memang TIDAK ADA dalam daftar
    # undangan campaign sama sekali (NTB Eksternal).
    "B27": {
        "desc": "Offering bukan kepada Nasabah terundang",
        "error_type": "Error - Human",
        # Sheet menaruh B27 di kategori yang SAMA dengan B20 ("Offering bukan kepada
        # CH"); yang membedakan keduanya hanya kolom Details Error. Disalin apa adanya
        # — jangan "dirapikan" menjadi kategori sendiri, karena kolom inilah yang
        # dipakai dashboard sebagai Failure Category dan harus cocok dengan sheet.
        "error_category": "Offering bukan kepada CH",
        "source": SOURCE_SCORECARD,
        "trigger": "Melakukan penawaran bukan kepada nasabah terundang (NTB Eksternal)",
        "risk_base": "M",
    },
    # B28 TIDAK diturunkan dari scorecard_result seperti kode B lainnya, melainkan dari
    # ``badword_check`` pada evaluasi: satu temuan badword = satu baris B28. Karena itu
    # sumbernya SOURCE_OTHERS — barisnya tidak menempel pada satu item_code scorecard.
    # Sampai 28 Agustus 2026 temuan badword hanya tampil di tabel Badword Summary dan
    # tidak pernah menjadi error code; sheet bank kini menamainya secara resmi.
    "B28": {
        "desc": (
            "Melakukan penawaran dengan kata atau kalimat tidak sopan, sarkas, "
            "tidak pantas dan bersifat menyinggung"
        ),
        "error_type": "Error - Human",
        "error_category": "Inappropriate Language",
        "source": SOURCE_OTHERS,
        "trigger": "Ucapan agent bersentimen negatif kepada nasabah (badword_check)",
        "risk_base": "M",
    },
    # Salah jenis dokumen (14 Agustus 2026): berkasnya DATANG, hanya bukan jenis yang
    # diminta — mis. KTP diunggah ke slot NPWP.
    #
    # Dipakai deret C, bukan B08, karena dokumen pendukung adalah berkas yang
    # diserahkan NASABAH; memilih berkas yang keliru adalah kegagalan sisi nasabah.
    # Karena itu Error Type "Error - Customer" dan Risk Base O (System) — TIDAK masuk
    # Total Risk, jadi tidak menaikkan Error Rate siapa pun.
    #
    # Batasnya dengan B09 sengaja tegas: C03 soal berkas yang SALAH, B09 soal berkas
    # yang TIDAK PERNAH DATANG sampai tenggat. Yang kedua tetap tanggungan agent,
    # karena menagih kelengkapan sebelum tenggat habis adalah pekerjaannya.
    #
    # Angka risk_base disalin dari sheet dan tidak boleh menyimpang. Kalau salah
    # jenis dokumen suatu saat harus membebani agent, yang diubah adalah KODENYA
    # (ke B08, Risk Base L), bukan risk_base C03.
    "C03": {
        "desc": "Doc Unclear/ Buram / Tidak Jelas/ Expired/Tidak Melampirkan Doc pendukung",
        "error_type": "Error - Customer",
        "error_category": "Dokumen Pendukung",
        "source": SOURCE_DOCUMENT,
        "trigger": "Dokumen yang diunggah bukan jenis yang diminta pada slot tersebut",
        "risk_base": "O",
    },
}

# Flat code -> Indonesian description (kept for convenience / imports).
ERROR_DESC_ID = {code: meta["desc"] for code, meta in ERROR_CODES.items()}


def error_type_of(code) -> str:
    """Kolom "Error Type" pada sheet QC, mis. ``"Error - Human"``. Kosong bila kode
    tidak ada di katalog (termasuk kode gabungan ``"B02/B03/B05"``)."""
    return (ERROR_CODES.get(code) or {}).get("error_type") or ""


def error_category_of(code) -> str:
    """Kolom "Error Categories" pada sheet QC, mis. ``"Data Input"``."""
    return (ERROR_CODES.get(code) or {}).get("error_category") or ""

# New-joiner rule: when a submission's agent joined < 18 days before submit_time,
# each Error Code whose Risk Base is L or M is softened to this value (H is kept).
NEW_JOINER_RISK_BASE = "N"


# Kode yang TIDAK ikut dilunakkan aturan new joiner (keputusan 14 Agustus 2026).
# Pelunakan itu memaafkan kesalahan yang wajar dilakukan agent yang belum
# berpengalaman di TELEPON. Urusan dokumen pendukung bukan soal itu: masa kerja
# tidak mengubah tenggat H+2, jadi B09 tetap M.
#
# Untuk C03 aturannya tidak pernah menggigit (Risk Base O, sementara yang dilunakkan
# hanya L/M). Tetap didaftarkan sebagai pernyataan NIAT, supaya keputusan ini tidak
# hilang diam-diam kalau risk_base-nya suatu saat berubah.
NEW_JOINER_EXEMPT_CODES = {"B09", "C03"}


def override_risk_base_for_new_joiner(rows: list) -> list:
    """Return error-code rows with Risk Base ``L``/``M`` replaced by ``N`` (the
    new-joiner rule); rows with any other Risk Base (e.g. ``H``) are unchanged, and
    so are the document codes in ``NEW_JOINER_EXEMPT_CODES``."""
    return [
        {**r, "risk_base": NEW_JOINER_RISK_BASE}
        if (r.get("risk_base") in ("L", "M")
            and r.get("error_code") not in NEW_JOINER_EXEMPT_CODES)
        else r
        for r in rows
    ]

# Codes whose authoritative, per-field source is a verification array rather than
# the LLM ``error_codes`` list. The LLM copy of these is skipped in the scorecard
# group (unless it explicitly references an SC_CL item) to avoid double-listing.
_VERIFICATION_CODES = {"B02", "B03", "B05", "B17"}

# Scorecard categories whose unmet (BELUM_SESUAI) items are surfaced as a derived
# error code, keyed to each item's own item_code.
CATEGORY_ERROR_CODES = [
    {"categories": [
        "Penjelasan Mega Cashline",
        "Final Konfirmasi Mega Cashline",
        "Final Konfirmasi Mega Ultima Shield",
    ], "code": "B10"},
    {"categories": ["Greeting"], "code": "B12"},
    {"categories": ["Legal Statement Mega Cashline", "Legal Statement Mega Ultima Shield"], "code": "B18"},
]

# Error codes currently surfaced in the Error Code table. To keep error_codes.py in
# sync with the KB/prompt, only this set is shown for now; every other code the LLM
# might emit (B11, B13, B15, B19, B20, B24, B26, ...) is HIDDEN, not deleted.
# Reversible: widen this set (or remove the filter in build_error_code_table) to
# restore a hidden code. Hidden codes carry no score deduction, so hiding them does
# not change any score or pass/fail outcome.
# B27 & B28 ditambahkan 28 Agustus 2026 bersama masuknya kedua kode itu ke sheet
# Error Reason bank. Keduanya Risk Base "M", jadi begitu muncul ia IKUT menaikkan
# Total Failure & Failure Rate — itu memang yang dimaksud, bukan efek samping.
ALLOWED_ERROR_CODES = {"B02", "B03", "B05", "B09", "B10", "B12", "B16",
                       "B17", "B18", "B27", "B28", "C03"}


def is_allowed_error_code(code: str) -> bool:
    """True if ``code`` should be surfaced in the Error Code table.

    Handles the cashline fallback ``"B02/B03/B05"`` by keeping the row if ANY of its
    slash-joined parts is allowed. Blank codes are kept (they carry no code to hide).
    """
    if not code:
        return True
    parts = [p.strip() for p in code.split("/") if p.strip()]
    return any(p in ALLOWED_ERROR_CODES for p in parts)

ITEM_CODE_RE = re.compile(r"SC_CL_\d+(?:_\d+)?")
_RUJUKAN_RE = re.compile(
    r"\s*Rujukan\s*:\s*SC_CL_\d+(?:_\d+)?(?:\s*,\s*SC_CL_\d+(?:_\d+)?)*\s*\.?",
    re.IGNORECASE,
)
_AGENT_RE = re.compile(r"^(\s*Agent\s+)(.*)$", re.IGNORECASE)


# --- Helpers ----------------------------------------------------------------
def category_error_code(category):
    """Return the derived error code for a scorecard category, or None."""
    for rule in CATEGORY_ERROR_CODES:
        if category in rule["categories"]:
            return rule["code"]
    return None


def clean_reason(reason) -> str:
    """Strip the trailing "Rujukan: SC_CL_XX" clause from a reason string."""
    if not reason:
        return ""
    return _RUJUKAN_RE.sub("", reason).strip()


# Parenthetical SC_CL references, e.g. "(SC_CL_2)" or "(SC_CL_2, SC_CL_3)".
_ITEM_CODE_PAREN_RE = re.compile(
    r"\s*\(\s*SC_CL_\d+(?:_\d+)?(?:\s*,\s*SC_CL_\d+(?:_\d+)?)*\s*\)"
)


def strip_item_codes(reason) -> str:
    """Remove SC_CL_XX references from a reason string for display in the Error
    Code table (the item code already has its own column). Strips parenthetical
    "(SC_CL_2)" groups and any bare SC_CL_X tokens, then tidies leftover
    separators/whitespace."""
    if not reason:
        return ""
    text = _ITEM_CODE_PAREN_RE.sub("", reason)
    text = ITEM_CODE_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([.,;:])", r"\1", text)
    return text.strip(" -\u2013\u2014,;:").strip()


def negate_requirement(req: str) -> str:
    """Negate an "Agent ..." requirement: "Agent menyebutkan nama agent" ->
    "Agent tidak menyebutkan nama agent". Non-"Agent" phrasings get a prefix."""
    if not req:
        return "Item scorecard belum terpenuhi."
    m = _AGENT_RE.match(req)
    return f"{m.group(1)}tidak {m.group(2)}" if m else f"Belum terpenuhi: {req}"


# STATIC card-holder verification fields and the scorecard/critical item each one
# drives (the inverse of CARD_HOLDER_STATIC_SCORECARD, plus a label for prose).
# Negating the requirement ("Agent tidak memverifikasi tanggal lahir nasabah") is
# WRONG for these items whenever the agent DID ask and the customer's answer simply
# did not match Ascend — the failure is the data, not a missing step. The reader
# cannot tell the two apart from the negated requirement alone, so these items get
# their own sentence.
STATIC_VERIFICATION_ITEMS = {
    "SC_CL_23_1": ("tanggal_lahir", "tanggal lahir"),
    "SC_CL_23_2": ("nama_ibu_kandung", "nama ibu kandung"),
}
# field -> label, kebalikan dari peta di atas (dipakai penjelasan kegagalan verifikasi statik).
STATIC_VERIFICATION_ITEMS_BY_FIELD = {
    field: label for field, label in STATIC_VERIFICATION_ITEMS.values()
}
# Field yang tunduk pada CEK KONSISTENSI antar-penyebutan (tahap 1).
STATIC_CONSISTENCY_FIELDS = tuple(STATIC_VERIFICATION_ITEMS_BY_FIELD)


def static_verification_pending_reason(field, evaluation: dict) -> str:
    """Kalimat "reason" untuk item scorecard yang DITANGGUHKAN karena verifikasi
    statiknya berada di zona abu-abu dan dokumen pendukungnya masih ditunggu.

    Dipakai ``_propagate_verification_to_scorecard`` saat menurunkan SC_CL_23_1 /
    SC_CL_23_2 ke PENDING. Tanpa penulisan ulang, item itu akan berbunyi PENDING
    sambil membawa kalimat LLM yang menjelaskan status SESUAI ("nasabah menyebutkan
    tanggal lahir dengan benar") — persis kebingungan yang dilaporkan pada tiket
    0602247CJA dan 010753NldX.

    Menyebutkan angka similarity dan jenis dokumen yang ditunggu, supaya QC tahu
    apa yang harus ditagih tanpa membuka tabel verifikasi.
    """
    label = STATIC_VERIFICATION_ITEMS_BY_FIELD.get(field, field)
    row = {}
    for v in (evaluation or {}).get("card_holder_verification") or []:
        if (v or {}).get("field") == field:
            row = v or {}
            break
    sim = row.get("similarity_percent")
    sim_txt = ""
    try:
        if sim is not None:
            n = float(sim)
            sim_txt = f" (similarity {int(n) if n == int(n) else n}%)"
    except (TypeError, ValueError):
        sim_txt = ""

    doc_txt = ""
    try:
        from compliance.documents import card_holder_doc_bands

        rule = card_holder_doc_bands(evaluation).get(field) or {}
        doc_type = rule.get("doc_type")
        if doc_type:
            doc_txt = f" Menunggu dokumen pendukung: {doc_type}."
    except Exception:
        # Nama dokumen hanya pelengkap kalimat; kegagalan mengambilnya tidak boleh
        # menghilangkan alasan penangguhannya.
        doc_txt = ""

    return (f"Verifikasi statik {label} berada di zona abu-abu{sim_txt} — belum sama "
            f"dengan Ascend tetapi cukup dekat, sehingga bank meminta dokumen "
            f"pendukung alih-alih menyalahkan agent.{doc_txt}")


def static_verification_failure_reason(item_code, evaluation: dict) -> "str | None":
    """Why a STATIC verification item failed, naming the ACTUAL cause.

    Returns None when ``item_code`` is not a static verification item or its field
    did not MISMATCH — the caller then keeps its own wording.

    Three causes, read off the ``card_holder_verification`` row itself:
      - customer never gave a value (``extracted_value`` empty) -> agent never asked;
      - the customer's repeated answers disagreed with each other (STATIC
        VERIFICATION CONSISTENCY RULE, < 90% between mentions) -> asked, but
        inconsistent. Detected from the verification reason, the only signal the
        evaluation carries for it; if it is worded differently the sentence falls
        back to the mismatch wording below, which is still accurate.
      - otherwise -> asked, but the answer does not match Ascend.
    """
    entry = STATIC_VERIFICATION_ITEMS.get(item_code)
    if not entry or not isinstance(evaluation, dict):
        return None
    field, label = entry
    row = None
    for v in evaluation.get("card_holder_verification") or []:
        if (v or {}).get("field") == field:
            row = v or {}
            break
    if not row or row.get("match") != "MISMATCH":
        return None
    extracted = str(row.get("extracted_value") or "").strip()
    reference = str(row.get("reference_value") or "").strip()
    if not extracted:
        return (f"Agent tidak menanyakan verifikasi statik {label} "
                f"(nasabah tidak pernah menyebutkan nilainya)")
    if _reason_says_inconsistent(row.get("reason")):
        # Kebijakan 21 Agustus 2026: kegagalan TAHAP 1 tidak lagi diberi kalimat
        # khusus. Kolom Critical Failure cukup berbunyi dengan negasi requirement,
        # sama seperti tiga item kritikal lainnya — QC melihat SEBAB detailnya pada
        # baris card_holder_verification, bukan pada ringkasan kolom. Aturannya
        # sendiri TIDAK berubah: barisnya tetap MISMATCH dan tetap menggugurkan tiket.
        return f"Agent tidak memverifikasi {label} nasabah"
    detail = f'disebut "{extracted}"' + (f', Ascend "{reference}"' if reference else "")
    return (f"Agent menanyakan verifikasi statik {label} tetapi data mismatch "
            f"dengan Ascend ({detail})")


def not_fulfilled_reason(item: dict, evaluation: dict = None,
                         prefer_item_reason: bool = False) -> str:
    """Why a scorecard item is unmet, tagged with its item code, e.g.
    "Agent menyebutkan nama agent" (SC_CL_2) -> "Agent tidak menyebutkan nama agent (SC_CL_2)".

    Pass ``evaluation`` so the STATIC verification items (SC_CL_23_1/23_2) can say
    whether the agent failed to ASK or asked and got a non-matching answer — see
    ``static_verification_failure_reason``. Without it the plain negation is used
    (kept for backwards compatibility).

    ``prefer_item_reason`` (28 Agustus 2026): pakai ``reason`` MILIK item scorecard
    itu bila ada, alih-alih negasi requirement-nya.

    Negasi requirement selalu benar tetapi selalu GENERIK — ia hanya membalik kalimat
    persyaratan, sehingga tidak pernah bisa menyebut SEBAB spesifik yang ditemukan
    penilai. Pada tiket 180936d7F2 scorecard SC_CL_7 berbunyi "Agent menyebut bunga
    2,09% per bulan, tetapi frasa wajib effective rate tidak disebutkan", sedangkan
    baris Error Code untuk item yang SAMA berbunyi "Agent tidak menjelaskan bunga
    Mega Cashline" — dua kalimat berbeda untuk satu kegagalan, dan yang generik itu
    menyesatkan (agent JELAS menjelaskan bunganya; yang salah frasanya).

    Dibuat opt-in, bukan perilaku bawaan: pemanggil satunya
    (``stats._non_tolerable_reasons``, kolom "Critical Failure" pada tabel Results)
    memang menghendaki negasi pendek satu baris — lihat catatan kebijakan
    21 Agustus 2026 di ``static_verification_failure_reason``.
    """
    item = item or {}
    item_code = item.get("item_code")
    suffix = f" ({item_code})" if item_code else ""
    special = static_verification_failure_reason(item_code, evaluation)
    if special:
        return f"{special}{suffix}"
    if prefer_item_reason:
        # ``clean_reason`` membuang klausa "Rujukan: SC_CL_XX" yang kadang menempel,
        # sama seperti yang dilakukan cabang error code dari LLM.
        own = clean_reason(item.get("reason"))
        if own:
            return f"{own}{suffix}"
    req = item.get("requirement") or ""
    if not req:
        return f"Item scorecard belum terpenuhi{suffix}."
    return f"{negate_requirement(req)}{suffix}"


def _weight_of(item: dict) -> float:
    """Numeric ``weight`` of a scorecard item, 0.0 when missing/unparseable."""
    w = (item or {}).get("weight")
    if isinstance(w, bool) or w is None:
        return 0.0
    try:
        f = float(w)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if f != f else f


def _tidy_number(value: float):
    """4.5 -> 4.5, 22.0 -> 22 (avoids "22.0" in the dashboard)."""
    return int(value) if value == int(value) else round(value, 2)


def _pending_reason_text(items) -> "str | None":
    """Alasan penangguhan sebuah kategori, dirangkai dari item-item PENDING-nya.

    Kalimatnya diambil dari ``reason`` item — yang pada jalur penangguhan sudah ditulis
    ulang oleh propagasi menjadi teks baris verifikasinya ("...; menunggu dokumen Cover
    Buku Tabungan sampai tenggat H+2"), jadi Ringkasan Kategori berbunyi sama dengan
    tabel verifikasi dan tabel Scorecard untuk penangguhan yang sama. Duplikat dibuang
    dan tiap kalimat dipastikan berakhiran titik."""
    out, seen = [], set()
    for it in items:
        text = str((it or {}).get("reason") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text if text.endswith((".", "!", "?")) else text + ".")
    return " ".join(out) or None


def derive_category_summary(evaluation: dict) -> dict:
    """Rebuild ``evaluation.category_summary`` from ``scorecard_result``.

    The LLM emits ``category_summary`` as its OWN block, so it can contradict the very
    scorecard it summarises — observed on live data: a category marked FAIL with score 0
    while every one of its items is SESUAI (and the reverse, PASS while an item failed),
    plus ~20% of rows carrying an ``earned_score``/``total_weight`` that does not match
    the items. The scorecard is the authority (it is what the ticket score is computed
    from), so the summary is derived from it instead of trusted:

      - ``total_weight``    = sum of the category's item weights, EXCLUDING items with
                              status ``TIDAK_DINILAI``;
      - ``earned_score``    = total_weight - weights of its BELUM_SESUAI items, i.e. the
                              same arithmetic as ai_score_phase_2;
      - ``category_result`` = FAIL when the category has >= 1 BELUM_SESUAI item,
                              ``TIDAK_DINILAI`` when EVERY item is skipped, PENDING when
                              >= 1 item masih ditangguhkan, else PASS;
      - ``fail_reason``     = the LLM's prose bila kategorinya gagal; bila kategorinya
                              PENDING, alasan penangguhan item-itemnya.

    STATUS PENDING IKUT DIRINGKAS (31 Agustus 2026). Sejak item scorecard bisa
    ditangguhkan menunggu dokumen (SC_CL_23_1/23_2 lewat
    ``_propagate_verification_to_scorecard``, SC_CL_13 lewat
    ``_propagate_cashline_to_scorecard``), kategori yang membawa item PENDING terbaca
    **PASS dengan nilai penuh** di Ringkasan Kategori — satu-satunya permukaan yang
    meringkas scorecard per kategori justru menyembunyikan bahwa tiketnya sedang
    menunggu bukti. FAIL tetap menang atas PENDING: sekali ada item yang sudah pasti
    gagal, kategorinya gagal, sama seperti di kedua fungsi propagasi.

    ``earned_score`` TIDAK berubah karena PENDING: penangguhan tidak memotong skor
    (lihat ``scoring.scorecard_score``), jadi bobot item PENDING tetap utuh — persis
    seperti angka yang dipakai ``ai_score_phase_2``.

    ITEM ``TIDAK_DINILAI`` TIDAK IKUT DIHITUNG (perbaikan 24 Agustus 2026). Item MUS
    dilewati ketika nasabah tidak berminat Mega Ultima Shield (MUS SCORECARD
    CONDITIONAL RULE) dan menurut KB "do not affect the score". Sebelumnya bobotnya
    tetap dijumlahkan, sehingga:
      * tiga kategori MUS tampil **PASS dengan nilai penuh** (22,5 + 11,25 + 7,5)
        padahal tidak satu pun itemnya dinilai — terbaca seolah agent menjalankan
        seluruh prosedur MUS dengan sempurna; dan
      * total Ringkasan Kategori menjadi 150 sementara ``maximum_score`` tiket itu
        108,75 (= 150 - 41,25 bobot MUS), jadi kedua angka di layar yang sama saling
        bertentangan.
    Perbaikan ini murni TAMPILAN: ``ai_score_phase_2`` tidak pernah menghitung item
    ``TIDAK_DINILAI`` (``scorecard_score`` hanya mengurangi bobot item BELUM_SESUAI
    dari ``max_score``), jadi tidak ada skor atau AI Status yang berubah.

    Categories appear in scorecard order, so a category the LLM forgot to summarise
    (32 occurrences on live data) is no longer missing. Display-only: no score, AI Status
    or error code reads ``category_summary``, and the stored result_json is untouched —
    this runs at read time.
    """
    if not isinstance(evaluation, dict):
        return evaluation
    items = evaluation.get("scorecard_result")
    if not isinstance(items, list) or not items:
        return evaluation
    old_reason = {
        c.get("category"): c.get("fail_reason")
        for c in (evaluation.get("category_summary") or [])
        if isinstance(c, dict)
    }
    order, agg = [], {}
    for it in items:
        if not isinstance(it, dict):
            continue
        cat = it.get("category")
        if cat not in agg:
            order.append(cat)
            agg[cat] = {"weight": 0.0, "lost": 0.0, "failed": 0,
                        "skipped": 0, "assessed": 0, "pending": []}
        bucket = agg[cat]
        status = str(it.get("status") or "").strip().upper()
        if status == "TIDAK_DINILAI":
            # Dilewati: tidak menambah bobot, tidak menambah perolehan. Dihitung
            # terpisah supaya kategori yang SELURUH itemnya dilewati bisa dikenali.
            bucket["skipped"] += 1
            continue
        w = _weight_of(it)
        bucket["weight"] += w
        bucket["assessed"] += 1
        if status == "BELUM_SESUAI":
            bucket["lost"] += w
            bucket["failed"] += 1
        elif status == "PENDING":
            # Bobotnya SUDAH ikut terhitung penuh di atas — penangguhan tidak memotong.
            # Yang disimpan hanya alasannya, untuk kolom Alasan.
            bucket["pending"].append(it)
    summary = []
    for cat in order:
        b = agg[cat]
        failed = b["failed"] > 0
        # Seluruh itemnya dilewati -> kategori memang tidak dinilai. Ditampilkan apa
        # adanya (bukan disembunyikan) supaya QC tetap melihat kategori itu ADA dan
        # tahu mengapa nihil, bukan mengira laporannya terpotong.
        skipped_all = b["assessed"] == 0 and b["skipped"] > 0
        pending = not failed and bool(b["pending"])
        if skipped_all:
            result = "TIDAK_DINILAI"
        elif failed:
            result = "FAIL"
        elif pending:
            result = "PENDING"
        else:
            result = "PASS"
        if failed:
            reason = old_reason.get(cat) or None
        elif pending:
            reason = _pending_reason_text(b["pending"])
        else:
            reason = None
        summary.append({
            "category": cat,
            "total_weight": _tidy_number(b["weight"]),
            "earned_score": _tidy_number(b["weight"] - b["lost"]),
            "category_result": result,
            "fail_reason": reason,
        })
    return {**evaluation, "category_summary": summary}


def annotate_critical_compliance_reasons(evaluation: dict) -> dict:
    """Return ``evaluation`` with a ``reason`` on every ``critical_compliance_check``
    checked item, so every surface renders the SAME sentence instead of negating the
    requirement on its own (the dashboard used to do that in three separate places,
    and the negation is wrong for the static verification items).

    PASS items get no reason. Non-destructive: returns the original object when there
    is nothing to annotate."""
    if not isinstance(evaluation, dict):
        return evaluation
    ccc = evaluation.get("critical_compliance_check")
    if not isinstance(ccc, dict):
        return evaluation
    items = ccc.get("checked_items")
    if not isinstance(items, list) or not items:
        return evaluation
    out = []
    for it in items:
        if not isinstance(it, dict):
            out.append(it)
            continue
        if str(it.get("status") or "").strip().upper() == "PASS":
            out.append({**it, "reason": None})
            continue
        reason = (static_verification_failure_reason(it.get("item_code"), evaluation)
                  or negate_requirement(it.get("requirement") or ""))
        out.append({**it, "reason": reason})
    return {**evaluation, "critical_compliance_check": {**ccc, "checked_items": out}}


def error_code_sort_key(code) -> tuple:
    """Natural sort key for an error code like "B10" -> ("B", 10), so codes order
    ascending numerically (B02 < B05 < B10 < B12) regardless of digit width."""
    m = re.match(r"([A-Za-z]*)(\d*)", code or "")
    letters = m.group(1)
    num = int(m.group(2)) if m.group(2) else 0
    return (letters, num, code or "")


def code_for_cashline_field(v: dict, codes: list, used: set) -> str:
    """Resolve the data-input error code (B02/B03/B05) for a cashline field by
    matching its field name / reference / extracted value inside a code's reason.
    Each code is consumed once; falls back to a generic label."""
    needles = []
    for key in ("field", "reference_value", "extracted_value"):
        val = v.get(key)
        if val is not None and str(val).strip():
            needles.append(str(val).strip().lower())
    field = v.get("field")
    if field:
        needles.append(str(field).replace("_", " ").strip().lower())
    for i, c in enumerate(codes):
        if i in used:
            continue
        reason = ((c.get("trigger_source") or {}).get("reason") or "").lower()
        if any(n in reason for n in needles):
            used.add(i)
            return c.get("error_code") or "B02/B03/B05"
    return "B02/B03/B05"


def titleize_field(field) -> str:
    """Format a snake_case field name into Title Case, e.g.
    "nominal_pencairan" -> "Nominal Pencairan"."""
    if not field:
        return ""
    return str(field).replace("_", " ").strip().title()


def _verification_reason(v: dict) -> str:
    """Reason text for a verification-derived row: prefer the verification reason,
    otherwise build a short "<Field>: <MATCH>" note."""
    v = v or {}
    reason = v.get("reason")
    if reason:
        return str(reason)
    field = titleize_field(v.get("field"))
    match = v.get("match") or ""
    return f"{field}: {match}".strip(": ").strip()


def _evidence_text(evidence: dict) -> str:
    evidence = evidence or {}
    return f'{evidence.get("timestamp") or ""} {evidence.get("quote") or ""}'.strip()


def _evidence_timestamp(evidence: dict) -> str:
    """Extract just the timestamp portion of an evidence object (empty if none)."""
    return str((evidence or {}).get("timestamp") or "").strip()


def _evidence_quote(evidence: dict) -> str:
    """Extract just the quote (text) portion of an evidence object (empty if none)."""
    return str((evidence or {}).get("quote") or "").strip()


# QC submits evidence as ONE "<timestamp> <quote>" string (the Manual Check / Add
# modals concatenate the two inputs). The dashboard renders timestamp and quote as
# separate parts, so split it back. Like splitEvidence() in
# dashboard/src/components/ErrorCodeManualCheckModal.vue, but also accepting the
# formats this app actually emits: fractional seconds ("00:02.30") and "->" ranges
# ("00:00.00 -> 00:02.06"), optionally wrapped in [ ].
_TS = r"\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?"
_QC_EVIDENCE_TS_RE = re.compile(
    rf"^\[?\s*{_TS}(?:\s*(?:->|[-–])\s*{_TS})?\s*\]?"
)


def _split_qc_evidence(text) -> tuple:
    """Split a combined QC evidence string into ``(timestamp, quote)``.

    Returns ``("", text)`` when the string does not start with a timestamp."""
    s = str(text or "").strip()
    if not s:
        return "", ""
    m = _QC_EVIDENCE_TS_RE.match(s)
    if not m:
        return "", s
    ts = m.group(0).strip().strip("[]").strip()
    return ts, s[m.end():].strip()


def _document_row(code: str, reason: str) -> dict:
    """Satu baris tabel Error Code bersumber dokumen pendukung.

    Bentuknya harus sama persis dengan baris keluaran ``build_error_code_table``
    (lihat ``add`` di sana) karena keduanya bercampur dalam satu tabel dan
    dikonsumsi pemakai yang sama — dashboard, export XLSX, dan tally Risk Base.

    ``item_code`` kosong: pemicunya berkas yang diunggah, bukan item scorecard.
    Konsekuensinya baris ini tidak pernah dianggap "L yang tolerable" oleh
    ``compliance.stats_aggregate.top_risk_base`` — dan itu benar, telat melengkapi
    dokumen bukan pelanggaran skrip yang bisa dimaklumi.
    """
    return {
        "sumber": SOURCE_LABELS[SOURCE_DOCUMENT],
        "error_code": code,
        "risk_base": (ERROR_CODES.get(code) or {}).get("risk_base") or "",
        "error_type": error_type_of(code),
        "error_category": error_category_of(code),
        "item_code": "",
        "details_error": ERROR_DESC_ID.get(code) or "",
        "reason": reason,
        "evidence": "",
        "timestamp": "",
        "evidence_quote": "",
        "ticket_id": "",
    }


def document_error_code_rows(missing=False, missing_labels=(), wrong_type=()) -> list:
    """Baris Error Code untuk urusan dokumen pendukung. DUA kode, dibedakan oleh
    siapa yang gagal:

      * berkas tidak pernah datang sampai tenggat  -> B09, Error - Human, Risk M
      * berkas datang tapi salah jenis             -> C03, Error - Customer, Risk O

    ``missing``         — dokumen yang diminta tidak pernah diunggah sampai tenggat
                          H+2 lewat. Terbit B09, SATU baris saja walau yang kurang
                          lebih dari satu berkas: yang dinilai adalah kelalaian
                          menagih kelengkapan, bukan jumlah berkasnya.
    ``missing_labels``  — jenis dokumen yang kurang, mis. ``["KK"]``, hanya untuk
                          memperjelas kalimat alasan. Boleh kosong: kewajiban yang
                          lahir dari perubahan data TMS / limit >= 50 juta bisa
                          dipenuhi dokumen apa pun, jadi tidak ada jenis yang bisa
                          disebut. Mengisi ini TIDAK menggantikan ``missing``.
    ``wrong_type``      — dokumen yang diunggah tetapi jenisnya bukan yang diminta,
                          tiap item ``{"expected": <label>, "detected": <label>}``.
                          Terbit C03, satu baris per slot yang keliru, karena tiap
                          slot adalah kesalahan unggah tersendiri.

    Sebuah tiket bisa kena KEDUANYA — slot NPWP diisi KTP (C03), sehingga kewajiban
    NPWP-nya tetap kosong dan tenggat lewat (B09). Itu memang dua fakta yang berbeda
    dan sengaja dilaporkan terpisah. Tally Risk Base tidak berlipat: tiap tiket tetap
    dihitung satu risk base tertinggi saja — dalam contoh itu M dari B09.
    """
    rows = []
    labels = [str(l).strip() for l in (missing_labels or []) if str(l or "").strip()]
    if missing:
        rows.append(_document_row(
            "B09",
            ("Dokumen " + ", ".join(labels) if labels else "Dokumen pendukung")
            + " tidak diunggah sampai tenggat H+2 terlewat",
        ))
    for item in wrong_type or []:
        expected = str((item or {}).get("expected") or "").strip()
        detected = str((item or {}).get("detected") or "").strip()
        if not expected:
            continue
        rows.append(_document_row(
            "C03",
            f"Dokumen yang diminta {expected}, "
            + (f"yang diunggah terbaca sebagai {detected}" if detected
               else "yang diunggah bukan dokumen tersebut"),
        ))
    return rows


# Pemisah antar item_code yang digabung menjadi SATU baris Error Code. Dipakai dua
# arah: menyusunnya di ``merge_dynamic_verification_rows`` dan memecahnya kembali di
# ``_apply_verification_appeals`` supaya satu banding pada baris gabungan tetap
# mengenai SEMUA field yang diwakilinya.
MERGED_ITEM_SEP = ", "


def _merge_reason_texts(rows) -> str:
    """Gabungkan ``reason`` beberapa baris menjadi satu paragraf.

    Kalimat yang sama persis tidak diulang, dan tiap kalimat dipastikan diakhiri
    titik supaya sambungannya terbaca sebagai paragraf, bukan satu kalimat panjang.
    Nama field TIDAK ditambahkan di depan: kalimat dari LLM sudah menyebutnya sendiri
    ("Alamat kantor yang disebut berbeda dengan Ascend ..."), jadi memprefiksnya
    hanya akan mengulang.
    """
    out, seen = [], set()
    for r in rows:
        text = str((r or {}).get("reason") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text if text.endswith((".", "!", "?")) else text + ".")
    return " ".join(out)


def _merge_verification_rows(group: list) -> dict:
    """Satukan beberapa baris Error Code sejenis menjadi satu baris.

    ``item_code`` menjadi daftar label yang digabung ``MERGED_ITEM_SEP`` — inilah
    kunci yang dipakai banding, dan ``_apply_verification_appeals`` memecahnya lagi
    saat menerapkan hasil banding. ``reference_value``/``extracted_value`` ikut
    digabung DENGAN label fieldnya, karena tanpa label sepasang nilai dari tiga field
    berbeda tidak bisa dibaca lagi di modal Manual Check.

    Baris pertama menjadi kerangkanya, sehingga posisi, ``sumber``, ``risk_base``,
    dan ``error_type``-nya tidak berubah.
    """
    base = dict(group[0])
    labels = [str((r or {}).get("item_code") or "").strip() for r in group]
    labels = [x for x in labels if x]
    base["item_code"] = MERGED_ITEM_SEP.join(labels)
    base["reason"] = _merge_reason_texts(group)
    # Evidence: pakai yang pertama yang benar-benar terisi (baris B17 umumnya kosong).
    for key in ("evidence", "timestamp", "evidence_quote", "ticket_id"):
        base[key] = next(
            (str((r or {}).get(key) or "").strip() for r in group
             if str((r or {}).get(key) or "").strip()),
            base.get(key) or "",
        )
    for key in ("reference_value", "extracted_value"):
        parts = [
            f"{(r or {}).get('item_code')}: {(r or {}).get(key)}"
            for r in group if (r or {}).get(key) not in (None, "")
        ]
        if parts:
            base[key] = " | ".join(parts)
    # Nilai ``match`` per field bisa berbeda; baris gabungan tidak mewakili satu pun
    # secara khusus, jadi dikosongkan alih-alih memakai punya baris pertama.
    if len(group) > 1 and "match" in base:
        base["match"] = ""
    return base


def merge_dynamic_verification_rows(rows: list) -> list:
    """Baris VERIFIKASI DINAMIS yang berulang dijadikan SATU baris (31 Agustus 2026).

    Permintaan bisnis: pada verifikasi dinamis, B17 dan B16 cukup muncul SEKALI per
    tiket dengan seluruh alasannya digabung. Tiket ``180107uT48`` misalnya menerbitkan
    tiga B17 — Alamat Rumah, Alamat Kantor, Alamat Email Terdaftar — yang menyatakan
    kegagalan YANG SAMA (verifikasi dinamis tidak terbukti) dari tiga sisi.

    **CATATAN, aturan lanjutan hari yang sama:** B17 kemudian dibatasi HANYA untuk
    field statik (lihat bagian 2 ``build_error_code_table``), sehingga baris B17
    dinamis tidak lagi pernah terbit dari evaluasi AI dan cabang B17 di sini praktis
    tidak terpakai. Fungsi ini dipertahankan sebagai penjaga: B16 tetap dilebur bila
    suatu saat terbit lebih dari satu, dan cabang B17-dinamis siap kembali berguna
    bila aturan itu diubah lagi. Baris hasil banding "add" TIDAK melewatinya —
    ketiga pemanggil menjalankannya SEBELUM ``inject_added_rows``, karena baris yang
    ditulis QC sendiri tidak boleh diam-diam digabung.

    Yang digabung HANYA:

    * **B17 dari 9 field DINAMIS** (``CARD_HOLDER_DYNAMIC_FIELDS``). B17 dari field
      STATIK (Tanggal Lahir, Nama Ibu Kandung) TIDAK ikut: keduanya kewajiban
      terpisah yang masing-masing punya item scorecard sendiri (SC_CL_23_1/23_2) dan
      irisan Critical Compliance sendiri, jadi meleburnya akan menyembunyikan
      kegagalan yang berbeda di balik satu baris.
    * **B16**, yang memang satu vonis tentang kecukupan verifikasi dinamis.

    Baris tunggal dibiarkan apa adanya — penggabungan hanya terjadi bila ada dua atau
    lebih baris sejenis.

    **HANYA UNTUK TAMPILAN.** Dipanggil di permukaan yang MENAMPILKAN tabel Error Code
    (detail tiket, Agent Error Summary, ekspor XLSX), TIDAK di dalam
    ``build_error_code_table``. Alasannya: keluaran builder itu juga menjadi bahan
    ``risk_base_tally``, yang menghitung PELANGGARAN per baris — melebur di sana
    berarti diam-diam memotong Total Failure dan Failure Rate.
    """
    dyn_labels = {titleize_field(f) for f in CARD_HOLDER_DYNAMIC_FIELDS}

    def bucket(r):
        code = (r or {}).get("error_code")
        if code == "B16":
            return "B16"
        if code == "B17" and (r or {}).get("item_code") in dyn_labels:
            return "B17_DYN"
        return None

    groups: dict = {}
    for r in rows:
        key = bucket(r)
        if key:
            groups.setdefault(key, []).append(r)

    mergeable = {k for k, g in groups.items() if len(g) > 1}
    if not mergeable:
        return rows

    out, done = [], set()
    for r in rows:
        key = bucket(r)
        if key not in mergeable:
            out.append(r)
            continue
        if key in done:
            continue  # sudah diwakili baris gabungan di posisi yang pertama
        done.add(key)
        out.append(_merge_verification_rows(groups[key]))
    return out


def build_error_code_table(evaluation: dict) -> list:
    """Build the grouped Error Code table rows from an LLM ``evaluation`` object.

    Returns a list of dicts, ordered by source group, each shaped as:
    {sumber, error_code, item_code, details_error, reason, evidence, ticket_id}

    - Scorecard            : derived B10/B12/B18 + LLM error_codes (split per SC_CL)
    - Card Holder Verif.   : B17 per mismatched/skipped field
    - Cashline Data Verif. : B02/B03/B05 per mismatched/skipped field
    """
    evaluation = evaluation or {}
    rows = []
    seen = set()  # f"{code}::{item_code}" — prevents the same code+item appearing twice

    def add(source, code, item_code, reason, evidence, ticket_id, details=None,
            extra=None, timestamp="", evidence_quote=""):
        key = f"{code}::{item_code or ''}"
        if item_code and key in seen:
            return
        if item_code:
            seen.add(key)
        row = {
            "sumber": SOURCE_LABELS.get(source, source),
            "error_code": code or "",
            "risk_base": (ERROR_CODES.get(code) or {}).get("risk_base") or "",
            # Kosakata sheet QC, dibawa per baris supaya dashboard & export tidak
            # perlu memetakan kode ke kategorinya sendiri-sendiri.
            "error_type": error_type_of(code),
            "error_category": error_category_of(code),
            "item_code": item_code or "",
            "details_error": ERROR_DESC_ID.get(code) or details or "",
            "reason": strip_item_codes(reason),
            "evidence": evidence or "",
            # Split evidence so the QC "Manual Check" modal can prefill the
            # Timestamp and Evidence-text fields separately (see _evidence_*).
            "timestamp": timestamp or "",
            "evidence_quote": evidence_quote or "",
            "ticket_id": ticket_id or "",
        }
        if extra:
            row.update(extra)
        rows.append(row)

    # --- 1) Scorecard: LLM error_codes (non-verification) split per SC_CL ---
    for entry in evaluation.get("error_codes") or []:
        code = entry.get("error_code") or ""
        trigger = entry.get("trigger_source") or {}
        reason_raw = trigger.get("reason") or ""
        item_codes = list(dict.fromkeys(ITEM_CODE_RE.findall(reason_raw)))
        # Verification-sourced codes are represented per-field below; skip the LLM
        # copy unless it explicitly references a scorecard item (SC_CL_*).
        if code in _VERIFICATION_CODES and not item_codes:
            continue
        evidence = trigger.get("evidence") or {}
        ev_text = _evidence_text(evidence)
        ev_ts = _evidence_timestamp(evidence)
        ev_quote = _evidence_quote(evidence)
        ticket_id = evidence.get("ticket_id") or ""
        reason = clean_reason(reason_raw)
        details = entry.get("details_error")
        if item_codes:
            for ic in item_codes:
                add(SOURCE_SCORECARD, code, ic, reason, ev_text, ticket_id, details,
                    timestamp=ev_ts, evidence_quote=ev_quote)
        else:
            add(SOURCE_SCORECARD, code, None, reason, ev_text, ticket_id, details,
                timestamp=ev_ts, evidence_quote=ev_quote)

    # --- 1b) Scorecard: derived B10/B12/B18 from BELUM_SESUAI items ---
    for item in evaluation.get("scorecard_result") or []:
        if (item or {}).get("status") != "BELUM_SESUAI":
            continue
        code = category_error_code((item or {}).get("category"))
        if not code:
            continue
        evidence = item.get("evidence") or {}
        add(
            SOURCE_SCORECARD,
            code,
            item.get("item_code"),
            # Reason baris Error Code = reason item scorecard-nya, supaya kedua tabel
            # tidak bisa berbunyi berbeda untuk kegagalan yang sama.
            not_fulfilled_reason(item, evaluation, prefer_item_reason=True),
            _evidence_text(evidence),
            evidence.get("ticket_id") or "",
            timestamp=_evidence_timestamp(evidence),
            evidence_quote=_evidence_quote(evidence),
        )

    # --- 1c) Scorecard: B16 diturunkan dari scorecard verifikasi ---------------
    # B16 = "Verifikasi statik BERHASIL, verifikasi dinamis kurang/tidak sesuai".
    # Sampai 31 Agustus 2026 baris ini HANYA muncul bila LLM sendiri menuliskannya di
    # ``error_codes``, dan ia tidak selalu melakukannya: pada korpus 98 tiket ada 2
    # tiket (030808fLO1, 100537MsNl) yang SC_CL_23_1/23_2-nya SESUAI dan SC_CL_24-nya
    # BELUM_SESUAI tetapi tidak menerbitkan B16 sama sekali. Sejak B17 dibatasi ke
    # field statik (lihat bagian 2), tiket seperti itu akan kehilangan SELURUH kode
    # verifikasinya — karena itu B16 kini diturunkan di kode, sama seperti B10/B12/B18.
    #
    # Syaratnya persis definisi katalognya: verifikasi dinamis GAGAL sementara TIDAK
    # SATU PUN item statik gagal. ``PENDING`` (zona abu-abu, dokumen pendukung masih
    # ditunggu) dihitung sebagai BELUM gagal — statiknya belum divonis, jadi B17 pun
    # belum terbit dan B16-lah yang benar.
    _sc_status = {
        (it or {}).get("item_code"): ((it or {}).get("status") or "").upper()
        for it in (evaluation.get("scorecard_result") or [])
    }
    _static_failed = any(
        _sc_status.get(code) == "BELUM_SESUAI"
        for code in CARD_HOLDER_STATIC_SCORECARD.values()
    )
    if (
        _sc_status.get(CARD_HOLDER_DYNAMIC_SCORECARD) == "BELUM_SESUAI"
        and not _static_failed
        and not any((c or {}).get("error_code") == "B16" for c in evaluation.get("error_codes") or [])
    ):
        _dyn_item = next(
            (it for it in (evaluation.get("scorecard_result") or [])
             if (it or {}).get("item_code") == CARD_HOLDER_DYNAMIC_SCORECARD),
            {},
        )
        _dyn_ev = _dyn_item.get("evidence") or {}
        add(
            SOURCE_SCORECARD,
            "B16",
            None,
            not_fulfilled_reason(_dyn_item, evaluation, prefer_item_reason=True),
            _evidence_text(_dyn_ev),
            _dyn_ev.get("ticket_id") or "",
            timestamp=_evidence_timestamp(_dyn_ev),
            evidence_quote=_evidence_quote(_dyn_ev),
        )

    # --- 2) Card Holder Verification: B17 per field STATIK yang MISMATCH ------
    # B17 = "Tidak ada Verifikasi / verifikasi statik kurang / tidak berhasil" —
    # katalognya menyebut VERIFIKASI STATIK, jadi hanya ``tanggal_lahir`` dan
    # ``nama_ibu_kandung`` yang boleh menerbitkannya (aturan 31 Agustus 2026).
    #
    # Sampai tanggal itu field DINAMIS ikut menerbitkan B17 satu per satu — pada
    # tiket 180107uT48 tiga sekaligus (Alamat Rumah, Alamat Kantor, Alamat Email)
    # padahal verifikasi statiknya sama sekali tidak gagal (tanggal lahir MATCH, nama
    # ibu kandung PENDING). Kegagalan dinamis sudah punya kodenya sendiri, B16, jadi
    # menerbitkannya sebagai B17 berarti memberi label "verifikasi statik tidak
    # berhasil" pada tiket yang statiknya justru berhasil — dan menghukumnya dengan
    # Risk Base H, bukan M.
    #
    # SKIPPED_NULL & PENDING tidak menerbitkan apa pun: yang pertama tidak berpenalti,
    # yang kedua masih menunggu dokumen dalam tenggat H+2. Begitu tenggatnya lewat,
    # ``apply_static_document_status`` mengubahnya menjadi MISMATCH dan B17 terbit.
    for v in evaluation.get("card_holder_verification") or []:
        if (v or {}).get("match") != "MISMATCH":
            continue
        if (v or {}).get("field") not in CARD_HOLDER_STATIC_SCORECARD:
            continue
        # Carry the reference/extracted/match so the QC "Manual Check" (banding)
        # modal can prefill them and snapshot the full AI row.
        add(
            SOURCE_CARD_HOLDER,
            "B17",
            titleize_field(v.get("field")),
            _verification_reason(v),
            "",
            "",
            extra={
                "reference_value": v.get("reference_value"),
                "extracted_value": v.get("extracted_value"),
                "match": v.get("match"),
            },
        )

    # --- 3) Cashline Data Verification: B02/B03/B05 per mismatched/skipped field ---
    cashline_codes = [
        c for c in (evaluation.get("error_codes") or []) if c.get("error_code") in ("B02", "B03", "B05")
    ]
    used: set = set()
    for v in evaluation.get("cashline_data_verification") or []:
        match = (v or {}).get("match")
        # SKIPPED_NULL fields carry no penalty and are no longer surfaced as errors —
        # only true MISMATCH rows become B02/B03/B05.
        if match != "MISMATCH":
            continue
        code = code_for_cashline_field(v, cashline_codes, used)
        # Carry reference/extracted/match so the QC "Manual Check" (banding) modal
        # can prefill them and snapshot the full AI row (same as B17 above).
        add(
            SOURCE_CASHLINE,
            code,
            titleize_field(v.get("field")),
            _verification_reason(v),
            "",
            "",
            extra={
                "reference_value": v.get("reference_value"),
                # Product terms from the RIPLAY. When a field's TMS column is
                # empty this is the reference the verdict was made against, and
                # the banding modal must show it instead.
                "tnc_product": v.get("tnc_product"),
                "extracted_value": v.get("extracted_value"),
                "match": v.get("match"),
            },
        )

    # --- 3b) Data-entry check: the TMS value itself vs the product terms ---
    # A TMS value the product does not allow (tenor 6 when only 12/24/36 exist,
    # provisi 5% when the RIPLAY says 2%) is an agent keying error, separate from
    # what they said on the call. Deterministic — see compliance/riplay.py.
    #
    # NO DOUBLE COUNTING: at most ONE data-input row per field. A field that
    # already produced a row above (transcript MISMATCH) has that row's reason
    # enriched instead of gaining a second row, and no item_score is touched here,
    # so this never changes ai_score / ai_status.
    verification_rows = evaluation.get("cashline_data_verification") or []
    by_field = {v.get("field"): v for v in verification_rows if (v or {}).get("field")}
    tms_ref = {f: v.get("reference_value") for f, v in by_field.items()}
    tnc_ref = {f: v.get("tnc_product") for f, v in by_field.items()}
    cashline_label = SOURCE_LABELS[SOURCE_CASHLINE]

    for field, detail in check_tms_against_tnc(tms_ref, tnc_ref).items():
        item_code = titleize_field(field)
        existing = next(
            (r for r in rows if r.get("sumber") == cashline_label and r.get("item_code") == item_code),
            None,
        )
        if existing is not None:
            if detail not in (existing.get("reason") or ""):
                existing["reason"] = f"{(existing.get('reason') or '').rstrip('. ')}. {detail}.".strip()
            continue
        source_row = by_field.get(field) or {}
        # Every field carrying a product term is financial data -> B03 (medium).
        # B02 covers the account fields, which have no product term at all.
        add(
            SOURCE_CASHLINE,
            "B03",
            item_code,
            f"{detail}.",
            "",
            "",
            extra={
                "reference_value": source_row.get("reference_value"),
                "tnc_product": source_row.get("tnc_product"),
                "extracted_value": source_row.get("extracted_value"),
                "match": source_row.get("match"),
            },
        )

    # --- 4) Badword -> B28 (Inappropriate Language) ---------------------------
    # Satu temuan badword = satu baris B28. Sampai 28 Agustus 2026 temuan badword
    # hanya hidup di tabel "Badword Summary" dan tidak pernah menjadi error code;
    # sheet bank kini menamainya B28, jadi ia harus ikut terhitung sebagai kegagalan.
    #
    # ``item_code`` sengaja KOSONG: temuan ini tidak menempel pada satu item scorecard
    # mana pun. Konsekuensinya de-dup pada ``add()`` tidak aktif untuk baris ini —
    # dan itu benar, sebab satu panggilan bisa memuat beberapa ucapan bermasalah yang
    # masing-masing berdiri sendiri. ``badword_rows`` sendiri sudah melipat temuan
    # kembar (timestamp + kutipan sama).
    #
    # Impor di dalam fungsi, bukan di kepala berkas: ``compliance.badwords`` mengimpor
    # modul ini untuk kosakata error code, jadi impor tingkat-modul akan melingkar.
    from compliance.badwords import badword_rows as _badword_rows

    for finding in _badword_rows(evaluation):
        quote = finding.get("quote") or ""
        timestamp = finding.get("timestamp") or ""
        add(
            SOURCE_OTHERS,
            "B28",
            "",
            finding.get("reason") or "Ucapan agent bersentimen negatif kepada nasabah.",
            finding.get("evidence") or quote,
            finding.get("ticket_id") or "",
            timestamp=timestamp,
            evidence_quote=quote,
        )

    # Hide error codes outside ALLOWED_ERROR_CODES (kept in sync with KB/prompt).
    # Reversible: this only drops rows from the surfaced table; nothing is deleted.
    rows = [r for r in rows if is_allowed_error_code(r.get("error_code"))]

    # Sort error codes ascending within each source group (B10, B12, ...),
    # preserving the group order in which sources first appear.
    group_order = {}
    for r in rows:
        group_order.setdefault(r["sumber"], len(group_order))
    rows.sort(key=lambda r: (group_order[r["sumber"]], error_code_sort_key(r["error_code"])))

    # Penggabungan B16 / B17-dinamis SENGAJA TIDAK dilakukan di sini — lihat
    # ``merge_dynamic_verification_rows``. Fungsi ini adalah sumber baris untuk
    # PENGHITUNGAN (risk base per pelanggaran di ``risk_base_tally``), bukan hanya
    # untuk tampilan; meleburnya di sini akan diam-diam mengecilkan Total Failure —
    # pada korpus 98 tiket, dari 114 menjadi 104.
    return rows


def _appeal_attr(appeal, name):
    """Read a field from an appeal that may be an ORM object or a plain dict."""
    if isinstance(appeal, dict):
        return appeal.get(name)
    return getattr(appeal, name, None)


def effective_appeal_status(appeal) -> str:
    """The FINAL status of a banding/AI-status request under the tiered flow
    (QC -> Team Leader QC -> [SPQ Head]). Returns 'approved' | 'rejected' | 'pending'.

    Team Leader QC may FINALIZE a request directly (approve/reject, no SPQ Head), or
    ESCALATE it to SPQ Head who then decides:
      - TL QC approved (final)                    -> 'approved'
      - TL QC rejected (final)                    -> 'rejected'
      - TL QC escalated + SPQ Head approved       -> 'approved'
      - TL QC escalated + SPQ Head rejected       -> 'rejected'
      - awaiting TL QC, or escalated awaiting SPQ -> 'pending'
    """
    tl = _appeal_attr(appeal, "tl_qc_status") or "pending"
    if tl == "approved":
        return "approved"
    if tl == "rejected":
        return "rejected"
    if tl == "escalated":
        appr = _appeal_attr(appeal, "approval_status") or "pending"
        return appr if appr in ("approved", "rejected") else "pending"
    return "pending"


# --- Banding kind (remove vs change) ---------------------------------------
# Error codes whose deduction is real (comes from the scorecard item weight /
# verification field penalty). A 'change' banding that relabels a row TO one of
# these keeps the row's existing deduction; relabelling to any other code zeroes it.
DEDUCTION_BEARING_CODES = {"B02", "B03", "B05", "B10", "B16", "B17"}

_ERROR_REASON_BY_CODE = None


def _reason_meta(code):
    """Catalog entry (risk_base/details/…) for an error code, or None."""
    global _ERROR_REASON_BY_CODE
    if _ERROR_REASON_BY_CODE is None:
        try:
            from compliance.error_reasons import ERROR_REASONS
            _ERROR_REASON_BY_CODE = {e.get("code"): e for e in ERROR_REASONS}
        except Exception:
            _ERROR_REASON_BY_CODE = {}
    return _ERROR_REASON_BY_CODE.get((code or "").strip().upper())


def _appeal_kind(appeal) -> str:
    return (_appeal_attr(appeal, "appeal_kind") or "remove")


def _appeal_new_code(appeal) -> str:
    return (_appeal_attr(appeal, "qc_new_error_code") or "").strip().upper()


def is_deduction_bearing(code) -> bool:
    return (code or "").strip().upper() in DEDUCTION_BEARING_CODES


def appeals_that_flip(approved_appeals: list) -> list:
    """Subset of approved appeals whose SCORE effect is to REMOVE the deduction
    (flip the scorecard item / verification field): every 'remove' banding, plus
    'change' bandings whose NEW code is NOT deduction-bearing. A 'change' to a
    deduction-bearing code keeps its deduction (relabel only), so it is excluded."""
    out = []
    for a in approved_appeals or []:
        if _appeal_kind(a) == "change" and is_deduction_bearing(_appeal_new_code(a)):
            continue  # relabel only — keep the deduction, do not flip
        out.append(a)
    return out


def _qc_overwrite(appeal) -> dict:
    """QC-submitted values that overwrite the AI row when a 'change' banding is
    approved: evidence / reason / ticket id, plus verification reference & extracted
    values. Empty submissions are skipped so the AI value is kept."""
    out = {}
    for src, dst in (
        ("qc_reason", "reason"),
        ("qc_evidence", "evidence"),
        ("qc_ticket_id", "ticket_id"),
        ("qc_reference_value", "reference_value"),
        ("qc_extracted_value", "extracted_value"),
    ):
        v = _appeal_attr(appeal, src)
        if v is not None and str(v).strip() != "":
            out[dst] = v
    return out


def relabel_error_table(table: list, approved_appeals: list) -> list:
    """Post-process the built + annotated Error Code table for approved bandings.

    - 'remove' banding  -> drop the matching row (scorecard/verification rows are
      already gone via the flip; this also hides free-floating codes with no hook).
    - 'change' banding   -> relabel the row's error_code (+ risk_base/details from
      the master catalog). If the row was flipped away ('change' -> non-deduction
      code), inject a relabeled row from the appeal's AI snapshot so the error still
      shows with the new code.

    Keyed by ``(error_code, item_code)``. MUST run AFTER ``_annotate_appeals`` so
    each row keeps the appeal metadata attached under its ORIGINAL code."""
    if not approved_appeals:
        return table
    changes, removes = {}, set()
    for a in approved_appeals:
        key = (_appeal_attr(a, "error_code"), _appeal_attr(a, "item_code") or "")
        if _appeal_kind(a) == "change":
            changes[key] = a
        else:
            removes.add(key)

    out, handled = [], set()
    for row in table:
        key = (row.get("error_code"), row.get("item_code") or "")
        if key in removes:
            continue
        a = changes.get(key)
        if a is not None:
            new_code = _appeal_new_code(a)
            meta = _reason_meta(new_code) or {}
            row = {
                **row,
                "error_code": new_code,
                # QC-edited override wins over the master-catalog default.
                "risk_base": _appeal_attr(a, "qc_risk_base") or meta.get("risk_base") or row.get("risk_base") or "",
                "details_error": meta.get("details") or row.get("details_error") or "",
                # Approved QC evidence/reason/ticket overwrite the AI values.
                **_qc_overwrite(a),
            }
            handled.add(key)
        out.append(row)

    # 'change' -> non-deduction code: the original row was flipped away, so inject
    # a relabeled row from the appeal snapshot to keep the error visible.
    for key, a in changes.items():
        if key in handled:
            continue
        new_code = _appeal_new_code(a)
        meta = _reason_meta(new_code) or {}
        base = {
            "sumber": _appeal_attr(a, "ai_sumber") or "",
            "error_code": new_code,
            "risk_base": _appeal_attr(a, "qc_risk_base") or meta.get("risk_base") or _appeal_attr(a, "ai_risk_base") or "",
            "item_code": _appeal_attr(a, "item_code") or "",
            "details_error": meta.get("details") or _appeal_attr(a, "ai_details_error") or "",
            "reason": _appeal_attr(a, "ai_reason") or "",
            "evidence": _appeal_attr(a, "ai_evidence") or "",
            "timestamp": "",
            "evidence_quote": "",
            "ticket_id": _appeal_attr(a, "ai_ticket_id") or "",
            "appealable": True,
            "appeal": None,
        }
        # Approved QC evidence/reason/ticket overwrite the AI snapshot values.
        out.append({**base, **_qc_overwrite(a)})
    return out


def apply_approved_appeals(evaluation: dict, approved_appeals: list) -> dict:
    """Return a copy of ``evaluation`` with approved Error Code appeals applied.

    For each approved appeal (keyed by scorecard ``item_code``), the matching
    ``scorecard_result`` item is flipped ``BELUM_SESUAI`` -> ``SESUAI`` and its
    evidence/ticket_id replaced with the values submitted by QC. Because the
    Error Code table, AI score and scorecard/ringkasan are all derived from
    ``scorecard_result``, this single change removes the appealed error row and
    lifts the score consistently everywhere. Non-destructive: the input
    ``evaluation`` is not mutated (approvals stay reversible)."""
    if not evaluation or not approved_appeals:
        return evaluation
    # item_code -> latest approved appeal for it
    by_item = {}
    for a in approved_appeals:
        item_code = _appeal_attr(a, "item_code")
        if item_code:
            by_item[item_code] = a
    if not by_item:
        return evaluation

    new_items = []
    changed = False
    for item in evaluation.get("scorecard_result") or []:
        appeal = by_item.get((item or {}).get("item_code"))
        if appeal is None or (item or {}).get("status") != "BELUM_SESUAI":
            new_items.append(item)
            continue
        updated = {**item, "status": "SESUAI"}
        # An approved item earns its full weight — the Scorecard "Skor" column must
        # reflect the weight, not stay at 0 (the failed score).
        if item.get("weight") is not None:
            updated["item_score"] = item.get("weight")
        evidence = dict(item.get("evidence") or {})
        qc_evidence = _appeal_attr(appeal, "qc_evidence")
        qc_ticket_id = _appeal_attr(appeal, "qc_ticket_id")
        if qc_evidence is not None:
            evidence["quote"] = qc_evidence
        if qc_ticket_id is not None:
            evidence["ticket_id"] = qc_ticket_id
        updated["evidence"] = evidence
        new_items.append(updated)
        changed = True

    if not changed:
        return evaluation
    return {**evaluation, "scorecard_result": new_items}


def _appeal_row_key(appeal) -> tuple:
    """Baris Error Code yang dituju sebuah banding: ``(error_code, item_code)``."""
    return (_appeal_attr(appeal, "error_code"), _appeal_attr(appeal, "item_code"))


def _latest_per_key(appeals: list) -> list:
    """Banding terbaru per ``(error_code, item_code, JENIS)`` — pengajuan berikutnya
    menggantikan yang sebelumnya.

    Penggantian berlaku HANYA sesama jenis. Sebelumnya kuncinya tanpa jenis, sehingga
    banding ``remove`` dianggap menggantikan banding ``add`` pada baris yang sama —
    padahal keduanya bukan dua versi dari permintaan yang sama, melainkan dua tahap
    berurutan: ``add`` MEMBUAT barisnya, ``remove`` MENGHAPUS baris itu.

    Akibat bug tersebut, begitu QC mengajukan penghapusan atas baris hasil ``add``,
    banding ``add``-nya lenyap dari semua penyaring — barisnya hilang dari tabel
    seketika padahal penghapusannya BARU DIAJUKAN (masih "menunggu"), lengkap dengan
    hilangnya pengurangan skornya. Persis alur yang dianjurkan docstring
    ``added_appeals_visible``: add ditolak -> baris tetap tampil -> QC mengajukan
    ``remove`` untuk membersihkannya."""
    latest = {}
    for a in appeals or []:
        latest[(*_appeal_row_key(a), _appeal_kind(a))] = a
    return list(latest.values())


def _appeal_seq(appeal) -> int:
    """Urutan pengajuan sebuah banding. ``id`` adalah serial yang naik terus, jadi
    id lebih besar = diajukan belakangan. 0 bila tidak ada (objek belum tersimpan)."""
    try:
        return int(_appeal_attr(appeal, "id") or 0)
    except (TypeError, ValueError):
        return 0


def _row_lifecycle(appeals: list) -> dict:
    """Riwayat hidup tiap baris Error Code: ``key -> (id_hapus_disetujui, id_add_terbaru)``.

    Sebuah baris bisa dihidupkan-dimatikan berkali-kali: ``add`` MEMBUAT baris,
    ``remove``/``change`` yang disetujui MENGHAPUSNYA, lalu ``add`` lagi bisa
    menghidupkannya kembali. Yang menentukan keadaan akhir adalah **mana yang
    diajukan belakangan**, bukan sekadar ada/tidaknya salah satu:

    - hapus disetujui SESUDAH add  -> barisnya hilang (add-nya batal);
    - add SESUDAH hapus disetujui  -> barisnya hidup lagi (hapusnya sudah terpakai).

    Tanpa perbandingan urutan ini, mengajukan ``add`` untuk kode yang penghapusannya
    pernah disetujui tidak akan pernah memunculkan barisnya kembali — hilang diam-diam
    walau bandingnya berstatus menunggu."""
    removed, added = {}, {}
    for a in appeals or []:
        key = _appeal_row_key(a)
        kind = _appeal_kind(a)
        seq = _appeal_seq(a)
        if kind == "add":
            if seq >= added.get(key, -1):
                added[key] = seq
        elif effective_appeal_status(a) == "approved":  # remove / change
            if seq >= removed.get(key, -1):
                removed[key] = seq
    return {k: (removed.get(k), added.get(k)) for k in set(removed) | set(added)}


def _keys_removed_by_approval(appeals: list) -> set:
    """Baris yang saat ini BENAR-BENAR terhapus: ada ``remove``/``change`` disetujui
    yang diajukan SESUDAH ``add`` terakhirnya (kalau ada). Penghapusan yang masih
    menunggu / ditolak tidak menghapus apa pun."""
    return {
        key for key, (removed_seq, added_seq) in _row_lifecycle(appeals).items()
        if removed_seq is not None and (added_seq is None or removed_seq > added_seq)
    }


def _keys_revived_after_removal(appeals: list) -> set:
    """Baris yang penghapusannya sudah TERPAKAI karena ada ``add`` yang lebih baru —
    penghapusan itu tidak boleh lagi menjatuhkan barisnya."""
    return {
        key for key, (removed_seq, added_seq) in _row_lifecycle(appeals).items()
        if removed_seq is not None and added_seq is not None and added_seq > removed_seq
    }


def approved_appeals_only(appeals: list) -> list:
    """Filter a list of appeals down to those whose current status is approved.

    An error code may be appealed repeatedly; only the latest appeal per
    ``(error_code, item_code, jenis)`` is authoritative, so an older approved row that
    was superseded by a newer pending/rejected one is ignored.

    Penghapusan yang sudah TERPAKAI juga dikeluarkan: kalau baris itu diajukan ``add``
    lagi SESUDAH penghapusannya disetujui, penghapusan lama tidak boleh menjatuhkan
    baris yang baru dihidupkan (lihat ``_row_lifecycle``)."""
    revived = _keys_revived_after_removal(appeals)
    return [
        a for a in _latest_per_key(appeals)
        if effective_appeal_status(a) == "approved"
        and not (_appeal_kind(a) in ("remove", "change") and _appeal_row_key(a) in revived)
    ]


def added_appeals_only(appeals: list) -> list:
    """Approved ``add`` bandings (latest per key). These attach a NEW error to an
    evaluation item/field and lower the score (except source ``others``).

    Baris yang penghapusannya SUDAH DISETUJUI dikecualikan — barisnya memang sudah
    tidak ada lagi, jadi pengurangan skornya ikut hilang."""
    gone = _keys_removed_by_approval(appeals)
    return [
        a for a in _latest_per_key(appeals)
        if _appeal_kind(a) == "add" and effective_appeal_status(a) == "approved"
        and _appeal_row_key(a) not in gone
    ]


def added_appeals_visible(appeals: list) -> list:
    """``add`` bandings that should render a row: pending (awaiting review), approved
    (applied), OR rejected. A rejected add stays visible (shown as ``rejected``)
    instead of vanishing — the QC keeps a record of the denied proposal and removes it
    later via a separate deletion request (a ``remove`` banding). It is display-only:
    the score uses ``added_appeals_only`` (approved), so a rejected add never deducts.

    Barisnya baru hilang setelah penghapusan itu DISETUJUI; selama penghapusannya
    masih menunggu, barisnya tetap tampil dengan status banding "menunggu"."""
    gone = _keys_removed_by_approval(appeals)
    return [
        a for a in _latest_per_key(appeals)
        if _appeal_kind(a) == "add" and effective_appeal_status(a) in ("pending", "approved", "rejected")
        and _appeal_row_key(a) not in gone
    ]


# Card Holder DYNAMIC verification (alamat kantor/rumah, telepon, email). Per KB_CL_24
# only 2 matches are required (min_verification_required = 2); each shortfall from the
# required 2 costs 7.5 (max deduction 15). Once >= 2 dynamic fields MATCH, the remaining
# MISMATCH dynamic fields add no further deduction.
#
# This MUST stay identical to the "address_group_score" rule in the campaign prompt
# (prompt_cashline_mus_v30.txt onwards) — the appeals recompute below works on the
# *delta* of this function, so any divergence produces a wrong appeal adjustment.
# SKIPPED_NULL (empty reference data) is NOT a violation and is never penalised: a
# group with zero MISMATCH scores 0 regardless of how few fields matched.
# The 9 DYNAMIC card-holder verification fields, aligned to KB_CL_24 VD_1..VD_9.
# A param counts toward the min-2 rule via a value MATCH (fields with reference data)
# OR a valid ask+answer event (event_verified, for reference-less params) — see
# card_holder_two_match_satisfied.
CARD_HOLDER_DYNAMIC_FIELDS = (
    "alamat_pengiriman_tagihan", "alamat_rumah", "no_telpon_terdaftar",
    "alamat_kantor", "no_telpon_kantor", "alamat_email_terdaftar",
    "nama_kartu_suplement", "jumlah_kartu_suplement", "nama_keluarga_relasi",
)
CARD_HOLDER_DYNAMIC_REQUIRED = 2
CARD_HOLDER_DYNAMIC_PENALTY = 7.5  # per shortfall from the required 2

# Per-field MISMATCH penalties, mirroring the campaign prompt
# (campaign_cashline/prompt_cashline_mus_v30.txt:1398-1421). Used ONLY by the QC
# "add error code" flow to deduct the right amount when flipping a currently-MATCH
# verification field to MISMATCH. The LLM otherwise sets these item_scores itself.
CASHLINE_FIELD_PENALTY = {
    "nominal_pencairan": 1, "tenor_dalam_bulan": 1, "nominal_cicilan_per_bulan": 1, "bunga": 1,
    "nama_bank": 2, "penalti_pelunasan_dipercepat": 2, "nomor_rekening": 2,
    "provisi": 4, "biaya_admin": 5,
}
# Card-holder verification no longer deducts from ai_score_verification. A static
# MISMATCH is expressed via the scorecard item (SC_CL_23_1/SC_CL_23_2) and a dynamic
# <2-match via SC_CL_24 — see _propagate_verification_to_scorecard. Penalties are 0 to
# avoid double-counting the same failure (v33 onwards).
CARD_HOLDER_STATIC_PENALTY = {
    "tanggal_lahir": 0, "nama_ibu_kandung": 0,
}
# CASHLINE field -> item scorecard yang dijatuhkan BELUM_SESUAI saat MISMATCH
# (aturan 31 Agustus 2026). Sebelumnya MISMATCH cashline memotong skor lewat jalurnya
# sendiri (``item_score`` per field -> ``ai_score_verification``) sementara item
# scorecard-nya tetap SESUAI — dua jalur potongan paralel untuk satu kegagalan.
#
# Model yang benar: scorecard menilai BUKAN HANYA apakah agent menanyakan/menjelaskan
# sesuatu, tetapi juga KEBENARAN datanya. Alurnya scorecard -> error code -> error code
# menjatuhkan item scorecard yang berasosiasi; TIDAK ADA potongan dari error code itu
# sendiri (lihat ``_propagate_cashline_to_scorecard``).
#
# Pemetaannya bukan tebakan: penalti per-field lama (``CASHLINE_FIELD_PENALTY``) SAMA
# PERSIS dengan bobot item di kolom kanan pada 9 dari 10 field — bukti bahwa tabel
# penalti itu memang diturunkan dari bobot item ini sejak awal. Yang dipilih adalah
# keluarga "menjelaskan/menanyakan" (SC_CL_6..15), tempat datanya PERTAMA KALI
# ditegakkan di panggilan; item "konfirmasi" (SC_CL_25..32) menilai kewajiban lain,
# yaitu ada-tidaknya recap penutup, dan dinilai sendiri.
#
# ``nama_pemilik_rekening`` satu-satunya yang belum punya penalti lama — hari ini ia
# menerbitkan B02 TANPA potongan sama sekali. Dipasangkan ke SC_CL_13, bobot 2.
CASHLINE_FIELD_SCORECARD = {
    "nominal_pencairan": "SC_CL_10",              # bobot 1
    "tenor_dalam_bulan": "SC_CL_6",               # bobot 1
    "nominal_cicilan_per_bulan": "SC_CL_9",       # bobot 1
    "bunga": "SC_CL_7",                           # bobot 1
    "nama_bank": "SC_CL_12",                      # bobot 2
    "penalti_pelunasan_dipercepat": "SC_CL_15",   # bobot 2
    "nomor_rekening": "SC_CL_14",                 # bobot 2
    "nama_pemilik_rekening": "SC_CL_13",          # bobot 2
    "provisi": "SC_CL_8",                         # bobot 4
    "biaya_admin": "SC_CL_11",                    # bobot 5
}

# Card Holder STATIC field -> the scorecard item it forces to BELUM_SESUAI on MISMATCH
# (VERIFICATION -> SCORECARD PROPAGATION in the campaign prompt). SC_CL_23_1 = tanggal
# lahir, SC_CL_23_2 = nama ibu kandung. The dynamic group maps to SC_CL_24 (2-match rule).
CARD_HOLDER_STATIC_SCORECARD = {
    "tanggal_lahir": "SC_CL_23_1",
    "nama_ibu_kandung": "SC_CL_23_2",
}
CARD_HOLDER_DYNAMIC_SCORECARD = "SC_CL_24"


def _card_holder_address_group_score(items) -> float:
    """Dynamic card-holder fields no longer deduct from ai_score_verification (v33).

    A shortfall of the KB_CL_24 2-match rule is now expressed as SC_CL_24 BELUM_SESUAI
    in the scorecard (see _propagate_verification_to_scorecard), which lowers
    ai_score_phase_2 instead. Always returns 0.0 so the same failure is not counted
    twice. Kept as a function so the appeal appliers' group_score_fn hook still works."""
    return 0.0


def _propagate_verification_to_scorecard(evaluation: dict) -> dict:
    """Force the scorecard verification items to BELUM_SESUAI based on the (possibly
    appeal-adjusted) card_holder_verification match state — the Python mirror of the
    prompt's VERIFICATION -> SCORECARD PROPAGATION rule:

    - static field ``tanggal_lahir``/``nama_ibu_kandung`` MISMATCH -> SC_CL_23_1/SC_CL_23_2
      BELUM_SESUAI.
    - static field PENDING (zona abu-abu, dokumen pendukung masih ditunggu dalam
      tenggat H+2) -> item scorecard yang sama ikut PENDING. Lihat di bawah.
    - fewer than 2 of the 9 dynamic params VERIFIED (value MATCH or event_verified) ->
      SC_CL_24 (hybrid KB_CL_24 2-match rule, see card_holder_two_match_satisfied).

    PENDING IKUT DIPROPAGASIKAN (28 Agustus 2026). Sebelumnya hanya MISMATCH yang
    turun, sehingga baris verifikasi berbunyi PENDING sementara item scorecard-nya
    tetap SESUAI — dua permukaan yang menjelaskan hal yang sama saling bertentangan
    di layar yang sama (tiket 0602247CJA tanggal lahir, 010753NldX nama ibu kandung).

    Bedanya dengan MISMATCH: PENDING **tidak memotong skor**. ``item_score`` dibiarkan
    apa adanya, karena zona abu-abu adalah PENANGGUHAN, bukan vonis — tiketnya sedang
    menunggu dokumen, belum gagal. Konsekuensinya otomatis benar di seluruh sistem:
    ``scorecard_score`` hanya mengurangi bobot item BELUM_SESUAI (PENDING tidak
    dikurangi), ``has_blocking_intolerable_item`` hanya memveto BELUM_SESUAI, dan
    ``_sync_critical_compliance`` hanya menjatuhkan item kritis yang BELUM_SESUAI —
    jadi tidak ada score bomb untuk item yang masih ditunggu. Begitu tenggat H+2 lewat
    tanpa unggahan, ``apply_static_document_status`` mengubah barisnya menjadi
    MISMATCH dan jalur BELUM_SESUAI yang biasa mengambil alih.

    Only ever forces BELUM_SESUAI (item_score 0) or PENDING (item_score kept); a
    MATCH / >=2-match never restores a SESUAI (to re-pass, QC appeals the scorecard item
    directly). Non-destructive; does not itself recompute ai_score_phase_2 (callers
    re-derive the score from scorecard_result).

    ``reason`` item yang dipaksa turun ikut ditulis ulang: teks aslinya ditulis LLM
    untuk menjelaskan status SESUAI ("nasabah menjawab dengan nama yang konsisten"),
    jadi membiarkannya membuat tabel Scorecard berbunyi BELUM_SESUAI dengan alasan
    yang justru menyatakan item itu terpenuhi. Sumber kalimatnya sama dengan kolom
    Reason tabel Error Code (``static_verification_failure_reason``) supaya kedua
    permukaan tidak bisa berbunyi berbeda untuk kegagalan yang sama."""
    if not evaluation:
        return evaluation
    ch = evaluation.get("card_holder_verification") or []
    if not ch:
        return evaluation
    ch_match = {(v or {}).get("field"): (v or {}).get("match") for v in ch}
    force_belum = set()
    # code -> field, untuk item yang ditangguhkan (dokumen pendukung masih ditunggu).
    force_pending = {}
    for field, code in CARD_HOLDER_STATIC_SCORECARD.items():
        state = ch_match.get(field)
        if state == "MISMATCH":
            force_belum.add(code)
        elif state == "PENDING":
            force_pending[code] = field
    if not card_holder_two_match_satisfied(ch):
        force_belum.add(CARD_HOLDER_DYNAMIC_SCORECARD)
    # BELUM_SESUAI menang bila keduanya entah bagaimana menunjuk item yang sama:
    # kegagalan yang sudah pasti mengalahkan penangguhan.
    force_pending = {c: f for c, f in force_pending.items() if c not in force_belum}
    if not force_belum and not force_pending:
        return evaluation
    new_items = []
    changed = False
    for it in evaluation.get("scorecard_result") or []:
        code = (it or {}).get("item_code")
        if code in force_pending and (it or {}).get("status") not in ("BELUM_SESUAI", "PENDING"):
            # item_score SENGAJA tidak disentuh: penangguhan tidak memotong skor.
            updated = {**it, "status": "PENDING"}
            updated["reason"] = static_verification_pending_reason(
                force_pending[code], evaluation)
            new_items.append(updated)
            changed = True
            continue
        if code in force_belum and (it or {}).get("status") != "BELUM_SESUAI":
            updated = {**it, "status": "BELUM_SESUAI", "item_score": 0}
            # Static: sebutkan SEBAB sebenarnya (tidak ditanya / tidak konsisten /
            # mismatch Ascend). Dynamic SC_CL_24 tidak punya satu field penyebab —
            # yang gagal adalah aturan minimal 2 parameter terverifikasi.
            reason = static_verification_failure_reason(code, evaluation)
            if reason is None and code == CARD_HOLDER_DYNAMIC_SCORECARD:
                reason = (f"Kurang dari {CARD_HOLDER_DYNAMIC_REQUIRED} parameter "
                          f"verifikasi dinamis yang terverifikasi (aturan KB_CL_24)")
            if reason:
                updated["reason"] = reason
            new_items.append(updated)
            changed = True
        else:
            new_items.append(it)
    return {**evaluation, "scorecard_result": new_items} if changed else evaluation


def _cashline_pending_applies(item, row) -> bool:
    """Bolehkah item scorecard ``item`` ditangguhkan menjadi PENDING oleh baris cashline
    ``row``?

    * item yang sudah PENDING -> tidak ada yang perlu diubah;
    * item SESUAI -> selalu boleh (jalur lama);
    * item BELUM_SESUAI -> hanya bila transkrip memang memuat nilainya
      (``extracted_value`` terisi). Lihat ``_propagate_cashline_to_scorecard``.
    """
    status = (item or {}).get("status")
    if status == "PENDING":
        return False
    if status != "BELUM_SESUAI":
        return True
    return bool(str((row or {}).get("extracted_value") or "").strip())


def _propagate_cashline_to_scorecard(evaluation: dict) -> dict:
    """MISMATCH ``cashline_data_verification`` menjatuhkan item scorecard pasangannya.

    Aturan 31 Agustus 2026. Cerminan dari VERIFICATION -> SCORECARD PROPAGATION yang
    sudah lama berlaku untuk card holder, kini dipasang juga untuk cashline:

        scorecard dibuat -> error code -> error code menjatuhkan item scorecard

    Konsekuensi pentingnya, dan alasan fungsi ini ada: **error code tidak lagi
    memotong skor sendiri.** Satu-satunya sumber potongan adalah item scorecard yang
    BELUM_SESUAI. Karena itu ``ai_score_verification`` ikut dinolkan di sini — kalau
    tidak, satu kegagalan yang sama akan dipotong DUA KALI (sekali lewat penalti
    per-field, sekali lewat bobot item scorecard). Sisi card holder sudah dinolkan
    sejak v33 dengan alasan yang persis sama; cashline adalah penyumbang terakhir yang
    tersisa di sana.

    Contoh ``010714jUKH``: ``nomor_rekening`` di TMS 4820008469 sedangkan transkrip
    4823008469 (digit ke-4 salah input). Sebelum aturan ini SC_CL_14 tetap SESUAI dan
    potongannya -2 datang dari ``ai_score_verification``. Sekarang SC_CL_14 menjadi
    BELUM_SESUAI dan -2 itu datang dari bobot itemnya sendiri.

    Hanya ``MISMATCH`` yang menjatuhkan; ``SKIPPED_NULL`` tidak berpenalti. Satu field
    punya zona penangguhan ``PENDING`` — ``nama_pemilik_rekening``, yang menunggu cover
    buku tabungan selama tenggat H+2 (lihat ``apply_cashline_document_status``): item
    pasangannya ikut PENDING, tidak memotong skor dan tidak memveto AI Status.

    PENANGGUHAN JUGA MENGANGKAT ITEM YANG SUDAH BELUM_SESUAI (31 Agustus 2026). Prompt
    menyuruh LLM menuliskan sendiri propagasi MISMATCH -> BELUM_SESUAI, sementara ia
    DILARANG menerbitkan PENDING (keadaan dokumen & tenggat H+2 hanya diketahui sistem).
    Akibatnya SC_CL_13 sudah tertulis BELUM_SESUAI sebelum fungsi ini berjalan, dan
    penjaga lama — yang hanya mengizinkan SESUAI -> PENDING — melewatinya: barisnya
    berbunyi PENDING tetapi item scorecard-nya tetap BELUM_SESUAI, memveto tiket menjadi
    Not Qualified plus iris 10% ``non_tolerable_bomb`` justru selama masa tunggu dokumen.
    Pada korpus 98 tiket ada 10 tiket dalam keadaan itu, seluruhnya SC_CL_13 (mis.
    ``060228OEvG``: 89% "Vonny Salomi Amnifu" vs TMS "VONI SALOMI AMNIFU", 148 -> 133 dan
    PASS -> FAIL). ``item_score`` dikembalikan ke bobot penuh agar tabel Scorecard tidak
    menampilkan 0 untuk item yang tidak dipotong.

    SYARATNYA transkrip memang MEMUAT nilainya (``extracted_value`` terisi). Bila kosong
    — agent tidak pernah menyebutkan nama pemilik rekening (mis. ``111030VH2c``) — item
    itu gagal karena kewajiban "menyebutkan/menanyakan"-nya sendiri, bukan karena beda
    ejaan, dan cover buku tabungan tidak bisa membuktikan sesuatu yang tidak pernah
    diucapkan. Item semacam itu TETAP BELUM_SESUAI.

    Non-destruktif. Dipanggil berdampingan dengan
    ``_propagate_verification_to_scorecard``.
    """
    if not evaluation:
        return evaluation
    rows = evaluation.get("cashline_data_verification") or []
    if not rows:
        return evaluation
    force, pending = {}, {}
    for v in rows:
        field = (v or {}).get("field")
        code = CASHLINE_FIELD_SCORECARD.get(field)
        if not code:
            continue
        state = (v or {}).get("match")
        if state == "MISMATCH":
            force.setdefault(code, v)
        elif state == "PENDING":
            pending.setdefault(code, v)
    # BELUM_SESUAI menang bila keduanya menunjuk item yang sama — kegagalan yang sudah
    # pasti mengalahkan penangguhan (sama seperti sisi statik).
    pending = {c: v for c, v in pending.items() if c not in force}
    result = evaluation
    if force or pending:
        new_items = []
        changed = False
        for it in evaluation.get("scorecard_result") or []:
            code = (it or {}).get("item_code")
            if code in force and (it or {}).get("status") != "BELUM_SESUAI":
                v = force[code]
                updated = {**it, "status": "BELUM_SESUAI", "item_score": 0}
                reason = str((v or {}).get("reason") or "").strip()
                if reason:
                    updated["reason"] = reason
                new_items.append(updated)
                changed = True
            elif code in pending and _cashline_pending_applies(it, pending[code]):
                # PENDING tidak memotong skor dan tidak memveto AI Status. Untuk item
                # yang datang dari SESUAI, ``item_score`` dibiarkan apa adanya; untuk
                # yang DIANGKAT dari BELUM_SESUAI, ia dikembalikan ke bobot penuh —
                # nilainya sudah dinolkan LLM padahal penangguhan tidak memotong.
                v = pending[code]
                updated = {**it, "status": "PENDING"}
                if (it or {}).get("status") == "BELUM_SESUAI":
                    weight = (it or {}).get("weight")
                    if isinstance(weight, (int, float)) and not isinstance(weight, bool):
                        updated["item_score"] = weight
                reason = str((v or {}).get("reason") or "").strip()
                if reason:
                    updated["reason"] = reason
                new_items.append(updated)
                changed = True
            else:
                new_items.append(it)
        if changed:
            result = {**evaluation, "scorecard_result": new_items}

    # Potongan per-field DILEPAS seluruhnya, terlepas dari ada tidaknya MISMATCH yang
    # dijatuhkan di atas: begitu potongannya pindah ke scorecard, membiarkan angka lama
    # tetap hidup di ``ai_score_verification`` berarti menghitungnya dua kali.
    try:
        already_zero = float(result.get("ai_score_verification")) == 0
    except (TypeError, ValueError):
        already_zero = result.get("ai_score_verification") is None
    if not already_zero:
        result = {**result, "ai_score_verification": 0}
    return result


def card_holder_two_match_satisfied(items) -> bool:
    """HYBRID KB_CL_24 min_verification_required=2 gate over the 9 dynamic card-holder
    fields (VD_1..VD_9). A param counts as VERIFIED when EITHER its value matches the
    reference (``match == "MATCH"``) OR — for reference-less params (no ascend column
    yet) — a valid ask+answer event occurred (``event_verified is True``). True when
    >= 2 params are verified. Callers use this to (a) hide penalty-free leftover
    MISMATCH rows once the rule is met and (b) drive SC_CL_24 status via
    ``_propagate_verification_to_scorecard``."""
    dyn = [
        v or {} for v in (items or [])
        if (v or {}).get("field") in CARD_HOLDER_DYNAMIC_FIELDS
    ]
    verified = sum(
        1 for v in dyn
        if v.get("match") == "MATCH" or v.get("event_verified") is True
    )
    return verified >= CARD_HOLDER_DYNAMIC_REQUIRED


def _apply_verification_appeals(
    evaluation: dict,
    approved_appeals: list,
    eval_key: str,
    code_match,
    group_fields: tuple = (),
    group_score_fn=None,
) -> dict:
    """Shared applier for data-verification banding (Card Holder / Cashline).

    For each approved appeal whose ``error_code`` satisfies ``code_match`` (keyed by
    the titleized field == ``item_code``), the matching ``evaluation[eval_key]`` item
    whose ``match`` is ``MISMATCH``/``SKIPPED_NULL`` is flipped to ``MATCH`` with
    ``item_score`` reset to 0 and its ``reference_value``/``extracted_value``
    replaced by the QC-submitted ones. The removed per-field deduction is added
    back to ``ai_score_verification`` (the single frozen verification-score field
    shared by both sources) so the score lifts consistently across the detail
    view, the Results list and the XLSX export. Non-destructive.

    Fields named in ``group_fields`` are excluded from the per-field delta; instead
    ``group_score_fn(items)`` is evaluated on the pre- and post-flip item lists and
    their difference is applied (used for dynamic card-holder verification, which is
    scored as a group of 4 with a min-2-match rule rather than field-by-field)."""
    if not evaluation or not approved_appeals:
        return evaluation
    # titleized field (item_code) -> latest approved appeal for it (this source only)
    #
    # ``item_code`` bisa berisi BEBERAPA field yang digabung (lihat
    # ``merge_dynamic_verification_rows``: "Alamat Rumah, Alamat Kantor, ..."). Satu
    # banding atas baris gabungan itu harus mengenai SEMUA field yang diwakilinya —
    # kalau tidak, banding yang disetujui tidak mengubah apa pun karena tak satu pun
    # nama field cocok dengan teks gabungannya.
    by_field = {}
    for a in approved_appeals:
        if not code_match((_appeal_attr(a, "error_code") or "").strip().upper()):
            continue
        item_code = _appeal_attr(a, "item_code")
        if not item_code:
            continue
        for part in str(item_code).split(MERGED_ITEM_SEP):
            part = part.strip()
            if part:
                by_field[part] = a
    if not by_field:
        return evaluation

    old_items = evaluation.get(eval_key) or []
    new_items = []
    delta = 0.0
    changed = False
    for v in old_items:
        appeal = by_field.get(titleize_field((v or {}).get("field")))
        if appeal is None or (v or {}).get("match") not in ("MISMATCH", "SKIPPED_NULL"):
            new_items.append(v)
            continue
        try:
            old_score = float(v.get("item_score"))
        except (TypeError, ValueError):
            old_score = 0.0
        # Fields scored as a group (e.g. dynamic card-holder verification) are settled
        # by ``group_score_fn`` below, NOT by their per-field item_score — skip them here.
        if old_score < 0 and (v or {}).get("field") not in group_fields:
            delta += -old_score  # add the removed deduction back to the score
        updated = {**v, "match": "MATCH", "item_score": 0, "similarity_percent": 100}
        ref = _appeal_attr(appeal, "qc_reference_value")
        ext = _appeal_attr(appeal, "qc_extracted_value")
        qc_reason = _appeal_attr(appeal, "qc_reason")
        if ref is not None:
            updated["reference_value"] = ref
        if ext is not None:
            updated["extracted_value"] = ext
        if qc_reason:
            updated["reason"] = qc_reason
        new_items.append(updated)
        changed = True

    if not changed:
        return evaluation
    # Group-scored fields: recompute the group total before vs after the flips and add
    # the difference (a flip only lifts the score when it raises matched_count to the
    # required minimum; extra matches beyond the minimum add nothing).
    if group_score_fn is not None:
        delta += group_score_fn(new_items) - group_score_fn(old_items)
    result = {**evaluation, eval_key: new_items}
    verif = evaluation.get("ai_score_verification")
    if delta and verif is not None:
        try:
            val = float(verif) + delta
            result["ai_score_verification"] = int(val) if val == int(val) else val
        except (TypeError, ValueError):
            pass
    return result


def is_cashline_code(error_code: str) -> bool:
    """A row/appeal belongs to Cashline Data Verification if its error code is
    B02/B03/B05 — or the generic ``"B02/B03/B05"`` fallback emitted when a field
    can't be matched to a specific code (``code_for_cashline_field``)."""
    ec = (error_code or "").strip().upper()
    return any(x in ec for x in ("B02", "B03", "B05"))


def _restore_scorecard_items(evaluation: dict, codes: set) -> dict:
    """Flip the named scorecard items ``BELUM_SESUAI`` -> ``SESUAI`` (restoring full
    weight). Shared mechanism for lifting a propagation-driven scorecard item when the
    verification banding that caused it is approved. Non-destructive; only ever
    restores the codes passed in, and never forces a BELUM_SESUAI."""
    if not evaluation or not codes:
        return evaluation
    new_items = []
    changed = False
    for item in evaluation.get("scorecard_result") or []:
        if (item or {}).get("item_code") in codes and (item or {}).get("status") == "BELUM_SESUAI":
            updated = {**item, "status": "SESUAI"}
            if item.get("weight") is not None:
                updated["item_score"] = item.get("weight")
            new_items.append(updated)
            changed = True
        else:
            new_items.append(item)
    return {**evaluation, "scorecard_result": new_items} if changed else evaluation


def _restore_critical_items(evaluation: dict, codes: set) -> dict:
    """Flip ``critical_compliance_check`` items ``FAIL`` -> ``PASS`` untuk ``codes``,
    lalu kembalikan potongan penaltinya pada ``ai_score_critical_compliance_check``.

    Keempat item kritikal (SC_CL_4/23_1/23_2/37) adalah baris scorecard biasa, jadi
    apa pun yang memulihkan item scorecard-nya WAJIB memulihkan irisan kritikalnya
    juga. Tanpa itu tiket bisa berbunyi MATCH + SESUAI di tabel verifikasi & scorecard
    tetapi tetap membawa penalti kritikal penuh — persis yang terjadi sebelum
    perbaikan 21 Agustus 2026, ketika ``normalize_static_verification`` memulihkan
    SC_CL_23_x tanpa menyentuh blok kritikal dan tiketnya tetap Not Qualified dengan
    skor 150 dari batas 135.

    ``ai_score_critical_compliance_check`` adalah penalti aditif beku
    (``-(maximum_score/4)`` per item yang FAIL). Tiap item yang dipulihkan
    mengembalikan tepat satu irisan, dihitung dari nilai bekunya:
    ``baru = beku * sisa_fail / fail_awal``. Non-destruktif."""
    if not evaluation or not codes:
        return evaluation
    ccc = evaluation.get("critical_compliance_check")
    if not isinstance(ccc, dict):
        return evaluation
    items = ccc.get("checked_items")
    if not items:
        return evaluation
    orig_fail = sum(1 for it in items if (it or {}).get("status") == "FAIL")
    if orig_fail == 0:
        return evaluation

    new_items = []
    flipped = 0
    for it in items:
        if (it or {}).get("status") == "FAIL" and (it or {}).get("item_code") in codes:
            new_items.append({**it, "status": "PASS"})
            flipped += 1
        else:
            new_items.append(it)
    if flipped == 0:
        return evaluation

    new_status = "PASS" if all((it or {}).get("status") == "PASS" for it in new_items) else "FAIL"
    result = {
        **evaluation,
        "critical_compliance_check": {**ccc, "status": new_status, "checked_items": new_items},
    }
    frozen = evaluation.get("ai_score_critical_compliance_check")
    if frozen is not None:
        try:
            val = float(frozen) * (orig_fail - flipped) / orig_fail
            result["ai_score_critical_compliance_check"] = int(val) if val == int(val) else val
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return result


def _card_holder_static_restore_codes(approved_appeals: list) -> set:
    """Scorecard item_code(s) an approved Card Holder STATIC (B17) remove/change banding
    must also restore. A static field (tanggal_lahir/nama_ibu_kandung) carries a 0
    penalty on the card_holder row itself — its real deduction lives on the linked
    scorecard item (SC_CL_23_1/SC_CL_23_2) and that item's critical check (VERIFICATION
    -> SCORECARD PROPAGATION). Appealing the B17 away therefore has to lift the scorecard
    item too, otherwise removing the only surfaced B17 row moves nothing. The banding's
    own item_code is the field LABEL ("Tanggal Lahir"), so map it back via
    ``CARD_HOLDER_STATIC_SCORECARD``. Dynamic fields map to the grouped SC_CL_24 and are
    settled separately after the flip."""
    labels = {titleize_field(f): c for f, c in CARD_HOLDER_STATIC_SCORECARD.items()}
    out = set()
    for a in approved_appeals or []:
        if (_appeal_attr(a, "error_code") or "").strip().upper() != "B17":
            continue
        # ``item_code`` bisa berisi beberapa label yang digabung (baris B17 dinamis
        # yang dilebur); dipecah supaya pencocokannya tetap per field. Baris STATIK
        # sendiri tidak pernah dilebur, jadi di praktiknya ini hanya satu bagian.
        for part in str(_appeal_attr(a, "item_code") or "").split(MERGED_ITEM_SEP):
            code = labels.get(part.strip())
            if code:
                out.add(code)
    return out


def _has_dynamic_card_holder_appeal(approved_appeals: list) -> bool:
    """True when an approved B17 banding targets one of the 9 DYNAMIC card-holder
    fields (item_code == titleized dynamic field). Used to gate the SC_CL_24 restore."""
    labels = {titleize_field(f) for f in CARD_HOLDER_DYNAMIC_FIELDS}
    # Baris B17 dinamis yang dilebur membawa BEBERAPA label sekaligus, jadi
    # pencocokan harus per bagian — kalau tidak, banding atas baris gabungan tidak
    # pernah membuka gerbang pemulihan SC_CL_24.
    return any(
        (_appeal_attr(a, "error_code") or "").strip().upper() == "B17"
        and any(part.strip() in labels
                for part in str(_appeal_attr(a, "item_code") or "").split(MERGED_ITEM_SEP))
        for a in approved_appeals or []
    )


# Kebijakan 10 Agustus 2026 (aturan) + 21 Agustus 2026 (teks): penyebutan nasabah yang
# berubah-ubah antar pengulangan tetap menggugurkan tiket — Not Qualified, tidak boleh
# singgah di PENDING/dokumen pendukung — tetapi TIDAK lagi diberi label "Indikasi
# Fraud" di permukaan mana pun. Yang menjalankan aturannya adalah
# ``static_consistency_failures`` di bawah; kalimat khususnya sudah dihapus.


def _reason_says_inconsistent(reason) -> bool:
    """True bila teks alasan menyatakan penyebutan nasabah TIDAK konsisten.

    Sengaja mencari frasa negatifnya, bukan kata "konsisten" saja: alasan seperti
    "Nasabah konsisten menyebut Zandra, tetapi tidak sama dengan Ascend" justru
    kebalikannya — konsisten tapi gagal di TAHAP 2. Mencocokkan kata telanjang
    membuat baris itu salah dibaca sebagai kegagalan tahap 1."""
    text = str(reason or "").casefold()
    return "tidak konsisten" in text or "inkonsisten" in text


def _is_static_consistency_failure(row: dict) -> bool:
    """True bila baris card_holder_verification ini gugur di CEK KONSISTENSI (tahap 1),
    bukan karena tidak cocok dengan Ascend (tahap 2).

    Penanda utamanya bendera ``consistency_failed`` dari prompt; hasil lama yang belum
    punya bendera itu dikenali dari kata "konsisten" pada ``reason`` — konvensi yang
    sama dengan ``static_verification_failure_reason`` dan normalisasi band."""
    v = row or {}
    if v.get("field") not in STATIC_CONSISTENCY_FIELDS:
        return False
    if (v.get("match") or "") != "MISMATCH":
        return False
    if v.get("consistency_failed") is True:
        return True
    return _reason_says_inconsistent(v.get("reason"))


def static_consistency_failures(evaluation: dict) -> list:
    """Label field statik yang gugur di tahap 1 (penyebutan tidak konsisten).

    Kosong = tidak ada kegagalan konsistensi dari aturan ini.

    SELALU kosong untuk evaluasi ber-``static_rules_version`` >= 2: revamp 21 Agustus
    2026 menghapus aturan konsistensi antar-penyebutan seluruhnya, jadi veto AI Status
    yang bersandar padanya ikut mati untuk tiket baru. Digatekan di sini, di satu
    tempat, supaya kalimat sisa dari LLM ("tidak konsisten" yang lolos ke reason) tidak
    bisa menghidupkan kembali aturan yang sudah dicabut."""
    from compliance.documents import static_rules_version

    if static_rules_version(evaluation) >= 2:
        return []
    out = []
    for row in (evaluation or {}).get("card_holder_verification") or []:
        if _is_static_consistency_failure(row):
            out.append(STATIC_VERIFICATION_ITEMS_BY_FIELD.get((row or {}).get("field"))
                       or titleize_field((row or {}).get("field")))
    return out


def _format_percent(value) -> str:
    """Persentase untuk teks reason, gaya Indonesia: 67 -> "67%", 87.5 -> "87,5%".
    String kosong bila angkanya tidak terbaca."""
    if isinstance(value, bool) or value is None:
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return ""
    if num != num:  # NaN
        return ""
    if num == int(num):
        return f"{int(num)}%"
    return f"{num:g}".replace(".", ",") + "%"


def _static_band_reason(row: dict, rule: dict) -> str:
    """Kalimat ``reason`` deterministik untuk satu baris verifikasi STATIK, ditulis
    dari angka yang SUDAH dihitung ulang Python.

    Perlu karena ``normalize_static_verification`` menimpa ``extracted_value``,
    ``similarity_percent`` dan ``match`` tetapi teks ``reason``-nya ditulis LLM untuk
    vonisnya sendiri. Pada tiket 020338gGlU baris ``nama_ibu_kandung`` berbunyi "masih
    di atas ambang match Ascend" padahal similarity hasil hitung ulang 67% (MISMATCH):
    kalimat yang bertolak belakang dengan kolom Match di sebelahnya membuat QC tidak
    bisa mempercayai keduanya, dan kalimat itu ikut terbawa ke kolom Reason tabel
    Error Code.

    Mengikuti pola yang diminta prompt (AMBANG DOKUMEN PENDUKUNG): nilai yang disebut,
    nilai Ascend, persentase, lalu tindak lanjut. Dua larangan kata dipatuhi:

    - di zona abu-abu kata "sesuai"/"cocok" TIDAK dipakai — nilainya memang belum
      sama, itu justru sebabnya dokumen diminta;
    - "tidak konsisten"/"inkonsisten" TIDAK pernah muncul. Frasa itu penanda
      kegagalan TAHAP 1 yang dibaca ``_reason_says_inconsistent``,
      sedangkan baris yang sampai ke sini justru sudah LOLOS tahap 1 dan gugur di
      tahap 2. Menuliskannya akan membuat tiket salah divonis fraud.
    """
    from compliance.documents import DOCUMENT_TYPES

    label = rule["label"]
    sim = row.get("similarity_percent")
    ext = str(row.get("extracted_value") or "").strip()
    ref = str(row.get("reference_value") or "").strip()
    if not ext:
        return (f"{label} tidak disebut nasabah sehingga data Ascend "
                f"tidak terverifikasi.")
    ref_part = f', Ascend "{ref}"' if ref else ""
    detail = f'{label} disebut "{ext}"{ref_part} — mirip {_format_percent(sim)}'
    if sim >= rule["match_min"]:
        return f"{detail}, sesuai Ascend."
    if sim >= rule["doc_min"]:
        doc = DOCUMENT_TYPES.get(rule["doc_type"], {}).get(
            "label", str(rule["doc_type"]).upper())
        return f"{detail}, perlu verifikasi dokumen {doc}."
    return (f"{detail}, di bawah ambang {_format_percent(rule['doc_min'])} "
            f"sehingga mismatch dengan Ascend.")


def _normalize_for_backing(value) -> str:
    """Bentuk bandingan longgar untuk mencocokkan nilai bersih LLM dengan ucapan
    mentah: huruf & angka saja, huruf kecil."""
    return re.sub(r"[^0-9a-z]+", "", str(value or "").casefold())


def _source_file_of_value(value, rows: list) -> "str | None":
    """Nama PDF tempat ``value`` diucapkan — ucapan PALING BARU yang nilainya sama,
    sejalan dengan aturan seri "yang paling baru menang".

    ``None`` bila nilainya tidak berasal dari ucapan mana pun (mis. nilai bersih LLM
    yang tidak identik dengan ucapan mentah) atau ucapannya tidak membawa nama berkas
    (hasil sebelum prompt v56) — pemanggil membiarkan ``source_file`` apa adanya."""
    found = None
    for r in rows or []:
        if r.get("value") == value and str(r.get("source_file") or "").strip():
            found = str(r["source_file"]).strip()
    if found:
        return found
    for r in rows or []:
        if _normalize_for_backing(r.get("value")) == _normalize_for_backing(value) and \
                str(r.get("source_file") or "").strip():
            found = str(r["source_file"]).strip()
    return found


def normalize_static_verification(evaluation: dict) -> dict:
    """Jadikan verifikasi STATIK card holder deterministik: hitung ulang
    ``similarity_percent`` di Python, pilih penyebutan TERBAIK, lalu tegakkan
    AMBANG DOKUMEN PENDUKUNG — tiga hal yang sebelumnya sepenuhnya bergantung pada
    kepatuhan & ketelitian LLM.

    1. **Similarity dihitung ulang** (``compliance.static_similarity``) dari
       ``reference_value`` vs penyebutan nasabah. Angka LLM terbukti bisa meleset —
       "ARNIYETTI" vs "Sarieti" pernah dilaporkan 44% padahal 56% — dan selisih
       sebesar itu bisa memindahkan tiket melewati ambang 80 / 87,5.
    2. **Penyebutan terbaik dipakai** (KB v23 / prompt v59, tanpa syarat tanggal): setiap
       elemen ``extracted_mentions`` diadu ke Ascend, yang tertinggi menjadi
       ``extracted_value``. Seri dimenangkan yang paling baru.
    3. **Ambang ditegakkan** seperti di bawah.
    4. **``reason`` ditulis ulang** (``_static_band_reason``) untuk setiap baris yang
       angkanya kita ubah. Tanpa ini baris yang sudah dikoreksi tetap membawa kalimat
       LLM yang menjelaskan vonis LAMA — tiket 020338gGlU berbunyi "masih di atas
       ambang match Ascend" tepat di sebelah kolom Match yang berbunyi MISMATCH.

    Aturannya (sama dengan ``compliance.documents.CARD_HOLDER_DOC_BANDS`` dan tabel
    di prompt): ``nama_ibu_kandung`` >= 80 dan ``tanggal_lahir`` >= 87,5 adalah
    **MATCH** — di zona abu-abu (di bawah ``match_min``) bank meminta dokumen
    pendukung, bukan menyalahkan agent. Di bawah ambang itu MISMATCH.

    ATURAN MANA yang dipakai dibaca dari cap ``static_rules_version`` pada evaluasi
    itu sendiri (``compliance.documents.static_rules_version``) — dicap sekali saat
    tiket diproses, jadi tiket lama tidak pernah dinilai ulang dengan aturan baru:

    * **Tiket lama** — seperti sebelumnya, termasuk pengecualian TAHAP 1: baris yang
      gagal lewat STATIC VERIFICATION CONSISTENCY RULE tidak disentuh, karena di baris
      itu ``similarity_percent`` berisi kemiripan ANTAR-PENYEBUTAN nasabah, BUKAN
      terhadap Ascend — membacanya sebagai nilai band akan "menyelamatkan" tiket yang
      justru gagal karena jawabannya berubah-ubah.
    * **Tiket baru (revamp 21 Agustus 2026, prompt v56)** — aturan konsistensi
      DIHAPUS: setiap penyebutan langsung diadu ke Ascend. Sebagai gantinya hanya
      SELURUH penyebutan nasabah diadu ke Ascend dan yang similarity-nya TERTINGGI
      dipakai, tanpa syarat tanggal apa pun. Ambang ``nama_ibu_kandung`` juga berubah
      menjadi 80 (MATCH) / 50 (batas MISMATCH).

    Sempat ada syarat tanggal panggilan (harus setanggal ``submit_time`` TMS, lalu
    dilonggarkan jadi prioritas berjenjang). **Dicabut atas konfirmasi Bank Mega**:
    tanggal panggilan tidak menjadi syarat pengambilan bukti verifikasi statik, jadi
    ucapan dari panggilan mana pun setara. ``source_file`` per ucapan tetap ditulis
    karena berguna menelusuri bukti.

    SATU PENGECUALIAN yang berlaku untuk kedua aturan: ``similarity_percent`` kosong
    (SKIPPED_NULL / tidak dilaporkan) dan tidak ada penyebutan yang bisa dihitung —
    tidak ada angka yang bisa dijadikan dasar, jadi vonis LLM dibiarkan.

    Bila sebuah field statik dikoreksi menjadi MATCH, item scorecard 1:1-nya
    (SC_CL_23_1 / SC_CL_23_2) ikut dipulihkan ke SESUAI — sama seperti yang dilakukan
    banding B17 yang disetujui. Tanpa itu tabel verifikasi akan berbunyi MATCH
    sementara skornya tetap dipotong.

    Non-destruktif: mengembalikan evaluasi baru hanya bila ada yang berubah. Panggil
    SEBELUM ``_propagate_verification_to_scorecard`` agar scorecard, skor, AI Status,
    tabel Error Code, dan permintaan dokumen semuanya ikut nilai yang sudah dikoreksi.
    """
    if not evaluation:
        return evaluation
    items = evaluation.get("card_holder_verification")
    if not isinstance(items, list) or not items:
        return evaluation
    from compliance.documents import card_holder_doc_bands, static_rules_version
    from compliance.static_similarity import (
        best_static_match,
        four_digit_year_required,
        mention_rows,
    )

    bands = card_holder_doc_bands(evaluation)
    v2 = static_rules_version(evaluation) >= 2

    # --- 1 & 2: similarity dihitung ulang atas penyebutan TERBAIK ---------------
    # ``dirty`` = indeks baris yang angka/vonisnya kita ubah; hanya baris itu yang
    # ``reason``-nya ditulis ulang di tahap 4. Baris yang angkanya sudah benar
    # dibiarkan apa adanya supaya kalimat LLM yang informatif (menyebut evidence,
    # cara nasabah mengeja, dsb) tidak tergantikan template yang lebih miskin.
    recomputed = []
    dirty: set = set()
    for idx, it in enumerate(items):
        v = it or {}
        field = v.get("field")
        if field not in STATIC_CONSISTENCY_FIELDS:
            recomputed.append(it)
            continue
        if not v2 and _is_static_consistency_failure(v):
            # Aturan LAMA, gugur TAHAP 1: similarity-nya kemiripan ANTAR-PENYEBUTAN,
            # bukan terhadap Ascend — tidak boleh dihitung ulang sebagai similarity
            # Ascend. Pada aturan baru cabang ini tidak ada lagi.
            recomputed.append(it)
            continue
        ref = v.get("reference_value")
        if ref is None or str(ref).strip() == "":
            recomputed.append(it)   # tanpa acuan tidak ada yang bisa dihitung
            continue

        usable = mention_rows(v.get("extracted_mentions"))
        if not usable:
            recomputed.append(it)
            continue

        # ATURAN TAHUN LAHIR 20XX WAJIB 4 DIGIT — diperiksa SEBELUM similarity, karena
        # vonisnya tidak bergantung pada seberapa cocok tanggalnya: kelahiran 2000-an
        # yang tahunnya disebut 2 digit adalah kegagalan verifikasi, titik. Ditegakkan
        # di sini (21 Agustus 2026) karena sebelumnya aturan ini hanya ada di prompt,
        # dan hitung-ulang similarity di bawah justru MENIMPA vonis LLM: "06 06 05"
        # untuk kelahiran 2005 dinormalkan menjadi 06062005 -> 100% -> MATCH.
        if field == "tanggal_lahir":
            yy = four_digit_year_required(v.get("reference_value"), usable)
            if yy is not None:
                recomputed.append({
                    **v,
                    "match": "MISMATCH",
                    "similarity_percent": 0,
                    "year_digits_violation": True,
                    "reason": (f"Tahun lahir 20XX wajib disebutkan 4 digit (YYYY); "
                               f"nasabah hanya menyebut {yy}."),
                })
                dirty.add(idx)
                continue

        cands = [r["value"] for r in usable]
        # Nilai pilihan LLM ikut jadi kandidat karena ia sudah DIBERSIHKAN (mis. gelar
        # "Hajah" dibuang, lead-in phrase dipangkas) sedangkan penyebutan mentah belum:
        # pada satu tiket acuan "AMINAH" cocok 100% dengan nilai LLM "Aminah" tapi
        # hanya 50% dengan penyebutan mentah "Hajah Aminah". Karena yang dipilih adalah
        # similarity TERTINGGI, ikut sertanya nilai LLM tidak pernah memperburuk hasil.
        # Ditaruh PALING BELAKANG supaya menang saat seri (aturan seri: paling baru).
        chosen = v.get("extracted_value")
        if chosen not in (None, "") and chosen not in cands:
            cands = [*cands, chosen]
        best = best_static_match(field, ref, cands)
        if best is None:
            recomputed.append(it)
            continue
        value, score = best
        source = _source_file_of_value(value, usable)
        if (
            v.get("extracted_value") == value
            and v.get("similarity_percent") == score
            and (source is None or v.get("source_file") == source)
        ):
            recomputed.append(it)
            continue
        updated = {**v, "extracted_value": value, "similarity_percent": score}
        if source is not None:
            updated["source_file"] = source
        recomputed.append(updated)
        dirty.add(idx)
    if dirty:
        evaluation = {**evaluation, "card_holder_verification": recomputed}
        items = recomputed

    # --- 3: ambang zona abu-abu ------------------------------------------------
    new_items = []
    restore: set = set()
    for idx, it in enumerate(items):
        v = it or {}
        rule = bands.get(v.get("field"))
        sim = v.get("similarity_percent")
        match = v.get("match")
        if (
            rule is None
            or match not in ("MATCH", "MISMATCH")
            or isinstance(sim, bool)
            or not isinstance(sim, (int, float))
            # ATURAN LAMA — gugur TAHAP 1: ``similarity_percent``-nya kemiripan
            # ANTAR-PENYEBUTAN, bukan terhadap Ascend, jadi membacanya sebagai nilai
            # band akan "menyelamatkan" tiket yang justru gagal karena jawabannya
            # berubah-ubah. Bendera ``consistency_failed`` (prompt v52) dibaca lewat
            # ``_is_static_consistency_failure``; cek teks dipertahankan untuk hasil
            # lama yang belum punya bendera itu. Pada aturan baru tidak ada lagi
            # baris seperti ini — prompt v56 tidak menerbitkan keduanya.
            or (not v2 and (_is_static_consistency_failure(v)
                            or _reason_says_inconsistent(v.get("reason"))))
            # Gugur ATURAN TAHUN LAHIR 20XX: vonisnya sudah final (MISMATCH), dan
            # angkanya bukan similarity terhadap Ascend. Jangan diangkat oleh ambang,
            # dan jangan meminta dokumen KTP — ini kesalahan agent.
            or v.get("year_digits_violation") is True
        ):
            new_items.append(it)
            continue
        want = "MATCH" if sim >= rule["doc_min"] else "MISMATCH"
        if want == match:
            new_items.append(it)
            continue
        new_items.append({**v, "match": want})
        dirty.add(idx)
        if want == "MATCH":
            code = CARD_HOLDER_STATIC_SCORECARD.get(v.get("field"))
            if code:
                restore.add(code)
    if not dirty:
        return evaluation

    # --- 4: reason ditulis ulang dari angka final -------------------------------
    final = []
    for idx, it in enumerate(new_items):
        v = it or {}
        rule = bands.get(v.get("field"))
        sim = v.get("similarity_percent")
        if (
            idx not in dirty
            or rule is None
            or isinstance(sim, bool)
            or not isinstance(sim, (int, float))
        ):
            final.append(it)
            continue
        if v.get("year_digits_violation") is True:
            final.append(it)     # kalimatnya sudah ditulis saat vonis dijatuhkan
            continue
        final.append({**v, "reason": _static_band_reason(v, rule)})
    result = {**evaluation, "card_holder_verification": final}
    # Item scorecard DAN irisan kritikalnya dipulihkan bersama: keduanya menyatakan
    # kegagalan yang sama, jadi memisahkannya membuat tiket berbunyi MATCH + SESUAI
    # sementara penalti kritikalnya tetap utuh.
    result = _restore_scorecard_items(result, restore)
    return _restore_critical_items(result, restore)


def _dynamic_side_missing(v: dict) -> bool:
    """Apakah salah satu SISI pembanding baris verifikasi dinamis ini kosong.

    Dua sisinya: ``reference_value`` (isian Ascend) dan ``extracted_value`` (nilai
    yang berhasil ditarik dari transkrip). Kosong = None atau string yang hanya
    berisi spasi. ``extracted_mentions`` SENGAJA tidak ikut menyelamatkan baris:
    ucapan seperti "Masih sama." atau "Nggak ada sih Mbak." memang terekam sebagai
    penyebutan, tetapi tidak menghasilkan nilai yang bisa diadu ke Ascend — dan
    justru barisnya itulah yang selama ini menjadi B17 tanpa dasar pembanding.
    """
    ref = str((v or {}).get("reference_value") or "").strip()
    ext = str((v or {}).get("extracted_value") or "").strip()
    return not ref or not ext


def _dynamic_skipped_reason(v: dict) -> str:
    """Kalimat pengganti untuk baris dinamis yang diturunkan menjadi SKIPPED_NULL.

    Ditulis dari sisi MANA yang kosong, karena kalimat asli LLM menjelaskan vonis
    LAMA ("...sehingga isian Ascend tidak terverifikasi") dan akan berbunyi seperti
    kesalahan agent di sebelah kolom Match yang sudah berbunyi SKIPPED_NULL.
    """
    ref = str((v or {}).get("reference_value") or "").strip()
    ext = str((v or {}).get("extracted_value") or "").strip()
    label = titleize_field((v or {}).get("field"))
    if not ref and not ext:
        return f"{label} tidak ada acuan pada Ascend maupun transkrip, tidak dinilai."
    if not ref:
        return f"{label} tidak ada acuannya pada Ascend, tidak ada yang bisa dibandingkan."
    return (
        f"{label} tidak menghasilkan nilai dari transkrip, tidak ada yang bisa "
        f"dibandingkan dengan isian Ascend."
    )


def normalize_dynamic_verification(evaluation: dict) -> dict:
    """Baris verifikasi DINAMIS yang salah satu sisinya kosong menjadi ``SKIPPED_NULL``.

    Aturan (31 Agustus 2026, atas permintaan Bank Mega): untuk 9 field dinamis
    ``CARD_HOLDER_DYNAMIC_FIELDS``, bila **isian Ascend ATAU nilai dari transkrip
    kosong** maka barisnya BUKAN MISMATCH melainkan ``SKIPPED_NULL`` — tidak ada
    dua sisi untuk dibandingkan, jadi tidak ada dasar untuk menyalahkan agent.

    Sebelumnya LLM hanya menulis ``SKIPPED_NULL`` ketika KEDUA sisi kosong; satu
    sisi kosong (paling sering ``no_telpon_kantor`` dan ``nama_keluarga_relasi``:
    ada di Ascend, tidak pernah disebut di panggilan) menjadi MISMATCH dan
    menerbitkan B17. Pada korpus 98 hasil saat aturan ini dibuat, 146 baris di 86
    tiket berada dalam keadaan itu.

    Yang IKUT berubah: baris tersebut hilang dari tabel Error Code (``build_error_code_table``
    hanya menerbitkan B17 untuk ``match == "MISMATCH"``), dari Agent Error Summary,
    dan dari rincian pengurangan di XLSX.

    Yang TIDAK berubah — dan memang tidak boleh:

    * **Skor.** Field dinamis tidak lagi memotong ``ai_score_verification`` sejak v33
      (``_card_holder_address_group_score`` selalu 0.0), dan aturan 2-match KB_CL_24
      menghitung yang TERVERIFIKASI (``MATCH`` atau ``event_verified``) — SKIPPED_NULL
      tidak pernah termasuk, dulu maupun sekarang. Jadi SC_CL_24 dan ``ai_score_phase_2``
      persis sama; yang hilang hanya baris error tanpa dasar pembanding.
    * **``event_verified``** dibiarkan apa adanya, sehingga param tanpa acuan Ascend
      yang benar-benar ditanya+dijawab tetap dihitung oleh ``card_holder_two_match_satisfied``.
    * **Field STATIK** (``tanggal_lahir``, ``nama_ibu_kandung``) tidak disentuh sama
      sekali: keduanya wajib ditanyakan, jadi transkrip yang kosong di situ justru
      kegagalan verifikasi yang sesungguhnya. Lihat ``normalize_static_verification``.

    ``similarity_percent`` dinolkan menjadi None dan ``item_score`` menjadi 0 supaya
    barisnya sebangun dengan SKIPPED_NULL terbitan LLM (dan tidak ikut terdaftar di
    rincian pengurangan XLSX, yang menyaring ``item_score < 0``).

    Dipanggil berdampingan dengan ``normalize_static_verification``, SEBELUM banding
    & skor dihitung. Non-destruktif: mengembalikan evaluasi baru hanya bila ada yang
    berubah.
    """
    if not evaluation:
        return evaluation
    items = evaluation.get("card_holder_verification")
    if not isinstance(items, list) or not items:
        return evaluation

    new_items = []
    changed = False
    for it in items:
        v = it or {}
        if (
            v.get("field") not in CARD_HOLDER_DYNAMIC_FIELDS
            or v.get("match") == "SKIPPED_NULL"
            or not _dynamic_side_missing(v)
        ):
            new_items.append(it)
            continue
        new_items.append({
            **v,
            "match": "SKIPPED_NULL",
            "similarity_percent": None,
            "item_score": 0,
            "reason": _dynamic_skipped_reason(v),
        })
        changed = True

    if not changed:
        return evaluation
    return {**evaluation, "card_holder_verification": new_items}


def apply_static_document_status(evaluation: dict, uploaded_types=(), sla_expired: bool = False) -> dict:
    """Terapkan status dokumen pada baris verifikasi STATIK yang berada di ZONA ABU-ABU.

    Zona abu-abu bukan vonis, melainkan penangguhan: nilainya cukup dekat untuk masuk
    akal tetapi belum sama dengan Ascend, jadi bank meminta dokumen pendukung. Sampai
    21 Agustus 2026 keadaan itu ditulis ``MATCH`` pada kolom Match — QC tidak bisa
    membedakan "cocok" dari "sedang menunggu bukti", dan setelah tenggat lewat pun
    barisnya tetap ``MATCH`` sehingga skornya tidak pernah dipotong meski dokumennya
    tak pernah datang.

    Sekarang ada tiga keadaan:

    * dokumen yang diminta **sudah diunggah** -> tetap ``MATCH``, kewajibannya selesai.
      Cukup jenis dokumennya benar; hasil OCR tidak ikut menentukan, sejalan dengan
      ``stats_aggregate._missing_docs_map``.
    * belum diunggah dan **tenggat H+2 BELUM lewat** -> ``PENDING``. Skor tidak
      dipotong: tiketnya memang sedang menunggu, bukan gagal.
    * belum diunggah dan **tenggat H+2 SUDAH lewat** -> ``MISMATCH``. Dari sini
      propagasi yang sudah ada mengambil alih: ``_propagate_verification_to_scorecard``
      menurunkan SC_CL_23_1/23_2 menjadi BELUM_SESUAI dan ``_sync_critical_compliance``
      menjatuhkan item kritisnya beserta irisan penaltinya.

    ``uploaded_types`` adalah jenis dokumen yang SUDAH terunggah untuk tiket itu, dan
    ``sla_expired`` hasil ``stats_aggregate._doc_sla_expired``. Keduanya fakta tingkat
    tiket yang hanya ada di database, jadi disuntikkan pemanggil — fungsi ini sendiri
    tidak menyentuh DB.

    Dipanggil SESUDAH ``normalize_static_verification`` (yang menetapkan band) dan
    SEBELUM applier banding + propagasi. Non-destruktif."""
    if not evaluation:
        return evaluation
    items = evaluation.get("card_holder_verification")
    if not isinstance(items, list) or not items:
        return evaluation
    from compliance.documents import (
        DOCUMENT_TYPES,
        card_holder_doc_bands,
        in_document_band,
    )

    bands = card_holder_doc_bands(evaluation)
    have = {str(t).strip() for t in (uploaded_types or ()) if str(t or "").strip()}
    out = []
    changed = False
    for it in items:
        v = it if isinstance(it, dict) else {}
        rule = bands.get(v.get("field"))
        # ``in_document_band`` memikul SELURUH keputusan "perlu dokumen atau tidak",
        # termasuk pembebasan CAKUPAN TOKEN (nama Ascend yang tercakup penuh di
        # ucapan nasabah). Harus persis predikat yang dipakai
        # ``card_holder_doc_requirements`` — bila keduanya berbeda, sebuah baris bisa
        # dibebaskan dari permintaan dokumen tetapi tetap jatuh ke MISMATCH di sini.
        if rule is None or v.get("match") != "MATCH" or not in_document_band(v, rule, evaluation):
            out.append(it)
            continue
        doc_type = rule["doc_type"]
        if doc_type in have:
            out.append(it)          # dokumennya sudah ada -> tetap MATCH
            continue
        if not sla_expired:
            out.append({**v, "match": "PENDING"})
            changed = True
            continue
        label = DOCUMENT_TYPES.get(doc_type, {}).get("label", str(doc_type).upper())
        base = str(v.get("reason") or "").strip()
        base = base[:-1] if base.endswith(".") else base
        out.append({
            **v,
            "match": "MISMATCH",
            "reason": (f"{base}; dokumen {label} tidak diunggah sampai tenggat H+2 "
                       f"sehingga verifikasi tidak terbukti." if base else
                       f"Dokumen {label} tidak diunggah sampai tenggat H+2 sehingga "
                       f"verifikasi tidak terbukti."),
        })
        changed = True
    return {**evaluation, "card_holder_verification": out} if changed else evaluation


def apply_cashline_document_status(evaluation: dict, uploaded_types=(), sla_expired: bool = False) -> dict:
    """Tangguhkan baris CASHLINE yang meminta dokumen pendukung selama tenggat H+2.

    Cerminan ``apply_static_document_status`` untuk sisi cashline. Satu-satunya field
    yang meminta dokumen adalah ``nama_pemilik_rekening`` (cover buku tabungan) —
    lihat ``compliance.documents._CASHLINE_DOC_FIELDS``.

    Tanpa penangguhan ini, aturan 31 Agustus 2026 (MISMATCH cashline menjatuhkan item
    scorecard) akan MEMOTONG JALUR DOKUMEN: bank meminta cover buku tabungan justru
    untuk membuktikan nama pemiliknya, tetapi tiketnya sudah Not Qualified sebelum
    dokumen itu datang. Pada korpus 98 tiket ada 15 tiket dalam keadaan itu, seluruhnya
    menunggu dokumen untuk ``nama_pemilik_rekening``.

    Tiga keadaan, sama persis dengan sisi statik:

    * dokumen yang diminta SUDAH diunggah -> baris dikembalikan ke ``MATCH``,
      kewajibannya selesai;
    * belum diunggah dan tenggat H+2 BELUM lewat -> ``PENDING`` (tidak memotong skor,
      tidak memveto status);
    * belum diunggah dan tenggat SUDAH lewat -> tetap ``MISMATCH``, dan propagasi
      menjatuhkan item scorecard-nya seperti biasa.

    Non-destruktif. Dipanggil SESUDAH ``apply_static_document_status`` dan SEBELUM
    ``_propagate_cashline_to_scorecard``.
    """
    if not evaluation:
        return evaluation
    items = evaluation.get("cashline_data_verification")
    if not isinstance(items, list) or not items:
        return evaluation
    from compliance.documents import DOCUMENT_TYPES, _CASHLINE_DOC_FIELDS

    have = {str(t).strip() for t in (uploaded_types or ()) if str(t or "").strip()}
    out, changed = [], False
    for it in items:
        v = it if isinstance(it, dict) else {}
        rule = _CASHLINE_DOC_FIELDS.get(v.get("field"))
        if rule is None or v.get("match") != "MISMATCH":
            out.append(it)
            continue
        if rule["doc_type"] in have:
            out.append({**v, "match": "MATCH"})
            changed = True
            continue
        if sla_expired:
            out.append(it)          # tenggat lewat -> tetap MISMATCH
            continue
        label = DOCUMENT_TYPES.get(rule["doc_type"], {}).get(
            "label", str(rule["doc_type"]).upper())
        base = str(v.get("reason") or "").strip()
        base = base[:-1] if base.endswith(".") else base
        out.append({
            **v,
            "match": "PENDING",
            "reason": (f"{base}; menunggu dokumen {label} sampai tenggat H+2."
                       if base else
                       f"Menunggu dokumen {label} sampai tenggat H+2."),
        })
        changed = True
    return {**evaluation, "cashline_data_verification": out} if changed else evaluation


def apply_approved_card_holder_appeals(evaluation: dict, approved_appeals: list) -> dict:
    """Apply approved Card Holder Verification (B17) banding to ``evaluation``.

    Beyond flipping the ``card_holder_verification`` field to MATCH, an approved STATIC
    banding also restores its 1:1-linked scorecard item (tanggal_lahir -> SC_CL_23_1,
    nama_ibu_kandung -> SC_CL_23_2). The static field's score lives entirely on that
    scorecard item (0 penalty on the card_holder row, v33), which also drives its
    Critical Compliance check — so without this restore, removing the only surfaced B17
    row would leave the score, scorecard and critical check unchanged. A DYNAMIC banding
    that lifts the KB_CL_24 verified count back to >= 2 restores the grouped SC_CL_24."""
    result = _apply_verification_appeals(
        evaluation, approved_appeals, "card_holder_verification", lambda ec: ec == "B17",
        group_fields=CARD_HOLDER_DYNAMIC_FIELDS,
        group_score_fn=_card_holder_address_group_score,
    )
    # STATIC: restore the 1:1 linked scorecard item(s) (critical check follows in
    # apply_approved_critical_compliance_appeals, which also honours these codes).
    result = _restore_scorecard_items(result, _card_holder_static_restore_codes(approved_appeals))
    # DYNAMIC: once an approved dynamic banding pushes the verified count to >= 2, the
    # <2-match SC_CL_24 propagation no longer holds, so restore that grouped item too.
    if _has_dynamic_card_holder_appeal(approved_appeals) and card_holder_two_match_satisfied(
        result.get("card_holder_verification")
    ):
        result = _restore_scorecard_items(result, {CARD_HOLDER_DYNAMIC_SCORECARD})
    return result


def apply_approved_cashline_appeals(evaluation: dict, approved_appeals: list) -> dict:
    """Apply approved Cashline Data Verification (B02/B03/B05) banding to ``evaluation``."""
    return _apply_verification_appeals(
        evaluation, approved_appeals, "cashline_data_verification", is_cashline_code
    )


def apply_approved_critical_compliance_appeals(evaluation: dict, approved_appeals: list) -> dict:
    """Return a copy of ``evaluation`` with approved appeals applied to the
    Critical Compliance Check.

    All four critical items (``SC_CL_4``/``SC_CL_23_1``/``SC_CL_23_2``/``SC_CL_37``) are
    ordinary scorecard rows, so the SAME approved scorecard appeal that flips a scorecard
    item to SESUAI also resolves its critical check (matched by ``item_code``). Card-holder
    static failures now live on ``SC_CL_23_1``/``SC_CL_23_2`` (via VERIFICATION -> SCORECARD
    PROPAGATION); to re-pass one, QC appeals that scorecard item directly (an approved B17
    does not auto-restore it). A still-``FAIL`` resolved entry becomes ``PASS``; overall
    ``status`` is ``PASS`` only when all pass.

    ``ai_score_critical_compliance_check`` is a frozen additive penalty
    (``-(maximum_score/4)`` per FAILing item). Each flipped item returns exactly
    one slice, computed from the frozen value to avoid ``maximum_score`` ambiguity:
    ``new = frozen * remaining_fail / orig_fail``. Non-destructive."""
    if not evaluation or not approved_appeals:
        return evaluation
    ccc = evaluation.get("critical_compliance_check")
    if not isinstance(ccc, dict):
        return evaluation
    items = ccc.get("checked_items")
    if not items:
        return evaluation

    approved_codes = {
        _appeal_attr(a, "item_code")
        for a in approved_appeals
        if _appeal_attr(a, "item_code")
    }
    # Card Holder STATIC bandings resolve their critical check via the linked scorecard
    # item (SC_CL_23_1/SC_CL_23_2), even though the banding's own item_code is the field
    # LABEL ("Tanggal Lahir") rather than the SC_CL code. Mirrors the scorecard restore
    # in apply_approved_card_holder_appeals so a removed B17 lifts the critical slice too.
    approved_codes |= _card_holder_static_restore_codes(approved_appeals)
    # All four critical items are ordinary scorecard rows (SC_CL_4/23_1/23_2/37), so an
    # approved scorecard appeal on that item_code resolves its critical slice. Mekanik
    # flip + pengembalian irisan penaltinya dipakai bersama ``normalize_static_verification``
    # lewat ``_restore_critical_items`` — satu salinan logika, satu perilaku.
    return _restore_critical_items(evaluation, approved_codes)

# ---------------------------------------------------------------------------
# QC "add error code" bandings — the INVERSE of the remove/verification appliers
# above. An approved add attaches a NEW error to an existing evaluation item/field
# and LOWERS the score. Source 'others' has no hook and is display-only (handled by
# inject_added_rows, not here). Applied non-destructively at read time.
# ---------------------------------------------------------------------------
def _newest_appeal(appeals: list):
    """The most recently submitted appeal (by ``requested_at``, then ``id``)."""
    def key(a):
        return (_appeal_attr(a, "requested_at") or datetime.min, _appeal_attr(a, "id") or 0)
    return max(appeals, key=key)


def apply_added_scorecard_appeals(evaluation: dict, added_appeals: list) -> dict:
    """Flip the targeted scorecard item ``SESUAI`` -> ``BELUM_SESUAI`` for each
    approved scorecard ``add`` banding (keyed by ``item_code``), so its weight is
    deducted from the scorecard score.

    The item's ``reason`` / ``evidence`` / ticket are replaced with what the QC
    submitted on the NEWEST approved add for that item — the AI values describe why
    the item PASSED, which is misleading once the row is an error. Non-destructive."""
    if not evaluation or not added_appeals:
        return evaluation
    # item_code -> every approved scorecard add targeting it (an item may be added
    # more than once with different codes); the newest submission wins.
    by_item: dict = {}
    for a in added_appeals:
        if _appeal_attr(a, "add_source") != SOURCE_SCORECARD:
            continue
        item_code = _appeal_attr(a, "item_code")
        if item_code:
            by_item.setdefault(item_code, []).append(a)
    if not by_item:
        return evaluation

    new_items = []
    changed = False
    for item in evaluation.get("scorecard_result") or []:
        candidates = by_item.get((item or {}).get("item_code"))
        if not candidates or (item or {}).get("status") != "SESUAI":
            new_items.append(item)
            continue
        appeal = _newest_appeal(candidates)
        updated = {**item, "status": "BELUM_SESUAI", "item_score": 0}
        qc_reason = _appeal_attr(appeal, "qc_reason")
        if qc_reason:
            updated["reason"] = qc_reason
        ts, quote = _split_qc_evidence(_appeal_attr(appeal, "qc_evidence"))
        updated["evidence"] = {
            "timestamp": ts,
            "quote": quote,
            "ticket_id": _appeal_attr(appeal, "qc_ticket_id") or "",
        }
        new_items.append(updated)
        changed = True
    if not changed:
        return evaluation
    return {**evaluation, "scorecard_result": new_items}


def _apply_added_verification_appeals(
    evaluation: dict,
    added_appeals: list,
    source: str,
    eval_key: str,
    penalty_map: dict,
    group_fields: tuple = (),
    group_score_fn=None,
) -> dict:
    """Flip a currently-``MATCH`` verification field to ``MISMATCH`` for each approved
    ``add`` banding of ``source`` (keyed by titleized field == ``item_code``), applying
    the per-field penalty (``penalty_map``) to ``ai_score_verification`` and filling
    reference/extracted from the QC submission. Group-scored fields (dynamic card
    holder) settle via ``group_score_fn`` delta instead of a fixed per-field penalty.
    The mirror image of ``_apply_verification_appeals``. Non-destructive."""
    if not evaluation or not added_appeals:
        return evaluation
    by_field = {}
    for a in added_appeals:
        if _appeal_attr(a, "add_source") != source:
            continue
        item_code = _appeal_attr(a, "item_code")
        if item_code:
            by_field[item_code] = a
    if not by_field:
        return evaluation

    old_items = evaluation.get(eval_key) or []
    new_items = []
    delta = 0.0
    changed = False
    for v in old_items:
        appeal = by_field.get(titleize_field((v or {}).get("field")))
        if appeal is None or (v or {}).get("match") != "MATCH":
            new_items.append(v)
            continue
        field = (v or {}).get("field")
        if field in group_fields:
            # Dynamic card-holder fields no longer carry a per-field deduction; the
            # failure is expressed via SC_CL_24 BELUM_SESUAI in the scorecard.
            item_score = 0
        else:
            penalty = penalty_map.get(field, 0)
            item_score = -penalty
            delta += -penalty
        updated = {**v, "match": "MISMATCH", "item_score": item_score, "similarity_percent": 0}
        ref = _appeal_attr(appeal, "qc_reference_value")
        ext = _appeal_attr(appeal, "qc_extracted_value")
        qc_reason = _appeal_attr(appeal, "qc_reason")
        if ref is not None:
            updated["reference_value"] = ref
        if ext is not None:
            updated["extracted_value"] = ext
        if qc_reason:
            updated["reason"] = qc_reason
        new_items.append(updated)
        changed = True

    if not changed:
        return evaluation
    if group_score_fn is not None:
        delta += group_score_fn(new_items) - group_score_fn(old_items)
    result = {**evaluation, eval_key: new_items}
    verif = evaluation.get("ai_score_verification")
    if delta and verif is not None:
        try:
            val = float(verif) + delta  # delta is negative — the score drops
            result["ai_score_verification"] = int(val) if val == int(val) else val
        except (TypeError, ValueError):
            pass
    return result


def apply_added_score_appeals(evaluation: dict, added_appeals: list) -> dict:
    """Apply ALL score-affecting ``add`` bandings (scorecard + both verifications +
    critical) in one call, THEN derive the scorecard/critical items that follow from
    the resulting verification state. ``others`` adds carry no score effect (display
    only). Use this in every read-time assembly path (detail view, Results list, XLSX,
    aggregates) so the score/AI-status stays consistent everywhere.

    VERIFICATION -> SCORECARD PROPAGATION berjalan SELALU, ada banding ``add`` atau
    tidak. Dulu ia ikut terjaga di balik ``if not added_appeals: return`` sehingga —
    pada mayoritas tiket, yang memang tidak punya banding — sebuah field statik
    MISMATCH tidak pernah menurunkan SC_CL_23_1/23_2. Akibatnya tiket 020338gGlU
    menampilkan B17 "Nama Ibu Kandung MISMATCH" di tabel Error Code sementara
    SC_CL_23_2-nya tetap SESUAI 15 poin dan AI Status-nya tetap PASS: error yang
    terlihat tapi tidak pernah dipotong. Propagasi ini turunan murni dari keadaan
    verifikasi, jadi tempatnya memang di luar cabang banding."""
    if not evaluation:
        return evaluation
    if added_appeals:
        evaluation = apply_added_scorecard_appeals(evaluation, added_appeals)
        evaluation = apply_added_card_holder_appeals(evaluation, added_appeals)
        evaluation = apply_added_cashline_appeals(evaluation, added_appeals)
    # Jalankan SEBELUM applier kritis: sebuah field statik yang MISMATCH (entah dari
    # evaluasi LLM, dari hitung ulang Python, atau dari banding 'add') menurunkan
    # SC_CL_23_1/23_2, dan item kritis yang baru turun itu harus ikut FAIL.
    evaluation = _propagate_verification_to_scorecard(evaluation)
    # Cashline menyusul dengan aturan yang sama: MISMATCH menjatuhkan item scorecard
    # pasangannya, dan potongan per-field-nya dilepas supaya tidak dihitung dua kali.
    # HARUS sebelum applier kritis, sama seperti baris di atas — item kritis yang baru
    # turun karenanya (mis. SC_CL_37 lewat banding) ikut FAIL.
    evaluation = _propagate_cashline_to_scorecard(evaluation)
    evaluation = _sync_critical_compliance(evaluation, added_appeals)
    return evaluation


def apply_added_cashline_appeals(evaluation: dict, added_appeals: list) -> dict:
    """Apply approved Cashline Data Verification ``add`` bandings."""
    return _apply_added_verification_appeals(
        evaluation, added_appeals, SOURCE_CASHLINE, "cashline_data_verification",
        CASHLINE_FIELD_PENALTY,
    )


def apply_added_card_holder_appeals(evaluation: dict, added_appeals: list) -> dict:
    """Apply approved Card Holder Verification ``add`` bandings (dynamic fields tunduk
    the 2-match group rule)."""
    return _apply_added_verification_appeals(
        evaluation, added_appeals, SOURCE_CARD_HOLDER, "card_holder_verification",
        CARD_HOLDER_STATIC_PENALTY,
        group_fields=CARD_HOLDER_DYNAMIC_FIELDS,
        group_score_fn=_card_holder_address_group_score,
    )


# Item scorecard yang kegagalannya memicu SCORE BOMB: satu iris penuh
# ``-(maximum_score / 4)`` pada ``ai_score_critical_compliance_check``.
#
# ``SC_CL_24`` (verifikasi dinamis minimal 2 data) DITAMBAHKAN 31 Agustus 2026 atas
# permintaan bisnis. Sebelumnya hanya verifikasi STATIK yang mengebom skor
# (SC_CL_23_1/23_2), sementara kegagalan verifikasi DINAMIS cuma memotong bobot
# itemnya sendiri — 15 dari 150. Akibatnya tiket yang verifikasi dinamisnya gagal
# total masih berskor 135/150, terbaca "nyaris sempurna" padahal bank menganggapnya
# kegagalan verifikasi. Vonisnya sendiri memang sudah Not Qualified lewat veto
# non-tolerable, jadi yang diperbaiki aturan ini adalah ANGKANYA, bukan statusnya.
CRITICAL_ITEM_CODES = ("SC_CL_4", "SC_CL_23_1", "SC_CL_23_2", "SC_CL_37", "SC_CL_24")


def _sync_critical_compliance(evaluation: dict, added_appeals: list) -> dict:
    """Selaraskan ``critical_compliance_check`` dengan scorecard: setiap item kritis
    (``CRITICAL_ITEM_CODES``) yang BELUM_SESUAI harus berentri ``FAIL``, dan tiap
    entri yang baru jatuh menambah satu iris beku (``-(maximum_score/4)``) ke
    ``ai_score_critical_compliance_check``. Non-destruktif.

    Dua sumber item kritis yang jatuh, keduanya ditangani di sini:

    1. Banding ``add`` pada item scorecard kritis — kebalikan dari pasangannya di
       ``apply_added_scorecard_appeals``.
    2. VERIFICATION -> SCORECARD PROPAGATION: field statik MISMATCH menurunkan
       SC_CL_23_1/23_2 lewat ``_propagate_verification_to_scorecard`` (jalan tepat
       sebelum fungsi ini). Sumbernya tidak dibedakan — MISMATCH hasil hitung ulang
       Python memicu score bomb yang sama dengan MISMATCH temuan LLM atau tambahan QC.

    Sebelumnya fungsi ini ikut terjaga di balik ``if not added_appeals: return``,
    padahal sebab (2) tidak ada hubungannya dengan banding. Efeknya tiket tanpa
    banding bisa berakhir tidak konsisten: scorecard BELUM_SESUAI tetapi entri
    kritisnya PASS. Karena itu ia kini jalan setiap kali ada item kritis yang
    BELUM_SESUAI; bila tidak ada yang perlu dibalik, fungsi ini no-op."""
    if not evaluation:
        return evaluation
    ccc = evaluation.get("critical_compliance_check")
    if not isinstance(ccc, dict):
        return evaluation
    items = ccc.get("checked_items")
    if not items:
        return evaluation
    added_codes = {
        _appeal_attr(a, "item_code")
        for a in (added_appeals or [])
        if _appeal_attr(a, "add_source") == SOURCE_SCORECARD and _appeal_attr(a, "item_code")
    }
    belum_critical = {
        (it or {}).get("item_code")
        for it in (evaluation.get("scorecard_result") or [])
        if (it or {}).get("status") == "BELUM_SESUAI"
        and (it or {}).get("item_code") in CRITICAL_ITEM_CODES
    }
    if not added_codes and not belum_critical:
        return evaluation

    orig_fail = sum(1 for it in items if (it or {}).get("status") == "FAIL")
    new_items = []
    flipped = 0
    for it in items:
        code = (it or {}).get("item_code")
        should_fail = code in added_codes or code in belum_critical
        if (it or {}).get("status") == "PASS" and should_fail:
            new_items.append({**it, "status": "FAIL"})
            flipped += 1
        else:
            new_items.append(it)

    # Item kritis yang BELUM PUNYA entri di ``checked_items`` ditambahkan di sini.
    # Perlu karena ``SC_CL_24`` baru menjadi item kritis pada 31 Agustus 2026: seluruh
    # hasil evaluasi yang sudah ada — dan hasil dari prompt yang belum diperbarui —
    # hanya memuat empat entri lama, sehingga tanpa penambahan ini score bomb-nya tidak
    # akan pernah menyala untuk tiket mana pun. Requirement-nya disalin dari item
    # scorecard yang bersangkutan supaya panel Critical Compliance menyebut kewajiban
    # yang sama dengan scorecard, bukan teks buatan sendiri.
    present = {(it or {}).get("item_code") for it in items}
    for code in CRITICAL_ITEM_CODES:
        if code in present or code not in (added_codes | belum_critical):
            continue
        src = next(
            (it for it in (evaluation.get("scorecard_result") or [])
             if (it or {}).get("item_code") == code),
            {},
        )
        new_items.append({
            "item_code": code,
            "status": "FAIL",
            "requirement": src.get("requirement") or code,
        })
        flipped += 1

    if flipped == 0:
        return evaluation

    new_status = "PASS" if all((it or {}).get("status") == "PASS" for it in new_items) else "FAIL"
    result = {
        **evaluation,
        "critical_compliance_check": {**ccc, "status": new_status, "checked_items": new_items},
    }
    frozen = evaluation.get("ai_score_critical_compliance_check")
    # Per-item slice = frozen/orig_fail when some already fail; otherwise derive from
    # maximum_score/4 (the definition of the frozen penalty).
    try:
        if orig_fail > 0 and frozen is not None:
            per_item = float(frozen) / orig_fail
        else:
            ms = _to_maximum_score(evaluation)
            per_item = -(ms / 4) if ms is not None else None
        if per_item is not None:
            val = per_item * (orig_fail + flipped)
            result["ai_score_critical_compliance_check"] = int(val) if val == int(val) else val
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return result


def _to_maximum_score(evaluation: dict):
    """maximum_score for the critical slice, from the evaluation field (numeric)."""
    try:
        return float(evaluation.get("maximum_score"))
    except (TypeError, ValueError):
        return None


def inject_added_rows(table: list, visible_add_appeals: list) -> list:
    """Add display rows for ``add`` bandings (pending + approved) into the Error Code
    table, keyed by the master ``error_code`` so ``_annotate_appeals`` can attach their
    review state. For approved SCORECARD adds the structural flip also produces a
    derived B10/B12/B18 row for the same ``item_code`` — drop it so the master code is
    shown once. ``others`` adds are pure display (no structural effect)."""
    if not visible_add_appeals:
        return table
    # An APPROVED add on a structured source flips the item/field, so the builder
    # already emits a derived row for it (B10/B12/B18 scorecard, B17 card holder,
    # B02/B03/B05 cashline). Drop that row so the QC's master code shows once, not
    # twice. Only approved adds are de-duplicated: a pending add has not flipped
    # anything, so any row with the same key is a genuine AI error and must stay.
    # 'others' never collides (it has no structural row at all).
    _STRUCTURED = (SOURCE_SCORECARD, SOURCE_CASHLINE, SOURCE_CARD_HOLDER)
    replaced = {
        (SOURCE_LABELS.get(_appeal_attr(a, "add_source")), _appeal_attr(a, "item_code") or "")
        for a in visible_add_appeals
        if _appeal_attr(a, "add_source") in _STRUCTURED
        and (_appeal_attr(a, "item_code") or "")
        and effective_appeal_status(a) == "approved"
    }
    out = []
    for row in table:
        if replaced and (row.get("sumber"), row.get("item_code") or "") in replaced:
            continue  # replaced by the master-coded added row below
        out.append(row)

    for a in visible_add_appeals:
        code = _appeal_attr(a, "error_code") or ""
        source = _appeal_attr(a, "add_source") or SOURCE_OTHERS
        meta = _reason_meta(code) or {}
        ev_ts, ev_quote = _split_qc_evidence(_appeal_attr(a, "qc_evidence"))
        out.append({
            "sumber": SOURCE_LABELS.get(source, SOURCE_LABELS[SOURCE_OTHERS]),
            "error_code": code,
            # QC-edited override wins over the master-catalog default.
            "risk_base": _appeal_attr(a, "qc_risk_base") or meta.get("risk_base") or "",
            "item_code": _appeal_attr(a, "item_code") or "",
            "details_error": meta.get("details") or "",
            "reason": _appeal_attr(a, "qc_reason") or "",
            "evidence": _appeal_attr(a, "qc_evidence") or "",
            # Split so the QC Manual Check modal can prefill the two inputs separately.
            "timestamp": ev_ts,
            "evidence_quote": ev_quote,
            "ticket_id": _appeal_attr(a, "qc_ticket_id") or "",
            "reference_value": _appeal_attr(a, "qc_reference_value"),
            "extracted_value": _appeal_attr(a, "qc_extracted_value"),
        })
    return out
