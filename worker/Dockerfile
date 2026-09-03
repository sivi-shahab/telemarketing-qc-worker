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
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Sertifikat cdn.bankmega.local. Server hanya mengirim leaf-nya tanpa CA
# penerbit ("Bank Mega Local Authority"), jadi leaf itu sendiri yang dipercaya
# --- sama seperti trust store host, dan sama dengan image API.
COPY certs/bankmegalocal.crt /usr/local/share/ca-certificates/bankmegalocal.crt
RUN update-ca-certificates

# minio memakai `ca_certs=os.environ.get("SSL_CERT_FILE") or certifi.where()`,
# dan certifi TIDAK membaca trust store sistem -- jadi SSL_CERT_FILE wajib
# ditunjuk eksplisit. Pasangannya `_build_http_client()` di
# core/services/multi_bucket_minio.py yang menyalakan VERIFY_X509_PARTIAL_CHAIN.
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

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
