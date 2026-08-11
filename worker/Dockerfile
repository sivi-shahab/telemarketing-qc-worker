# Image worker — self-contained, TIDAK mengandalkan bind-mount `.:/app`.
# Image yang sama dipakai service `worker` dan `flower` (bedanya cuma command).
# Build context = root repo (submodule `core/` harus sudah ter-checkout).
#
#   docker build -f worker/Dockerfile -t qc-worker .
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY core/requirements.txt   /tmp/core-requirements.txt
COPY worker/requirements.txt /tmp/worker-requirements.txt
RUN pip install --no-cache-dir -r /tmp/core-requirements.txt -r /tmp/worker-requirements.txt

# Isi core di-COPY DATAR ke /app supaya import tetap `from db import crud`.
# `prompt/` ikut karena compliance/documents.py me-resolve-nya lewat
# importlib.import_module("prompt.ocr_ktp") — kalau tidak ada, error-nya baru
# muncul saat runtime OCR, bukan saat build.
COPY core/db              /app/db
COPY core/compliance      /app/compliance
COPY core/services        /app/services
COPY core/prompt          /app/prompt
COPY core/sales_lookup.py /app/sales_lookup.py
COPY core/core_config.py  /app/core_config.py

COPY worker  /app/worker
COPY scripts /app/scripts

# Tempat kerja unduhan transkrip/dokumen sebelum diproses.
RUN mkdir -p /tmp/audio

CMD ["celery", "-A", "worker.celery_app", "worker", "--loglevel=info"]
