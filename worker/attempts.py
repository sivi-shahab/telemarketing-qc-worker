"""Batas percobaan task yang prosesnya mati mendadak.

Kasus 8 Oktober 2026 (worker kube): proses anak Celery dibunuh ``SIGKILL`` (OOM pod)
di tengah ``process_transcript``. Dengan ``task_acks_late`` + ``task_reject_on_worker_lost``
pesannya langsung dikembalikan ke antrean dan dimulai lagi dari nol — lalu mati lagi
di titik yang sama. 47 tiket berputar begitu berjam-jam: tidak pernah ``done``, tidak
pernah ``failed``, dan pembersih ``processing`` basi tidak pernah kena karena tiap
putaran menulis ``started_at`` baru.

Penghitungnya disimpan di Redis broker, bukan di memori proses (yang ikut mati) dan
bukan di DB (butuh migrasi). ``mulai`` menaikkan hitungan setiap task dimulai;
``selesai`` menghapusnya saat task berakhir normal — sukses MAUPUN gagal biasa. Proses
yang di-``SIGKILL`` tidak sempat memanggil ``selesai``, jadi hanya kematian mendadak
beruntun yang menumpuk. Broker yang sama dengan yang mengembalikan pesannya, jadi
worker kube (Redis sendiri) menghitung sendiri.

Redis bermasalah = tidak ada batas (``mulai`` mengembalikan 1): penghitung ini jaring
pengaman, bukan syarat — tiket tidak boleh gagal hanya karena Redis tersendat.

Konfigurasi (env var):
  - TASK_MAX_ATTEMPTS   jumlah mulai yang diizinkan per tiket (default 3)
"""
import logging
import os

from worker.celery_app import REDIS_URL

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = int(os.getenv("TASK_MAX_ATTEMPTS", "3"))
# Lebih lama dari satu putaran kirim-ulang mana pun (visibility_timeout 60 menit), tapi
# tidak selamanya: hitungan yatim dari tiket yang sudah dihapus hilang sendiri.
_TTL_SEC = 24 * 3600
_TIMEOUT_SEC = 2.0
_state = {"client": None}


def _client():
    if _state["client"] is None:
        import redis
        _state["client"] = redis.Redis.from_url(
            REDIS_URL, socket_timeout=_TIMEOUT_SEC, socket_connect_timeout=_TIMEOUT_SEC,
        )
    return _state["client"]


def _key(task: str, ident: str) -> str:
    return f"qc:attempts:{task}:{ident}"


def mulai(task: str, ident: str) -> int:
    """Catat satu kali mulai; kembalikan urutan percobaan ini (1 = pertama)."""
    try:
        key = _key(task, ident)
        pipe = _client().pipeline()
        pipe.incr(key)
        pipe.expire(key, _TTL_SEC)
        n, _ = pipe.execute()
        return int(n)
    except Exception as exc:  # noqa: BLE001 — lihat docstring modul
        logger.warning("[attempts] Redis tidak bisa dipakai (%s) — batas percobaan "
                       "%s %s dilewati", exc, task, ident)
        return 1


def selesai(task: str, ident: str) -> None:
    """Task berakhir normal (sukses atau gagal biasa): lupakan hitungannya."""
    try:
        _client().delete(_key(task, ident))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[attempts] gagal menghapus hitungan %s %s (%s)", task, ident, exc)


def pesan_habis(n_mati: int, tahap) -> str:
    """Isi ``error_message`` row yang dihentikan karena batas ini."""
    return (
        f"Proses terhenti mendadak {n_mati} kali berturut-turut di tahap "
        f"'{tahap or 'belum mulai'}' (proses worker mati — kemungkinan kehabisan "
        "memori/OOM atau melewati batas waktu). Tidak dicoba lagi otomatis; "
        "silakan proses ulang setelah worker diperbaiki."
    )
