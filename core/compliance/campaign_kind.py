"""Pembeda campaign QC Collection (penagihan) dari campaign sales (cashline).

Satu-satunya sumber kebenaran adalah variabel lingkungan ``COLLECTION_CAMPAIGNS``
— daftar nama campaign dipisah koma. Modul ini dipakai bersama oleh API
(``api/dependencies.py``) dan worker (``worker/config.py``) supaya keduanya
menormalkan dan mencocokkan nama dengan aturan yang sama; kalau tidak, sebuah
tiket bisa dievaluasi sebagai collection tapi tetap muncul di tabel sales.

Nilai default kosong berarti fitur collection mati total: tidak ada campaign yang
dianggap collection dan seluruh perilaku existing tidak berubah. Ini juga
perilaku saat rollback.
"""
import os


def parse_collection_campaigns(raw) -> frozenset:
    """Ubah ``"a, B , ,c"`` menjadi ``frozenset({"a", "b", "c"})``.

    Tiap entri di-trim lalu di-casefold, entri kosong dibuang. Input yang bukan
    string (None, list, dsb.) menghasilkan frozenset kosong — konfigurasi salah
    bentuk mematikan fitur, bukan melempar exception saat startup.
    """
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(part.strip().casefold() for part in raw.split(",") if part.strip())


def collection_campaigns_from_env() -> frozenset:
    """``COLLECTION_CAMPAIGNS`` yang sudah dinormalkan, dibaca TIAP kali (tanpa cache)
    — dipakai kode bersama (crud, agregasi Statistics) yang tidak punya akses ke
    ``api.rbac``. Kosong = fitur collection mati."""
    return parse_collection_campaigns(os.getenv("COLLECTION_CAMPAIGNS", ""))


def is_collection(name, allowed) -> bool:
    """True bila ``name`` (di-trim + casefold) ada di ``allowed``.

    ``allowed`` adalah hasil :func:`parse_collection_campaigns`. Himpunan kosong
    selalu menghasilkan False.
    """
    if not allowed or not isinstance(name, str):
        return False
    return name.strip().casefold() in allowed


# --- Campaign New To Bank (5 November 2026) ---------------------------------------
#
# NTB (TMS ``NTB002`` "New To Bank Eksternal") adalah penjualan KARTU KREDIT BARU ke
# calon nasabah. Data acuannya TIDAK sama dengan Cashline: hanya baris TMS NTB dari
# App A ``/campaign/new-to-bank-eksternal/{id}`` — tidak ada baris TMS Cashline dan
# tidak ada card holder Ascend (calon nasabah belum punya kartu). Tanpa pembeda ini
# setiap tiket NTB jatuh ke gerbang data acuan Cashline dan tersimpan PENDING
# "Data TMS Kosong · Data Ascend Kosong" tanpa pernah dinilai.
#
# Bawaannya SENGAJA terisi (bukan kosong seperti ``COLLECTION_CAMPAIGNS``): campaign
# ``New To Bank Eksternal`` sudah ada di DB, dan env yang lupa diset di salah satu
# proses (api, worker, kube worker) tidak boleh diam-diam mengembalikan perilaku
# Cashline. Set ``NTB_CAMPAIGNS=`` (kosong) untuk mematikannya (rollback).
DEFAULT_NTB_CAMPAIGNS = "New To Bank Eksternal"


def ntb_campaigns_from_env() -> frozenset:
    """``NTB_CAMPAIGNS`` yang sudah dinormalkan, dibaca TIAP kali (tanpa cache)."""
    return parse_collection_campaigns(os.getenv("NTB_CAMPAIGNS", DEFAULT_NTB_CAMPAIGNS))


def is_ntb(name, allowed=None) -> bool:
    """True bila ``name`` campaign NTB. ``allowed`` None = baca ``NTB_CAMPAIGNS``."""
    return is_collection(name, ntb_campaigns_from_env() if allowed is None else allowed)
