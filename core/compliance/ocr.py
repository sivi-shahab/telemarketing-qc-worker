"""OCR + verification of a document via Mistral Document AI (Azure-hosted).

The PDF is sent as a base64 ``document_url`` data URI to the Mistral Document AI
OCR endpoint, together with a strict JSON ``document_annotation_format`` (schema)
and a per-document-type ``document_annotation_prompt`` (with the bank reference
"acuan" values injected). The response's ``document_annotation`` (a JSON string)
is parsed and returned as a dict.

This replaces the previous multimodal-chat / page-image approach: Mistral
Document AI ingests the PDF directly, so no page rendering is needed.
"""
import base64
import json

import requests

from compliance.evaluator import _parse_llm_json_object


def ocr_document(
    pdf_bytes: bytes,
    prompt: str,
    schema: dict,
    base_url: str,
    api_key: str,
    model: str,
    timeout: int = 180,
) -> dict:
    """Run a single OCR + verification call over a PDF and return parsed JSON.

    ``schema`` is the strict ``document_annotation_format``; ``prompt`` is the
    ``document_annotation_prompt`` (already has the reference values injected).
    Raises ``ValueError`` on missing config or unparseable output, and
    ``requests.HTTPError`` on a non-2xx response.
    """
    if not pdf_bytes:
        raise ValueError("ocr_document requires non-empty PDF bytes")
    if not base_url:
        raise ValueError("OCR_BASE_URL is not configured")
    if not model:
        raise ValueError("OCR_MODEL is not configured")

    b64 = base64.b64encode(pdf_bytes).decode("ascii")
    resp = requests.post(
        base_url,
        timeout=timeout,
        headers={
            "Authorization": f"Bearer {api_key or ''}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "document": {
                "type": "document_url",
                "document_url": f"data:application/pdf;base64,{b64}",
            },
            "document_annotation_format": schema,
            "document_annotation_prompt": prompt,
        },
    )
    if resp.status_code >= 400:
        # ``raise_for_status()`` sendirian hanya menyimpan baris status ("422 Client
        # Error … for url: …") — badan respons yang MENJELASKAN sebabnya hilang, dan
        # pesan itulah yang tersimpan di documents.error_message serta tampil ke user.
        # Tanpa badan respons, kegagalan seperti ini tidak bisa didiagnosis sama sekali
        # setelah kejadiannya lewat.
        body = (resp.text or "").strip()
        raise requests.HTTPError(
            f"{resp.status_code} {resp.reason} dari {base_url}"
            + (f" — {body[:1000]}" if body else " — (respons kosong)"),
            response=resp,
        )

    annotation = resp.json().get("document_annotation")
    if annotation is None:
        raise ValueError("OCR response missing 'document_annotation'")
    if isinstance(annotation, dict):
        return annotation
    if isinstance(annotation, str):
        try:
            return json.loads(annotation)
        except json.JSONDecodeError:
            # Fall back to the best-effort extractor used for transcript eval.
            return _parse_llm_json_object(annotation)
    raise ValueError(f"Unexpected 'document_annotation' type: {type(annotation).__name__}")
