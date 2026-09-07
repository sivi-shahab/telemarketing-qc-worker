"""
services/s3_buckets.py — akses S3 (AWS SDK / boto3) dengan 1 kredensial per bucket.

Menggantikan SDK ``minio``. Permukaan method-nya sengaja dibuat SAMA PERSIS
dengan client Minio lama (``put_object(bucket, name, data, length=, content_type=)``,
``get_object(bucket, name).read()``, ``list_objects(bucket, prefix=, recursive=)``
yang menghasilkan objek ber-``.object_name``, dst) supaya seluruh pemanggil di
routers dan worker TIDAK perlu diubah — yang berganti hanya mesin di baliknya.

Tiap bucket punya access key/secret sendiri (MINIO_ACCESS_KEY_<BUCKET>), jadi
wrapper ini me-routing tiap panggilan ke client boto3 yang sesuai berdasarkan
argumen ``bucket_name`` pertama.
"""
import logging
import os
import ssl
from typing import Dict, Optional

import boto3
import botocore.httpsession as _httpsession
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# --- TLS: cdn.bankmega.local hanya mengirim sertifikat leaf-nya ---------------
#
# Server tidak mengirim CA penerbit ("Bank Mega Local Authority") dan CA itu tidak
# tersedia di host mana pun; yang dipercaya adalah leaf-nya langsung (lihat
# certs/bankmegalocal.crt yang dipasang Dockerfile). OpenSSL bawaan Python menolak
# leaf tanpa issuer, sementara curl di host lolos karena memakai partial chain.
#
# botocore membangun SSLContext-nya sendiri lewat ``create_urllib3_context`` dan
# TIDAK menyediakan cara resmi mengatur verify_flags, jadi fungsi itu dibungkus di
# sini. Terverifikasi: tanpa pembungkus ini boto3 gagal SSLError; dengan ini,
# list_objects mengembalikan objek sungguhan.
#
# Verifikasi TIDAK dimatikan — CERT_REQUIRED dan pemeriksaan hostname tetap
# menyala. Efeknya sertifikat yang dipin, bukan verify=False.
_ORIGINAL_CREATE_CONTEXT = _httpsession.create_urllib3_context


def _create_context_with_partial_chain(*args, **kwargs):
    ctx = _ORIGINAL_CREATE_CONTEXT(*args, **kwargs)
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    return ctx


_httpsession.create_urllib3_context = _create_context_with_partial_chain


# Urutan field di Settings: (nama bucket, access key, secret key). Bucket yang
# field-nya tidak ada di Settings aplikasi ybs dilewati (lihat getattr di bawah).
_BUCKET_CREDENTIAL_FIELDS = (
    ("minio_bucket_transcripts", "minio_access_key_transcripts", "minio_secret_key_transcripts"),
    ("minio_bucket_results", "minio_access_key_results", "minio_secret_key_results"),
    ("minio_bucket_campaigns", "minio_access_key_campaigns", "minio_secret_key_campaigns"),
    ("minio_bucket_documents", "minio_access_key_documents", "minio_secret_key_documents"),
    ("minio_bucket_audio", "minio_access_key_audio", "minio_secret_key_audio"),
    ("minio_bucket_sales_database", "minio_access_key_sales_database", "minio_secret_key_sales_database"),
)


class CopySource:
    """Pengganti ``minio.commonconfig.CopySource`` dengan bentuk pemanggilan sama."""

    def __init__(self, bucket_name: str, object_name: str):
        self.bucket_name = bucket_name
        self.object_name = object_name


class _S3Object:
    """Satu entri hasil ``list_objects`` — meniru objek minio (``.object_name``)."""

    __slots__ = ("object_name", "size", "last_modified", "etag", "is_dir")

    def __init__(self, object_name, size=0, last_modified=None, etag=None, is_dir=False):
        self.object_name = object_name
        self.size = size
        self.last_modified = last_modified
        self.etag = etag
        self.is_dir = is_dir


class _S3Stat:
    """Hasil ``stat_object`` — meniru objek minio."""

    __slots__ = ("object_name", "size", "content_type", "last_modified", "etag")

    def __init__(self, object_name, size, content_type, last_modified, etag):
        self.object_name = object_name
        self.size = size
        self.content_type = content_type
        self.last_modified = last_modified
        self.etag = etag


class _S3GetResponse:
    """Hasil ``get_object``.

    Pemanggil lama memakai pola urllib3 milik minio: ``.read()`` lalu di blok
    ``finally`` memanggil ``.close()`` dan ``.release_conn()``. StreamingBody boto3
    punya read/close tapi tidak punya release_conn, jadi disediakan sebagai no-op
    supaya pola itu tetap jalan apa adanya.
    """

    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers or {}

    def read(self, *args, **kwargs):
        return self._body.read(*args, **kwargs)

    def stream(self, chunk_size: int = 32 * 1024):
        while True:
            chunk = self._body.read(chunk_size)
            if not chunk:
                return
            yield chunk

    @property
    def data(self):
        return self._body.read()

    def close(self):
        try:
            self._body.close()
        except Exception:
            pass

    def release_conn(self):
        # boto3 mengembalikan koneksi ke pool saat body ditutup; tidak ada padanan.
        return None


class MultiBucketS3Client:
    """Router: tiap method menerima ``bucket_name`` lalu meneruskan ke client
    boto3 milik bucket itu. API-nya identik dengan client Minio lama."""

    def __init__(self, bucket_clients: Dict[str, "boto3.client"]):
        self._clients = bucket_clients

    def is_configured(self) -> bool:
        return bool(self._clients)

    def _client_for(self, bucket_name: str):
        client = self._clients.get(bucket_name)
        if client is None:
            raise ValueError(
                f"[s3] Bucket '{bucket_name}' tidak punya kredensial di Settings "
                f"(yang terdaftar: {sorted(self._clients)})"
            )
        return client

    # --- tulis ---
    def put_object(self, bucket_name, object_name, data, length=None,
                   content_type="application/octet-stream", **kwargs):
        body = data.read() if hasattr(data, "read") else data
        params = {"Bucket": bucket_name, "Key": object_name, "Body": body}
        if content_type:
            params["ContentType"] = content_type
        return self._client_for(bucket_name).put_object(**params)

    def copy_object(self, bucket_name, object_name, source, **kwargs):
        src = {"Bucket": getattr(source, "bucket_name", bucket_name),
               "Key": source.object_name}
        return self._client_for(bucket_name).copy_object(
            Bucket=bucket_name, Key=object_name, CopySource=src
        )

    def remove_object(self, bucket_name, object_name, **kwargs):
        return self._client_for(bucket_name).delete_object(
            Bucket=bucket_name, Key=object_name
        )

    # --- baca ---
    def get_object(self, bucket_name, object_name, **kwargs):
        resp = self._client_for(bucket_name).get_object(
            Bucket=bucket_name, Key=object_name
        )
        return _S3GetResponse(resp["Body"], resp.get("ResponseMetadata", {}).get("HTTPHeaders"))

    def fget_object(self, bucket_name, object_name, file_path, **kwargs):
        self._client_for(bucket_name).download_file(bucket_name, object_name, file_path)
        return file_path

    def list_objects(self, bucket_name, prefix=None, recursive=False, **kwargs):
        client = self._client_for(bucket_name)
        params = {"Bucket": bucket_name}
        if prefix:
            params["Prefix"] = prefix
        if not recursive:
            # minio: non-recursive berhenti di "folder" berikutnya.
            params["Delimiter"] = "/"
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(**params):
            for cp in page.get("CommonPrefixes", []) or []:
                yield _S3Object(cp["Prefix"], is_dir=True)
            for obj in page.get("Contents", []) or []:
                yield _S3Object(obj["Key"], obj.get("Size", 0),
                                obj.get("LastModified"), obj.get("ETag"))

    def stat_object(self, bucket_name, object_name, **kwargs):
        r = self._client_for(bucket_name).head_object(Bucket=bucket_name, Key=object_name)
        return _S3Stat(object_name, r.get("ContentLength", 0),
                       r.get("ContentType"), r.get("LastModified"), r.get("ETag"))

    # --- bucket ---
    def bucket_exists(self, bucket_name) -> bool:
        try:
            self._client_for(bucket_name).head_bucket(Bucket=bucket_name)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchBucket"):
                return False
            raise

    def make_bucket(self, bucket_name, **kwargs):
        return self._client_for(bucket_name).create_bucket(Bucket=bucket_name)


def _endpoint_url(settings) -> str:
    """``cdn.bankmega.local`` (gaya minio, tanpa skema) -> URL utuh untuk boto3."""
    endpoint = str(getattr(settings, "minio_endpoint", "") or "").strip()
    if endpoint.startswith(("http://", "https://")):
        return endpoint
    scheme = "https" if getattr(settings, "minio_secure", False) else "http"
    return f"{scheme}://{endpoint}"


def _region(settings) -> str:
    """Region wajib diisi. MinIO di balik nginx menolak GetBucketLocation
    (``GET /<bucket>?location=``) dengan HTML 403, dan SDK yang mencoba menebak
    region akan gagal di situ sebelum operasi sebenarnya jalan."""
    return (getattr(settings, "minio_region", "")
            or os.getenv("MINIO_REGION", "")
            or "us-east-1")


def _make_client(settings, access_key: str, secret_key: str):
    return boto3.client(
        "s3",
        endpoint_url=_endpoint_url(settings),
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=_region(settings),
        # Wajib path-style: bucket sebagai prefix path, BUKAN subdomain —
        # ``transcripts.cdn.bankmega.local`` tidak ada di DNS.
        config=Config(
            signature_version="s3v4",
            s3={
                # Wajib path-style: bucket sebagai prefix path, BUKAN subdomain --
                # ``transcripts.cdn.bankmega.local`` tidak ada di DNS.
                "addressing_style": "path",
                # Payload TIDAK ikut ditandatangani (UNSIGNED-PAYLOAD). nginx di
                # depan CDN meneruskan body apa adanya; signature streaming
                # membuat perhitungan hash tidak cocok di sisi MinIO.
                "payload_signing_enabled": False,
            },
            connect_timeout=5,
            read_timeout=30,
        ),
        # Bukan verify=False: sertifikat leaf cdn.bankmega.local dipasang di image
        # dan diverifikasi lewat partial chain (lihat patch di atas), jadi
        # CERT_REQUIRED + cek hostname tetap menyala.
        verify=os.environ.get("SSL_CERT_FILE") or True,
    )


def build_s3_client(settings) -> MultiBucketS3Client:
    """Bangun satu client boto3 per bucket, masing-masing dengan kredensialnya."""
    bucket_clients: Dict[str, object] = {}
    for bucket_field, access_key_field, secret_key_field in _BUCKET_CREDENTIAL_FIELDS:
        # getattr, BUKAN akses langsung: tiap aplikasi punya Settings sendiri
        # (API lengkap, worker lebih sedikit) dan bucket yang tidak dikenal
        # Settings itu memang tidak dipakai di sana.
        bucket_name = getattr(settings, bucket_field, "")
        access_key = getattr(settings, access_key_field, "")
        secret_key = getattr(settings, secret_key_field, "")
        if not bucket_name:
            continue
        if not access_key or not secret_key:
            logger.warning(
                "[s3] Kredensial kosong untuk bucket '%s' -- dilewati (akan error "
                "jelas kalau dipakai, bukan salah diam-diam).", bucket_name,
            )
            continue
        bucket_clients[bucket_name] = _make_client(settings, access_key, secret_key)
        logger.info("[s3] Bucket '%s' -> access_key='%s' @ %s",
                    bucket_name, access_key, _endpoint_url(settings))
    return MultiBucketS3Client(bucket_clients)


# Nama lama dipertahankan supaya api/dependencies.py dan worker tidak perlu diubah.
build_minio_client = build_s3_client
