"""Celery task: salin teks dari satu gambar menu OCR Gambar.

Alur per ``ocr_images.id``:
  1. lewati bila baris tidak ada atau sudah ``done``/``failed`` (pesan basi —
     ``failed`` hanya kembali ke antrean lewat endpoint retry yang me-reset ke
     ``pending``)
  2. status -> processing
  3. unduh gambar dari bucket dokumen
  4. model vision (deployment ``LLM_MODEL``) diminta menyalin teks apa adanya
  5. simpan teks + token -> done, atau failed + error_message

Mesin OCR-nya LLM, bukan ``compliance.ocr`` (Mistral Document AI): endpoint OCR
belum dikonfigurasi di server ini, sedangkan deployment LLM sudah dipakai dan
terbukti menerima gambar (spike 1 Oktober 2026). Sengaja TANPA
``reasoning_effort``: menyalin teks tidak butuh penalaran.
"""
import base64
import logging
import uuid
from datetime import datetime, timezone

from compliance.evaluator import _extract_usage
from db.models import OcrImage
from worker.celery_app import celery_app
from worker.config import get_worker_settings
from worker.tasks.process_document import _download_object, _session_factory
from worker.tasks.process_transcript import _llm_client

logger = logging.getLogger(__name__)

NO_TEXT = "(tidak ada teks)"
OCR_SYSTEM_PROMPT = (
    "You are an OCR engine. Transcribe ALL text in the image verbatim, preserving "
    "line breaks and reading order; render tables as markdown tables. Do not "
    "translate, summarize, correct, or add commentary. If the image has no text, "
    f"output exactly: {NO_TEXT}"
)
_SKIP = {"done", "failed"}


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def transcribe_image(client, model, image_bytes, mime_type):
    """``(teks, token_usage)`` untuk satu gambar."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": OCR_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
            ]},
        ],
    )
    text = (response.choices[0].message.content or "").strip()
    return (text or NO_TEXT), _extract_usage(response)


@celery_app.task(name="worker.tasks.process_ocr_image.process_ocr_image")
def process_ocr_image(image_id: str):
    try:
        key = uuid.UUID(str(image_id))
    except ValueError:
        logger.warning("ocr image id tidak valid: %r", image_id)
        return
    db = _session_factory()()
    try:
        row = db.get(OcrImage, key)
        if row is None or row.status in _SKIP:
            logger.info("ocr image %s dilewati (status=%s)", image_id, getattr(row, "status", None))
            return
        row.status = "processing"
        row.started_at = _utcnow()
        db.commit()
        try:
            data = _download_object(row.object_path)
            text, usage = transcribe_image(_llm_client(), get_worker_settings().llm_model,
                                           data, row.mime_type)
            row.text, row.token_usage = text, usage
            row.status, row.error_message = "done", None
        except Exception as exc:  # noqa: BLE001
            logger.exception("OCR gambar gagal untuk %s", image_id)
            db.rollback()
            row.status = "failed"
            row.error_message = str(exc)[:1000]
        row.finished_at = _utcnow()
        db.commit()
    finally:
        db.close()
