"""Celery task: reproses SATU ticket id.

Dipakai dua pintu masuk, dengan alur yang sama persis:

* menu **Reprocess All Ticket** — satu job berisi semua tiket sebuah campaign
  (``reprocess_jobs.scope = 'campaign'``);
* tombol **Reprocess** pada kolom Action di menu **Results** — satu job berisi tepat
  satu tiket (``scope = 'ticket'``).

Satu task = satu unique ticket id = satu ``ReprocessJobItem``. Alurnya per tiket:

  1. lewati bila job sudah dibatalkan (status item -> ``skipped``)
  2. salin transkrip PDF row lama TERBARU ke prefix row BARU di MinIO
  3. buat row Result baru (campaign sama; konfigurasi campaign-nya dibaca ulang dari
     DB oleh ``process_transcript``, jadi otomatis memakai prompt/scorecard/KB yang
     berlaku sekarang)
  4. jalankan ``process_transcript`` SEGARIS (bukan dikirim ke antrean lagi) supaya
     langkah 5 tahu persis kapan evaluasinya selesai
  5. HANYA bila row baru berstatus ``done``: hapus seluruh row lama ticket itu,
     sehingga tersisa tepat satu row per unique id
  6. bila gagal: row baru dibuang dan row LAMA dipertahankan apa adanya — sebuah
     ticket id tidak pernah berakhir tanpa hasil sama sekali

Hasil evaluasi baru sengaja BERSIH: banding error code, usulan/approval Manual
Status, dan dokumen pendukung milik row lama TIDAK ikut disalin (keputusan 20
Agustus 2026). Alasannya, error code hasil evaluasi baru bisa berbeda total dari
yang dibandingkan dulu. Ini perilaku yang sama dengan mode "⟳ Proses ulang (LLM)"
di menu Upload Transcript.

Berkas transkrip & JSON hasil milik row lama TIDAK dihapus dari MinIO, sama seperti
tombol Delete tiket yang sudah ada (``/delete_ticket``) — yang dihapus hanya
row-nya di database.
"""
import logging
from datetime import datetime, timezone
from functools import lru_cache

from minio.commonconfig import CopySource
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import crud
from worker.celery_app import celery_app
from worker.config import get_worker_settings

logger = logging.getLogger(__name__)


@lru_cache()
def _session_factory():
    settings = get_worker_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _copy_transcripts(client, bucket: str, src_id: str, dst_id: str) -> int:
    """Salin semua PDF ``{src_id}/`` -> ``{dst_id}/`` di bucket transkrip.

    Disalin, bukan dipindah: row lama harus tetap utuh sampai row barunya benar-benar
    ``done``, karena kalau reproses gagal row lama itulah yang dipertahankan.
    """
    copied = 0
    for obj in client.list_objects(bucket, prefix=f"{src_id}/", recursive=True):
        name = obj.object_name.rsplit("/", 1)[-1]
        if not name.lower().endswith(".pdf"):
            continue
        client.copy_object(bucket, f"{dst_id}/{name}", CopySource(bucket, obj.object_name))
        copied += 1
    return copied


def _remove_transcripts(client, bucket: str, result_id: str) -> None:
    """Buang salinan transkrip milik row baru yang gagal.

    Hanya menyentuh objek di prefix row BARU — salinan itu dibuat beberapa detik
    sebelumnya oleh task ini sendiri. Transkrip row lama tidak pernah disentuh.
    """
    for obj in client.list_objects(bucket, prefix=f"{result_id}/", recursive=True):
        client.remove_object(bucket, obj.object_name)


@celery_app.task(name="worker.tasks.reprocess_ticket.reprocess_ticket")
def reprocess_ticket(item_id: int):
    from worker.tasks.process_transcript import _minio_client, process_transcript

    settings = get_worker_settings()
    Session = _session_factory()
    db = Session()
    new_result_id = None
    try:
        item = crud.get_reprocess_item(db, item_id)
        if item is None:
            logger.warning("reprocess item %s tidak ditemukan", item_id)
            return {"item_id": item_id, "status": "missing"}
        job_id = str(item.job_id)

        # Item yang sudah tidak `pending` berarti sudah dikerjakan (mis. task
        # dikirim ulang setelah worker mati) — jangan proses dua kali.
        if item.status != "pending":
            return {"item_id": item_id, "status": item.status}

        job = crud.get_reprocess_job(db, job_id)
        if job is None or job.status == "cancelled":
            crud.update_reprocess_item(db, item_id, status="skipped", finished_at=_utcnow())
            crud.finish_reprocess_job_if_complete(db, job_id)
            return {"item_id": item_id, "status": "skipped"}

        crud.update_reprocess_item(db, item_id, status="processing", started_at=_utcnow())

        source = crud.get_result(db, item.source_result_id) if item.source_result_id else None
        if source is None:
            raise ValueError(
                f"Row sumber untuk ticket '{item.ticket_id}' sudah tidak ada — "
                "mungkin terhapus setelah job dibuat."
            )

        # Row BARU: identitas upload-nya diwarisi dari row sumber, bukan dari Admin
        # yang menekan tombol. `uploaded_by_role` ikut menentukan cakupan siapa yang
        # melihat tiket ini (lihat qc_scope: isolasi qc_support), jadi menggantinya
        # akan diam-diam memindahkan tiket antar cakupan.
        new_result = crud.create_result(
            db,
            campaign=source.campaign,
            source_files=source.source_files,
            num_calls=source.num_calls,
            transcript_path=None,
            uploaded_by_username=source.uploaded_by_username,
            uploaded_by_role=source.uploaded_by_role,
        )
        new_result_id = str(new_result.id)
        crud.update_reprocess_item(db, item_id, new_result_id=new_result.id)

        client = _minio_client()
        copied = _copy_transcripts(
            client, settings.minio_bucket_transcripts, str(source.id), new_result_id
        )
        if not copied:
            raise ValueError(
                f"Tidak ada transkrip PDF di storage untuk row {source.id} "
                f"(ticket '{item.ticket_id}')."
            )
        new_result.transcript_path = f"{new_result_id}/"
        db.commit()

        # Dijalankan SEGARIS: task ini memang bertugas menunggui satu tiket sampai
        # tuntas, dan penghapusan row lama harus terjadi setelah evaluasinya jadi.
        # Kegagalan di dalamnya sudah dicatat pada row-nya sendiri, lalu dilempar
        # ulang — ditangkap di bawah.
        process_transcript(new_result_id)

        db.expire_all()
        refreshed = crud.get_result(db, new_result_id)
        if refreshed is None or refreshed.status != "done":
            raise ValueError(
                (refreshed.error_message if refreshed else None)
                or "Reproses tidak menghasilkan status 'done'."
            )

        # Row lama dibuang HANYA setelah row barunya jadi. Yang dihapus persis id
        # yang dibekukan saat job dibuat — upload yang masuk di tengah job tidak
        # ikut, walau ticket id-nya sama.
        deleted = crud.delete_results_by_ids(db, item.old_result_ids)
        crud.update_reprocess_item(
            db, item_id, status="done", deleted_old=deleted, finished_at=_utcnow(),
        )
        crud.finish_reprocess_job_if_complete(db, job_id)
        return {"item_id": item_id, "status": "done", "deleted_old": deleted}

    except Exception as exc:  # noqa: BLE001 — kegagalan satu tiket bukan kegagalan job
        logger.exception("reprocess_ticket gagal untuk item %s", item_id)
        db.rollback()
        # Row baru yang gagal dibuang supaya ticket id itu tidak meninggalkan baris
        # 'failed' tanpa evaluasi di halaman Results; row lamanya tetap utuh.
        if new_result_id:
            try:
                crud.delete_results_by_ids(db, [new_result_id])
                _remove_transcripts(
                    _minio_client(), settings.minio_bucket_transcripts, new_result_id
                )
            except Exception:  # pragma: no cover — best effort
                logger.exception("gagal membuang row baru %s", new_result_id)
        try:
            item = crud.get_reprocess_item(db, item_id)
            if item is not None:
                crud.update_reprocess_item(
                    db, item_id, status="failed", error_message=str(exc)[:2000],
                    new_result_id=None, finished_at=_utcnow(),
                )
                crud.finish_reprocess_job_if_complete(db, str(item.job_id))
        except Exception:  # pragma: no cover — best effort
            logger.exception("gagal mencatat kegagalan item %s", item_id)
        return {"item_id": item_id, "status": "failed", "error": str(exc)}

    finally:
        db.close()
