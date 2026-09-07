"""Konfigurasi milik core — sengaja minimal.

Sebelum pemisahan repo, `sales_lookup` mengambil MinIO client dan Settings dari
``api.dependencies``. Itu membuat core (dan lewat ``compliance.stats_aggregate``
juga worker) tidak bisa jalan tanpa folder ``api/``. Modul ini menggantikannya
dengan konfigurasi milik core sendiri: HANYA field yang benar-benar dipakai kode
bersama, dibaca dari environment / ``.env`` yang sama persis dengan API dan
worker, sehingga nilainya identik tanpa perlu saling impor.

Nama file sengaja ``core_config`` (bukan ``config``) karena isi core di-copy
datar ke ``/app`` di image API dan worker — ``config.py`` terlalu umum dan
berisiko bentrok dengan modul lain di root.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings

from services.s3_buckets import build_minio_client


class CoreSettings(BaseSettings):
    """Subset Settings yang dipakai kode bersama (default sama dengan API)."""

    minio_endpoint: str = "minio:4003"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "changeme123"
    # API memakai secure=False (MinIO internal via http). Dibuat env-overridable
    # supaya deployment lewat CDN/https tidak perlu ubah kode.
    minio_secure: bool = False
    minio_bucket_sales_database: str = "sales-database"
    # Kredensial khusus bucket sales-database (deployment CDN). Kalau kosong,
    # build_minio_client() jatuh ke minio_access_key/minio_secret_key di atas.
    minio_access_key_sales_database: str = ""
    minio_secret_key_sales_database: str = ""

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"


@lru_cache()
def get_core_settings() -> CoreSettings:
    return CoreSettings()


_minio_client = None


def get_minio():
    """MinIO client milik core (lazy, satu instance per proses).

    Bisa berupa ``Minio`` biasa atau ``MultiBucketMinioClient`` — keduanya
    dipakai dengan cara yang sama, lihat ``build_minio_client()``.
    """
    global _minio_client
    if _minio_client is None:
        _minio_client = build_minio_client(get_core_settings())
    return _minio_client
