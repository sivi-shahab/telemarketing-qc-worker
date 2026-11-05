"""Aturan KTP campaign NTB: KTP lama ditolak, lalu usia 21-65.

KTP lama (bukan e-KTP) ditolak: NIK hasil OCR harus tepat 16 digit (``NTB_NIK_LENGTH``). NIK yang
tidak terbaca tidak dianggap KTP lama (tidak bisa dibuktikan). Aturan usia di bawah.

Usia dibaca dari tanggal lahir pada KTP (hasil OCR, ``tanggal_lahir`` YYYY-MM-DD) dan
dihitung pada tanggal ``submit_time`` tiket di ``tms_ntb`` (tahun penuh). Di bawah 21
atau di atas 65 tahun = tiket ditolak: skor dipaksa 0 dan AI Status FAIL, apa pun
isi percakapannya (lewat ``scoring.no_product_interest``). Nantinya data KTP datang
dari microsite Bank Mega; sementara ini dari foto KTP yang diunggah.
"""
from __future__ import annotations

from datetime import date, datetime

from compliance.campaign_profile import is_ntb_evaluation

NTB_MIN_AGE = 21
NTB_MAX_AGE = 65
NTB_NIK_LENGTH = 16
REJECTION_KEY = "ntb_ktp_rejection"
KIND_OLD_KTP = "ktp_lama"
KIND_AGE = "usia"


def _to_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def age_on(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


def _document_nik(ocr_json) -> str | None:
    """Digit NIK pada dokumen (baris verifikasi ``NIK``), atau None bila tidak terbaca."""
    for row in (ocr_json or {}).get("verifications") or []:
        if isinstance(row, dict) and str(row.get("field") or "").strip().casefold() == "nik":
            digits = "".join(ch for ch in str(row.get("document") or "") if ch.isdigit())
            return digits or None
    return None


def ntb_old_ktp_rejection(ocr_json) -> dict | None:
    """``{"kind": "ktp_lama", "nik", "reason"}`` bila NIK pada KTP bukan 16 digit (ciri KTP lama,
    bukan e-KTP), selain itu ``None`` (termasuk NIK yang tidak terbaca)."""
    if not isinstance(ocr_json, dict):
        return None
    nik = _document_nik(ocr_json)
    if nik is None or len(nik) == NTB_NIK_LENGTH:
        return None
    return {
        "kind": KIND_OLD_KTP,
        "nik": nik,
        "reason": (
            f"NIK pada KTP berjumlah {len(nik)} digit, bukan {NTB_NIK_LENGTH} digit "
            "(KTP lama, bukan e-KTP); tiket ditolak."
        ),
    }


def ntb_age_rejection(ocr_json, submit_time) -> dict | None:
    """``{"age", "tanggal_lahir", "submit_date", "reason"}`` bila usianya di luar 21-65,
    selain itu ``None`` (termasuk tanggal lahir atau tanggal submit yang tidak terbaca)."""
    if not isinstance(ocr_json, dict):
        return None
    birth = _to_date(ocr_json.get("tanggal_lahir"))
    submitted = _to_date(submit_time)
    if birth is None or submitted is None:
        return None
    age = age_on(birth, submitted)
    if NTB_MIN_AGE <= age <= NTB_MAX_AGE:
        return None
    side = f"di bawah {NTB_MIN_AGE}" if age < NTB_MIN_AGE else f"di atas {NTB_MAX_AGE}"
    return {
        "kind": KIND_AGE,
        "age": age,
        "tanggal_lahir": birth.isoformat(),
        "submit_date": submitted.isoformat(),
        "reason": (
            f"Usia nasabah {age} tahun ({side} tahun) menurut tanggal lahir KTP "
            f"{birth.isoformat()} per tanggal pengajuan {submitted.isoformat()}; "
            "tiket ditolak."
        ),
    }


def apply_ntb_ktp_rejection(evaluation, ocr_by_doc_type, db, customer_id):
    """Salinan ``evaluation`` dengan ``ntb_ktp_rejection`` bila KTP-nya KTP lama (NIK bukan 16 digit)
    atau menunjukkan usia di luar batas. Tidak menyentuh evaluasi non-NTB atau tiket tanpa KTP terunggah."""
    if not is_ntb_evaluation(evaluation) or not customer_id:
        return evaluation
    ktp = dict(ocr_by_doc_type or {}).get("ktp")
    if not isinstance(ktp, dict):
        return evaluation
    rejection = ntb_old_ktp_rejection(ktp)
    if rejection is None:
        from db import crud

        row = crud.get_tms_ntb_by_result_id(db, customer_id) or {}
        rejection = ntb_age_rejection(ktp, row.get("submit_time"))
    if rejection is None:
        return evaluation
    return {**evaluation, REJECTION_KEY: rejection}
