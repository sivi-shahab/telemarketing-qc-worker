"""PDF transcript parser for the Transcript QA system.

Parses "Transcription Report" PDFs (diarization + transcription) into an
ordered list of conversation segments, and merges multiple PDFs belonging to
the same session into a single chronologically-ordered transcript.

Format per PDF (after a header block + separator line)::

    [SPEAKER_1]:
    [00:00.21 -> 00:29.81] teks segmen ...
    [00:29.81 -> 00:47.28] lanjutan teks yang bisa
    membentang ke beberapa baris ...
    [SPEAKER_0]:
    [00:47.32 -> 01:32.81] teks segmen ...
"""
import logging
import os
import re
from datetime import datetime

import pdfplumber

from compliance.call_ownership import fix_speaker_roles

logger = logging.getLogger(__name__)

# Strip a trailing duplicate suffix like " (1)" / " (2)" from a filename stem.
_DUP_SUFFIX_RE = re.compile(r"\s*\(\d+\)\s*$")

# A speaker header line (legacy format), e.g. "[SPEAKER_1]:"
_SPEAKER_RE = re.compile(r"^\[SPEAKER_(\d+)\]:\s*$")

# A speaker header line (newer upstream format), e.g. "AGENT_BM (female) [NETRAL]:"
# or "[NASABAH (male) [NETRAL]]:" — a named speaker followed by (gender) and
# [emotion], optionally wrapped in outer brackets. The (…) and […] groups are kept
# deliberately loose to tolerate gender (male/female) and emotion (NETRAL/POSITIF/
# NEGATIF) variations. Profile lines like "(cid:127) AGENT_BM : ..." don't match
# (they start with "(" and carry trailing text after the colon).
_SPEAKER_NAMED_RE = re.compile(
    r"^\[?\s*([A-Za-z][A-Za-z0-9_]*)\s*\([^)]*\)\s*\[[^\]]*\]\s*\]?\s*:\s*$"
)

# A segment line, e.g. "[00:00.21 -> 00:29.81] teks ..."
_SEGMENT_RE = re.compile(r"^\[(\d+:\d+\.\d+)\s*->\s*(\d+:\d+\.\d+)\]\s*(.*)$")

# The "Generated" timestamp on the first page of a Transcription Report, e.g.
# "Generated : 2026-07-07 22:58:27" (date/time separator tolerant).
_GENERATED_RE = re.compile(
    r"Generated\s*:\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", re.IGNORECASE
)


def parse_filename_timestamp(filename: str):
    """Extract the ``YYYYMMDDHHMMSS`` timestamp embedded in a transcript filename.

    Format: ``<prefix>_<YYYYMMDDHHMMSS>.pdf`` (e.g.
    ``201134FTJu_20260520110338.pdf``). A duplicate suffix like `` (1)`` is
    stripped before parsing.

    Returns a ``datetime`` on success, or ``None`` if the timestamp token can't
    be parsed (caller decides the fallback ordering).
    """
    name = os.path.basename(filename)
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    stem = _DUP_SUFFIX_RE.sub("", stem).strip()
    token = stem.rsplit("_", 1)[-1]
    try:
        return datetime.strptime(token, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def parse_generated_timestamp(path: str):
    """Extract the ``Generated : YYYY-MM-DD HH:MM:SS`` timestamp from a transcript PDF.

    The line lives in the first-page header of a "Transcription Report" (see module
    docstring); only page 1 is read. The value is the wall-clock time the transcript
    was produced and is returned as a naive ``datetime`` (no timezone shift applied).
    Returns ``None`` if the first page has no recognizable ``Generated`` line.
    """
    try:
        with pdfplumber.open(path) as pdf:
            if not pdf.pages:
                return None
            text = pdf.pages[0].extract_text() or ""
    except Exception:
        return None
    m = _GENERATED_RE.search(text)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def latest_generated_timestamp(pdf_paths: list[str]):
    """Latest ``Generated`` timestamp across a ticket's PDFs (``None`` if none parse).

    A single ticket id can carry multiple transcript PDFs; the chart dates the ticket
    by the most recent ``Generated`` value among them (``parse_generated_timestamp``).
    """
    stamps = [ts for ts in (parse_generated_timestamp(p) for p in pdf_paths) if ts is not None]
    return max(stamps) if stamps else None


def ticket_id_from_filename(filename: str) -> str:
    """Derive the ``ticket_id`` from a transcript filename.

    The ``ticket_id`` is the full filename stem (without ``.pdf`` and without a
    duplicate suffix like `` (1)``). For ``130220dkIM_20260519132045.pdf`` this
    is ``130220dkIM_20260519132045``. The customer/session ``id`` is the prefix
    before the underscore (``130220dkIM``); derive it via
    ``ticket_id.rsplit("_", 1)[0]`` when needed.
    """
    name = os.path.basename(filename)
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    return _DUP_SUFFIX_RE.sub("", stem).strip()


def parse_transcript_pdf(path: str) -> list[dict]:
    """Parse a single transcript PDF into a list of segment dicts.

    Each segment is ``{"speaker": "SPEAKER_n", "timestamp": "<start> -> <end>",
    "text": "..."}``. The header block (everything before the first
    ``[SPEAKER_n]:`` line) is skipped. Segment text that wraps onto subsequent
    lines — including across page boundaries — is joined back together.
    """
    lines: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(text.split("\n"))

    segments: list[dict] = []
    current_speaker = None
    current = None          # the segment dict currently accumulating text
    started = False         # True once we've passed the header (first SPEAKER)

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        # Legacy header ("[SPEAKER_1]:") keeps the "SPEAKER_n" label; the newer
        # named header ("AGENT_BM (female) [NETRAL]:") keeps the real speaker name.
        speaker_match = _SPEAKER_RE.match(line)
        if speaker_match:
            current_speaker = f"SPEAKER_{speaker_match.group(1)}"
            current = None
            started = True
            continue
        named_match = _SPEAKER_NAMED_RE.match(line)
        if named_match:
            current_speaker = named_match.group(1)
            current = None
            started = True
            continue

        # Skip header/separator lines until the first speaker marker.
        if not started:
            continue

        segment_match = _SEGMENT_RE.match(line)
        if segment_match:
            start_ts, end_ts, text = segment_match.groups()
            current = {
                "speaker": current_speaker,
                "timestamp": f"{start_ts} -> {end_ts}",
                "text": text.strip(),
            }
            segments.append(current)
        elif current is not None:
            # Continuation of the current segment's text (wrapped line).
            current["text"] = f"{current['text']} {line}".strip()

    return segments


def call_duration(path: str) -> str:
    """Durasi terucap satu PDF, format sama dengan kolom Call Duration ("12m 3s").

    ``build_transcript`` sudah menghitung ini untuk PDF yang ikut dinilai; helper ini
    untuk PDF yang justru DIBUANG (milik agent lain, lihat
    ``compliance.call_ownership``) sehingga tidak pernah melewati build_transcript —
    kolom Call Duration tetap menyebutkan panggilan itu berikut durasinya.
    """
    segments = parse_transcript_pdf(path)
    seconds = _end_timestamp_seconds(segments[-1]["timestamp"]) if segments else 0.0
    return format_audio_duration(seconds)


def transcript_plain_text(path: str) -> str:
    """Isi PERCAKAPAN satu PDF sebagai teks polos, tanpa blok header.

    Dipakai ``compliance.call_ownership`` untuk mencari perkenalan agent ("saya Alvin
    dari Bank Mega"). Sengaja tidak membaca teks halaman mentah: blok header memuat
    baris profil pembicara ("Agent (Agent ) : 307.2 detik") yang bukan ucapan siapa pun.
    """
    return "\n".join(seg["text"] for seg in parse_transcript_pdf(path))


def _end_timestamp_seconds(timestamp: str) -> float:
    """Convert the *end* of a ``"<start> -> <end>"`` segment timestamp to seconds.

    Timestamps are ``MM:SS.ss`` (minutes:seconds), e.g. ``"01:32.81"`` → 92.81.
    Returns ``0.0`` if the value can't be parsed.
    """
    end = timestamp.split("->")[-1].strip()
    parts = end.split(":")
    try:
        minutes = int(parts[0])
        seconds = float(parts[1])
    except (ValueError, IndexError):
        return 0.0
    return minutes * 60 + seconds


def format_audio_duration(total_seconds: float) -> str:
    """Format a duration in seconds as ``"<m>m <s>s"`` (e.g. ``"40m 30s"``).

    The total is rounded to the nearest whole second before splitting.
    """
    total = int(round(total_seconds))
    minutes, seconds = divmod(total, 60)
    return f"{minutes}m {seconds}s"


def _sort_key(path: str, idx: int):
    """Ordering key for a PDF: filename timestamp, then mtime, then original order."""
    ts = parse_filename_timestamp(path)
    if ts is None:
        try:
            ts = datetime.fromtimestamp(os.path.getmtime(path))
        except OSError:
            ts = datetime.max  # unknown → push to the end, but keep stable
    return (ts, idx)


def build_transcript(
    pdf_paths: list[str],
) -> tuple[list[str], list[dict], str, list[dict]]:
    """Merge multiple transcript PDFs into one chronologically ordered transcript.

    PDFs are sorted ascending by their filename timestamp (falling back to mtime,
    then original order). Returns
    ``(sorted_filenames, messages, audio_duration, per_file_durations)`` where each
    message is ``{"call_index", "ticket_id", "speaker", "timestamp", "text"}`` and
    ``call_index`` is 1-based per source file in chronological order. The
    ``ticket_id`` is the source filename stem (see ``ticket_id_from_filename``).

    ``audio_duration`` is the total spoken duration across all PDFs — the end
    timestamp of the last segment of each PDF summed together — formatted as
    ``"<m>m <s>s"`` (e.g. ``"40m 30s"``).

    ``per_file_durations`` memecah total itu per PDF:
    ``[{"file": "<nama.pdf>", "duration": "12m 3s"}, ...]`` dalam urutan
    kronologis yang sama dengan ``sorted_filenames``. Dipakai kolom Call Duration
    tata letak Demo, yang menyebut durasi tiap panggilan berikut nama berkasnya —
    angka totalnya saja tidak cukup di sana. PDF yang tidak menghasilkan satu
    segmen pun tetap masuk daftar dengan ``"0m 0s"``, supaya jumlah butirnya
    selalu sama dengan jumlah source file.
    """
    indexed = sorted(enumerate(pdf_paths), key=lambda t: _sort_key(t[1], t[0]))
    sorted_paths = [path for _, path in indexed]
    sorted_filenames = [os.path.basename(path) for path in sorted_paths]

    messages: list[dict] = []
    total_seconds = 0.0
    per_file: list[dict] = []
    for call_index, path in enumerate(sorted_paths, start=1):
        ticket_id = ticket_id_from_filename(path)
        raw_segments = parse_transcript_pdf(path)
        # Label "Agent"/"Customer" dari diarization hulu kadang TERTUKAR (31 dari 217
        # PDF). Diperbaiki di sini, sebelum transkrip dikirim ke LLM: verifikasi statik
        # card holder menilai UCAPAN NASABAH, jadi peran yang tertukar membuat jawaban
        # nasabah tidak pernah terhitung. Lihat compliance.call_ownership.
        segments = fix_speaker_roles(raw_segments)
        if segments is not raw_segments:
            logger.warning(
                "peran pembicara tertukar pada %s — label Agent/Customer ditukar balik",
                os.path.basename(path),
            )
        for seg in segments:
            messages.append(
                {
                    "call_index": call_index,
                    "ticket_id": ticket_id,
                    "speaker": seg["speaker"],
                    "timestamp": seg["timestamp"],
                    "text": seg["text"],
                }
            )
        seconds = (
            _end_timestamp_seconds(segments[-1]["timestamp"]) if segments else 0.0
        )
        total_seconds += seconds
        per_file.append(
            {
                "file": os.path.basename(path),
                "duration": format_audio_duration(seconds),
            }
        )

    return sorted_filenames, messages, format_audio_duration(total_seconds), per_file
