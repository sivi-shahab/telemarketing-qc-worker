"""Backfill ``agent_id`` + ``submit_time`` ke ``reference_data.cashline`` hasil lama.

Statistics memetakan tiket ke agen lewat ``crud.cashline_agent_index()``, yang
membaca ``result_data.result_json -> reference_data -> cashline`` -- satu query
SQL, tanpa HTTP. Hasil evaluasi yang dibuat SEBELUM Aplikasi A menyimpan
``agent_id``/``submit_time`` di cache-nya tidak punya kedua field itu, jadi tiap
Statistics recompute, index harus jatuh ke fallback DWH API untuk baris-baris
tersebut.

Script ini menambal baris lama itu sekali jalan supaya fallback berhenti
terpanggil. MURNI OPTIMASI -- angka Statistics sudah benar tanpa ini, karena
fallback memang menutup celahnya.

Yang ditulis HANYA dua field itu, dan HANYA kalau nilainya belum ada; sisa isi
``reference_data`` tidak disentuh. Salinan JSON di MinIO ``results/{id}.json``
ikut diperbarui supaya tidak berbeda dari Postgres (pakai ``--skip-minio`` untuk
melewatinya).

Jalankan di dalam container ``worker`` (punya akses DB + MinIO + DWH API)::

    docker compose exec worker python //app/scripts/backfill_reference_cashline_ids.py --dry-run
    docker compose exec worker python //app/scripts/backfill_reference_cashline_ids.py

``--dry-run`` melaporkan tanpa menulis apa pun (DB di-rollback, MinIO dilewati).
"""
import argparse
import io
import json
import sys

sys.path.insert(0, "/app")

from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from db.models import Result, ResultData  # noqa: E402
from services import data_dwh  # noqa: E402
from worker.config import get_worker_settings  # noqa: E402
from worker.tasks.process_transcript import _minio_client, _session_factory  # noqa: E402

_FIELDS = ("agent_id", "submit_time")


def _customer_id(source_files):
    """Customer/session id = prefix sebelum ``_`` pada source file pertama.

    Sama persis dengan ``_customer_id()`` di compliance/stats_aggregate.py dan
    api/routers/agent_error.py -- kalau yang di sana berubah, ubah juga di sini.
    """
    if not source_files:
        return None
    first = source_files[0]
    if not isinstance(first, str) or not first:
        return None
    return first.split("_", 1)[0].strip() or None


def _needs_backfill(result_json) -> bool:
    """True kalau snapshot cashline-nya belum memuat agent_id/submit_time."""
    if not isinstance(result_json, dict):
        return False
    cashline = ((result_json.get("reference_data") or {}).get("cashline")) or {}
    if not isinstance(cashline, dict):
        return False
    return any(not str(cashline.get(f) or "").strip() for f in _FIELDS)


def _mirror_to_minio(result_id: str, payload: dict) -> None:
    """Tulis ulang results/{result_id}.json supaya sama dengan Postgres."""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    _minio_client().put_object(
        get_worker_settings().minio_bucket_results,
        f"{result_id}.json",
        io.BytesIO(raw),
        length=len(raw),
        content_type="application/json",
    )


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dry-run", action="store_true", help="laporkan tanpa menulis")
    ap.add_argument("--skip-minio", action="store_true",
                    help="hanya perbarui Postgres, jangan sentuh salinan MinIO")
    args = ap.parse_args()

    db = _session_factory()()
    try:
        rows = (
            db.query(ResultData, Result.source_files)
            .join(Result, Result.id == ResultData.result_id)
            .order_by(ResultData.created_at)
            .all()
        )
        todo = [(rd, sf) for rd, sf in rows if _needs_backfill(rd.result_json)]
        print(f"Backfill reference_data.cashline: {len(todo)} dari {len(rows)} baris "
              f"perlu ditambal" + (" [DRY RUN]" if args.dry_run else ""))

        patched = no_cid = no_dwh = 0
        for rd, source_files in todo:
            rid = str(rd.result_id)
            cid = _customer_id(source_files)
            if not cid:
                no_cid += 1
                print(f"  {rid}: source_files tidak punya customer id (dilewati)")
                continue

            try:
                cashline = data_dwh.fetch_bundle(cid).get("cashline") or {}
            except Exception as exc:  # noqa: BLE001 — satu kegagalan jangan hentikan sisanya
                no_dwh += 1
                print(f"  {rid} (cid {cid}): DWH error -- {exc} (dilewati)")
                continue

            found = {f: (cashline.get(f) or "").strip() for f in _FIELDS}
            if not found["agent_id"]:
                no_dwh += 1
                print(f"  {rid} (cid {cid}): DWH tidak punya agent_id (dilewati)")
                continue

            # Tulis HANYA field yang masih kosong -- jangan timpa data yang sudah ada.
            target = rd.result_json["reference_data"]["cashline"]
            written = {
                f: v for f, v in found.items()
                if v and not str(target.get(f) or "").strip()
            }
            if not written:
                continue
            target.update(written)
            patched += 1
            print(f"  {rid} (cid {cid}): {written}")

            if not args.dry_run:
                # result_json JSONB dimutasi di tempat; tanpa flag_modified,
                # SQLAlchemy tidak menganggap baris ini kotor dan tidak menulisnya.
                flag_modified(rd, "result_json")
                if not args.skip_minio:
                    try:
                        _mirror_to_minio(rid, rd.result_json)
                    except Exception as exc:  # noqa: BLE001
                        print(f"    ! gagal mirror ke MinIO: {exc} "
                              f"(Postgres tetap ditulis)")

        if args.dry_run:
            db.rollback()
        else:
            db.commit()
        print(f"Selesai. ditambal={patched} tanpa_cid={no_cid} tanpa_data_dwh={no_dwh} "
              f"{'(DRY RUN, dibatalkan)' if args.dry_run else '(commit)'}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
