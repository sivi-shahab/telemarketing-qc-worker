#!/usr/bin/env python3
"""
services/data_dwh.py

Client HTTP untuk reference data (CASHLINE + CARD HOLDER) yang dulu dibaca dari
CSV lalu dari DB (tms_cashline / ascend_custp). Sekarang sumbernya API Aplikasi A
(data warehouse) di API/V1 (port 8002), lewat 2 endpoint:

    GET {DWH_API_BASE_URL}/campaign/cashline-ntb-asscend/{result_id}  (CACHE, cepat)
    GET {DWH_API_BASE_URL}/campaign/cashline-ntb/{result_id}          (ASLI, fallback)

  - ``cashline`` = dashboard.campaign_cashline_ntb  (skema kolom == tms_cashline lama)
  - ``customer`` = dashboard.current_cc_scmcustp    (skema kolom == ascend_custp lama;
    dicocokkan OLEH Aplikasi A by ``no-ktpkitas`` dari cashline — bukan lagi by cust_name)

[NEW] STRATEGI CACHE-FIRST: coba endpoint CACHE dulu (baca dari tabel
dashboard."cashline-ntb-asscend", diisi Job 3 secara proaktif tiap hari
+ setiap kali endpoint asli dipanggil). Kalau cache belum terisi (404 --
mis. result_id baru yang belum sempat di-warm Job 3, seperti upload
manual di luar jadwal), FALLBACK otomatis ke endpoint ASLI (yang query
DWH langsung DAN sekaligus mengisi cache untuk permintaan berikutnya).
Endpoint cache cuma punya 48 field terpilih (bukan semua field endpoint
asli) -- cukup untuk semua kebutuhan App B saat ini (lihat services/db.py
App A, _ASSCEND_CASHLINE_FIELDS/_ASSCEND_CUSTOMER_FIELDS).

Karena satu response sudah memuat cashline + customer sekaligus, hasilnya
di-cache singkat per ``result_id`` (cache IN-MEMORY di App B, TERPISAH dari
cache database di App A) supaya pemanggilan cashline & customer yang
berurutan (lihat reference_data.build_reference_data) hanya menembak API sekali.

Konfigurasi (env var):
  - DWH_API_BASE_URL      base URL API Aplikasi A (default "http://localhost:8002")
  - DWH_API_TIMEOUT_SEC   timeout per request (default 10)
  - DWH_API_CACHE_TTL_SEC TTL cache in-memory App B per result_id (default 300)
"""
import logging
import os
import time
from threading import Lock
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DWH_API_BASE_URL = os.getenv("DWH_API_BASE_URL", "http://localhost:8002").rstrip("/")
_API_PATH_CACHE = "/campaign/cashline-ntb-asscend/{result_id}"
_API_PATH_ORIGINAL = "/campaign/cashline-ntb/{result_id}"
_TIMEOUT_SEC = float(os.getenv("DWH_API_TIMEOUT_SEC", "10"))
# TTL menyatukan call cashline + customer dalam satu proses evaluasi, DAN menahan
# hasilnya lintas-request untuk halaman agregat.
#
# [FIX] Dulu 5 detik -- LEBIH PENDEK dari satu pass agregat itu sendiri (pass
# /stats/ai_status_timeseries makan puluhan detik), jadi entry-nya kedaluwarsa di
# tengah loop dan cid yang sama ditembak ulang: 342 cid unik jadi 566 HTTP request.
# Halaman Statistik juga auto-refresh tiap 30 detik, sehingga TTL di bawah 30 detik
# berarti TIDAK ADA satu pun poll yang kena cache. 300 detik menutup keduanya.
#
# Ongkosnya: perubahan reference data di DWH baru kelihatan setelah maksimal 5 menit.
# Aman karena sumbernya sendiri (Job 3 di App A) hanya di-refresh harian; turunkan
# lewat env var kalau ada alur yang butuh data DWH lebih segar dari itu.
_CACHE_TTL_SEC = float(os.getenv("DWH_API_CACHE_TTL_SEC", "300"))

_EMPTY_BUNDLE = {"cashline": None, "customer": None}

# result_id -> (monotonic_timestamp, bundle)
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = Lock()


def _cache_get(result_id: str) -> Optional[dict]:
    with _cache_lock:
        hit = _cache.get(result_id)
        if hit is None:
            return None
        ts, bundle = hit
        if (time.monotonic() - ts) > _CACHE_TTL_SEC:
            _cache.pop(result_id, None)
            return None
        return bundle


def _cache_put(result_id: str, bundle: dict) -> None:
    with _cache_lock:
        _cache[result_id] = (time.monotonic(), bundle)


def _fetch_from_url(url: str) -> tuple[Optional[dict], Optional[int]]:
    """Panggil 1 URL, balikkan (bundle_dict_atau_None, http_status_atau_None).
    http_status None berarti request gagal total (network error/timeout) --
    beda dari 404 (server merespons, tapi datanya tidak ada)."""
    try:
        resp = requests.get(url, timeout=_TIMEOUT_SEC)
    except requests.RequestException as exc:
        logger.warning("[data-dwh] gagal call %s: %s", url, exc)
        return None, None

    if resp.status_code == 404:
        return None, 404

    if not resp.ok:
        logger.error("[data-dwh] %s -> HTTP %s: %s", url, resp.status_code, resp.text[:200])
        return None, resp.status_code

    try:
        data = resp.json()
    except ValueError as exc:
        logger.error("[data-dwh] response bukan JSON dari %s: %s", url, exc)
        return None, resp.status_code

    return {"cashline": data.get("cashline"), "customer": data.get("customer")}, resp.status_code


def fetch_bundle(result_id: str) -> dict:
    """Ambil ``{"cashline", "customer"}`` untuk ``result_id`` dari DWH (di-cache).

    [NEW] Strategi cache-first:
      1. Coba endpoint CACHE (/campaign/cashline-ntb-asscend/{id}) dulu -- cepat,
         48 field terpilih.
      2. Kalau cache 404 (belum pernah di-warm) ATAU gagal total (network error),
         FALLBACK ke endpoint ASLI (/campaign/cashline-ntb/{id}) -- lebih lambat,
         tapi selalu ada datanya kalau memang ada di DWH, dan sekaligus MENGISI
         cache untuk permintaan berikutnya (self-healing).

    Selalu mengembalikan dict dengan kunci ``"cashline"`` & ``"customer"`` (nilai
    ``None`` bila tidak ada / kedua endpoint 404). Error jaringan / HTTP non-404 /
    body non-JSON di-log lalu mengembalikan bundle kosong supaya pipeline evaluasi
    tetap jalan (LLM menandai field SKIPPED_NULL).
    """
    rid = str(result_id or "").strip()
    if not rid:
        return dict(_EMPTY_BUNDLE)

    cached = _cache_get(rid)
    if cached is not None:
        return cached

    # --- 1. Coba endpoint CACHE (database App A, cepat) dulu ---
    cache_url = DWH_API_BASE_URL + _API_PATH_CACHE.format(result_id=rid)
    bundle, status = _fetch_from_url(cache_url)
    if bundle is not None:
        # Cache hit -- langsung pakai, TIDAK perlu panggil endpoint asli.
        _cache_put(rid, bundle)
        return bundle

    if status is not None and status != 404:
        # Server merespons tapi BUKAN 404/200 (mis. 500) -- tetap coba fallback
        # ke endpoint asli di bawah, siapa tahu itu yang bermasalah, bukan datanya.
        logger.warning(
            "[data-dwh] endpoint cache %s balas HTTP %s (bukan 404) -- coba fallback ke endpoint asli",
            cache_url, status,
        )

    # --- 2. Fallback ke endpoint ASLI (query DWH langsung, isi cache utk next time) ---
    logger.info(
        "[data-dwh] cache kosong/gagal untuk result_id=%s -- fallback ke endpoint asli", rid,
    )
    original_url = DWH_API_BASE_URL + _API_PATH_ORIGINAL.format(result_id=rid)
    bundle, status = _fetch_from_url(original_url)

    if bundle is None:
        # 404 di endpoint asli juga -> memang tidak ada datanya di DWH.
        # Cache-kan bundle KOSONG supaya tidak nembak API berulang utk id yg sama.
        bundle = dict(_EMPTY_BUNDLE)

    _cache_put(rid, bundle)
    return bundle


def clear_cache() -> None:
    """Kosongkan cache in-memory App B (dipakai di test / setelah data direfresh)."""
    with _cache_lock:
        _cache.clear()