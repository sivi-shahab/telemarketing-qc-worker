"""LLM evaluator for the Transcript QA system.

Takes an ordered transcript (from :mod:`compliance.pdf_parser`) plus a campaign's
prompt / knowledge base / scorecard, makes a single LLM call, and returns the
parsed JSON evaluation produced by the model (per the OUTPUT FORMAT defined in
the campaign prompt).

Scoring / pass decision is computed by the LLM itself (via ``category_summary``);
``passing_grade`` is carried as metadata only. A Python-side recomputation of the
score is a deferred TODO (see PROGRESS_NEW_SCHEMA.md → Catatan/TODO).
"""
import json
import re
from datetime import datetime

from compliance.pdf_parser import parse_filename_timestamp, ticket_id_from_filename


def format_transcript_for_llm(messages: list[dict], source_files: list[str] | None = None) -> str:
    """Render parsed transcript messages into a single string for the LLM.

    Each call (group of segments sharing a ``call_index``) is separated by a
    marker ``=== Panggilan ke-N (file: X, waktu: T) ===`` so the model can tell
    apart multiple calls that each restart their timestamps at ``00:00``. The
    ``speaker`` label is included on every line to help the model distinguish
    agent vs. customer (the actual role is still inferred by the LLM).

    EVERY line also carries a ``[Pn]`` call tag, and (when ``source_files`` is
    given) the transcript opens with a ``=== DAFTAR PANGGILAN ===`` legend mapping
    each tag to its ticket_id. Reason (13 Agustus 2026): the ticket_id used to live
    ONLY in the section header, so filling ``evidence.ticket_id`` meant remembering
    a header that could be tens of thousands of characters up the page. It failed
    exactly where you would predict — on ticket 030808fLO1 a quote 32.515 characters
    below its header (but only ~51 lines above the NEXT header) was attributed to
    the following call, while a quote 7.290 characters below the same header was
    attributed correctly. An audit of 12 multi-call tickets found 2 more evidence
    rows whose timestamp cannot exist in the call they name. With the tag on the
    line itself the model no longer needs long-range memory: the answer sits in the
    line it is quoting. Cost is ~5 characters per line (<3% of a large transcript).

    ``source_files`` (optional) is the chronologically-sorted filename list from
    ``build_transcript``; when provided, each call marker is enriched with the
    file name and the timestamp parsed from it.
    """
    lines: list[str] = []
    current_call = None

    legend = _call_legend(messages, source_files)
    if legend:
        lines.extend(legend)

    for msg in messages:
        call_index = msg.get("call_index", 1)
        if call_index != current_call:
            current_call = call_index
            lines.append(_call_marker(call_index, source_files))

        speaker = msg.get("speaker") or "SPEAKER_?"
        timestamp = msg.get("timestamp", "")
        text = msg.get("text", "")
        lines.append(f"[P{call_index}] [{speaker}] [{timestamp}] {text}")

    return "\n".join(lines)


def _call_legend(messages: list[dict], source_files: list[str] | None) -> list[str]:
    """``=== DAFTAR PANGGILAN ===`` block: one ``Pn = <ticket_id>`` line per call.

    Empty when there are no ``source_files`` — without filenames there is no
    ticket_id to map a tag to, and an legend of bare numbers teaches nothing.

    Legenda ini sempat membawa penanda tanggal per panggilan (``[TANGGAL SUBMIT]`` /
    ``[H±n]``) untuk saringan verifikasi statik. Saringan itu dicabut atas konfirmasi
    Bank Mega — tanggal panggilan tidak menjadi syarat pengambilan bukti — jadi
    penandanya ikut dibuang: menyisakannya hanya mengundang model memberi bobot pada
    sesuatu yang tidak lagi menjadi aturan."""
    if not source_files:
        return []
    seen = sorted({m.get("call_index", 1) for m in messages})
    out = ["=== DAFTAR PANGGILAN (pakai tag [Pn] di tiap baris untuk mengisi evidence.ticket_id) ==="]
    for idx in seen:
        if not 1 <= idx <= len(source_files):
            continue
        filename = source_files[idx - 1]
        ts = parse_filename_timestamp(filename)
        when = ts.strftime("%Y-%m-%d %H:%M:%S") if isinstance(ts, datetime) else "?"
        out.append(f"P{idx} = {ticket_id_from_filename(filename)}   (waktu: {when})")
    out.append("=== AKHIR DAFTAR PANGGILAN ===")
    return out


def _call_marker(call_index: int, source_files: list[str] | None) -> str:
    if source_files and 1 <= call_index <= len(source_files):
        filename = source_files[call_index - 1]
        ts = parse_filename_timestamp(filename)
        when = ts.strftime("%Y-%m-%d %H:%M:%S") if isinstance(ts, datetime) else "?"
        ticket_id = ticket_id_from_filename(filename)
        cust_id = ticket_id.rsplit("_", 1)[0]
        return (
            f"=== Panggilan ke-{call_index} "
            f"(ticket_id: {ticket_id}, id: {cust_id}, file: {filename}, waktu: {when}) ==="
        )
    return f"=== Panggilan ke-{call_index} ==="


def _parse_llm_json_object(content: str) -> dict:
    """Best-effort extraction of a JSON object from an LLM response.

    Tries, in order: direct parse → strip ```json code fence``` → outermost
    ``{...}`` slice. Raises ``ValueError`` if none yield a JSON object.
    """
    if content is None:
        raise ValueError("LLM returned empty content")

    text = content.strip()

    # 1) direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2) strip a fenced code block (```json ... ``` or ``` ... ```)
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3) outermost {...}
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError("Could not parse JSON object from LLM response")


def _build_user_content(
    messages: list[dict],
    kb_text: str,
    scorecard_text: str,
    source_files: list[str] | None,
    reference_text: str = "",
) -> str:
    transcript = format_transcript_for_llm(messages, source_files)
    # URUTAN INI DISENGAJA — bagian yang identik antar-panggilan berdiri PALING DEPAN,
    # bagian yang berubah tiap tiket didorong ke belakang (7 September 2026, dipertajam
    # 10 September 2026 dengan memisah reference data dari scorecard).
    #
    # Endpoint melakukan prompt caching otomatis atas AWALAN permintaan yang identik;
    # terukur 18.176 dari 18.571 token ter-cache (98%) pada panggilan kedua dengan
    # awalan sama. Selama sesuatu yang berubah tiap tiket berdiri di depan, semua yang
    # di belakangnya ikut dingin — termasuk KB 24.290 token yang identik SELURUH tiket.
    #
    # Lapisannya, dari yang paling stabil:
    #   1. prompt campaign (49.832 token) — identik semua panggilan
    #   2. KB (24.290)                    — identik semua tiket
    #   3. SCORECARD (campaign.scorecard_text saja, ~3.788) — identik semua tiket
    #   4. REFERENCE DATA (data nasabah dari DB) — per tiket, tetapi identik untuk
    #      SEMUA panggilan paralel satu tiket, jadi tetap ter-cache di panggilan ke-2..N
    #   5. TRANSCRIPT — unik per rekaman
    #
    # ``reference_text`` DULU digabung ke ``scorecard_text`` oleh pemanggil, membuat
    # lapisan 3 ikut berubah tiap tiket. Dipisah di sini supaya scorecard tetap masuk
    # awalan bersama.
    #
    # Isi TIDAK berubah — hanya letaknya. Tetapi bentuk prompt yang berubah bisa
    # menggeser perilaku model, jadi wajib diuji dengan reprocess pembanding.
    blocks = [
        "KB:\n" f"{kb_text}",
        "SCORECARD:\n" f"{scorecard_text}",
    ]
    if (reference_text or "").strip():
        blocks.append("REFERENCE DATA:\n" f"{reference_text}")
    blocks.append("TRANSCRIPT:\n" f"{transcript}")
    return "\n\n".join(blocks)


def evaluate(
    prompt_text: str,
    messages: list[dict],
    kb_text: str,
    scorecard_text: str,
    llm_client,
    model: str,
    source_files: list[str] | None = None,
    max_retries: int = 2,
    temperature: float = 1.0,
    seed: int | None = None,
    reasoning_effort: str | None = None,
    return_usage: bool = False,
    reference_text: str = "",
    required_keys: list[str] | None = None,
):
    """Run a single LLM evaluation of the transcript against the campaign config.

    ``prompt_text`` is the campaign's instruction prompt (defines rules + the
    strict JSON output format). The transcript, KB and scorecard are supplied as
    the user message. The call uses ``temperature`` (loaded from
    ``LLM_TEMPERATURE``, default 1.0). ``seed`` (``LLM_SEED``) is passed for
    deterministic sampling when supported, and ``reasoning_effort``
    (``LLM_REASONING_EFFORT``) is forwarded via ``extra_body`` to control the
    model's reasoning budget. If the response can't be parsed as a JSON object,
    the call is retried up to ``max_retries`` times
    (total attempts = ``max_retries + 1``).

    ``required_keys`` (optional) names top-level keys the parsed JSON must
    contain — e.g. ``["cashline_data_verification", "card_holder_verification"]``
    when the caller already confirmed matching reference rows exist for this
    ticket. A response that parses fine as JSON but is missing one of these keys
    is treated the same as an unparseable response: it is retried, then raises
    after the same ``max_retries`` budget. This guards against the LLM silently
    dropping whole mandatory sections while still returning otherwise-valid JSON
    (case seen on ticket 030226vUJS, 18 September 2026 — reference data was
    complete, but the model's response never included the four Task B/C/D keys,
    and a JSON-parseability check alone let it through as ``status=done``).

    Returns the parsed JSON object produced by the LLM. If ``return_usage`` is
    True, returns ``(evaluation, usage)`` instead, where ``usage`` is
    ``{"input_token": int | None, "output_token": int | None}`` taken from the
    LLM response's token accounting.

    ``reference_text`` (data acuan nasabah dari DB) dikirim sebagai blok tersendiri
    SETELAH scorecard dan SEBELUM transkrip. Dipisah dari ``scorecard_text`` supaya
    scorecard campaign — yang identik antar-tiket — tetap masuk awalan yang ter-cache;
    lihat ``_build_user_content``.
    """
    user_content = _build_user_content(
        messages, kb_text, scorecard_text, source_files, reference_text
    )
    chat_messages = [
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": user_content},
    ]

    create_kwargs: dict = {
        "model": model,
        "messages": chat_messages,
        "temperature": temperature,
    }
    if seed is not None:
        create_kwargs["seed"] = seed
    if reasoning_effort:
        create_kwargs["extra_body"] = {"reasoning_effort": reasoning_effort}

    last_error: Exception | None = None
    for _ in range(max_retries + 1):
        response = llm_client.chat.completions.create(**create_kwargs)
        content = response.choices[0].message.content
        try:
            evaluation = _parse_llm_json_object(content)
        except ValueError as exc:
            last_error = exc
            continue
        missing_keys = [k for k in (required_keys or []) if k not in evaluation]
        if missing_keys:
            last_error = ValueError(
                f"LLM response valid JSON but missing required key(s): {missing_keys}"
            )
            continue
        if return_usage:
            return evaluation, _extract_usage(response)
        return evaluation

    raise ValueError(
        f"LLM did not return a complete evaluation after {max_retries + 1} attempts: {last_error}"
    )


def _extract_usage(response) -> dict:
    """Pull token counts from an OpenAI-compatible response (``None`` if absent).

    ``cached_token`` dan ``reasoning_token`` ikut diambil (7 September 2026) karena
    keduanya menentukan biaya sebenarnya dan tidak bisa diperkirakan dari luar:

    * endpoint melakukan prompt caching otomatis — terukur 98% pada awalan identik — dan
      bagian yang ter-cache ditagih lebih murah, jadi "berapa token masuk" saja tidak
      cukup untuk menghitung ongkos;
    * ``reasoning_effort`` menghasilkan token penalaran yang ditagih sebagai keluaran
      tetapi TIDAK muncul di hasil: pada panggilan klasifikasi, JSON-nya hanya empat
      baris sementara ``completion_tokens``-nya 1.228.

    Keduanya opsional pada respons; ``None`` bila server tidak melaporkannya.
    """
    usage = getattr(response, "usage", None)
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    return {
        "input_token": getattr(usage, "prompt_tokens", None),
        "output_token": getattr(usage, "completion_tokens", None),
        "cached_token": getattr(prompt_details, "cached_tokens", None),
        "reasoning_token": getattr(completion_details, "reasoning_tokens", None),
    }
