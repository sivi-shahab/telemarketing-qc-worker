"""Celery task: OCR + verify the uploaded documents of one result.

For each ``Document`` row of ``result_id`` still in ``pending``:
  1. status -> processing
  2. download the PDF bytes from MinIO (documents bucket)
  3. build the bank reference ("acuan") for the document type, looked up in the
     CSVs by the customer/session ID derived from the result's source PDFs
  4. build the per-type prompt (with acuan injected) + strict JSON schema
  5. ocr_document(...) via Mistral Document AI -> structured verification JSON
  6. save ocr_json + status done (or status failed + error_message)

Each document is handled independently so one failure does not block the others.
"""
import logging
from functools import lru_cache

from minio import Minio
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from compliance.documents import build_ocr_request
from compliance.ocr import ocr_document
from compliance.reference_data import (
    build_document_reference,
    customer_id_from_filenames,
)
from db import crud
from worker.celery_app import celery_app
from worker.config import get_worker_settings

logger = logging.getLogger(__name__)


@lru_cache()
def _session_factory():
    settings = get_worker_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


@lru_cache()
def _minio_client() -> Minio:
    settings = get_worker_settings()
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )


def _download_object(object_name: str) -> bytes:
    settings = get_worker_settings()
    client = _minio_client()
    response = client.get_object(settings.minio_bucket_documents, object_name)
    try:
        return response.read()
    finally:
        try:
            response.close()
            response.release_conn()
        except Exception:
            pass


@celery_app.task(name="worker.tasks.process_document.process_document")
def process_document(result_id: str):
    settings = get_worker_settings()
    Session = _session_factory()
    db = Session()
    try:
        # Customer/session ID for reference-data lookup is derived from the
        # result's source PDFs (the same prefix matched against the CSV result_id).
        result = crud.get_result(db, result_id)
        source_files = list(result.source_files or []) if result else []
        customer_id = None
        if source_files:
            try:
                customer_id = customer_id_from_filenames(source_files)
            except Exception:
                logger.warning("could not derive customer_id for result %s", result_id)
        if not customer_id:
            logger.warning(
                "result %s has no source_files; OCR runs without bank reference", result_id
            )

        documents = crud.list_documents(db, result_id)
        for doc in documents:
            if doc.status != "pending":
                continue
            doc_id = doc.id
            try:
                crud.update_document_status(db, doc_id, "processing")
                pdf_bytes = _download_object(doc.object_path)

                # Build the bank reference ("acuan") for this document type.
                reference: dict = {}
                if customer_id:
                    reference, warns = build_document_reference(
                        customer_id,
                        doc.doc_type,
                        db,
                    )
                    for warn in warns:
                        logger.warning(
                            "doc reference (%s / %s): %s", doc_id, doc.doc_type, warn
                        )

                prompt, schema = build_ocr_request(doc.doc_type, reference)
                ocr_json = ocr_document(
                    pdf_bytes=pdf_bytes,
                    prompt=prompt,
                    schema=schema,
                    base_url=settings.ocr_base_url,
                    api_key=settings.ocr_api_key,
                    model=settings.ocr_model,
                )
                crud.set_document_result(db, doc_id, ocr_json)
                logger.info("OCR done for document %s (%s)", doc_id, doc.doc_type)
            except Exception as exc:  # noqa: BLE001
                logger.exception("OCR failed for document %s", doc_id)
                crud.set_document_failed(db, doc_id, str(exc))
    finally:
        db.close()
