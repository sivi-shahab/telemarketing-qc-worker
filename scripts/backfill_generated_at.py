"""Backfill ``results.generated_at`` from each ticket's transcript PDFs.

The AI-status chart (Statistics tab) dates a ticket by the latest ``Generated``
timestamp in its transcript PDFs (see ``compliance/pdf_parser.py``). New tickets
get this filled by the worker on processing; this script populates rows created
before that wiring existed.

Run inside the ``worker`` container (it has MinIO access + pdfplumber)::

    docker compose exec worker python //app/scripts/backfill_generated_at.py
    docker compose exec worker python //app/scripts/backfill_generated_at.py --all --dry-run

By default only rows with ``generated_at IS NULL`` are touched. ``--all`` re-parses
every row. ``--dry-run`` reports without committing.
"""
import argparse
import shutil
import sys

sys.path.insert(0, "/app")

from compliance.pdf_parser import latest_generated_timestamp  # noqa: E402
from db.models import Result  # noqa: E402
from worker.tasks.process_transcript import (  # noqa: E402
    TMP_ROOT,
    _download_transcripts,
    _session_factory,
)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true",
                    help="re-parse every result, not just generated_at IS NULL")
    ap.add_argument("--dry-run", action="store_true",
                    help="report without writing")
    args = ap.parse_args()

    db = _session_factory()()
    try:
        q = db.query(Result)
        if not args.all:
            q = q.filter(Result.generated_at.is_(None))
        rows = q.order_by(Result.uploaded_at).all()
        print(f"Backfill generated_at: {len(rows)} kandidat"
              + (" (semua)" if args.all else " (generated_at NULL)")
              + (" [DRY RUN]" if args.dry_run else ""))

        filled = missing = 0
        for r in rows:
            rid = str(r.id)
            try:
                paths = _download_transcripts(rid)
                ts = latest_generated_timestamp(paths) if paths else None
            finally:
                shutil.rmtree(f"{TMP_ROOT}/{rid}", ignore_errors=True)
            if ts is None:
                missing += 1
                print(f"  {rid}: tidak ada 'Generated' (dilewati)")
                continue
            filled += 1
            print(f"  {rid}: generated_at = {ts.isoformat()}")
            if not args.dry_run:
                r.generated_at = ts

        if args.dry_run:
            db.rollback()
        else:
            db.commit()
        print(f"Selesai. terisi={filled} tanpa_generated={missing} "
              f"{'(DRY RUN, dibatalkan)' if args.dry_run else '(commit)'}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
