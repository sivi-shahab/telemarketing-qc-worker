"""Client API Microsite (``/data/API-Microsite``, service ``microsite-ocr``): KTP nasabah NTB hasil OCR.

Microsite mengambil dokumen KTP tiket dari DMS-Cardsys berdasarkan nomor tiket (``NomorDokumen`` =
``result_id`` di TMS NTB), lalu meng-OCR-nya lewat OCR API (Qwen2.5-VL)::

    GET {MICROSITE_API_BASE_URL}/tickets/{ticket_id}/ocr/ktp   (header ``X-API-Key``)
    -> {"ticket_id", "data": [{"filename", "document_type", "created_at", "data": {NIK, NAMA, TTL, ...},
                               "warnings": [...], "error"}], "skipped": [...], "error"}

Hasilnya menjadi sumber "Microsite" pada NTB Data Verification (voting Microsite/TMS/transkrip) dan
aturan KTP lama / usia 21-65 (``compliance.ntb_ktp``). Worker memanggilnya SATU KALI saat tiket
diproses dan menyimpannya di snapshot ``reference_data.microsite_ktp`` (lihat :func:`fetch_ktp`);
pembaca (API) hanya membaca snapshot itu, tidak memanggil Microsite.

``MICROSITE_API_BASE_URL`` kosong = fitur mati (snapshot ``None``, perilaku tanpa Microsite).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)

MICROSITE_API_BASE_URL = (os.getenv("MICROSITE_API_BASE_URL") or "").strip().rstrip("/")
MICROSITE_API_KEY = (os.getenv("MICROSITE_API_KEY") or "").strip()
# DMS search + download + OCR per KTP; satu tiket bisa beberapa detik sampai puluhan detik.
_TIMEOUT_SEC = float(os.getenv("MICROSITE_TIMEOUT_SEC") or 180)
_API_PATH_KTP = "/tickets/{ticket_id}/ocr/ktp"

STATUS_OK = "ok"            # minimal satu KTP terbaca
STATUS_NO_KTP = "no_ktp"    # tiket tidak punya KTP di DMS (atau KTP-nya terbaca kosong)
STATUS_ERROR = "error"      # Microsite tidak bisa dihubungi atau menjawab error

# Field OCR Microsite -> kunci nilai KTP yang dipakai ``compliance.ntb_verification``.
_FIELD_MAP = {
    "NIK": "nik", "NAMA": "nama", "JENIS_KELAMIN": "jenis_kelamin", "ALAMAT": "alamat",
    "KEL_DESA": "kelurahan", "KECAMATAN": "kecamatan", "STATUS_PERKAWINAN": "status_perkawinan",
    "PEKERJAAN": "pekerjaan", "KEWARGANEGARAAN": "kewarganegaraan",
}
_EMPTY = {"", "-", ".", "null", "none"}


def enabled() -> bool:
    return bool(MICROSITE_API_BASE_URL)


def _clean(value) -> Optional[str]:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    return None if s.casefold() in _EMPTY else s


def _split_ttl(ttl) -> tuple[Optional[str], Optional[str]]:
    """``"KOTA, 01-01-1981"`` -> ``("KOTA", "1981-01-01")``; bagian yang tak terbaca -> None."""
    text = _clean(ttl)
    if not text:
        return None, None
    m = re.search(r"(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{4})", text)
    tanggal = None
    if m:
        try:
            tanggal = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date().isoformat()
        except ValueError:
            tanggal = None
    tempat = _clean((text[: m.start()] if m else text).strip(" ,"))
    return tempat, tanggal


def _split_rt_rw(value) -> tuple[Optional[str], Optional[str]]:
    parts = [p for p in re.split(r"\s*/\s*", _clean(value) or "") if p]
    if len(parts) != 2:
        return None, None
    return _clean(parts[0]), _clean(parts[1])


def ktp_values(ocr_data) -> dict:
    """Satu objek ``data`` OCR KTP Microsite -> nilai KTP (``nik``, ``nama``, ``tanggal_lahir``
    YYYY-MM-DD, ``tempat_lahir``, ``jenis_kelamin``, ``status_perkawinan``, ``pekerjaan``,
    ``kewarganegaraan``, ``alamat``, ``rt``, ``rw``, ``kelurahan``, ``kecamatan``); hanya yang terbaca."""
    if not isinstance(ocr_data, dict):
        return {}
    out = {}
    for src, key in _FIELD_MAP.items():
        v = _clean(ocr_data.get(src))
        if v:
            out[key] = v
    tempat, tanggal = _split_ttl(ocr_data.get("TTL"))
    rt, rw = _split_rt_rw(ocr_data.get("RT_RW"))
    for key, v in (("tempat_lahir", tempat), ("tanggal_lahir", tanggal), ("rt", rt), ("rw", rw)):
        if v:
            out[key] = v
    return out


def snapshot_from_response(body) -> dict:
    """Response ``/tickets/{id}/ocr/ktp`` -> snapshot ``reference_data.microsite_ktp``::

        {"status": ok|no_ktp|error, "values": {...}, "documents": [{filename, created_at, warnings, error}],
         "error": str|None}

    Beberapa KTP: per field, KTP terbaru (``created_at``) yang terbaca menang."""
    if not isinstance(body, dict):
        return {"status": STATUS_ERROR, "values": {}, "documents": [], "error": "Response Microsite bukan objek JSON."}
    docs = [d for d in (body.get("data") or []) if isinstance(d, dict) and d.get("document_type") == "ktp"]
    docs.sort(key=lambda d: str(d.get("created_at") or ""))
    values = {}
    for d in docs:
        if not d.get("error"):
            values.update(ktp_values(d.get("data")))
    documents = [{
        "filename": d.get("filename"),
        "created_at": d.get("created_at"),
        "warnings": [w.get("code") for w in (d.get("warnings") or []) if isinstance(w, dict) and w.get("code")],
        "error": d.get("error"),
    } for d in docs]
    if body.get("error"):
        return {"status": STATUS_ERROR, "values": values, "documents": documents, "error": str(body["error"])}
    if not values and any(d.get("error") for d in docs):
        # KTP ada di DMS tetapi OCR-nya gagal semua: bukan "tidak punya KTP".
        return {"status": STATUS_ERROR, "values": {}, "documents": documents, "error": "OCR KTP Microsite gagal."}
    return {"status": STATUS_OK if values else STATUS_NO_KTP, "values": values, "documents": documents, "error": None}


def fetch_ktp(ticket_id: str) -> Optional[dict]:
    """Snapshot KTP Microsite untuk ``ticket_id`` (lihat :func:`snapshot_from_response`), atau ``None``
    bila fitur mati / ``ticket_id`` kosong. Gagal hubung / HTTP error -> ``status`` ``error``
    (tidak melempar exception: tiket tetap dinilai tanpa Microsite)."""
    tid = str(ticket_id or "").strip()
    if not enabled() or not tid:
        return None
    url = MICROSITE_API_BASE_URL + _API_PATH_KTP.format(ticket_id=tid)
    headers = {"X-API-Key": MICROSITE_API_KEY} if MICROSITE_API_KEY else {}
    try:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT_SEC)
    except requests.RequestException as exc:
        logger.warning("[microsite] gagal call %s: %s", url, exc)
        return {"status": STATUS_ERROR, "values": {}, "documents": [], "error": f"Microsite tidak bisa dihubungi: {exc.__class__.__name__}"}
    if not resp.ok:
        logger.error("[microsite] %s -> HTTP %s: %s", url, resp.status_code, resp.text[:300])
        return {"status": STATUS_ERROR, "values": {}, "documents": [], "error": f"Microsite HTTP {resp.status_code}"}
    try:
        body = resp.json()
    except ValueError:
        logger.error("[microsite] response bukan JSON dari %s", url)
        return {"status": STATUS_ERROR, "values": {}, "documents": [], "error": "Response Microsite bukan JSON."}
    snap = snapshot_from_response(body)
    for d in snap["documents"]:
        if d.get("error"):
            logger.warning("[microsite] %s KTP %s gagal OCR: %s", tid, d.get("filename"), str(d["error"])[:200])
    return snap


def values_from_snapshot(snapshot) -> dict:
    """Nilai KTP dari snapshot ``reference_data.microsite_ktp`` (kosong bila tidak ada)."""
    if not isinstance(snapshot, dict):
        return {}
    values = snapshot.get("values")
    return dict(values) if isinstance(values, dict) else {}
