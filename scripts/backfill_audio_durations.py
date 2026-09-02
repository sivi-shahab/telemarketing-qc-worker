"""Isi ``result_json["audio_durations"]`` untuk tiket yang sudah terlanjur diproses.

Kolom Call Duration tata letak Demo menyebut durasi TIAP panggilan berikut nama
PDF-nya. Sebelum 27 Agustus 2026 worker hanya menyimpan totalnya
(``audio_duration``), jadi tiket lama tidak punya rinciannya. Skrip ini
menghitungnya ulang dengan mengunduh PDF asli dari MinIO dan mem-parse-nya lagi —
sumber angkanya sama persis dengan yang dipakai worker (``build_transcript``).

Yang TIDAK disentuh: ``audio_duration`` (total), evaluasi, skor, status. Kalau
total hasil hitung ulang berbeda dari yang tersimpan, skrip hanya memperingatkan
dan tetap membiarkan nilai lama — tiket lama tidak boleh berubah nilainya karena
alasan tampilan.

Salinan JSON di bucket MinIO ``results`` sengaja tidak ikut ditulis: yang dibaca
API adalah baris ``result_data`` di PostgreSQL, dan memproses ulang sebuah tiket
akan menulis keduanya dari awal.

Jalankan di dalam container worker (di sanalah pdfplumber & kredensial MinIO ada):

    docker compose exec worker python -m scripts.backfill_audio_durations [--dry-run]
"""
import argparse
import os
import shutil
import sys

from sqlalchemy.orm.attributes import flag_modified

from compliance.pdf_parser import build_transcript
from db.models import Result, ResultData
from worker.tasks.process_transcript import (
    TMP_ROOT,
    _download_transcripts,
    _session_factory,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="hitung saja, jangan tulis")
    args = ap.parse_args()

    db = _session_factory()()
    rows = (
        db.query(Result, ResultData)
        .join(ResultData, ResultData.result_id == Result.id)
        .filter(Result.status == "done")
        .order_by(Result.uploaded_at)
        .all()
    )

    todo = [
        (r, d)
        for r, d in rows
        if isinstance(d.result_json, dict) and not d.result_json.get("audio_durations")
    ]
    print(f"{len(rows)} tiket selesai, {len(todo)} belum punya audio_durations")

    filled = skipped = mismatched = 0
    for result, data in todo:
        rid = str(result.id)
        try:
            paths = _download_transcripts(rid)
            if not paths:
                print(f"  LEWAT {rid}: tidak ada PDF di MinIO")
                skipped += 1
                continue
            _files, _messages, total, per_file = build_transcript(paths)
        except Exception as exc:  # noqa: BLE001 — satu tiket gagal tidak menghentikan sisanya
            print(f"  GAGAL {rid}: {type(exc).__name__}: {exc}")
            skipped += 1
            continue
        finally:
            shutil.rmtree(os.path.join(TMP_ROOT, rid), ignore_errors=True)

        stored = data.result_json.get("audio_duration")
        if stored and stored != total:
            print(f"  BEDA  {rid}: total tersimpan {stored!r} vs hitung ulang {total!r} — total dibiarkan")
            mismatched += 1

        if not args.dry_run:
            data.result_json["audio_durations"] = per_file
            flag_modified(data, "result_json")
        filled += 1
        print(f"  OK    {rid}: {len(per_file)} panggilan")

    if not args.dry_run:
        db.commit()
    print(
        f"selesai — terisi {filled}, dilewati {skipped}, total berbeda {mismatched}"
        + (" (DRY RUN, tidak ada yang ditulis)" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
