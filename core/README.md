# telemarketing-qc-core

Kode bersama untuk `telemarketing-qc-api` dan `telemarketing-qc-worker`.
Repo ini **library** — tidak punya Dockerfile dan tidak menghasilkan image.

## Isi

| Paket | Fungsi |
|---|---|
| `db/` | SQLAlchemy models + `crud` (satu-satunya akses DB) |
| `compliance/` | Evaluator, scoring, error codes, reference data, OCR, PDF parser, agregasi statistik |
| `services/` | Klien DWH API (`data_dwh`) dan MinIO multi-bucket |
| `prompt/` | Prompt OCR (KTP/KK/NPWP/cover buku tabungan), di-`importlib` dari `compliance/documents.py` |
| `sales_lookup.py` | Lookup sales database aktif (xlsx di MinIO): nama agent, join date, new joiner, hierarki TL/AM |
| `core_config.py` | Konfigurasi milik core (MinIO + bucket sales database) |

## Aturan dependensi

Core **tidak boleh** meng-import `api` maupun `worker`. Arah panah selalu
`api → core` dan `worker → core`. Stage `No Leaking Imports` di Jenkinsfile
menegakkan aturan ini.

Dulu `sales_lookup.py` ada di `api/` dan meng-import `api.dependencies`, sehingga
worker tidak bisa jalan tanpa folder `api/`. Sekarang settings MinIO yang dipakai
kode bersama ada di `core_config.py`, membaca environment/`.env` yang sama dengan
API dan worker — jadi nilainya identik tanpa saling impor.

## Dipasang di repo lain

Sebagai git submodule di direktori `core/`:

```bash
git submodule add <URL-repo-ini> core
git commit -m "chore: tambah core sebagai submodule"
```

Saat build image, isi core di-`COPY` **datar** ke `/app` (`core/db` → `/app/db`,
`core/sales_lookup.py` → `/app/sales_lookup.py`, dst.), jadi import-nya tetap
`from db import crud`, bukan `from core.db import crud`. Untuk menjalankan di
luar Docker, cukup tambahkan `core/` ke `PYTHONPATH`:

```bash
PYTHONPATH=.:core pytest -q
```

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=. python -c "import sales_lookup, db.crud, compliance.stats_aggregate"
```

## Catatan

- `services/` sengaja tidak punya `__init__.py` (namespace package). Kalau nanti
  core dikemas jadi paket pip, tambahkan `__init__.py` di sana lebih dulu.
- Perubahan di repo ini otomatis memicu build API dan worker (lihat `Jenkinsfile`).
  Karena keduanya memakai submodule, mereka baru ikut berubah setelah pointer
  submodule di-update dan di-commit di repo masing-masing.
