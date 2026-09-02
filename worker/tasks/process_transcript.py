"""Celery task: evaluate a submitted transcript (set of PDFs) for one result.

Pipeline (per result_id):
  1. status -> processing, set started_at
  2. download all PDFs from MinIO transcripts/{result_id}/ to /tmp
  3. build_transcript(pdf_paths) -> messages + sorted_filenames
  4. load active campaign config from DB (prompt/scorecard/kb)
  5. evaluate(...) -> LLM output
  6. assemble final JSON
  7. upload final JSON to MinIO results bucket
  8. save result_data row (PostgreSQL)
  9. status -> done (result_path, completed_at, processing_sec)
 10. on any exception -> status=failed + error_message; always clean /tmp
"""
import io
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from functools import lru_cache

from openai import OpenAI, AzureOpenAI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services.multi_bucket_minio import build_minio_client
from compliance.evaluator import evaluate
from compliance.pdf_parser import build_transcript, latest_generated_timestamp
from compliance.static_similarity import stamp_static_rules
from compliance.reference_data import (
    build_reference_data,
    customer_id_from_filenames,
)
from db import crud
from worker.celery_app import celery_app
from worker.config import get_worker_settings

logger = logging.getLogger(__name__)

TMP_ROOT = "/tmp/transcripts"


# ---------------------------------------------------------------------------
# Lazy singletons (one engine / minio / llm client per worker process)
# ---------------------------------------------------------------------------

@lru_cache()
def _session_factory():
    settings = get_worker_settings()
    engine = create_engine(
        settings.database_url, pool_pre_ping=True, connect_args=settings.db_connect_args
    )
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


@lru_cache()
def _minio_client():
    # Minio biasa atau MultiBucketMinioClient, tergantung .env — API-nya sama.
    return build_minio_client(get_worker_settings())


@lru_cache()
def _llm_client_openai() -> OpenAI:
    settings = get_worker_settings()
    return OpenAI(
        base_url=settings.llm_base_url, 
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout
    )


@lru_cache()
def _llm_client() -> AzureOpenAI:
    settings = get_worker_settings()
    return AzureOpenAI(
        azure_endpoint=settings.llm_base_url,      # https://foundry-dmanalytics-hub.cognitiveservices.azure.com
        api_key=settings.llm_api_key,
        api_version="2025-04-01-preview",           # terbukti valid dari test curl
        azure_deployment=settings.llm_model,        # "gpt-5.4-mini"
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _download_transcripts(result_id: str) -> list[str]:
    """Download all PDFs under transcripts/{result_id}/ to a local temp dir."""
    settings = get_worker_settings()
    client = _minio_client()
    local_dir = os.path.join(TMP_ROOT, str(result_id))
    os.makedirs(local_dir, exist_ok=True)

    paths: list[str] = []
    prefix = f"{result_id}/"
    for obj in client.list_objects(
        settings.minio_bucket_transcripts, prefix=prefix, recursive=True
    ):
        filename = os.path.basename(obj.object_name)
        if not filename.lower().endswith(".pdf"):
            continue
        local_path = os.path.join(local_dir, filename)
        client.fget_object(settings.minio_bucket_transcripts, obj.object_name, local_path)
        paths.append(local_path)

    return paths


@celery_app.task(name="worker.tasks.process_transcript.process_transcript")
def process_transcript(result_id: str):
    settings = get_worker_settings()
    Session = _session_factory()
    db = Session()
    started_at = _utcnow()

    try:
        # 1. mark processing
        crud.update_result_status(db, result_id, "processing", started_at=started_at)

        result = crud.get_result(db, result_id)
        if result is None:
            raise ValueError(f"Result {result_id} not found")

        # 2. download PDFs
        pdf_paths = _download_transcripts(result_id)
        if not pdf_paths:
            raise ValueError(f"No transcript PDFs found for result {result_id}")

        # 3. build ordered transcript
        sorted_filenames, messages, audio_duration, audio_durations = build_transcript(
            pdf_paths
        )
        # Latest "Generated" timestamp across the ticket's PDFs — dates the ticket on
        # the Statistics AI-status chart (see compliance/pdf_parser.py). None-safe:
        # a NULL generated_at falls back to uploaded_at on that chart only.
        generated_at = latest_generated_timestamp(pdf_paths)
        # Guard: an empty transcript means no segments could be parsed from the
        # PDFs (e.g. the upstream diarization format changed and no speaker header
        # was recognized). Fail loudly instead of sending a blank transcript to the
        # LLM, which would silently produce a garbage evaluation with status=done.
        if not messages:
            raise ValueError(
                "Transkrip kosong: tidak ada segmen yang bisa di-parse dari PDF "
                "(format transkrip mungkin tidak dikenali)."
            )

        # 4. load campaign config
        campaign = crud.get_active_campaign(db, result.campaign)
        if campaign is None:
            raise ValueError(f"Active campaign '{result.campaign}' not found")

        # A campaign may exist as a PLACEHOLDER — created so it can be assigned to a
        # role (see the Manage Role menu) before its QC config has been uploaded. Its
        # prompt/KB/scorecard are empty, and feeding those to the LLM would produce a
        # garbage evaluation that still lands as status=done. Fail loudly instead, the
        # same way an unparseable transcript does above.
        missing = [
            label
            for label, text in (
                ("prompt", campaign.prompt_text),
                ("scorecard", campaign.scorecard_text),
                ("KB", campaign.kb_text),
            )
            if not (text or "").strip()
        ]
        if missing:
            raise ValueError(
                f"Campaign '{result.campaign}' belum punya konfigurasi QC: "
                f"{', '.join(missing)} masih kosong. Upload dulu lewat menu "
                "Upload Campaign sebelum memproses transkrip campaign ini."
            )

        # 4b. build CASHLINE + CARD HOLDER reference data from the DB (looked up
        # by the customer/session ID derived from the earliest PDF filename) and
        # append it to the scorecard text, so the LLM can verify against it.
        customer_id = customer_id_from_filenames(sorted_filenames)
        reference_text, ref_warnings, reference_raw = build_reference_data(
            customer_id, db, riplay_extraction=campaign.riplay_extraction
        )
        for warn in ref_warnings:
            logger.warning("reference data (%s / id=%s): %s", result_id, customer_id, warn)
        scorecard_text = f"{campaign.scorecard_text}\n\n{reference_text}"

        # 5. LLM evaluation. `evaluate(return_usage=True)` returns (evaluation,
        # usage); we intentionally discard the token usage here — input/output
        # tokens are not persisted to MinIO/DB nor exposed via the API/dashboard.
        evaluation, _usage = evaluate(
            prompt_text=campaign.prompt_text,
            messages=messages,
            kb_text=campaign.kb_text,
            scorecard_text=scorecard_text,
            llm_client=_llm_client(),
            model=settings.llm_model,
            source_files=sorted_filenames,
            temperature=settings.llm_temperature,
            seed=settings.llm_seed,
            reasoning_effort=settings.llm_reasoning_effort,
            return_usage=True,
        )

        # 5b. Cap versi aturan verifikasi statik ke dalam evaluasi (revamp 21 Agustus
        # 2026), supaya seluruh permukaan pembaca memakai ambang yang benar tanpa perlu
        # menebak dari waktu upload — dan tiket lama tidak ikut dinilai ulang.
        evaluation = stamp_static_rules(evaluation)

        # 6. assemble final JSON
        completed_at = _utcnow()
        processing_sec = (completed_at - started_at).total_seconds()
        final_json = {
            "result_id": str(result_id),
            "campaign": result.campaign,
            "source_files": sorted_filenames,
            "num_calls": len(sorted_filenames),
            "audio_duration": audio_duration,
            # Rincian durasi per PDF (kolom Call Duration tata letak Demo). Total di
            # atas tetap ditulis apa adanya: itu yang dibaca seluruh tampilan lain.
            "audio_durations": audio_durations,
            "processed_at": completed_at.isoformat(),
            "processing_sec": round(processing_sec, 2),
            "evaluation": evaluation,
            # [NEW] Data cashline+customer MENTAH (dari build_reference_data(),
            # sudah diambil di atas utk prompt LLM) disisipkan di sini juga --
            # supaya ResultsView (_build_items() di stats.py) bisa baca
            # customer_name/account_number/change_flags LANGSUNG dari sini
            # (result_data yang SUDAH di-load), TANPA hit App A lagi tiap
            # dashboard dibuka. Tidak ada tabel/bucket baru -- ini cuma field
            # tambahan di JSON yang SUDAH disimpan ke Postgres+MinIO di bawah.
            "reference_data": reference_raw,
        }

        # 7. upload result JSON to MinIO
        payload = json.dumps(final_json, ensure_ascii=False).encode("utf-8")
        result_path = f"{result_id}.json"
        _minio_client().put_object(
            settings.minio_bucket_results,
            result_path,
            io.BytesIO(payload),
            length=len(payload),
            content_type="application/json",
        )

        # 8. persist result_data
        crud.save_result_data(db, result_id, final_json)

        # 9. mark done
        crud.update_result_status(
            db,
            result_id,
            "done",
            result_path=result_path,
            completed_at=completed_at,
            processing_sec=round(processing_sec, 2),
            generated_at=generated_at,
        )

        return {"result_id": str(result_id), "status": "done"}

    except Exception as exc:  # noqa: BLE001 — record any failure on the row
        logger.exception("process_transcript failed for %s", result_id)
        try:
            crud.update_result_status(
                db, result_id, "failed", error_message=str(exc)
            )
        except Exception:  # pragma: no cover — best effort
            logger.exception("failed to record failure status for %s", result_id)
        raise

    finally:
        db.close()
        # 10. cleanup temp dir
        shutil.rmtree(os.path.join(TMP_ROOT, str(result_id)), ignore_errors=True)
