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


def parse_collection_campaigns(raw) -> frozenset:
    """Ubah ``"a, B , ,c"`` menjadi ``frozenset({"a", "b", "c"})``.

    Tiap entri di-trim lalu di-casefold, entri kosong dibuang. Input yang bukan
    string (None, list, dsb.) menghasilkan frozenset kosong — konfigurasi salah
    bentuk mematikan fitur, bukan melempar exception saat startup.
    """
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(part.strip().casefold() for part in raw.split(",") if part.strip())


def is_collection(name, allowed) -> bool:
    """True bila ``name`` (di-trim + casefold) ada di ``allowed``.

    ``allowed`` adalah hasil :func:`parse_collection_campaigns`. Himpunan kosong
    selalu menghasilkan False.
    """
    if not allowed or not isinstance(name, str):
        return False
    return name.strip().casefold() in allowed
