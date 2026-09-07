#!/usr/bin/env python3
"""
services/tickets_daily.py

Klien HTTP untuk ``GET {TMS_API_BASE_URL}/tickets-daily`` (Aplikasi C,
recording_tms_api) — sumber baris halaman Recording Tickets dan Assign Ticket.

Dulu kedua halaman itu menembak App C LANGSUNG DARI BROWSER. Dua akibatnya:

1. ``X-API-Key`` App C ikut ter-inline ke bundle JS, jadi terbaca siapa pun yang
   membuka DevTools (lihat komentar di TranscriptsView.vue). Di sini key-nya
   tinggal di server.
2. Tidak ada satu pun gate campaign di jalur itu — permintaannya tidak pernah
   melewati App B, sehingga ``rbac.effective_campaigns_for`` tidak berlaku dan
   login yang dibatasi ke campaign ``Collection`` tetap membaca tiket ``ACT02`` /
   ``LOC26``. Penyaringannya sekarang dilakukan di ``api/routers/tickets_daily.py``
   di atas modul ini.

App C membatasi ``limit`` di 100 per halaman, jadi loop paginasi yang dulu ada di
kedua komponen Vue pindah ke sini — satu kali panggil, seluruh halaman terkumpul.
Itu juga syarat agar penyaringan benar: memotong hasil SETELAH disaring membuat
``total`` dan nomor halaman App C tidak lagi cocok dengan apa yang ditampilkan.

Konfigurasi (env var):
  - TMS_API_BASE_URL      base URL App C (default "https://call-qc.bankmega.local")
  - TMS_API_KEY           nilai header X-API-Key ("" = header tidak dikirim)
  - TMS_API_TIMEOUT_SEC   timeout per request (default 30)
  - TMS_API_VERIFY_SSL    "false" untuk sertifikat internal (default "true")
  - TMS_API_CACHE_TTL_SEC TTL cache in-memory per kombinasi filter (default 60)
"""
import logging
import os
import time
from threading import Lock
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TMS_API_BASE_URL = os.getenv("TMS_API_BASE_URL", "https://call-qc.bankmega.local").rstrip("/")
_API_PATH = "/tickets-daily"
_API_KEY = os.getenv("TMS_API_KEY", "")
_TIMEOUT_SEC = float(os.getenv("TMS_API_TIMEOUT_SEC", "30"))
_VERIFY_SSL = os.getenv("TMS_API_VERIFY_SSL", "true").strip().lower() not in ("false", "0", "no")

# Batas atas paginasi App C.
PAGE_SIZE = 100

# Cache singkat per kombinasi filter. Dibutuhkan karena satu layar bisa memanggil
# endpoint ini beberapa kali berturut-turut (mis. Assign Ticket memuat ulang
# setelah assign), dan satu panggilan di sini berarti belasan request ke App C.
# TTL-nya sengaja pendek: tiket baru harus terlihat dalam hitungan detik.
_CACHE_TTL_SEC = float(os.getenv("TMS_API_CACHE_TTL_SEC", "60"))

# (tiket_id, load_date, max_pages) -> (monotonic_timestamp, payload)
_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = Lock()


class TicketsDailyError(RuntimeError):
    """App C tidak bisa dihubungi / membalas non-200 / body bukan JSON."""


def _cache_get(key: tuple) -> Optional[dict]:
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        ts, payload = hit
        if (time.monotonic() - ts) > _CACHE_TTL_SEC:
            _cache.pop(key, None)
            return None
        return payload


def _cache_put(key: tuple, payload: dict) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), payload)


def _get_page(params: dict) -> dict:
    headers = {"Accept": "application/json"}
    if _API_KEY:
        headers["X-API-Key"] = _API_KEY
    url = TMS_API_BASE_URL + _API_PATH
    try:
        resp = requests.get(
            url, params=params, headers=headers, timeout=_TIMEOUT_SEC, verify=_VERIFY_SSL
        )
    except requests.RequestException as exc:
        logger.error("[tickets-daily] gagal call %s: %s", url, exc)
        raise TicketsDailyError(f"Gagal menghubungi App C: {exc}") from exc

    if not resp.ok:
        logger.error("[tickets-daily] %s -> HTTP %s: %s", url, resp.status_code, resp.text[:200])
        raise TicketsDailyError(f"App C membalas HTTP {resp.status_code}")

    try:
        return resp.json()
    except ValueError as exc:
        logger.error("[tickets-daily] response bukan JSON dari %s: %s", url, exc)
        raise TicketsDailyError("Response App C bukan JSON") from exc


def fetch_all(
    *,
    tiket_id: Optional[str] = None,
    load_date: Optional[str] = None,
    max_pages: int = 100,
) -> dict:
    """Seluruh baris /tickets-daily untuk satu kombinasi filter (di-cache singkat).

    Mengembalikan ``{"mode", "load_date", "items", "total", "truncated"}``.
    ``truncated`` True bila ``max_pages`` habis sebelum seluruh baris terkumpul —
    pemanggil menampilkannya sebagai peringatan "persempit pencarian", bukan
    memperbesar batasnya: pencarian tanpa tanggal bisa mengembalikan puluhan ribu
    baris.

    ``total`` adalah jumlah baris yang BENAR-BENAR terkumpul, bukan ``total`` App C —
    dua angka itu berbeda begitu ``truncated`` True.

    Melempar ``TicketsDailyError`` bila App C tidak bisa dihubungi. Sengaja tidak
    dijadikan daftar kosong: layar yang kosong karena galat jaringan tidak boleh
    tampak sama dengan layar yang kosong karena memang tidak ada tiket.
    """
    key = (tiket_id or "", load_date or "", int(max_pages))
    cached = _cache_get(key)
    if cached is not None:
        return cached

    collected: list = []
    truncated = False
    mode = None
    resolved_load_date = None

    page = 1
    while page <= max_pages:
        params = {"page": page, "limit": PAGE_SIZE}
        if tiket_id:
            params["tiket_id"] = tiket_id
        if load_date:
            params["load_date"] = load_date

        data = _get_page(params)
        if page == 1:
            mode = data.get("mode")
            resolved_load_date = data.get("load_date")

        items = data.get("items") or []
        collected.extend(items)
        if not items or len(collected) >= (data.get("total") or 0):
            break
        if page == max_pages:
            truncated = True
        page += 1

    payload = {
        "mode": mode,
        "load_date": resolved_load_date,
        "items": collected,
        "total": len(collected),
        "truncated": truncated,
    }
    _cache_put(key, payload)
    return payload
