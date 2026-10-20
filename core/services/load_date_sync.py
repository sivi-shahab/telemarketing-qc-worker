"""Sinkron ``load_date`` App C ke tabel ``ticket_load_dates`` (20 Oktober 2026).

Results & Stats memfilter tanggal berdasarkan ``load_date`` App C (tanggal tiket
dimuat), tapi App C hanya bisa ditanya per tanggal — tidak per daftar tiket. Maka
isi /tickets-daily untuk satu tanggal disalin ke tabel lokal; filter lalu cukup
join ke tabel itu tanpa memanggil App C per request.

Kegagalan App C untuk satu tanggal TIDAK menghapus apa pun: tanggal itu dilewati
(tidak masuk hasil) dan baris lamanya tetap. Layar yang menyimpang karena App C
sedang mati lebih baik daripada filter yang kehilangan tiket.
"""
import logging
from datetime import date, timedelta
from typing import Iterable

from db import crud
from services import tickets_daily

logger = logging.getLogger(__name__)

# Sama dengan batas halaman yang dipakai pemanggil lain; satu tanggal tidak
# mungkin menembusnya (peringatan "terpotong" muncul bila ternyata iya).
MAX_PAGES = 200


def recent_dates(today: date, days_back: int) -> list:
    """``[today - days_back, ..., today]`` — tanggal yang masih bisa berubah di App C."""
    return [today - timedelta(days=i) for i in range(days_back, -1, -1)]


def sync_dates(db, dates: Iterable[date]) -> dict:
    """Salin tiket App C per tanggal ke ``ticket_load_dates``; ``{tanggal: jumlah baris}``.

    Commit per tanggal supaya tanggal yang sudah beres tidak hilang bila tanggal
    berikutnya gagal. Tanggal diproses TERBARU DULU (20 Oktober 2026): tiket baru
    hari ini yang paling dibutuhkan filter (tanpa barisnya jatuh ke aturan lama),
    jadi tanggal lama yang bermasalah tidak boleh menahannya. Kegagalan apa pun
    pada satu tanggal (App C, item rusak, DB) hanya melewati tanggal itu: rollback,
    log lengkap, lanjut ke tanggal berikutnya. Baris lama tidak pernah dihapus.
    """
    result = {}
    for d in sorted(set(dates), reverse=True):
        try:
            page = tickets_daily.fetch_all(load_date=d.isoformat(), max_pages=MAX_PAGES)
        except tickets_daily.TicketsDailyError as exc:
            logger.warning("sinkron load_date %s dilewati, App C gagal: %s", d, exc)
            continue
        except Exception:
            logger.exception("sinkron load_date %s dilewati, error tak terduga saat ambil App C", d)
            continue
        try:
            if page.get("truncated"):
                logger.warning("sinkron load_date %s terpotong di %s halaman; sebagian "
                               "tiket tidak tersimpan", d, MAX_PAGES)
            rows = []
            for item in page.get("items") or []:
                tid, ld = item.get("id"), item.get("load_date")
                if not tid or not ld:
                    continue
                rows.append((tid, date.fromisoformat(str(ld)[:10]), item.get("campaign")))
            count = crud.upsert_ticket_load_dates(db, rows)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("sinkron load_date %s dilewati, gagal diproses/disimpan", d)
            continue
        result[d] = count
    return result
