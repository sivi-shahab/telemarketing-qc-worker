"""Worker evaluasi selalu mengambil data DWH SEGAR (tidak membaca cache Redis).

Cache Redis DWH (``services/data_dwh.py``) menyimpan data hingga 30 hari untuk
mempercepat halaman. Evaluasi tidak boleh ikut memakainya: reproses justru dilakukan
karena data TMS berubah. Worker tetap MENULIS ke Redis sehingga halaman ikut segar.
"""


def test_worker_mematikan_baca_cache_redis_dwh():
    import worker.celery_app  # noqa: F401 — dimuat oleh setiap proses worker
    from services import data_dwh

    assert data_dwh._REDIS_READ is False


def test_worker_mengisi_agent_id_dari_endpoint_asli():
    """Cache App A lama tanpa agent_id/submit_time -> worker bertanya ke endpoint asli
    (6 Oktober 2026). Lihat test_data_dwh_cashline_ids.py di telemarketing-qc-core."""
    import worker.celery_app  # noqa: F401
    from services import data_dwh

    assert data_dwh._FILL_CASHLINE_IDS is True
