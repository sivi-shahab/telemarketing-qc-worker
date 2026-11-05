"""Penyimpanan draf Builder di MinIO (bucket campaign, awalan ``_builder/``): tanpa tabel DB baru.

Satu draf = satu folder ``_builder/{id}/`` berisi ``draft.json`` dan berkas sumber (script, scorecard, gambar).
Folder ini bukan campaign: daftar campaign dibaca dari DB, bukan dari bucket.
"""
import io
import json
import re
import uuid
from datetime import datetime, timezone

PREFIX = "_builder/"
_ID = re.compile(r"^[0-9a-f]{32}$")


def new_id() -> str:
    return uuid.uuid4().hex


def valid_id(draft_id: str) -> bool:
    return bool(_ID.match(draft_id or ""))


def _key(draft_id: str, name: str = "draft.json") -> str:
    if not valid_id(draft_id):
        raise ValueError("id draf tidak valid")
    return f"{PREFIX}{draft_id}/{name}"


def put_bytes(client, bucket: str, draft_id: str, name: str, data: bytes, content_type: str = "application/octet-stream"):
    client.put_object(bucket, _key(draft_id, name), io.BytesIO(data), length=len(data), content_type=content_type)


def get_bytes(client, bucket: str, draft_id: str, name: str) -> bytes:
    r = client.get_object(bucket, _key(draft_id, name))
    try:
        return r.read()
    finally:
        r.close()
        r.release_conn()


def save(client, bucket: str, draft: dict) -> None:
    draft["updated_at"] = datetime.now(timezone.utc).isoformat()
    put_bytes(client, bucket, draft["id"], "draft.json", json.dumps(draft, ensure_ascii=False).encode("utf-8"),
              "application/json")


def load(client, bucket: str, draft_id: str):
    try:
        return json.loads(get_bytes(client, bucket, draft_id, "draft.json"))
    except Exception:  # noqa: BLE001 -- objek tidak ada / bukan JSON
        return None


def list_drafts(client, bucket: str) -> list:
    out = []
    for obj in client.list_objects(bucket, prefix=PREFIX, recursive=False):
        draft_id = obj.object_name[len(PREFIX):].strip("/")
        d = load(client, bucket, draft_id) if valid_id(draft_id) else None
        if d:
            out.append({k: d.get(k) for k in ("id", "name", "created_by", "created_at", "updated_at", "status")} |
                       {"items": len(d.get("items") or []), "script_filename": (d.get("script") or {}).get("filename")})
    return sorted(out, key=lambda x: x.get("updated_at") or "", reverse=True)


def delete(client, bucket: str, draft_id: str) -> int:
    n = 0
    for obj in client.list_objects(bucket, prefix=f"{PREFIX}{draft_id}/", recursive=True):
        client.remove_object(bucket, obj.object_name)
        n += 1
    return n
