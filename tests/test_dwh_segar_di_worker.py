"""Worker evaluasi selalu mengambil data DWH SEGAR (tidak membaca cache Redis).

Cache Redis DWH (``services/data_dwh.py``) menyimpan data hingga 30 hari untuk
mempercepat halaman. Evaluasi tidak boleh ikut memakainya: reproses justru dilakukan
karena data TMS berubah. Worker tetap MENULIS ke Redis sehingga halaman ikut segar.
"""


def test_worker_mematikan_baca_cache_redis_dwh():
    import worker.celery_app  # noqa: F401 — dimuat oleh setiap proses worker
    from services import data_dwh

    assert data_dwh._REDIS_READ is False
