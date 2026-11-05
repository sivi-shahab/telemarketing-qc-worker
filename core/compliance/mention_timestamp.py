"""Pagar timestamp penyebutan nasabah (``extracted_mentions``) — 28 Oktober 2026.

LLM menulis ``timestamp`` tiap penyebutan sendiri, dan pada blok panjang hasil diarisasi
(segmen yang menyatukan beberapa menit percakapan) ia MENEBAK. Tiket 080822Ha4J: kalimat
"Yosi Nara Ramaya" ada di blok ``00:01.18 -> 18:00.30``, tetapi LLM menulis
``18:00.68 - 18:08.74`` — detik yang bahkan jatuh DI LUAR blok. Hasil lama menulis
``00:01.18`` (awal blok). Keduanya tidak bisa dipakai QC untuk menemukan titik di audio.

Modul ini memeriksa tiap penyebutan terhadap segmen PDF MENTAH (yang juga dilihat QC):

* nilai penyebutan dicari di segmen (normalisasi sama dengan ``reason_speaker``);
* timestamp LLM dipertahankan bila awalnya jatuh DI DALAM segmen yang memuat nilai itu
  (toleransi 1 detik sebelum awal, karena LLM sering memotong pecahan detik);
* selain itu diganti dengan RENTANG segmen yang memuat nilai: ``"<awal> - <akhir>"``.
  Rentang itu jujur — pada blok panjang QC tetap harus mencari di dalamnya, tetapi
  tidak pernah dikirim ke detik yang salah. Timestamp asli LLM disimpan di
  ``timestamp_llm`` dan ``timestamp_corrected`` bernilai True.

Tidak menyentuh apa pun bila nilai tidak ditemukan di PDF (tidak mengarang timestamp),
dan hanya mengubah ``timestamp``. Non-destruktif; idempoten.
"""
import os
import re

_TS = re.compile(r"(\d+):(\d+(?:\.\d+)?)")
_BEFORE_TOLERANCE = 1.0
_VERIFICATION_LISTS = (
    "card_holder_verification",
    "cashline_data_verification",
    "campaign_interest_verification",
)


def _norm(text) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(text or "").lower())).strip()


def _seconds(ts) -> list:
    """Semua waktu (detik) dalam string timestamp, urut kemunculan."""
    return [int(m) * 60 + float(s) for m, s in _TS.findall(str(ts or ""))]


def _format_range(segment_ts: str) -> str:
    parts = _TS.findall(str(segment_ts or ""))
    if len(parts) >= 2:
        a = f"{parts[0][0].zfill(2)}:{parts[0][1]}"
        b = f"{parts[1][0].zfill(2)}:{parts[1][1]}"
        return f"{a} - {b}"
    return str(segment_ts or "").strip()


def fix_mention_timestamp(mention, segments: list):
    """Kembalikan ``mention`` (dict) dengan timestamp yang sudah divalidasi ke
    ``segments`` (hasil ``parse_transcript_pdf``). Elemen bukan dict dan nilai yang
    tidak ditemukan dikembalikan apa adanya."""
    if not isinstance(mention, dict):
        return mention
    value = _norm(mention.get("value"))
    if len(value) < 3:
        return mention
    needle = f" {value} "
    hits = []
    for seg in segments or []:
        times = _seconds(seg.get("timestamp"))
        if len(times) < 2:
            continue
        if needle in f" {_norm(seg.get('text'))} ":
            hits.append((times[0], times[1], seg))
    if not hits:
        return mention
    stated = _seconds(mention.get("timestamp"))
    if stated:
        for start, end, _ in hits:
            if start - _BEFORE_TOLERANCE <= stated[0] <= end:
                return mention                       # sudah di dalam segmen yang benar
        # Diganti ke segmen terdekat dengan waktu yang disebut LLM.
        start, end, seg = min(
            hits, key=lambda h: min(abs(stated[0] - h[0]), abs(stated[0] - h[1]))
        )
    else:
        start, end, seg = hits[0]
    return {
        **mention,
        "timestamp": _format_range(seg.get("timestamp")),
        "timestamp_llm": mention.get("timestamp"),
        "timestamp_corrected": True,
    }


def fix_mention_timestamps(evaluation, segments_by_file: dict):
    """Terapkan ``fix_mention_timestamp`` ke semua ``extracted_mentions`` di
    ``evaluation``. ``segments_by_file``: ``{nama_berkas.pdf: [segmen, ...]}``.
    Mengembalikan evaluasi baru hanya bila ada yang berubah."""
    if not isinstance(evaluation, dict) or not segments_by_file:
        return evaluation
    changed = False
    out = dict(evaluation)
    for key in _VERIFICATION_LISTS:
        rows = evaluation.get(key)
        if not isinstance(rows, list):
            continue
        new_rows = []
        for row in rows:
            mentions = row.get("extracted_mentions") if isinstance(row, dict) else None
            if not isinstance(mentions, list) or not mentions:
                new_rows.append(row)
                continue
            fixed = []
            for m in mentions:
                src = os.path.basename(str(m.get("source_file") or "")) if isinstance(m, dict) else ""
                if src and src in segments_by_file:
                    segs = segments_by_file[src]
                else:
                    segs = [s for lst in segments_by_file.values() for s in lst]
                fixed.append(fix_mention_timestamp(m, segs))
            if fixed != mentions:
                changed = True
                row = {**row, "extracted_mentions": fixed}
            new_rows.append(row)
        out[key] = new_rows
    return out if changed else evaluation


def stamp_mention_timestamps(evaluation, pdf_paths):
    """Baca PDF mentah lalu panggil ``fix_mention_timestamps``. Satu PDF rusak tidak
    boleh menggagalkan penilaian."""
    if not pdf_paths:
        return evaluation
    from compliance.pdf_parser import parse_transcript_pdf

    segments_by_file = {}
    for path in pdf_paths:
        try:
            segments_by_file[os.path.basename(path)] = parse_transcript_pdf(path)
        except Exception:
            continue
    return fix_mention_timestamps(evaluation, segments_by_file)
