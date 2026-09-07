#!/usr/bin/env python3
"""
services/view_streams.py

Klien HTTP untuk ``GET {VIEW_STREAM_BASE_URL}/api/view-streams/{tiket_id}`` —
PDF transkrip satu tiket, disajikan Aplikasi C di port 8010 (berbeda dari
``/tickets-daily`` yang ada di 8008, lihat ``services/tickets_daily.py``).

Dulu halaman detail transkrip mengambil PDF ini LANGSUNG DARI BROWSER. Dua
akibatnya, sama persis dengan yang dulu terjadi pada daftar tiketnya:

1. ``X-API-Key`` App C ikut ter-inline ke bundle JS. Bukan cuma lewat env var
   ``VITE_TMS_API_KEY`` — komponennya menulis nilai key itu sebagai FALLBACK
   literal, jadi key-nya terbawa ke bundle produksi bahkan ketika env var-nya
   tidak disetel sama sekali. Di sini key-nya tinggal di server.
2. Permintaannya tidak melewati App B, jadi ``rbac.effective_campaigns_for``
   tidak berlaku: siapa pun yang membaca key itu di DevTools bisa mengunduh PDF
   tiket campaign mana pun. Gate-nya sekarang di
   ``api/routers/transcript_pdf.py`` di atas modul ini.

PDF-nya dibaca sekaligus ke memori, tidak di-stream per potong: satu transkrip
hanya ratusan KB, dan meneruskannya utuh membuat pemanggil bisa membedakan
"App C gagal" dari "PDF kosong" SEBELUM satu byte pun terkirim ke browser.

Konfigurasi (env var):
  - VIEW_STREAM_BASE_URL      base URL App C :8010 (default "https://call-qc.bankmega.local")
  - VIEW_STREAM_API_KEY       nilai header X-API-Key; kosong = ikut TMS_API_KEY
  - VIEW_STREAM_TIMEOUT_SEC   timeout per request (default 60 — PDF, bukan JSON)
  - VIEW_STREAM_VERIFY_SSL    "false" untuk sertifikat internal (default "true")
"""
import logging
import os

import requests

logger = logging.getLogger(__name__)

# Jamak ("view-streams"). Bentuk tunggal tidak dikenali nginx dan lolos ke
# catch-all SPA, sehingga yang kembali index.html ber-status 200, bukan PDF.
_API_PATH = "/api/view-streams/"


class ViewStreamError(RuntimeError):
    """App C tidak bisa dihubungi / membalas non-200 / body-nya bukan PDF."""


def _base_url() -> str:
    return os.getenv("VIEW_STREAM_BASE_URL", "https://call-qc.bankmega.local").rstrip("/")


def _api_key() -> str:
    """Key App C. Jatuh ke ``TMS_API_KEY`` karena keduanya aplikasi yang sama —
    deploy yang sudah menyetel satu key tidak perlu menyetel dua."""
    return os.getenv("VIEW_STREAM_API_KEY") or os.getenv("TMS_API_KEY", "")


def fetch_pdf(tiket_id: str) -> bytes:
    """Isi PDF transkrip ``tiket_id``. Melempar ``ViewStreamError`` bila gagal.

    Sengaja TIDAK mengembalikan bytes kosong saat gagal: layar yang gagal karena
    App C mati tidak boleh tampak sama dengan tiket yang PDF-nya memang kosong.
    """
    url = _base_url() + _API_PATH + requests.utils.quote(str(tiket_id), safe="")
    headers = {"Accept": "application/pdf"}
    key = _api_key()
    if key:
        headers["X-API-Key"] = key

    verify = os.getenv("VIEW_STREAM_VERIFY_SSL", "true").strip().lower() not in ("false", "0", "no")
    timeout = float(os.getenv("VIEW_STREAM_TIMEOUT_SEC", "60"))

    try:
        resp = requests.get(url, headers=headers, timeout=timeout, verify=verify)
    except requests.RequestException as exc:
        logger.error("[view-streams] gagal call %s: %s", url, exc)
        raise ViewStreamError(f"Gagal menghubungi App C: {exc}") from exc

    if not resp.ok:
        logger.error("[view-streams] %s -> HTTP %s", url, resp.status_code)
        raise ViewStreamError(f"App C membalas HTTP {resp.status_code}")

    # Penjaga untuk kekeliruan routing yang paling mungkin terjadi: sebuah proxy
    # yang membalas halaman SPA ber-status 200 alih-alih PDF. Tanpa ini, pdf.js di
    # browser yang menemukannya, dan pesannya tidak menunjuk ke sebab aslinya.
    body = resp.content
    if not body.startswith(b"%PDF"):
        ctype = resp.headers.get("Content-Type", "?")
        logger.error("[view-streams] %s membalas %s, bukan PDF", url, ctype)
        raise ViewStreamError(f"App C membalas {ctype}, bukan PDF")
    return body
