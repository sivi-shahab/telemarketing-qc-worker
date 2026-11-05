"""Pemeriksa keberadaan kutipan evidence di transkrip MENTAH (28 Oktober 2026).

Aturan: evidence adalah kutipan transkrip, TIDAK BOLEH dikarang. LLM kadang menulis kutipan yang
meniru bunyi script/KB padahal tidak pernah diucapkan (tiket 010121GT1p, SC_RI_31: "Pada saat proses
analisa apabila analis kami memerlukan dokumen tambahan, mohon kesediaan Bapak/Ibu untuk melengkapinya"
tidak ada di transkrip), atau mengutip kalimat nyata yang tidak mendukung klaimnya.

Modul ini hanya menjawab satu pertanyaan DETERMINISTIK: seberapa banyak isi kutipan benar-benar ada di
transkrip PDF mentah (bukan versi yang sudah dibalik labelnya untuk LLM). Ukurannya DUKUNGAN SHINGLE:
potongan 3 kata berurutan dari kutipan yang ditemukan di teks transkrip (normalisasi sama dengan
``reason_speaker``: huruf kecil, tanda baca dibuang). Nilai 0..1.

TIDAK menilai apakah kutipan mendukung klaim item (itu tugas KB/LLM); hanya apakah kutipannya NYATA.
"""
import re

# Kutipan LLM sering menyambung beberapa segmen pendek (jawaban nasabah yang terpotong, mis. "Tanggal 23 Desember.
# Tahun 1992." = dua segmen dengan ucapan agent di antaranya), jadi kalimat diperiksa terpisah.
_PIECE_SPLIT = re.compile(r"\|\||\||\.{3,}|…|\n+|(?<=[.?!])\s+")
_TS_TOKEN = re.compile(r"\[?\s*\d+:\d+(?:\.\d+)?\s*(?:->|-|–|—|to)\s*\d+:\d+(?:\.\d+)?\s*\]?")
_LABEL = re.compile(r"^\s*\[?\s*(?:p\d+\s*\]?\s*\[?\s*)?(?:agent|customer|nasabah|speaker_?\d+)\b[^:\]]{0,24}[:\]]\s*", re.IGNORECASE)
N = 3
# Kutipan yang didominasi angka (nomor rekening, NIK, nominal) tidak diperiksa: ASR menuliskan angka yang sama
# dengan banyak variasi ("kosong delapan" / "08" / "eh empat dua delapan"), sehingga kemiripan shingle-nya rendah
# walau kutipannya nyata (SC_CL_14 nomor rekening: 0,57-0,62).
_ANGKA = {"nol", "kosong", "satu", "dua", "tiga", "empat", "lima", "enam", "tujuh", "delapan", "sembilan",
          "sepuluh", "sebelas", "belas", "puluh", "ratus", "ribu", "juta", "seratus", "se"}
_ANGKA_DOMINAN = 0.5


def _norm(text) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(text or "").lower())).strip()


def quote_pieces(quote) -> list:
    """Potongan kutipan (token, sudah dinormalisasi) tanpa label pembicara/timestamp/penanda panggilan."""
    out = []
    for raw in _PIECE_SPLIT.split(str(quote or "")):
        raw = _TS_TOKEN.sub(" ", raw)
        raw = _LABEL.sub("", raw)
        toks = _norm(raw).split()
        if toks:
            out.append(toks)
    return out


def _shingles(tokens, n=N):
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def haystack(segments) -> tuple:
    """``(daftar_token, himpunan_shingle, teks)`` dari segmen PDF mentah, urut kemunculan."""
    toks = []
    for seg in segments or []:
        toks.extend(_norm((seg or {}).get("text")).split())
    return toks, _shingles(toks), " " + " ".join(toks) + " "


def support(quote, hay) -> "float | None":
    """Dukungan kutipan terhadap ``hay`` (hasil ``haystack``): 0..1, atau None bila kutipan tak punya
    kata yang bisa diperiksa. Potongan < 3 kata dicari sebagai rangkaian kata utuh."""
    _toks, shingles, text = hay
    semua = [t for toks in quote_pieces(quote) for t in toks]
    if semua and sum(1 for t in semua if t in _ANGKA or t.isdigit()) / len(semua) >= _ANGKA_DOMINAN:
        return None
    total = found = 0
    for toks in quote_pieces(quote):
        if len(toks) < N:
            total += 1
            found += 1 if f" {' '.join(toks)} " in text else 0
            continue
        sh = _shingles(toks)
        total += len(sh)
        found += sum(1 for s in sh if s in shingles)
    return None if total == 0 else found / total


# ---------------------------------------------------------------------------------------------
# Penerapan pada hasil penilaian
# ---------------------------------------------------------------------------------------------
import os  # noqa: E402

#: Dukungan di bawah ini = kutipan dianggap TIDAK ADA di transkrip. Dikalibrasi pada audit 28 Oktober 2026
#: (``core/scripts/audit_evidence_quotes.py``): 4.349 kutipan nyata di 115 tiket (Cashline, NTB, Reinstate)
#: bernilai minimal 0,75; kutipan karangan (tiket 010121GT1p, SC_RI_31) bernilai 0,00.
THRESHOLD_TIDAK_DITEMUKAN = 0.5


def enabled() -> bool:
    """Sakelar darurat: ``EVIDENCE_CHECK=off`` mematikan pemeriksaan (perilaku sebelum 28 Okt 2026)."""
    return os.environ.get("EVIDENCE_CHECK", "on").strip().lower() != "off"


def apply_evidence_check(evaluation, pdf_paths):
    """Periksa kutipan evidence tiap baris ``scorecard_result`` terhadap transkrip PDF mentah.

    * ``SESUAI`` dengan kutipan yang tidak ditemukan -> diturunkan ke ``BELUM_SESUAI`` (skor 0, kutipan
      dikosongkan): kredit tidak boleh diberikan atas bukti yang tidak ada.
    * ``BELUM_SESUAI`` dengan kutipan yang tidak ditemukan -> status tetap, hanya ditandai (``evidence_check``):
      kegagalan item tidak bergantung pada kutipan, dan kutipan nama/ejaan kadang berbeda ejaan dari PDF.
    * ``TIDAK_DINILAI``, baris tanpa kutipan, dan kutipan yang tak bisa diperiksa (kosong, didominasi angka)
      dilewati. Kutipan asli LLM disimpan di ``evidence_check.quote_llm`` untuk penelusuran.

    HARUS dipanggil pada keluaran LLM mentah, SEBELUM langkah yang menulis ulang evidence (mis. kutipan rakitan
    "TMS: ... | ucapan: ..." di NTB) dan sebelum ``stamp_reason_speaker``. Non-destruktif bila tidak ada yang berubah.

    Yang TIDAK diperiksa: evidence di luar scorecard (objek minat, error code, badword) dan apakah kutipan yang nyata
    benar-benar MENDUKUNG klaim item (itu tugas aturan KB)."""
    if not enabled() or not isinstance(evaluation, dict) or not pdf_paths:
        return evaluation
    rows = evaluation.get("scorecard_result")
    if not isinstance(rows, list) or not rows:
        return evaluation
    from compliance.pdf_parser import parse_transcript_pdf  # impor lambat: modul ini juga dipakai skrip audit

    segments = []
    for path in pdf_paths:
        try:
            segments.extend(parse_transcript_pdf(path))
        except Exception:  # satu PDF rusak tidak boleh menggagalkan penilaian
            continue
    return apply_evidence_check_segments(evaluation, segments)


def apply_evidence_check_segments(evaluation, segments):
    """Inti ``apply_evidence_check`` untuk segmen yang sudah di-parse (dipakai juga oleh tes)."""
    if not segments or not isinstance(evaluation, dict):
        return evaluation
    rows = evaluation.get("scorecard_result")
    if not isinstance(rows, list) or not rows:
        return evaluation
    hay = haystack(segments)

    out, diturunkan, ditandai = [], [], []
    for row in rows:
        r = row if isinstance(row, dict) else {}
        status = str(r.get("status") or "").upper()
        ev = r.get("evidence") if isinstance(r.get("evidence"), dict) else {}
        quote = ev.get("quote")
        sup = support(quote, hay) if quote and status in ("SESUAI", "BELUM_SESUAI") else None
        if sup is None or sup >= THRESHOLD_TIDAK_DITEMUKAN:
            out.append(row)
            continue
        info = {"support": round(sup, 2), "threshold": THRESHOLD_TIDAK_DITEMUKAN, "quote_llm": quote}
        code = r.get("item_code")
        if status == "SESUAI":
            diturunkan.append(code)
            out.append({
                **r,
                "status": "BELUM_SESUAI",
                "item_score": 0,
                "evidence": {"timestamp": None, "quote": None, "ticket_id": ev.get("ticket_id")},
                "reason": ("Kutipan evidence yang diajukan tidak ditemukan di transkrip, sehingga item tidak dapat "
                           f"dinyatakan sesuai (dukungan {sup:.0%})."),
                "point_of_improvement": r.get("point_of_improvement") or (
                    f"Sampaikan dengan jelas dan lengkap: {str(r.get('requirement') or '').strip().rstrip('.')}."),
                "evidence_check": {**info, "tindakan": "diturunkan_ke_belum_sesuai", "status_llm": "SESUAI"},
            })
        else:
            ditandai.append(code)
            out.append({**r, "evidence_check": {**info, "tindakan": "ditandai"}})
    if not diturunkan and not ditandai:
        return evaluation
    return {**evaluation, "scorecard_result": out,
            "evidence_check": {"diturunkan": diturunkan, "ditandai": ditandai}}
