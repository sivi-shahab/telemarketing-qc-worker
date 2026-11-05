"""Sebut PENGUCAP kutipan evidence di ``reason`` scorecard — memakai label ASLI transkrip.

Permintaan QC Bank Mega (15 Oktober 2026): kolom Reason perlu menyebut apakah kutipan
evidence diucapkan agent atau customer. QC membaca PDF transkrip ASLI (``/transcript_pdf``),
sedangkan LLM membaca versi yang labelnya sudah dibalik ``fix_speaker_roles`` supaya
evidence dipilih pada pihak yang benar. Bila keterangan pengucap mengikuti label hasil
perbaikan, QC melihat "customer" di Reason tetapi "Agent" di PDF-nya sendiri — dan mengira
AI salah baca. Maka pembagian tugasnya:

* ``fix_speaker_roles``  -> hanya untuk PEMILIHAN evidence (apa yang dinilai AI);
* modul ini              -> keterangan PENGUCAP, dari label mentah PDF, deterministik
                            (tidak diminta ke LLM).

Contoh 120220DIh2 (SC_CL_23_1): jawaban tanggal lahir customer tercatat di giliran
berlabel "Agent". Evidence-nya tepat, tetapi di transkrip asli diucapkan "Agent" -> verifikasi
statik BELUM_SESUAI, dan Reason kini menuliskannya.
"""
import re

from compliance.pdf_parser import parse_transcript_pdf, ticket_id_from_filename

MARKER = "pada transkrip asli"
_MIN_FRAGMENT = 14


def _norm(text) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(text or "").lower())).strip()


def _label(raw) -> str:
    low = str(raw or "").strip().lower()
    if low.startswith("agent"):
        return "Agent"
    if low.startswith("customer") or low.startswith("nasabah"):
        return "Customer"
    return str(raw or "").strip()


def _fragments(quote: str) -> list[str]:
    """Potongan kutipan yang dicari di segmen mentah (kutipan LLM bisa bertanda '...')."""
    out = []
    for part in re.split(r"\.{3,}|…", str(quote or "")):
        n = _norm(part)
        if len(n) < _MIN_FRAGMENT:
            continue
        out.append(n[:60])
        if len(n) > 120:
            out.append(n[-60:])
    return out


def speaker_labels(quote: str, segments: list[dict]) -> list[str]:
    """Label mentah (urut kemunculan) segmen yang memuat potongan kutipan."""
    frags = _fragments(quote)
    if not frags:
        return []
    found = []
    for seg in segments:
        text = _norm(seg.get("text"))
        if any(f in text for f in frags):
            lab = _label(seg.get("speaker"))
            if lab and lab not in found:
                found.append(lab)
    return found


def _sentence(labels: list[str]) -> str:
    return f"Kutipan evidence berlabel {' dan '.join(labels)} {MARKER}."


def stamp_reason_speaker(evaluation, pdf_paths):
    """Tempel keterangan pengucap ke ``reason`` tiap baris scorecard yang punya kutipan.

    Dipanggil SEBELUM ``stamp_reason_provenance`` (ekor "- Evidence diambil dari ..."
    dibuang dan dipasang ulang di sana; kalimat ini berada di badan reason, jadi utuh).
    Baris tanpa kutipan, kutipan yang tidak ditemukan di transkrip mentah, atau reason
    yang sudah memuatnya dibiarkan. Non-destruktif; idempoten.
    """
    rows = (evaluation or {}).get("scorecard_result")
    if not isinstance(rows, list) or not pdf_paths:
        return evaluation
    raw = {}
    for path in pdf_paths:
        try:
            raw[ticket_id_from_filename(path)] = parse_transcript_pdf(path)
        except Exception:  # satu PDF rusak tidak boleh menggagalkan penilaian
            continue
    if not raw:
        return evaluation

    out, berubah = [], False
    for row in rows:
        r = row if isinstance(row, dict) else {}
        ev = r.get("evidence") or {}
        quote = ev.get("quote")
        reason = str(r.get("reason") or "")
        if not quote or not reason or MARKER in reason:
            out.append(row)
            continue
        stem = str(ev.get("ticket_id") or "").strip().removesuffix(".pdf")
        labels = speaker_labels(quote, raw.get(stem) or [])
        if not labels:
            out.append(row)
            continue
        tail = re.search(r"\s*[-–—]\s*Evidence\s+diambil\s+dari\s+[^.]*\.?\s*$", reason, re.IGNORECASE)
        pokok = reason[: tail.start()] if tail else reason
        pokok = pokok.strip().rstrip(".;,- ")
        baru = f"{pokok}. {_sentence(labels)}"
        if tail:
            baru += f" {tail.group(0).strip()}"
        out.append({**r, "reason": baru})
        berubah = True
    if not berubah:
        return evaluation
    return {**evaluation, "scorecard_result": out}
