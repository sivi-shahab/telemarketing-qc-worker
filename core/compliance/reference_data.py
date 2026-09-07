"""Build the CASHLINE + CARD HOLDER reference-data block appended to the prompt.

Sumber reference data sekarang dari DWH API Aplikasi A (API/V1 :8000) lewat
``db.crud`` -> ``services.data_dwh``, bukan lagi dari CSV/DB. Satu call API
per ``customer_id`` mengembalikan cashline + customer sekaligus:
  - CASHLINE   : ``dashboard.campaign_cashline_ntb``, dicocokkan API by ``result_id``.
  - CARD HOLDER: ``dashboard.current_cc_scmcustp``, dicocokkan API by ``no-ktpkitas``
    (dari cashline) — bukan lagi by ``CUST_LOCAL_NAME`` == ``cust_name``.
Skema kolom sama dengan tabel lama (tms_cashline / ascend_custp), sehingga peta
field di bawah tidak berubah.
Fields built from several columns are joined left->right with a single space.
"""
import json

from sqlalchemy.orm import Session

from compliance.pdf_parser import ticket_id_from_filename
from compliance.riplay import build_tnc_product_reference
from db import crud

# --- Reference data field maps ---------------------------------------------
# Single-column cashline fields: ref field -> cashline column.
CASHLINE_SINGLE_COLS = {
    "nominal_pencairan": "nominal-transfer",
    "nama_bank": "nama-bank",
    "tenor_dalam_bulan": "tenor",
    "nominal_cicilan_per_bulan": "cicilan-per-bulan",
    "biaya_admin": "admin-fee",
    "nomor_rekening": "nomor-rekening",
    "nama_pemilik_rekening": "nama-di-rekening",
    # Added in migration 0028 (previously constants in this module). An empty
    # column falls back to the campaign's RIPLAY value — see build_reference_data.
    "provisi": "provisi",
    "penalti_pelunasan_dipercepat": "penalti-pelunasan-dipercepat",
}
CASHLINE_FIELD_ORDER = [
    "nominal_pencairan", "nama_bank", "tenor_dalam_bulan",
    "nominal_cicilan_per_bulan", "biaya_admin",
    "nomor_rekening", "nama_pemilik_rekening",
    "provisi", "penalti_pelunasan_dipercepat",
]
# Customer identity fields for the dashboard Results table (Sales Agent role).
# customer_name -> cashline `cust_name`; account_number (ditampilkan sebagai
# "Nomor Kartu") -> customer `CUST_CR_CARD1` (lihat get_customer_info).
CUSTOMER_INFO_COLS = {
    "customer_name": "cust_name",
}
# Single-column card holder fields: ref field -> customer column.
CARDHOLDER_SINGLE_COLS = {
    "tanggal_lahir": "CUST_DTE_BIRTH",
    "nama_ibu_kandung": "CUST_MOM_NAME",
    "no_telpon_terdaftar": "CUST_MOBILE_PHONE",   # VD_3 (registered/mobile phone)
    "no_telpon_kantor": "CUST_EMP_PHONE",         # VD_5 (office phone)
    "alamat_email_terdaftar": "CUST_EMAIL_ADDR",
    "nama_keluarga_relasi": "CUST_GLOCAL_NAME",   # VD_9 (guarantor/relation name)
}
# Multi-column card holder fields (joined left->right). Office & home addresses use
# the FULL Ascend address (street lines + city/province/zip) so the matcher has the
# complete reference to test the agent's spoken address against.
#
# Sejak prompt v69 (28 Agustus 2026) pencocokannya TOKEN COVERAGE, bukan lagi
# substring/Levenshtein: prompt memecah kedua sisi jadi token, menyamakan singkatan
# (jl/jalan, no/nomor, nol di depan RT/RW), MEMBUANG token kode pos 5 digit, lalu
# menghitung berapa persen token acuan yang benar-benar dibacakan. Konsekuensinya
# kode pos di ekor join ini tidak lagi ikut dinilai — dibiarkan ada supaya blok
# reference tetap menampilkan alamat utuh untuk dibaca manusia.
CARDHOLDER_DOB_COLS = ["CUST_DTE_BIRTH"]
CARDHOLDER_OFFICE_COLS = [
    "CUST_EMP_NAME",
    "CUST_EMP_ADDR1", "CUST_EMP_ADDR2", "CUST_EMP_ADDR3", "CUST_EMP_ADDR4",
    "CUST_EMP_CITY", "CUST_EMP_ZIP",
]
CARDHOLDER_HOME_COLS = [
    "CUST_ADDR1", "CUST_ADDR2", "CUST_ADD_CITY", "CUST_ADD_PROVINCE", "CUST_ADD_ZIPCODE",
]
# Billing / mailing address (VD_1 alamat_pengiriman_tagihan): concat the mailing
# address columns, same shape as office & home above.
CARDHOLDER_MAILING_COLS = [
    "CUST_MADDR1", "CUST_MADDR2", "CUST_MADDR3", "CUST_MADDR4",
    "CUST_MADD_CITY", "CUST_MADD_PROVINCE", "CUST_MADD_ZIPCODE",
]
# Card holder verification fields: 2 STATIC + 9 DYNAMIC (aligned to KB_CL_24 VD_1..VD_9).
# Wired to ascend columns:
#   alamat_pengiriman_tagihan (VD_1) <- CARDHOLDER_MAILING_COLS join (CUST_MADDR*)
#   nama_keluarga_relasi      (VD_9) <- CUST_GLOCAL_NAME (in CARDHOLDER_SINGLE_COLS)
# Still reference-less (seeded to None, "di-null-kan dulu") — no ascend source yet:
#   nama_kartu_suplement      (VD_7)
#   jumlah_kartu_suplement    (VD_8)
# The SC_CL_24 hybrid rule counts a reference-less dynamic param as verified via a
# valid ask+answer EVENT (event_verified) instead of a value MATCH — see the prompt.
CARDHOLDER_FIELD_ORDER = [
    "tanggal_lahir", "nama_ibu_kandung",
    "alamat_pengiriman_tagihan", "alamat_rumah", "no_telpon_terdaftar",
    "alamat_kantor", "no_telpon_kantor", "alamat_email_terdaftar",
    "nama_kartu_suplement", "jumlah_kartu_suplement", "nama_keluarga_relasi",
]
# Single-column campaign interest fields: ref field -> cashline column.
CAMPAIGN_INTEREST_SINGLE_COLS = {
    "mega_cashline": "jenis-kartu-yang-dikehendaki",
    "mega_ultima_shield": "pendaftaran-credit-shield",
}
CAMPAIGN_INTEREST_FIELD_ORDER = ["mega_cashline", "mega_ultima_shield"]

# --- Document OCR reference ("acuan") field maps ----------------------------
DOC_KTP_HOME_ADDR_COLS = [
    "alamat-rumah-1-new", "rumah-rt-new", "rumah-rw-new", "rumah-kelurahan-new",
    "rumah-kecamatan-new", "rumah-kabupatenkota-new", "rumah-provinsi-new",
    "rumah-kode-pos-new",
]
DOC_KTP_SINGLE_COLS = {"nama_acuan": "cust_name", "nik_acuan": "nik-new"}
DOC_NPWP_COLS = {"nomor_npwp_acuan": "no-npwp-new"}
DOC_COVER_BUKU_TABUNGAN_COLS = {
    "nama_bank_acuan": "nama-bank",
    "nama_pemilik_rekening_acuan": "nama-di-rekening",
    "nomor_rekening_acuan": "nomor-rekening",
}


def customer_id_from_filenames(sorted_filenames: list[str]) -> str:
    """Customer/session ID = prefix before the underscore of the earliest PDF."""
    ticket_id = ticket_id_from_filename(sorted_filenames[0])
    return ticket_id.rsplit("_", 1)[0]


def _clean(value) -> str:
    """Normalise a cell to a stripped string ('' for missing)."""
    return ("" if value is None else str(value)).strip()


def _mask_card_number(value) -> str | None:
    """Mask the middle 8 digits of a card number, keeping the first and last 4
    visible. Empty or a literal ``NTB`` yields ``None`` (rendered as "NTB" —
    New To Bank — in the Sales Agent Results view). Separators such as ``-`` are
    preserved; only digits are masked. Cards too short to keep 4+4 are fully
    masked so no partial PAN leaks.
    """
    s = _clean(value)
    if not s or s.upper() == "NTB":
        return None
    n = sum(c.isdigit() for c in s)
    if n <= 8:
        return "*" * len(s)
    out, di = [], 0
    for c in s:
        if c.isdigit():
            out.append("*" if 4 <= di < n - 4 else c)
            di += 1
        else:
            out.append(c)
    return "".join(out)


def _join_cols(row: dict, cols: list[str]) -> str:
    """Join columns left->right with a single space, skipping empty cells."""
    return " ".join(p for p in (_clean(row.get(c)) for c in cols) if p)


def _to_number(value) -> float | None:
    """Parse a reference-data cell into a float, or ``None`` if not numeric.
    Tolerates thousands separators (commas) so values like ``"40,000,000"`` parse.
    """
    s = _clean(value).replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def compute_bunga(cashline_ref: dict) -> str | None:
    """Compute the interest rate (bunga) from cashline reference fields.

    Formula::
        ((nominal_cicilan_per_bulan * tenor_dalam_bulan - nominal_pencairan)
         / tenor_dalam_bulan / nominal_pencairan) * 100
    rounded to 2 decimals with a ``%`` suffix (e.g. ``"2.09%"``). Returns ``None``
    if any input is missing/non-numeric or ``nominal_pencairan``/``tenor_dalam_bulan``
    is zero (avoids division by zero).
    """
    nominal_pencairan = _to_number(cashline_ref.get("nominal_pencairan"))
    tenor = _to_number(cashline_ref.get("tenor_dalam_bulan"))
    cicilan = _to_number(cashline_ref.get("nominal_cicilan_per_bulan"))
    if not nominal_pencairan or not tenor or cicilan is None:
        return None
    bunga = ((cicilan * tenor - nominal_pencairan) / tenor / nominal_pencairan) * 100
    return f"{round(bunga, 2)}%"


def build_reference_data(
    customer_id: str,
    db: Session,
    riplay_extraction: dict | None = None,
) -> tuple[str, list[str], dict]:
    """Build the CASHLINE + CARD HOLDER reference-data text block for ``customer_id``.

    Returns ``(text, warnings, raw)``. ``text`` adalah blok referensi untuk prompt
    LLM, sedangkan ``raw`` berisi baris MENTAH ``{"cashline", "customer"}`` yang
    disimpan pemanggil ke ``final_json["reference_data"]`` supaya dashboard tidak
    perlu menembak Aplikasi A lagi. Missing rows/fields produce ``null`` values
    (and a warning) rather than failing, so a submission without matching reference
    rows still evaluates (the LLM marks unmatched fields as SKIPPED_NULL).

    ``riplay_extraction`` (the campaign's stored RIPLAY) adds a TNC PRODUCT block:
    the product-level terms each cashline field must sit within. TMS stays the
    per-ticket ground truth; TnC Product only becomes the reference for a field
    whose TMS column is empty.
    """
    warnings: list[str] = []

    # --- CASHLINE: campaign API, dicocokkan by result_id ---
    cashline_ref = {field: None for field in CASHLINE_FIELD_ORDER}
    campaign_interest_ref = {field: None for field in CAMPAIGN_INTEREST_FIELD_ORDER}
    cust_name = ""
    cashline_row = crud.get_tms_cashline_by_result_id(db, customer_id)
    if cashline_row is None:
        warnings.append(f"no campaign_cashline_ntb row with result_id == '{customer_id}'")
    else:
        cust_name = _clean(cashline_row.get("cust_name"))
        for field, col in CASHLINE_SINGLE_COLS.items():
            cashline_ref[field] = _clean(cashline_row.get(col)) or None
        cashline_ref["bunga"] = compute_bunga(cashline_ref)
        for field, col in CAMPAIGN_INTEREST_SINGLE_COLS.items():
            campaign_interest_ref[field] = _clean(cashline_row.get(col)) or None

    # --- CARD HOLDER: campaign API, dicocokkan API by no-ktpkitas (dari cashline) ---
    # [FIX] Blok `elif cashline_row is not None:` di bawah punya kondisi IDENTIK
    # dengan `if` di atasnya, sehingga tidak pernah tereksekusi (dead code) dan
    # warning-nya tidak pernah muncul. Dihapus; pencocokan customer sekarang
    # memakai no-ktpkitas (bukan cust_name), jadi warning itu memang tidak relevan.
    custp_row = None
    cardholder_ref = {field: None for field in CARDHOLDER_FIELD_ORDER}
    if cashline_row is not None:
        custp_row = crud.get_ascend_custp_by_result_id(db, customer_id)
        if custp_row is None:
            warnings.append(
                f"no current_cc_scmcustp row for result_id == '{customer_id}' "
                "(matched by no-ktpkitas)"
            )
        else:
            for field, col in CARDHOLDER_SINGLE_COLS.items():
                cardholder_ref[field] = _clean(custp_row.get(col)) or None
            # cardholder_ref["tanggal_lahir"] = _join_cols(custp_row, CARDHOLDER_DOB_COLS) or None
            cardholder_ref["alamat_kantor"] = _join_cols(custp_row, CARDHOLDER_OFFICE_COLS) or None
            cardholder_ref["alamat_rumah"] = _join_cols(custp_row, CARDHOLDER_HOME_COLS) or None
            cardholder_ref["alamat_pengiriman_tagihan"] = _join_cols(custp_row, CARDHOLDER_MAILING_COLS) or None

    # Built last: the instalment row instantiates the RIPLAY formula with this
    # ticket's TMS figures, so it needs the finished cashline_ref.
    tnc_ref = build_tnc_product_reference(riplay_extraction, cashline_ref)

    text = (
        "=== CASHLINE REFERENCE DATA ===\n"
        + json.dumps(cashline_ref, ensure_ascii=False, indent=2)
        + "\n\n=== TNC PRODUCT REFERENCE DATA ===\n"
        + json.dumps(tnc_ref, ensure_ascii=False, indent=2)
        + "\n\n=== CARD HOLDER REFERENCE DATA ===\n"
        + json.dumps(cardholder_ref, ensure_ascii=False, indent=2)
        + "\n\n=== CAMPAIGN INTEREST REFERENCE DATA ===\n"
        + json.dumps(campaign_interest_ref, ensure_ascii=False, indent=2)
    )
    # [NEW] Balikkan RAW dict cashline_row/custp_row juga (bukan cuma teks utk
    # prompt) -- dipakai caller (process_transcript.py) untuk disisipkan ke
    # final_json["reference_data"], supaya ResultsView bisa baca langsung dari
    # result_json TANPA hit App A lagi tiap dashboard dibuka.
    raw = {
        "cashline": cashline_row if cashline_row is not None else None,
        "customer": custp_row if (cashline_row is not None and custp_row is not None) else None,
    }
    return text, warnings, raw


def build_document_reference(
    customer_id: str,
    doc_type: str,
    db: Session,
) -> tuple[dict, list[str]]:
    """Build the OCR reference ("acuan") dict for one document type + ``customer_id``.

    Looks up the cashline row by ``result_id`` == ``customer_id`` via the campaign
    API. Returns ``(reference, warnings)``; missing rows/fields produce ``None``
    values and a warning rather than failing.

    Acuan per document type:
      - ``npwp``: ``nomor_npwp_acuan`` <- ``no-npwp-new``
      - ``ktp``: ``nama_acuan`` <- ``cust_name``, ``nik_acuan`` <- ``nik-new``,
        ``alamat_rumah_acuan`` <- home-address columns joined with a space
      - ``cover_buku_tabungan``: ``nama_bank_acuan`` / ``nama_pemilik_rekening_acuan`` /
        ``nomor_rekening_acuan``
      - ``kk``: ``nama_ibu_kandung_acuan`` <- ``CUST_MOM_NAME`` (customer), dicocokkan
        API by ``no-ktpkitas`` dari cashline.
    """
    warnings: list[str] = []
    cashline_row = crud.get_tms_cashline_by_result_id(db, customer_id)
    if cashline_row is None:
        warnings.append(f"no cashline row with result_id == '{customer_id}'")

    if doc_type == "npwp":
        reference = {
            field: (_clean(cashline_row.get(col)) or None) if cashline_row else None
            for field, col in DOC_NPWP_COLS.items()
        }
    elif doc_type == "ktp":
        reference = {
            "alamat_rumah_acuan": (
                _join_cols(cashline_row, DOC_KTP_HOME_ADDR_COLS) or None
            )
            if cashline_row
            else None
        }
        for field, col in DOC_KTP_SINGLE_COLS.items():
            reference[field] = (_clean(cashline_row.get(col)) or None) if cashline_row else None
    elif doc_type == "cover_buku_tabungan":
        reference = {
            field: (_clean(cashline_row.get(col)) or None) if cashline_row else None
            for field, col in DOC_COVER_BUKU_TABUNGAN_COLS.items()
        }
    elif doc_type == "kk":
        cust_name = _clean(cashline_row.get("cust_name")) if cashline_row else ""
        # nama_anak = si nasabah (cust_name); customer dicari via API by no-ktpkitas
        # sehingga OCR tahu 'nama ibu kandung' siapa yang harus dibaca/diverifikasi.
        reference = {"nama_ibu_kandung_acuan": None, "nama_anak": cust_name or None}
        if cashline_row is not None:
            custp_row = crud.get_ascend_custp_by_result_id(db, customer_id)
            if custp_row is None:
                warnings.append(
                    f"no current_cc_scmcustp row for result_id == '{customer_id}' "
                    "(matched by no-ktpkitas)"
                )
            else:
                reference["nama_ibu_kandung_acuan"] = (
                    _clean(custp_row.get("CUST_MOM_NAME")) or None
                )
    else:
        warnings.append(f"unknown doc_type for reference: {doc_type}")
        reference = {}

    return reference, warnings


def get_customer_info(
    customer_id: str,
    db: Session,
) -> tuple[dict, list[str]]:
    """Fetch the customer name + card number for the Sales Agent Results view.

    ``customer_name`` dari cashline (``cust_name``, match by ``result_id``).
    ``account_number`` — di dashboard tampil sebagai **"Nomor Kartu"** — dari
    ``current_cc_scmcustp.CUST_CR_CARD1``, diambil dari bundle customer yang sama
    (dicocokkan App A by ``no-ktpkitas``). Baris/field yang hilang -> ``None`` +
    warning, tidak raise (konsisten dengan build_reference_data).
    """
    warnings: list[str] = []
    info = {"customer_name": None, "account_number": None, "credit_limit": None}
    cashline_row = crud.get_tms_cashline_by_result_id(db, customer_id)
    if cashline_row is None:
        warnings.append(f"no campaign_cashline_ntb row with result_id == '{customer_id}'")
        return info, warnings

    # [FIX] cust_name sebelumnya dipakai tanpa pernah di-assign -> NameError setiap
    # kali cashline_row ditemukan. Diambil dari cashline row, sama seperti
    # build_reference_data() dan get_credit_limit().
    info["customer_name"] = _clean(cashline_row.get("cust_name")) or None

    # Nomor Kartu: CUST_CR_CARD1 dari current_cc_scmcustp, dicocokkan App A by
    # ``no-ktpkitas`` (dari cashline) — BUKAN lagi by CUST_LOCAL_NAME.
    # credit_limit ("limit sebelumnya"): CUST_CRLIMIT dari baris customer yang sama;
    # hanya nasabah non-NTB (punya kartu) yang memilikinya.
    #
    # [FIX] Sebelumnya memanggil get_ascend_custp_by_local_name() — fungsi LEGACY
    # yang query tabel lokal ``ascend_custp``, kosong sejak migrasi ke DWH API.
    # Akibatnya account_number & credit_limit selalu None (kolom Nomor Kartu kosong,
    # trigger NPWP limit >= 50jt tidak pernah aktif).
    custp_row = crud.get_ascend_custp_by_result_id(db, customer_id)
    if custp_row is None:
        warnings.append(
            f"no current_cc_scmcustp row for result_id == '{customer_id}' "
            "(matched by no-ktpkitas)"
        )
        return info, warnings

    info["account_number"] = _mask_card_number(custp_row.get("CUST_CR_CARD1"))
    # "Limit sebelumnya" hanya berlaku untuk nasabah non-NTB (punya kartu);
    # nasabah New-To-Bank tidak punya limit sebelumnya.
    if info["account_number"]:
        info["credit_limit"] = _clean(custp_row.get("CUST_CRLIMIT")) or None
    return info, warnings


# Disbursement limit at/above which an NPWP upload is required (Rp 50 juta).
NPWP_LIMIT_THRESHOLD = 50_000_000


def get_credit_limit(customer_id: str, db: Session):
    """Raw ``CUST_CRLIMIT`` ("limit sebelumnya") for a NON-NTB customer, or None. Same
    cashline→ascend join as ``get_customer_info`` but limit-only, for callers that
    need the figure without the name/card (e.g. the NPWP-upload trigger). Returns None
    for New-To-Bank customers (no card), who have no prior limit."""
    cashline_row = crud.get_tms_cashline_by_result_id(db, customer_id)
    if cashline_row is None:
        return None
    # [FIX] Sebelumnya get_ascend_custp_by_local_name() (tabel lokal legacy yang
    # kosong) -> selalu None. Sekarang lewat DWH API, dicocokkan by no-ktpkitas.
    custp_row = crud.get_ascend_custp_by_result_id(db, customer_id)
    if custp_row is None:
        return None
    if not _clean(custp_row.get("CUST_CR_CARD1")):  # NTB — no card, no prior limit
        return None
    return _clean(custp_row.get("CUST_CRLIMIT")) or None


def npwp_required_by_limit(credit_limit_raw) -> bool:
    """True when the disbursement limit is at/above the NPWP threshold (Rp 50 juta)."""
    val = _to_number(credit_limit_raw)
    return val is not None and val >= NPWP_LIMIT_THRESHOLD