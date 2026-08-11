# telemarketing-qc-worker

Celery worker sistem QC Telemarketing: evaluasi transkrip lewat LLM dan OCR
dokumen pendukung. Termasuk Flower (monitoring) di port `4005`.

Worker dan Flower memakai **satu image yang sama** — bedanya cuma `command`.

## Repo terkait

| Repo | Isi |
|---|---|
| `telemarketing-qc-core` | Kode bersama (`db`, `compliance`, `services`, `prompt`, `sales_lookup`) — submodule di `core/` |
| `telemarketing-qc-api` | FastAPI + migrasi Alembic (pemilik skema) |
| `telemarketing-qc-dashboard` | Frontend Vue 3 |

## Setup

```bash
git clone <URL-repo-ini> && cd telemarketing-qc-worker
git submodule update --init --recursive     # WAJIB — mengisi core/
cp .env.example .env                        # isi; nilainya harus sama dgn .env API
docker compose up -d --build
```

Infra (postgres, redis, minio) **tidak** dijalankan dari sini — semuanya ada di
compose repo API. Keduanya bertemu di network eksternal `qc-net`
(`docker network create qc-net`, sekali per host). Karena beda compose file, di
sini tidak ada `depends_on`; worker cukup `restart: unless-stopped` sampai infra
siap.

## Task

| Task | Pemicu |
|---|---|
| `worker.tasks.process_transcript.process_transcript` | upload transkrip / webhook |
| `worker.tasks.process_document.process_document` | upload dokumen pendukung / webhook |

API mengirimnya **by name** (`celery_app.send_task(...)`), jadi API tidak perlu
kode repo ini — cukup broker URL yang sama.

## Migrasi

Worker **tidak pernah** menjalankan `alembic upgrade`. Kalau sebuah rilis
mengubah skema, deploy job API dulu (migrasi), baru worker.

## Scripts

Dua script maintenance yang butuh runtime worker (akses MinIO + pdfplumber +
session factory milik task):

```bash
docker compose exec worker python /app/scripts/backfill_generated_at.py --dry-run
docker compose exec worker python /app/scripts/backfill_reference_cashline_ids.py --dry-run
```

Keduanya pindah ke sini dari repo API justru karena dependensi itu — kalau
ditinggal di repo API, repo API jadi butuh kode worker.

## Development tanpa Docker

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r core/requirements.txt -r worker/requirements.txt
PYTHONPATH=.:core celery -A worker.celery_app worker --loglevel=info
```
