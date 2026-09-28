"""Cache Redis bersama untuk data yang mahal diambil dan tidak berubah.

Dipakai ``crud.result_json_map`` (28 September 2026): JSON hasil evaluasi per baris
``result_data`` tidak pernah diubah sesudah tersimpan (reproses selalu menambah baris
baru), tetapi mengambilnya dari Postgres remote makan detik per halaman.

Redis opsional. Tanpa ``REDIS_URL`` atau saat Redis bermasalah, ``get_many``
mengembalikan ``{}`` dan ``set_many`` diam — pemanggil jatuh ke sumber aslinya.
Sesudah gagal, Redis tidak dicoba lagi selama ``_RETRY_AFTER_SEC`` supaya halaman
tidak menunggu timeout berulang.

Konfigurasi (env var):
  - REDIS_URL               Redis bersama (sama dengan broker Celery)
  - REDIS_CACHE_TTL_SEC     TTL bawaan (default 30 hari)
"""
import logging
import os
import time

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "").strip()
DEFAULT_TTL_SEC = int(os.getenv("REDIS_CACHE_TTL_SEC", str(30 * 24 * 3600)))
_TIMEOUT_SEC = 1.0
_RETRY_AFTER_SEC = 30.0
_state = {"client": None, "down_until": 0.0}


def _client():
    if not REDIS_URL:
        return None
    if _state["client"] is None:
        import redis
        _state["client"] = redis.Redis.from_url(
            REDIS_URL, socket_timeout=_TIMEOUT_SEC, socket_connect_timeout=_TIMEOUT_SEC,
        )
    return _state["client"]


def _available() -> bool:
    return time.monotonic() >= _state["down_until"]


def _failed(exc: Exception) -> None:
    logger.warning("[redis-cache] Redis tidak bisa dipakai (%s) -- jeda %.0f detik",
                   exc, _RETRY_AFTER_SEC)
    _state["down_until"] = time.monotonic() + _RETRY_AFTER_SEC


def reset() -> None:
    """Lupakan status gagal (dipakai test)."""
    _state["down_until"] = 0.0


def get_many(keys: list) -> dict:
    """``{key: str}`` untuk key yang ada; key yang tidak ada tidak muncul."""
    if not keys or not _available():
        return {}
    try:
        client = _client()
        if client is None:
            return {}
        values = client.mget(keys)
    except Exception as exc:  # noqa: BLE001 — Redis opsional
        _failed(exc)
        return {}
    out = {}
    for key, raw in zip(keys, values):
        if raw is not None:
            out[key] = raw.decode() if isinstance(raw, bytes) else raw
    return out


def set_many(items: dict, ttl_sec: int = None) -> None:
    """Simpan ``{key: str}`` dengan TTL (bawaan ``DEFAULT_TTL_SEC``)."""
    if not items or not _available():
        return
    ttl = ttl_sec or DEFAULT_TTL_SEC
    try:
        client = _client()
        if client is None:
            return
        pipe = client.pipeline(transaction=False)
        for key, value in items.items():
            pipe.set(key, value, ex=ttl)
        pipe.execute()
    except Exception as exc:  # noqa: BLE001 — Redis opsional
        _failed(exc)
