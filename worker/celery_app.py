import os

from celery import Celery

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6378/0")
CELERY_CONCURRENCY = int(os.getenv("CELERY_CONCURRENCY", "4"))

celery_app = Celery(
    "bank_qa",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["worker.tasks.process_transcript", "worker.tasks.process_document"],
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
    task_time_limit=1800,
    task_soft_time_limit=1500,
)
