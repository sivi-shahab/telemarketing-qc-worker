"""Document-type config for the Upload Document / OCR + verification feature.

Shared between the API (form field validation + labels) and the worker (resolving
the per-type prompt module). The API only touches ``DOCUMENT_TYPES`` /
``is_valid_doc_type`` (dependency-free); ``build_ocr_request`` is used by the
worker and lazily imports the prompt module for the given document type.

Each prompt module (``prompt/ocr_<type>.py``) defines ``PROMPT`` / ``PROPS`` /
``REQUIRED`` / ``SCHEMA`` plus ``build_prompt(reference)``, which injects the bank
reference ("acuan") values fetched from the CSVs.
"""
import importlib
import os
import re
import sys
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher

# Repo root: <repo>/compliance/documents.py -> <repo>. The prompt modules live in
# <repo>/prompt and are imported lazily (importlib) at task runtime; the celery
# worker's sys.path does not reliably include the repo root, so ensure it here.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Ordered: defines the order of slots in the upload modal and display.
DOCUMENT_TYPES: dict[str, dict] = {
    "ktp": {"label": "KTP", "prompt_module": "prompt.ocr_ktp"},
    "kk": {"label": "KK", "prompt_module": "prompt.ocr_kk"},
    "npwp": {"label": "NPWP", "prompt_module": "prompt.ocr_npwp"},
    "cover_buku_tabungan": {
        "label": "Cover Buku Tabungan",
        "prompt_module": "prompt.ocr_cover_buku_tabungan",
    },
    "mus_exception_confirmation": {
        "label": "Konfirmasi Pengecualian MUS",
        "prompt_module": "prompt.ocr_mus_exception",
    },
}


def is_valid_doc_type(doc_type: str) -> bool:
    return doc_type in DOCUMENT_TYPES


# --------------------------------------------------------------------------
# Card-holder similarity bands -> required supporting document (Fase #5).
# --------------------------------------------------------------------------
# A STATIC card-holder field whose ``similarity_percent`` lands in the "grey band"
# is neither a clean MATCH nor an agent error: the customer's answer is close
# enough to be plausible but not identical, so the bank asks for the supporting
# document instead of failing the ticket outright. While that document is missing
# the ticket is AI Status PENDING (H+2 SLA, see ``stats_aggregate``); past the SLA
# it becomes Not Qualified — exactly the same machinery the TMS-change triggers use.
#
# Aturan LAMA (tiket yang belum pernah diproses dengan prompt v56):
#   nama_ibu_kandung  >= 90         -> MATCH, no document needed
#                     80   ..  < 90 -> KK required   (AI Status PENDING)
#                     < 80          -> MISMATCH (agent error)
#   tanggal_lahir     == 100        -> MATCH, no document needed
#                     87.5 .. < 100 -> KTP required  (AI Status PENDING)
#                     < 87.5        -> MISMATCH (agent error)
#
# ``doc_min`` is ALSO the MISMATCH boundary the campaign prompt applies, so the
# LLM's own match decision agrees with this table. Change a threshold here and in
# the prompt's MATCH DECISION BY SIMILARITY THRESHOLD block together.
CARD_HOLDER_DOC_BANDS: dict[str, dict] = {
    "nama_ibu_kandung": {
        "doc_type": "kk",
        "doc_min": 80.0,    # inclusive lower bound of the band (= MISMATCH boundary)
        "match_min": 90.0,  # at/above this the field is a clean MATCH, no document
        "label": "Nama Ibu Kandung",
    },
    "tanggal_lahir": {
        "doc_type": "ktp",
        "doc_min": 87.5,
        "match_min": 100.0,
        "label": "Tanggal Lahir",
    },
}

# Aturan BARU (revamp verifikasi statik, 21 Agustus 2026 — prompt v56). Hanya
# ``nama_ibu_kandung`` yang ambangnya berubah; ``tanggal_lahir`` tetap seperti semula.
#
#   nama_ibu_kandung  >= 80         -> MATCH, no document needed
#                     50   ..  < 80 -> KK required   (AI Status PENDING, SLA H+2)
#                     < 50          -> MISMATCH (agent error, TANPA dokumen)
#   tanggal_lahir     == 100        -> MATCH  (tidak berubah)
#                     87.5 .. < 100 -> KTP required
#                     < 87.5        -> MISMATCH
CARD_HOLDER_DOC_BANDS_V2: dict[str, dict] = {
    "nama_ibu_kandung": {
        "doc_type": "kk",
        "doc_min": 50.0,
        "match_min": 80.0,
        "label": "Nama Ibu Kandung",
    },
    "tanggal_lahir": dict(CARD_HOLDER_DOC_BANDS["tanggal_lahir"]),
}

# Aturan mana yang dipakai TIDAK ditebak dari waktu upload melainkan DICAP ke dalam
# evaluasi saat tiket diproses (``compliance.static_similarity.stamp_static_rules``):
# ``evaluation["static_rules_version"] = 2`` untuk tiket yang dievaluasi dengan prompt
# v56 ke atas. Cap itu membekukan aturan pada versi prompt yang benar-benar dipakai —
# lebih tepat daripada ambang waktu, yang akan meleset untuk tiket yang di-upload di
# sekitar jam deploy atau diproses ulang belakangan.
#
# Konsekuensinya persis yang diinginkan: band dibaca ulang setiap kali halaman dibuka,
# tetapi tiket LAMA tetap dibaca dengan tabel lamanya — tidak ada riwayat yang
# diam-diam dinilai ulang, dan tidak ada tiket yang tiba-tiba PENDING dengan tenggat
# H+2 yang sudah lama tutup.
STATIC_RULES_VERSION_KEY = "static_rules_version"


def static_rules_version(evaluation) -> int:
    """Versi aturan verifikasi statik yang membekukan sebuah evaluasi: 2 (revamp 21
    Agustus 2026) atau 1 (sebelumnya, termasuk semua hasil yang belum punya cap)."""
    if not isinstance(evaluation, dict):
        return 1
    try:
        return 2 if int(evaluation.get(STATIC_RULES_VERSION_KEY) or 1) >= 2 else 1
    except (TypeError, ValueError):
        return 1


def card_holder_doc_bands(evaluation=None) -> dict:
    """Tabel band yang berlaku untuk sebuah evaluasi (lihat ``static_rules_version``)."""
    return CARD_HOLDER_DOC_BANDS_V2 if static_rules_version(evaluation) >= 2 else CARD_HOLDER_DOC_BANDS


# The similarity bands are evaluated at READ time from an already-stored
# ``similarity_percent``, so without a cutoff they would silently re-judge every
# historical ticket — tickets whose H+2 upload window closed long ago, i.e. an
# instant Qualified -> Not Qualified flip nobody can act on. Only results uploaded
# at/after this moment are subject to the bands. Set to ``None`` to apply the rule
# to the whole history.
#
# Compared against ``Result.uploaded_at``, which is naive UTC (see
# ``stats_aggregate._wib_month``), so this instant is UTC too — 07:00 WIB on the 6th.
CARD_HOLDER_DOC_BANDS_EFFECTIVE_FROM = datetime(2026, 8, 6)


def card_holder_bands_apply(uploaded_at) -> bool:
    """True when the similarity bands govern a result uploaded at ``uploaded_at``."""
    cutoff = CARD_HOLDER_DOC_BANDS_EFFECTIVE_FROM
    if cutoff is None:
        return True
    if not isinstance(uploaded_at, datetime):
        return False  # unknown upload time -> treat as historical (no retro-flip)
    return uploaded_at >= cutoff


def _evaluation_of(result_json) -> dict | None:
    """Accept either a stored ``result_json`` or the ``evaluation`` object itself."""
    if not isinstance(result_json, dict):
        return None
    evaluation = result_json.get("evaluation")
    if isinstance(evaluation, dict):
        return evaluation
    return result_json if "card_holder_verification" in result_json else None


def _similarity(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return None if num != num else num  # NaN -> None


def in_document_band(item, rule, evaluation=None) -> bool:
    """Baris ini berada di zona abu-abu yang MEWAJIBKAN dokumen pendukung?

    Satu-satunya sumber kebenaran untuk pertanyaan itu — dipakai bersama oleh
    ``card_holder_doc_requirements`` (dokumen apa yang diminta) dan
    ``error_codes.apply_static_document_status`` (MATCH -> PENDING/MISMATCH),
    supaya keduanya tidak pernah berbeda pendapat.

    Murni pembacaan tabel band. Kasus "nama di Ascend terpotong/disingkat" TIDAK
    lagi ditangani di sini: sejak 24 Agustus 2026 ia diselesaikan satu lapis lebih
    awal oleh ``static_similarity._align_abbreviations``, yang menyelaraskan ucapan
    nasabah ke bentuk singkatan Ascend sebelum similarity dihitung. Hasilnya
    ``similarity_percent`` sendiri sudah benar (010550Vosa: 54% -> 84%), sehingga
    tabel band cukup dibaca apa adanya dan tidak ada pengecualian yang harus
    diketahui banyak tempat sekaligus.
    """
    if rule is None or not isinstance(item, dict):
        return False
    sim = _similarity(item.get("similarity_percent"))
    return sim is not None and rule["doc_min"] <= sim < rule["match_min"]


def card_holder_doc_requirements(result_json) -> list[dict]:
    """Supporting documents required because a STATIC card-holder field landed in
    the grey band above.

    Returns ``[{"field", "doc_type", "similarity", "label"}]`` ordered like
    ``DOCUMENT_TYPES``; empty when nothing is required.

    Baris yang dihitung: ``MATCH`` (zona abu-abu yang belum diproses status
    dokumennya) dan ``PENDING`` (zona abu-abu yang dokumennya memang sedang ditunggu —
    lihat ``error_codes.apply_static_document_status``). Keduanya WAJIB diterima:
    kalau hanya ``MATCH`` yang dihitung, sebuah baris yang sudah berubah menjadi
    ``PENDING`` akan kehilangan kewajiban dokumennya dan tenggat H+2-nya berhenti
    berjalan. ``MISMATCH`` tidak dihitung — itu sudah kesalahan agent dan tidak
    membutuhkan dokumen.

    Tabel band-nya dipilih dari cap versi pada evaluasi itu sendiri (lihat
    ``card_holder_doc_bands``). Pada tiket LAMA sebuah MISMATCH statik bisa membawa
    nilai minimum ANTAR-PENYEBUTAN di ``similarity_percent`` (STATIC VERIFICATION
    CONSISTENCY RULE yang sudah dihapus untuk tiket baru), bukan kemiripan terhadap
    Ascend — itu tidak boleh dibaca sebagai nilai band, dan tidak akan terbaca karena
    barisnya MISMATCH.
    """
    evaluation = _evaluation_of(result_json)
    if evaluation is None:
        return []
    items = evaluation.get("card_holder_verification")
    if not isinstance(items, list):
        return []
    bands = card_holder_doc_bands(evaluation)
    found: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        rule = bands.get(item.get("field"))
        if rule is None or item.get("match") not in ("MATCH", "PENDING"):
            continue
        if not in_document_band(item, rule, evaluation):
            continue
        sim = _similarity(item.get("similarity_percent"))
        found.setdefault(item["field"], {
            "field": item["field"],
            "doc_type": rule["doc_type"],
            "similarity": sim,
            "label": rule["label"],
        })
    order = list(DOCUMENT_TYPES)
    return sorted(found.values(), key=lambda r: order.index(r["doc_type"]))


def format_similarity(value: float) -> str:
    """``85.0 -> "85"``, ``87.5 -> "87,5"`` (Indonesian decimal comma)."""
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}".replace(".", ",")


# --------------------------------------------------------------------------
# Cashline data -> dokumen pendukung (24 Agustus 2026).
# --------------------------------------------------------------------------
# ``nama_pemilik_rekening`` menentukan apakah dana bisa cair: beda ejaan sekecil
# apa pun sudah cukup membuat transfer ditolak bank penerima. Ambangnya karena itu
# 90% (bukan 80% seperti field lain), dan setiap MISMATCH WAJIB dibuktikan dengan
# **cover buku tabungan**.
#
# Sebelum ini kewajibannya HANYA berupa kalimat di ``reason`` yang ditulis LLM —
# ``db/crud.py`` bahkan mencatat "cover_buku_tabungan still has no trigger at all".
# Akibatnya QC disuruh mengejar dokumen yang tidak pernah diminta sistem: ia tidak
# muncul di daftar dokumen wajib, tiketnya tidak pernah PENDING menunggunya, dan
# slot unggahnya tidak terbuka.
#
# Dijadikan pemicu penuh (keputusan 24 Agustus 2026), setara KK/KTP: masuk daftar
# wajib, membuat tiket PENDING selama tenggat H+2 berjalan, dan menerbitkan B09
# bila tenggat lewat tanpa unggah. Konsekuensi yang diterima dengan sadar: tiket
# lama yang tenggatnya sudah lewat langsung terbaca Not Qualified.
_CASHLINE_DOC_FIELDS = {
    "nama_pemilik_rekening": {
        "doc_type": "cover_buku_tabungan",
        "label": "Nama Pemilik Rekening",
    },
}


def cashline_doc_requirements(result_json) -> list[dict]:
    """Dokumen pendukung yang diminta oleh ``cashline_data_verification``.

    Bentuk kembaliannya SAMA dengan ``card_holder_doc_requirements``
    (``[{"field", "doc_type", "similarity", "label"}]``) supaya kedua sumber
    kewajiban bisa disatukan pemanggilnya tanpa perlakuan khusus.

    Dipicu baris ``MISMATCH`` DAN ``PENDING``. Berbeda dari verifikasi statik yang
    zona abu-abunya ditulis ``MATCH``, di sini zona bawah tetap MISMATCH — skor tetap
    dipotong DAN dokumen tetap diminta. ``PENDING`` adalah keadaan antara sejak
    31 Agustus 2026: dokumen sudah diminta dan tenggat H+2 masih berjalan, jadi
    permintaannya harus tetap muncul.
    """
    evaluation = _evaluation_of(result_json)
    if evaluation is None:
        return []
    items = evaluation.get("cashline_data_verification")
    if not isinstance(items, list):
        return []
    found: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        rule = _CASHLINE_DOC_FIELDS.get(item.get("field"))
        # ``PENDING`` ikut memicu (31 Agustus 2026): sejak baris cashline yang menunggu
        # dokumen ditulis PENDING oleh ``apply_cashline_document_status``, membatasi
        # pemicu pada MISMATCH akan membuat permintaan dokumennya HILANG tepat setelah
        # penangguhan dimulai — dokumen berhenti diminta justru selama masa tunggunya.
        if rule is None or item.get("match") not in ("MISMATCH", "PENDING"):
            continue
        found.setdefault(item["field"], {
            "field": item["field"],
            "doc_type": rule["doc_type"],
            "similarity": _similarity(item.get("similarity_percent")),
            "label": rule["label"],
        })
    return list(found.values())


# --------------------------------------------------------------------------
# Konfirmasi pengecualian MUS untuk penyakit whitelist (11 September 2026).
# --------------------------------------------------------------------------
# Lihat docs/csv_bank/11 September 2026/MUS_logic_update.md. Saat
# ``mus_exemption.status`` == EXEMPT karena penyakit yang TERMASUK
# ``mus_exemption.DISEASE_WHITELIST`` (``mus_exemption.disease_listed``,
# diklasifikasi LLM), Bank Mega meminta screenshot email konfirmasi sebelum
# pengecualiannya benar-benar berlaku — lihat
# ``error_codes.apply_mus_exception_document_status``.
MUS_EXCEPTION_DOC_TYPE = "mus_exception_confirmation"


def mus_exception_doc_requirements(result_json) -> list[dict]:
    """Dokumen konfirmasi yang diminta karena pengecualian MUS berasal dari penyakit
    whitelist.

    Bentuk kembaliannya sama dengan ``card_holder_doc_requirements`` /
    ``cashline_doc_requirements``. Dipicu status ``EXEMPT`` (belum digerbang, bacaan
    mentah LLM) DAN ``PENDING`` (sudah digerbang read-time, dokumen masih ditunggu) —
    sama seperti kedua fungsi saudaranya, supaya permintaan dokumen tidak hilang
    begitu penangguhan dimulai.
    """
    evaluation = _evaluation_any(result_json)
    if not isinstance(evaluation, dict):
        return []
    block = evaluation.get("mus_exemption")
    if not isinstance(block, dict):
        return []
    if str(block.get("status") or "").strip().upper() not in ("EXEMPT", "PENDING"):
        return []
    if not block.get("disease_listed"):
        return []
    return [{
        "field": "mus_exemption",
        "doc_type": MUS_EXCEPTION_DOC_TYPE,
        "similarity": None,
        "label": "Pengecualian MUS",
    }]


def mus_exception_doc_types(result_json) -> list[str]:
    """Jenis dokumen wajib khusus pengecualian MUS: ``[]`` atau
    ``["mus_exception_confirmation"]``."""
    return [req["doc_type"] for req in mus_exception_doc_requirements(result_json)]


def mus_exception_doc_confirmed(ticket_id, ocr_by_doc_type: dict) -> bool:
    """True bila dokumen ``mus_exception_confirmation`` milik tiket ini MEMBUKTIKAN
    pengecualian: OCR sudah selesai, menemukan kalimat persetujuan, DAN ticket ID
    yang terbaca pada email cocok dengan ``ticket_id`` yang sedang dinilai.

    ``ocr_by_doc_type`` = ``{doc_type: ocr_json}`` milik SATU tiket (lihat
    ``crud.document_ocr_by_result``). Pencocokan ticket ID case-insensitive + trim —
    keputusan 11 September 2026 TIDAK menyerahkan perbandingan ini ke LLM, supaya
    model tidak sekadar menggemakan ticket ID yang "diharapkan" alih-alih membaca
    dokumennya. Dokumen yang belum/gagal OCR dianggap belum mengonfirmasi apa pun.
    """
    ocr_json = (ocr_by_doc_type or {}).get(MUS_EXCEPTION_DOC_TYPE)
    if not isinstance(ocr_json, dict):
        return False
    if not ocr_json.get("approval_statement_present"):
        return False
    found = str(ocr_json.get("ticket_id_found") or "").strip().casefold()
    expected = str(ticket_id or "").strip().casefold()
    return bool(found) and bool(expected) and found == expected


# --------------------------------------------------------------------------
# Pembebasan kewajiban dokumen (3 September 2026).
# --------------------------------------------------------------------------
# Tiket yang SUDAH kena pelanggaran non-tolerable dari transkrip tidak lagi diminta
# dokumen pendukung apa pun. Alasannya sederhana: vonisnya sudah Not Qualified dan
# tidak ada dokumen yang bisa membatalkannya, jadi meminta berkas hanya memindahkan
# pekerjaan sia-sia ke Team Leader Sales — dan menerbitkan B09 karena berkas itu
# tidak datang berarti menghukum kelalaian yang tidak pernah punya jalan keluar.
#
# Dicabut SELURUHNYA (permintaan 3 September 2026), bukan sekadar disembunyikan
# tulisannya: tidak ada permintaan dokumen, tidak ada tenggat H+2, dan slot
# unggahnya tertutup.
#
# Yang dibaca adalah evaluasi PRA-status-dokumen — itulah yang membuat kalimatnya
# berbunyi "error LAIN yang non-tolerable". Item scorecard yang jatuh JUSTRU karena
# urusan dokumen (SC_CL_23_1/23_2 setelah tenggat lewat) belum ada pada titik itu,
# jadi pembebasan ini tidak bisa membenarkan dirinya sendiri.


def _evaluation_any(result_json) -> "dict | None":
    """``_evaluation_of`` yang tidak mensyaratkan adanya ``card_holder_verification``.

    Dipakai pemeriksaan non-tolerable, yang bahannya ``scorecard_result``."""
    if not isinstance(result_json, dict):
        return None
    evaluation = result_json.get("evaluation")
    if isinstance(evaluation, dict):
        return evaluation
    keys = ("scorecard_result", "card_holder_verification")
    return result_json if any(k in result_json for k in keys) else None


def _codes_awaiting_document(evaluation) -> set:
    """Item scorecard yang kegagalannya SEDANG MENUNGGU dokumen pendukung.

    Inilah satu-satunya item yang boleh dikecualikan dari uji "error non-tolerable
    lain": kegagalannya belum final, dan berkas yang diminta memang bisa
    menyembuhkannya. Dipetakan dari kewajiban dokumen yang benar-benar berlaku pada
    evaluasi ini —

      * band verifikasi statik -> ``SC_CL_23_1`` / ``SC_CL_23_2`` (KTP / KK);
      * cashline nama pemilik rekening -> ``SC_CL_13`` (cover buku tabungan);

    — bukan dari daftar kode tetap. Bedanya menentukan: sebuah ``SC_CL_23_2`` yang
    jatuh karena nama ibu kandung MISMATCH DI BAWAH band tidak menunggu dokumen apa
    pun (tidak ada berkas yang membatalkannya), jadi ia memang "error lain" dan
    membebaskan tiket dari kewajiban dokumen yang tersisa. Daftar kode tetap akan
    mengecualikannya juga, dan pembebasan itu tidak akan pernah berlaku.

    Peta field -> item diambil dari ``error_codes`` supaya tidak ada literal yang
    bisa menyimpang. Diimpor malas: ``error_codes`` sendiri mengimpor modul ini.
    """
    from compliance.error_codes import (
        CARD_HOLDER_STATIC_SCORECARD,
        CASHLINE_FIELD_SCORECARD,
    )

    codes = set()
    for req in card_holder_doc_requirements(evaluation):
        code = CARD_HOLDER_STATIC_SCORECARD.get(req["field"])
        if code:
            codes.add(code)
    for req in cashline_doc_requirements(evaluation):
        code = CASHLINE_FIELD_SCORECARD.get(req["field"])
        if code:
            codes.add(code)
    return codes


def doc_requirements_waived(result_json) -> bool:
    """True bila kewajiban dokumen tiket ini DICABUT karena sudah ada pelanggaran
    non-tolerable **lain** (lihat catatan di atas).

    Kata "lain" yang menanggung seluruh beban aturan ini: item scorecard yang jatuh
    JUSTRU karena verifikasi yang sedang dibuktikan dokumen itu
    (``_codes_awaiting_document``) TIDAK dihitung. Tanpa pengecualian itu aturannya
    memakan dirinya sendiri — MISMATCH nama pemilik rekening menjatuhkan SC_CL_13
    (non-tolerable), pembebasan lalu mencabut permintaan cover buku tabungan yang
    seharusnya menyembuhkannya, dan jalur dokumen yang sengaja dibuka 31 Agustus 2026
    tertutup lagi.
    """
    evaluation = _evaluation_any(result_json)
    if not isinstance(evaluation, dict):
        return False
    rows = evaluation.get("scorecard_result")
    if not isinstance(rows, list):
        return False
    awaiting = None
    for item in rows:
        it = item or {}
        if str(it.get("tolerable") or "").strip().upper() != "NO":
            continue
        if str(it.get("status") or "").strip().upper() != "BELUM_SESUAI":
            continue
        if awaiting is None:              # dihitung sekali, hanya bila perlu
            awaiting = _codes_awaiting_document(evaluation)
        if str(it.get("item_code") or "").strip().upper() in awaiting:
            continue
        return True
    return False


def required_doc_requirements(result_json) -> list[dict]:
    """SELURUH dokumen pendukung yang diminta hasil evaluasi — band verifikasi
    statik (KK/KTP) DITAMBAH cashline (cover buku tabungan).

    Satu-satunya fungsi yang boleh dipakai pemanggil untuk menjawab "dokumen apa
    yang wajib untuk tiket ini". Memisahkannya per sumber pernah membuat satu
    jalur tahu dan jalur lain tidak — persis cara cover buku tabungan sebelumnya
    diminta di kalimat ``reason`` tetapi tidak pernah diwajibkan sistem.

    Kosong bila ``doc_requirements_waived`` — tiket yang sudah kena pelanggaran
    non-tolerable lain tidak diminta dokumen apa pun.
    """
    if doc_requirements_waived(result_json):
        return []
    return (
        card_holder_doc_requirements(result_json)
        + cashline_doc_requirements(result_json)
        + mus_exception_doc_requirements(result_json)
    )


def required_doc_types(result_json) -> list[str]:
    """Jenis dokumen wajib (statik + cashline), tanpa duplikat, urut DOCUMENT_TYPES."""
    seen = {req["doc_type"] for req in required_doc_requirements(result_json)}
    return [t for t in DOCUMENT_TYPES if t in seen]


def card_holder_doc_trigger_labels(result_json) -> list[str]:
    """Human labels for the Upload Document "Diperlukan karena:" chips, e.g.
    ``"Nama Ibu Kandung 85% (perlu KK)"``."""
    labels = []
    for req in required_doc_requirements(result_json):
        doc_label = DOCUMENT_TYPES.get(req["doc_type"], {}).get("label", req["doc_type"].upper())
        sim = req.get("similarity")
        pct = f"{format_similarity(sim)}% " if sim is not None else ""
        labels.append(f"{req['label']} {pct}(perlu {doc_label})")
    return labels


def card_holder_doc_types(result_json) -> list[str]:
    """Jenis dokumen wajib. Sejak 24 Agustus 2026 mencakup KEDUA sumber (band
    verifikasi statik + cashline), sehingga seluruh pemanggil lama otomatis ikut
    mewajibkan cover buku tabungan. Nama lama dipertahankan agar pemanggilnya tidak
    perlu diubah; ``required_doc_types`` adalah nama yang lebih tepat."""
    return required_doc_types(result_json)


def load_prompt_module(doc_type: str):
    """Import and return the prompt module for ``doc_type``."""
    if doc_type not in DOCUMENT_TYPES:
        raise KeyError(f"unknown document type: {doc_type}")
    return importlib.import_module(DOCUMENT_TYPES[doc_type]["prompt_module"])


def normalize_ocr_json(doc_type: str, ocr_json):
    """Post-process the raw OCR output before it is stored.

    Verification rows whose ``field`` is listed in the prompt module's
    ``NUMERIC_FIELDS`` (NPWP number, NIK, account number) are reduced to
    digits-only on both sides — the document prints them with dots/dashes, the
    bank CSV stores bare digits — and their ``match`` is recomputed from those
    digits. Everything else is passed through untouched.

    Returns ``ocr_json`` unchanged when it is not the expected shape, so a
    surprising OCR payload is still stored as-is rather than dropped.
    """
    if not isinstance(ocr_json, dict):
        return ocr_json
    rows = ocr_json.get("verifications")
    if not isinstance(rows, list):
        return ocr_json
    try:
        module = load_prompt_module(doc_type)
    except KeyError:
        return ocr_json
    numeric = {str(f).strip().casefold() for f in getattr(module, "NUMERIC_FIELDS", ())}
    if not numeric:
        return ocr_json

    from prompt._common import normalize_numeric_row

    out = dict(ocr_json)
    out["verifications"] = [
        normalize_numeric_row(row)
        if isinstance(row, dict) and str(row.get("field", "")).strip().casefold() in numeric
        else row
        for row in rows
    ]
    return out


def build_ocr_request(doc_type: str, reference: dict | None = None) -> tuple[str, dict]:
    """Build the Mistral OCR request for ``doc_type``.

    Returns ``(prompt, schema)`` where ``prompt`` is the per-type prompt with the
    reference ("acuan") values injected and ``schema`` is the strict JSON
    ``document_annotation_format``.

    Instruksi identifikasi jenis dokumen ditempelkan DI SINI, bukan di tiap modul
    prompt: kalimatnya sama untuk keempat jenis, dan menyalinnya empat kali adalah
    empat kesempatan untuk lupa memperbaruinya. Ditempel SESUDAH ``build_prompt``
    supaya tidak ikut ``str.format`` yang mengisi nilai acuan.

    Dilewati untuk jenis dokumen yang TIDAK ikut klasifikasi ``jenis_dokumen``
    (``doc_type`` di luar ``_DOC_KIND_EXPECTED``, mis. ``mus_exception_confirmation``
    — bukan dokumen identitas dengan acuan bank, schema-nya sendiri tidak punya
    field itu, jadi instruksinya hanya akan membingungkan tanpa efek).
    """
    from prompt._common import DOC_KIND_INSTRUCTION

    module = load_prompt_module(doc_type)
    prompt = module.build_prompt(reference or {})
    if doc_type in _DOC_KIND_EXPECTED:
        prompt += DOC_KIND_INSTRUCTION
    return prompt, module.SCHEMA


# Slot dokumen -> nilai ``jenis_dokumen`` yang dianggap BENAR untuk slot itu.
# Cermin dari ``prompt._common.DOC_KIND_VALUES``.
_DOC_KIND_EXPECTED = {
    "ktp": "KTP",
    "kk": "KK",
    "npwp": "NPWP",
    "cover_buku_tabungan": "COVER_BUKU_TABUNGAN",
}


def wrong_document_type(doc_type: str, ocr_json) -> "dict | None":
    """``{"expected", "detected"}`` bila dokumen yang diunggah ke slot ``doc_type``
    ternyata jenis lain; None bila cocok atau tidak bisa dipastikan.

    Tidak dianggap salah jenis — sengaja, semuanya berarti "tidak tahu", dan
    menerbitkan error code atas ketidaktahuan lebih buruk daripada melewatkannya:

      * ``TIDAK_JELAS``  — berkasnya tidak terbaca. Itu keluhan mutu berkas, bukan
        salah jenis; menuduh agent mengunggah dokumen keliru berdasarkan berkas yang
        tidak terbaca adalah tuduhan tanpa bukti.
      * field ``jenis_dokumen`` tidak ada — hasil OCR dari sebelum field ini
        diperkenalkan. Dokumen lama tidak boleh tiba-tiba melahirkan error code baru.
      * slot yang tidak dikenal katalog.
    """
    from prompt._common import DOC_KIND_KEY

    expected = _DOC_KIND_EXPECTED.get(doc_type)
    if expected is None or not isinstance(ocr_json, dict):
        return None
    detected = str(ocr_json.get(DOC_KIND_KEY) or "").strip().upper()
    if not detected or detected == "TIDAK_JELAS" or detected == expected:
        return None
    label = DOCUMENT_TYPES.get(doc_type, {}).get("label", doc_type.upper())
    detected_label = next(
        (v["label"] for k, v in DOCUMENT_TYPES.items() if _DOC_KIND_EXPECTED.get(k) == detected),
        detected.replace("_", " ").title(),
    )
    return {"expected": label, "detected": detected_label}


# --------------------------------------------------------------------------
# Dokumen yang jenisnya benar tetapi ISINYA tidak cocok dengan acuan bank
# (3 September 2026).
# --------------------------------------------------------------------------
# Sampai tanggal ini sebuah dokumen dianggap MEMENUHI kewajibannya begitu jenisnya
# benar — hasil OCR-nya tidak ikut menentukan sama sekali. Akibatnya nyata di
# lapangan: 140909co8Q mengunggah NPWP bernomor 357691724502000 terhadap acuan
# 257681957526000 (similarity 0) dan tiketnya langsung lolos; 011013QyLm mengunggah
# KK yang OCR-nya sendiri menyimpulkan "bukan KK atas nama nasabah" (nilai terbaca
# kosong) dan tiketnya juga lolos. Dokumen diminta untuk MEMBUKTIKAN sebuah data,
# jadi berkas yang tidak membuktikan apa pun tidak boleh menutup kewajibannya.
#
# Sejak sekarang: dokumen yang salah satu baris verifikasinya TIDAK cocok dianggap
# BELUM memenuhi. Konsekuensinya mengikuti jalur yang sudah ada — tiket tetap
# PENDING selama tenggat H+2 berjalan dan jatuh Not Qualified (B09) bila tenggat
# lewat — persis seperti dokumen yang keliru jenisnya.
#
# Yang SENGAJA tidak dihitung sebagai ketidakcocokan, karena semuanya berarti
# "tidak bisa dipastikan" dan menghukum atas ketidaktahuan lebih buruk daripada
# melewatkannya:
#   * OCR belum/gagal selesai (``ocr_json`` kosong atau bukan bentuk yang dikenal);
#   * baris tanpa nilai ACUAN — data bank yang tidak tersedia bukan kesalahan
#     nasabah maupun agent, dan prompt-nya memang menulis ``match=false`` di situ;
#   * dokumen yang jenisnya keliru — itu sudah ditangani ``wrong_document_type``
#     (C03) dan tidak perlu dilaporkan dua kali.


def _blank(value) -> bool:
    return value is None or not str(value).strip()


def document_verification_mismatches(doc_type: str, ocr_json) -> list[dict]:
    """Baris verifikasi OCR yang TIDAK cocok dengan acuan bank untuk satu dokumen.

    ``[{"field", "acuan", "document", "similarity"}, ...]``; kosong bila dokumennya
    cocok atau ketidakcocokannya tidak bisa dipastikan (lihat catatan di atas)."""
    if not isinstance(ocr_json, dict):
        return []
    if wrong_document_type(doc_type, ocr_json):
        return []          # sudah dilaporkan sebagai C03 salah jenis
    rows = ocr_json.get("verifications")
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _blank(row.get("acuan")):
            continue       # tidak ada pembanding -> tidak tahu, bukan tidak cocok
        if row.get("match"):
            continue
        out.append({
            "field": str(row.get("field") or "").strip(),
            "acuan": row.get("acuan"),
            "document": row.get("document"),
            "similarity": row.get("similarity"),
        })
    return out


def document_verification_failed(doc_type: str, ocr_json) -> bool:
    """True bila dokumen di slot ``doc_type`` tidak membuktikan acuan banknya."""
    return bool(document_verification_mismatches(doc_type, ocr_json))
