# Deployment — telemarketing-qc-worker

Panduan menyalakan **worker** (Celery) dan **flower**. Panduan repo lain:
`telemarketing-qc-api/deployment_guidelines.md` dan
`telemarketing-qc-dashboard/deployment_guidelines.md`.

> **Nyalakan API lebih dulu.** Repo ini TIDAK menjalankan migrasi Alembik — API
> pemilik tunggalnya — dan broker Redis yang dipakai worker hidup di compose
> repo API. Worker yang naik duluan akan terus gagal dan mencoba lagi
> (`restart: unless-stopped`) sampai keduanya siap.

---

## 1. Prasyarat

### Network `qc-net`

Sama dengan repo lain, dibuat sekali di host:

```bash
docker network create qc-net
```

Compose di sini memakai `external: true`, jadi tidak akan membuatkannya sendiri.

### Submodule `core`

Isi `db/`, `compliance/`, `services/`, `prompt/`, `sales_lookup.py` datang dari
submodule — bukan dari repo ini.

```bash
git submodule update --init --recursive
git submodule status          # '-' = belum checkout, '+' = beda commit
```

**Wajib satu commit dengan repo API.** Kalau berbeda, worker dan API memakai
versi `crud`/`models`/`s3_buckets` yang tidak sama — gejalanya bisa berupa
`ModuleNotFoundError` saat start, atau yang lebih buruk: perilaku berbeda diam-diam
di dua proses yang seharusnya sepakat.

Menyamakan dengan core terbaru:

```bash
git -C core fetch origin <branch> && git -C core merge --ff-only FETCH_HEAD
git add core && git commit -m "bump core"
```

### Berkas `.env`

Tidak ikut Git, dan **nilainya harus sama dengan `.env` repo API** untuk
Postgres, Redis, S3, DWH, dan LLM. Kalau berbeda, worker menulis ke tempat yang
berbeda dari yang dibaca API.

Yang khusus di sini: `CELERY_CONCURRENCY` (default 8).

Sertifikat CDN (`certs/bankmegalocal.crt`) ikut di repo dan dipasang Dockerfile
ke trust store image, sama seperti API. Jangan menggantinya dengan `verify=False`.

---

## 2. Menyalakan

```bash
cd /data/scorecard_v2/telemarketing-qc-worker
docker compose up -d --build
```

Dua service dari **image yang sama**, hanya beda `command`:

| Service | Perintah | Port |
|---|---|---|
| `worker` | `celery ... worker --concurrency=${CELERY_CONCURRENCY:-8}` | — |
| `flower` | `celery ... flower --port=4005` | 4005 |

Tidak ada `depends_on` ke Postgres/Redis karena keduanya di compose file lain.
Penggantinya `restart: unless-stopped`.

> **Flower terbuka tanpa autentikasi.** `FLOWER_UNAUTHENTICATED_API=true` wajib
> di-set karena Flower 2.x membalas 401 untuk `/api/*` tanpa itu, dan tidak ada
> flag CLI-nya. Batasi port 4005 di level firewall.

Catatan saat menyunting compose: blok `environment` milik `flower` **menimpa**
milik anchor, bukan menggabung — jadi `PYTHONPATH` dan `DWH_API_BASE_URL` harus
ditulis ulang di sana.

---

## 3. Verifikasi

```bash
docker logs --since 2m telemarketing-qc-worker-worker-1 | grep -iE "ready|Connected"
```

Sehat kalau muncul dua baris ini:

```
Connected to redis://redis:6378/0
celery@<hostname> ready.
```

Koneksi DB (memakai schema yang sama dengan API):

```bash
docker exec telemarketing-qc-worker-worker-1 python -c "
from worker.config import get_worker_settings
from sqlalchemy import create_engine, text
s = get_worker_settings()
e = create_engine(s.database_url, connect_args={'options': f'-csearch_path={s.postgres_schema},public'} if s.postgres_schema else {})
with e.connect() as c:
    print('user       :', c.execute(text('select current_user')).scalar())
    print('search_path:', c.execute(text('show search_path')).scalar())
"
```

Koneksi S3:

```bash
docker exec telemarketing-qc-worker-worker-1 python -c "
from worker.config import get_worker_settings
from services.s3_buckets import build_minio_client
s = get_worker_settings(); c = build_minio_client(s)
for f in ['minio_bucket_transcripts','minio_bucket_results','minio_bucket_documents']:
    b = getattr(s, f, '')
    if b: print(f'{b:20s} exists={c.bucket_exists(b)}')
"
```

Flower:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:4005/    # 200
```

---

## 4. Menyetel jumlah pekerja

```bash
# .env
CELERY_CONCURRENCY=8
```

Lalu `docker compose up -d worker` (tidak perlu rebuild — nilainya dibaca saat
container dibuat, bukan saat build).

---

## 5. Yang BUKAN tanggung jawab worker

Celery worker di repo ini menangani pipeline transkrip/dokumen/reproses. Ia
**tidak** menyentuh alur audio → STT sama sekali.

Audio diproses oleh dua daemon di luar ketiga repo:

```
/data/script_antrian/producer_watch.py    inotify pada /data/recording
/data/script_antrian/consumer_worker.py   POST ke :8000 (GPU0) / :8001 (GPU1)
```

Keduanya berjalan sebagai root dan **tidak** dikelola compose mana pun. Kalau
unggahan audio tidak pernah selesai, periksa di sana lebih dulu — bukan di log
Celery:

```bash
ps aux | grep -E "producer_watch|consumer_worker" | grep -v grep
ls -la /data/recording/         # menumpuk = consumer tidak jalan
```

Menambah `CELERY_CONCURRENCY` tidak akan mempercepat transkripsi audio; yang
membatasi adalah `MAX_INFLIGHT` di producer dan kapasitas dua GPU.

---

## 6. Masalah yang pernah terjadi

| Gejala | Sebab & penanganan |
|---|---|
| `ModuleNotFoundError: services.s3_buckets` | Submodule `core` tertinggal dari repo API. Samakan commit-nya lalu rebuild. |
| Worker restart terus di awal | Redis/DB belum siap karena API belum dinyalakan. Nyalakan API dulu; ini memang perilaku yang diinginkan, bukan kerusakan. |
| Flower 401 di `/api/*` | `FLOWER_UNAUTHENTICATED_API` hilang dari blok `environment` milik flower — ingat blok itu menimpa anchor, bukan menggabung. |
| Task jalan tapi hasilnya tidak terlihat di dashboard | `.env` worker dan API menunjuk `POSTGRES_SCHEMA` atau bucket yang berbeda. Samakan. |
| `CERTIFICATE_VERIFY_FAILED` saat akses CDN | Sertifikat tidak terpasang di image, atau `SSL_CERT_FILE` tidak menunjuk bundle hasil `update-ca-certificates`. |
| Compose gagal: network `qc-net` not found | `docker network create qc-net`. |
