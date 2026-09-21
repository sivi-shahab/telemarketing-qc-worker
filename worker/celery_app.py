import os

from celery import Celery

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6378/0")
CELERY_CONCURRENCY = int(os.getenv("CELERY_CONCURRENCY", "4"))

celery_app = Celery(
    "bank_qa",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=[
        "worker.tasks.process_transcript",
        "worker.tasks.process_document",
        "worker.tasks.reprocess_ticket",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Jakarta",
    enable_utc=True,
    worker_concurrency=CELERY_CONCURRENCY,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Batas keras dinaikkan dari 1800 -> 3000 (10 September 2026) untuk alur penilaian
    # PARALEL: satu tiket kini menembakkan sampai 3 panggilan LLM berbarengan
    # (compliance.parallel_pass, lihat ThreadPoolExecutor di
    # tasks/process_transcript.py), dan tiket dengan > 3 rekaman valid berjalan dalam
    # beberapa gelombang. Endpoint melambat saat dibebani serentak — 5 panggilan
    # sekaligus pernah menembus 1800. Batas lunak 2400 memberi worker ~10 menit untuk
    # menutup rapi sebelum SIGKILL.
    #
    # CELERY_CONCURRENCY di atas sengaja TIDAK ikut dinaikkan: menambah worker paralel
    # di atas penilaian yang sudah paralel melipatgandakan beban serentak ke endpoint
    # yang sama — persis keadaan yang membuat batas ini perlu dinaikkan.
    task_time_limit=3000,
    task_soft_time_limit=2400,
    # Default 4 berarti tiap worker proses menahan concurrency*4 tugas sekaligus
    # (di-"reserve" dari Redis) walau baru mengerjakan 1 — dengan task selambat ini
    # (bisa ~50 menit), tiket lain yang seharusnya bisa dikerjakan proses lain malah
    # tertahan di antrean proses yang sudah penuh. 1 = ambil tugas baru hanya saat ada
    # slot kosong, supaya beban merata antar proses worker (17 September 2026).
    worker_prefetch_multiplier=1,
)
