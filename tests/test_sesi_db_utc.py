"""Sesi DB worker dipaksa ``timezone=UTC`` supaya ``server_default=now()`` menulis UTC,
sama dengan ``_utcnow()`` worker dan konvensi "simpan UTC, tampilkan WIB" (lihat
repo api ``tests/test_simpan_utc.py``). Tanpa ini Postgres produksi (TimeZone
Asia/Jakarta) menulis WIB."""
from worker.config import get_worker_settings


def test_connect_args_memaksa_timezone_utc():
    opts = get_worker_settings().db_connect_args.get("options", "")
    assert "-ctimezone=UTC" in opts


def test_connect_args_utc_walau_tanpa_schema(monkeypatch):
    s = get_worker_settings().model_copy(update={"postgres_schema": ""})
    assert "-ctimezone=UTC" in s.db_connect_args.get("options", "")
