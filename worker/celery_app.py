import os
from datetime import timedelta

from celery import Celery
from celery.schedules import crontab

from qc_auto_assign import SCHEDULE_WIB
from services import data_dwh

# Evaluasi selalu memakai data DWH segar (reproses dilakukan justru karena data TMS
# berubah); cache Redis 30 hari hanya untuk halaman. Worker tetap MENULIS ke Redis
# sehingga halaman ikut segar sesudah tiket dinilai (28 September 2026).
data_dwh.set_redis_read(False)
# Cache App A lama tanpa agent_id/submit_time dilengkapi dari endpoint asli
# (6 Oktober 2026) -- lihat services/data_dwh.set_fill_cashline_ids.
data_dwh.set_fill_cashline_ids(True)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6378/0")
CELERY_CONCURRENCY = int(os.getenv("CELERY_CONCURRENCY", "4"))
# Masa simpan hasil task (celery-task-meta-*) di Redis. Bawaan Celery 1 hari;
# dinaikkan ke 30 hari atas permintaan pemilik sistem (28 September 2026).
CELERY_RESULT_EXPIRES_DAYS = int(os.getenv("CELERY_RESULT_EXPIRES_DAYS", "30"))

celery_app = Celery(
    "bank_qa",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=[
        "worker.tasks.process_transcript",
        "worker.tasks.process_document",
        "worker.tasks.process_ocr_image",
        "worker.tasks.reprocess_ticket",
        "worker.tasks.maintenance",
        "worker.tasks.auto_assign",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Jakarta",
    enable_utc=True,
    worker_concurrency=CELERY_CONCURRENCY,
    result_expires=timedelta(days=CELERY_RESULT_EXPIRES_DAYS),
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
    # Nilai bawaan Redis, ditulis eksplisit karena pembersih di bawah bergantung
    # padanya: task yang belum di-ack dikirim ulang tiap selang ini, dan worker
    # menulis started_at baru. crud.STALE_PROCESSING_AFTER + interval beat harus
    # di bawah angka ini (dijaga tests/test_stale_processing_reaper.py).
    broker_transport_options={"visibility_timeout": 3600},
    # Dijalankan oleh service `beat` di docker-compose.yml (28 September 2026).
    # Idempoten dan memakai SKIP LOCKED, jadi aman walau ada lebih dari satu beat
    # yang mengarah ke DB yang sama.
    beat_schedule={
        "fail-stale-processing-results": {
            "task": "worker.tasks.maintenance.fail_stale_processing_results",
            "schedule": 120.0,
        },
        "fail-stale-ocr-images": {
            "task": "worker.tasks.maintenance.fail_stale_ocr_images",
            "schedule": 120.0,
        },
        "refresh-stats-snapshot": {
            "task": "worker.tasks.maintenance.refresh_stats_snapshot",
            "schedule": 120.0,
        },
        # Batch auto assign tiket ke QC (jam WIB; timezone di atas = Asia/Jakarta),
        # 6 Oktober 2026. Saklarnya QC_AUTO_ASSIGN_ENABLED (default mati) dibaca task-nya
        # sendiri, jadi entri ini aman ada walau fiturnya belum dinyalakan.
        **{
            f"auto-assign-{h:02d}{m:02d}": {
                "task": "worker.tasks.auto_assign.scheduled_auto_assign",
                "schedule": crontab(hour=h, minute=m),
            }
            for h, m in SCHEDULE_WIB
        },
    },
)
