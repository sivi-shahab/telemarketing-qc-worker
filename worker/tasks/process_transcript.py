"""Celery task: evaluate a submitted transcript (set of PDFs) for one result.

[MERGE A 1ccd74c..aec99ce — kandidat, BELUM di-commit] Pipeline (per result_id):
  1. status -> processing, set started_at
  2. download all PDFs from S3 transcripts/{result_id}/ to /tmp
     (campaign Collection keluar di sini -> ``_process_collection``)
  2a. cari agent yang ditugaskan TMS (via DWH) untuk "assigned_agent" + SC_CL_2
  2a-bis. VALIDASI PDF + pilih recording utama (``compliance.recording_validation``,
      regex deterministik, TANPA LLM). Tiket satu PDF tidak tersentuh.
  3. build_transcript(pdf_paths) -> messages + sorted_filenames
  4. load active campaign config from DB (prompt/scorecard/kb)
  4b. reference data dari DWH API (+ blok AGENT REFERENCE DATA / NAME ONLINE)
  4c. GERBANG DATA ACUAN: tanpa baris cashline dan/atau customer DWH -> tidak dinilai,
      langsung PENDING (tanpa panggilan LLM)
  5. evaluate(...) -> LLM output. PARALEL (``compliance.parallel_pass``) bila >= 2
     rekaman lolos validasi & ``parallel_recording_eval``: satu panggilan per rekaman,
     berjalan bersamaan (``parallel_recording_max_workers``), digabung dengan rekaman
     utama sebagai dasar. Selain itu satu panggilan atas transkrip gabungan.
  6. assemble final JSON (termasuk ``reference_data`` snapshot milik repo ini)
  7. upload final JSON to results bucket
  8. save result_data row (PostgreSQL)
  9. status -> done (result_path, completed_at, processing_sec)
 10. on any exception -> status=failed + error_message; always clean /tmp

Sejak port ini DIHAPUS dari alur (mengikuti A, 16 September 2026): SLA H-7
(``two_pass.recordings_within_sla``), "Double Agent" (``call_ownership.
filter_calls_by_agent``), klasifikasi jenis rekaman berbasis LLM
(``recording_type.classify_recordings``), dan override SC_CL_2 lewat
``call_ownership.apply_agent_name_verdict`` (SC_CL_2 kini dinilai LLM dengan blok
AGENT REFERENCE DATA; butuh prompt/KB campaign versi v82/KB_CL_2 baru di DB).
"""
import io
import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from functools import lru_cache

from openai import OpenAI, AzureOpenAI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services.s3_buckets import build_minio_client
from services import data_dwh
from compliance.processing_stages import PROCESSING_STAGES, STAGE_KEYS

from compliance.call_ownership import agent_name_verdict, stamp_agent_name_reason
from compliance.campaign_kind import is_collection, parse_collection_campaigns
from compliance.collection_report import (
    apply_configured_weights,
    build_collection_result_json,
    normalize_weighted_report,
    scorecard_maximum,
)
from compliance.evaluator import evaluate
from compliance.pdf_parser import (
    build_transcript,
    format_audio_duration,
    latest_generated_timestamp,
    parse_filename_timestamp,
    sort_pdf_paths,
    transcript_plain_text,
)
from compliance.parallel_pass import map_concurrently, merge_parallel
from compliance.recording_type import stamp_evidence_tags, stamp_reason_provenance
from compliance.recording_validation import pilih_recording_utama, tags_by_file
from compliance.mus_exemption import stamp as stamp_mus_exemption
from compliance.static_similarity import stamp_static_rules
from compliance.two_pass import _resync_critical, resync_scores
from compliance.reference_data import (
    build_reference_data,
    customer_id_from_filenames,
)
from compliance.sales_roster import parse_roster
from prompt.recording_type import TAG_LABELS, TAG_UTAMA
from db import crud
from worker.celery_app import celery_app
from worker.config import get_worker_settings

logger = logging.getLogger(__name__)

TMP_ROOT = "/tmp/transcripts"

# Bucket database sales (roster "Update Sales Telemarketing …xlsx"). Namanya sama
# dengan default di ``config.BaseAppSettings.minio_bucket_sales_database``.
SALES_BUCKET = os.getenv("MINIO_BUCKET_SALES_DATABASE", "sales-database")


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


def _llm_client_openai() -> OpenAI:
    settings = get_worker_settings()
    return OpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout,
    )


@lru_cache()
def _llm_client() -> AzureOpenAI:
    settings = get_worker_settings()
    return AzureOpenAI(
        azure_endpoint=settings.llm_base_url,      # https://foundry-dmanalytics-hub.cognitiveservices.azure.com
        api_key=settings.llm_api_key,
        api_version="2025-04-01-preview",           # terbukti valid dari test curl
        azure_deployment=settings.llm_model,        # "gpt-5.4-mini"
        # Tanpa ini SDK memakai bawaan 600 detik per percobaan (+2 retry), sehingga
        # LLM_TIMEOUT tidak berlaku: penilaian transkrip panjang yang butuh >10 menit
        # dipotong lalu diulang dari awal — pernah 3 percobaan / 1.813 detik untuk
        # satu tiket (17 September 2026).
        timeout=settings.llm_timeout,
    )


# [ADAPTASI] Urutan & label tiap checkpoint ``_Tahap.catat`` tinggal di
# ``compliance.processing_stages``, bukan di berkas ini. Di repo monolit daftar itu
# didefinisikan di sini dan API mengimpornya dari sini juga; sesudah repo dipisah,
# image API tidak memuat ``worker/`` sama sekali sehingga impor itu mustahil.
# Kuncinya HARUS persis sama dengan nama yang dipakai tiap ``tahap.catat(...)`` di
# bawah — ``STAGE_KEYS`` ada supaya salah ketik ketahuan, bukan diam-diam terabaikan.


class _Tahap:
    """Pencatat lama tiap tahap pemrosesan satu tiket, dalam detik.

    Dipakai untuk menjawab "waktunya habis di mana" tanpa menebak. Sebelumnya yang
    tercatat hanya ``processing_sec`` total, sehingga sendatan endpoint (terukur dua
    kali ~1.795 detik pada panggilan yang normalnya 8 detik) tidak bisa dibedakan dari
    pekerjaan yang memang berat. Hasilnya ikut disimpan ke ``result_json.timings``
    supaya bisa dibaca ulang belakangan, bukan hanya lewat log yang bisa terputar.

    14 September 2026: SELAIN mencatat lama tiap tahap (in-memory, hanya terlihat
    setelah tiket selesai), ``catat`` sekarang JUGA menulis checkpoint itu ke
    ``results.current_stage`` (commit langsung — lihat ``crud.set_result_stage``)
    bila ``db``/``result_id`` diberikan, supaya dashboard bisa menampilkan progres
    LIVE selagi tiket masih pending/processing, bukan cuma "Status: processing"
    tanpa rincian. Kegagalan menulis stage (mis. DB sempat terputus) TIDAK BOLEH
    menggagalkan pemrosesan tiket itu sendiri — dibungkus try/except, hanya di-log.
    """

    def __init__(self, db=None, result_id=None):
        self._t = time.monotonic()
        self.data = {}
        self._db = db
        self._result_id = result_id

    def catat(self, nama: str) -> None:
        # [ADAPTASI] Nama tahap dicocokkan ke katalog bersama. Sejak daftarnya pindah
        # ke ``compliance.processing_stages`` (dibaca API untuk menyusun tabel
        # progres), salah ketik di sini tidak lagi sekadar salah label: tahap yang
        # tidak ada di katalog membuat ``stage_table`` menganggap tiketnya belum mulai
        # sama sekali. Dicatat sebagai peringatan, TIDAK melempar — pencatat progres
        # tidak boleh menggagalkan pemrosesan tiket.
        if nama not in STAGE_KEYS:
            logger.warning(
                "tahap '%s' tidak ada di PROCESSING_STAGES — tabel progres tidak akan "
                "mengenalinya (result %s)", nama, self._result_id,
            )
        sekarang = time.monotonic()
        self.data[nama] = round(sekarang - self._t, 2)
        self._t = sekarang
        if self._db is not None and self._result_id is not None:
            try:
                crud.set_result_stage(self._db, self._result_id, nama)
            except Exception:
                logger.exception(
                    "gagal menulis current_stage=%s untuk result %s (tidak fatal)",
                    nama, self._result_id,
                )

    def ringkas(self) -> str:
        return " | ".join(f"{k}={v}s" for k, v in self.data.items())


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


def _assigned_name_online(db, customer_id: str) -> "tuple[str | None, str | None, tuple, str]":
    """``(agent_id, NAME ONLINE, seluruh NAME ONLINE roster, nama karyawan)`` tiket ini.

    ``agent_id`` dari ``tms_cashline.agent_id`` (satu agent per tiket), NAME ONLINE
    dari kolom E roster sales yang aktif. Daftar NAME ONLINE seluruh roster ikut
    dikembalikan: ``call_ownership.agent_name_verdict`` memakainya untuk membedakan
    "agent menyebut nama on-air agent LAIN di roster" dari sekadar salah ucap.
    Semuanya kosong bila tiketnya tidak ada di TMS atau agent-nya tidak ada di
    roster — pemanggil lalu tidak menjatuhkan apa pun (lihat docstring
    ``agent_name_verdict``).
    """
    if not customer_id:
        return None, None, (), ""
    try:
        row = crud.get_tms_cashline_by_result_id(db, customer_id) or {}
        agent_id = (row.get("agent_id") or "").strip() or None
        if not agent_id:
            return None, None, (), ""
        sales_db = crud.get_active_sales_database(db)
        if sales_db is None:
            return agent_id, None, (), ""
        resp = _minio_client().get_object(SALES_BUCKET, sales_db.object_path)
        try:
            data = resp.read()
        finally:
            resp.close()
            resp.release_conn()
        roster = parse_roster(data)
        entry = roster.get(agent_id.casefold()) or {}
        all_names = tuple(sorted({
            (e.get("name_online") or "").strip()
            for e in roster.values() if (e.get("name_online") or "").strip()
        }))
        return agent_id, (entry.get("name_online") or None), all_names, (entry.get("name") or "")
    except Exception:
        logger.exception("gagal membaca NAME ONLINE agent untuk id=%s", customer_id)
        return None, None, (), ""


def _collection_campaigns() -> frozenset:
    # Dibaca tiap tiket, tidak di-cache — sama dengan api.rbac.collection_campaigns_from_env.
    return parse_collection_campaigns(os.getenv("COLLECTION_CAMPAIGNS", ""))


def _process_collection(db, result, result_id, pdf_paths, settings, tahap, started_at):
    """Jalur campaign Collection: audit BERBOBOT POJK 22/2023.

    Sengaja PENDEK. Seluruh langkah Cashline yang bergantung pada TMS atau Ascend —
    pemilik panggilan (tms_cashline.agent_id), jangkar submit_time, reference data
    DWH/Ascend, riplay, klasifikasi rekaman, MUS, resync_scores — dilewati: tiket
    penagihan tidak punya baris di sana, dan membacanya hanya menghasilkan acuan
    kosong yang tampak seperti data.
    """
    sorted_filenames, messages, audio_duration, _durations = build_transcript(pdf_paths)
    tahap.catat("rangkai_transkrip")
    if not messages:
        raise ValueError(
            "Transkrip kosong: tidak ada segmen yang bisa di-parse dari PDF "
            "(format transkrip mungkin tidak dikenali)."
        )

    campaign = crud.get_active_campaign(db, result.campaign)
    if campaign is None:
        raise ValueError(f"Active campaign '{result.campaign}' not found")
    missing = [label for label, text in (("prompt", campaign.prompt_text),
                                         ("scorecard", campaign.scorecard_text),
                                         ("KB", campaign.kb_text))
               if not (text or "").strip()]
    if missing:
        raise ValueError(
            f"Campaign '{result.campaign}' belum punya konfigurasi QC: "
            f"{', '.join(missing)} masih kosong. Upload dulu lewat menu Upload Campaign."
        )
    # Maksimum milik konfigurasi, bukan milik subset item yang dijawab model —
    # balasan terpotong tidak boleh mengecilkan penyebut dan menggelembungkan persen.
    configured_max = scorecard_maximum(campaign.scorecard_text)
    if configured_max is None:
        logger.warning("result %s: scorecard campaign %s bukan JSON array berbobot; "
                       "maksimum diambil dari item jawaban", result_id, result.campaign)
    tahap.catat("campaign_dan_acuan")

    raw, usage = evaluate(
        prompt_text=campaign.prompt_text,
        messages=messages,
        kb_text=campaign.kb_text,
        scorecard_text=campaign.scorecard_text,
        reference_text="",
        llm_client=_llm_client(),
        model=settings.llm_model,
        source_files=sorted_filenames,
        temperature=settings.llm_temperature,
        seed=settings.llm_seed,
        reasoning_effort=settings.llm_reasoning_effort,
        return_usage=True,
    )
    tahap.catat("penilaian_llm")
    logger.info("token penilaian (collection): masuk=%s (ter-cache=%s) keluar=%s",
                usage.get("input_token"), usage.get("cached_token"), usage.get("output_token"))

    # Bobot tiap item milik konfigurasi scorecard, bukan milik model.
    raw = apply_configured_weights(raw, campaign.scorecard_text)
    report = normalize_weighted_report(raw, configured_maximum=configured_max)
    if report["call_id"] == "-" and sorted_filenames:
        # Prompt tidak menerima call_id dari pipeline, jadi model biasanya diam;
        # ID tiket (prefix nama berkas) lebih berguna daripada "-" di header laporan.
        report["call_id"] = customer_id_from_filenames(sorted_filenames)
    tahap.catat("gabung_dan_skor")

    completed_at = _utcnow()
    processing_sec = (completed_at - started_at).total_seconds()
    final_json = build_collection_result_json(
        result_id=result_id, campaign=result.campaign, source_files=sorted_filenames,
        report=report, processed_at=completed_at.isoformat(),
        processing_sec=processing_sec, audio_duration=audio_duration,
    )
    final_json["timings"] = tahap.data

    payload = json.dumps(final_json, ensure_ascii=False).encode("utf-8")
    result_path = f"{result_id}.json"
    _minio_client().put_object(settings.minio_bucket_results, result_path,
                               io.BytesIO(payload), length=len(payload),
                               content_type="application/json")
    crud.save_result_data(db, result_id, final_json)
    tahap.catat("simpan_hasil")

    crud.update_result_status(db, result_id, "done", result_path=result_path,
                              completed_at=completed_at,
                              processing_sec=round(processing_sec, 2),
                              generated_at=latest_generated_timestamp(pdf_paths))
    tahap.catat("tandai_selesai")
    logger.info("waktu per tahap %s (collection): %s", result_id, tahap.ringkas())
    return {"result_id": str(result_id), "status": "done"}


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

        tahap = _Tahap(db, result_id)

        # 2. download PDFs
        pdf_paths = _download_transcripts(result_id)
        tahap.catat("unduh_pdf")
        if not pdf_paths:
            raise ValueError(f"No transcript PDFs found for result {result_id}")

        # Campaign Collection keluar di sini, SEBELUM langkah pertama yang membaca
        # TMS (2b). Kegagalan di dalamnya jatuh ke except/finally yang sama di bawah,
        # jadi status failed + pembersihan /tmp tetap berlaku.
        if is_collection(result.campaign, _collection_campaigns()):
            return _process_collection(db, result, result_id, pdf_paths,
                                       settings, tahap, started_at)

        # 2a. agent yang ditugaskan TMS — dipakai untuk tampilan "assigned_agent" DAN
        # penilaian SC_CL_2 (nama on-air) di bawah.
        customer_id_early = customer_id_from_filenames(
            [os.path.basename(p) for p in pdf_paths]
        )
        agent_id, name_online, roster_names, nama_karyawan = _assigned_name_online(
            db, customer_id_early)
        texts = {p: transcript_plain_text(p) for p in pdf_paths}
        tahap.catat("baca_teks_pdf")

        # 2a-bis. VALIDASI PDF + PEMILIHAN RECORDING UTAMA (18 September 2026).
        #
        # Bank Mega tidak bisa memberi penanda jenis rekaman dari hulu, jadi sistem
        # menentukannya sendiri: buang PDF yang tidak layak dinilai, lalu dari yang
        # TERSISA ambil yang TERTUA sebagai recording utama. Urutan itu wajib —
        # "tertua" tanpa validasi salah pada 1 dari 4 tiket sampel (0308549YQ4:
        # rekaman tertua adalah panggilan yang verifikasinya gagal lalu ditunda agent).
        # Rancangan & buktinya: docs/csv_bank/17 September 2026/multiple_call.md.
        #
        # TIKET SATU PDF TIDAK TERSENTUH — dijamin jaring pengaman di dalam
        # ``pilih_recording_utama``, dan itulah yang membuat seluruh tiket
        # single-recording yang sudah ada tidak bergeser sama sekali.
        seleksi = pilih_recording_utama(pdf_paths, expected_ticket_id=customer_id_early)
        if seleksi.get("fallback") and len(pdf_paths) > 1:
            logger.warning(
                "result %s: jaring pengaman seleksi rekaman menyala (%s)",
                result_id, seleksi["fallback"],
            )
        for d in seleksi.get("dibuang") or []:
            logger.info("result %s: rekaman dicoret — %s (%s): %s",
                        result_id, d["filename"], d["tag"], d["reason"])

        dinilai = [seleksi["utama"], *seleksi.get("pendamping", [])] if seleksi["utama"] else list(pdf_paths)
        ordered_paths = sort_pdf_paths(dinilai)
        # Bentuk butirnya mengikuti yang dibaca kolom Call Duration di Results
        # (``ResultsView.callDurations``): kuncinya ``filename``, dan ``duration``
        # berupa teks siap tampil — bukan detik mentah.
        _durasi_berkas = {
            x["filename"]: format_audio_duration(x["durasi"])
            for x in seleksi.get("laporan") or []
        }
        excluded_calls = [
            {"filename": d["filename"], "kind": "validasi_rekaman",
             "tag": d["tag"], "tag_label": d["tag_label"], "reason": d["reason"],
             "duration": _durasi_berkas.get(d["filename"], "—")}
            for d in seleksi.get("dibuang") or []
        ]
        recording_tags = tags_by_file(seleksi) or {
            os.path.basename(p): {"tag": TAG_UTAMA, "reason": None} for p in pdf_paths
        }
        # Transkrip yang dikirim ke LLM hanya berisi rekaman yang dinilai.
        pdf_paths = ordered_paths
        texts = {p: t for p, t in texts.items() if p in set(ordered_paths)}

        # 2b. Nama on-air: perkenalan agent WAJIB memakai NAME ONLINE (aturan bisnis
        # 7 September 2026, dikonfirmasi Bank Mega). Vonisnya diterapkan sesudah
        # penilaian, lihat 5e.
        name_verdict = agent_name_verdict(texts, name_online or "", roster_names, nama_karyawan)
        if name_verdict.get("match") is False:
            logger.warning("result %s: %s", result_id, name_verdict.get("reason"))
        tahap.catat("cek_nama_agent")

        # 3. build ordered transcript
        sorted_filenames, messages, audio_duration, audio_durations = build_transcript(
            pdf_paths
        )
        tahap.catat("rangkai_transkrip")
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
        # by the customer/session ID derived from the earliest PDF filename). It is
        # passed to ``evaluate`` as its own block — AFTER the campaign scorecard,
        # BEFORE the transcript — so the scorecard, identik antar-tiket, tetap masuk
        # awalan yang ter-cache. Lihat ``evaluator._build_user_content``.
        customer_id = customer_id_from_filenames(sorted_filenames)
        # [ADAPTASI] build_reference_data di repo ini mengembalikan baris MENTAH DWH
        # (``reference_raw``), bukan ``found_rows`` seperti di A. Snapshot mentah itu
        # tetap disimpan ke result_json (dibaca stats/agent_error/cashline_agent_index/
        # reference_snapshot_index), dan ``ref_found`` diturunkan darinya — definisi
        # yang SAMA dengan ``crud.reference_snapshot_index`` (data_gap saat BACA).
        # JANGAN diganti ke query tabel tms_cashline/ascend_custp: tabel itu kosong
        # di produksi, jadi setiap tiket akan jatuh ke gerbang PENDING di bawah.
        reference_text, ref_warnings, reference_raw = build_reference_data(
            customer_id, db, riplay_extraction=campaign.riplay_extraction
        )
        for warn in ref_warnings:
            logger.warning("reference data (%s / id=%s): %s", result_id, customer_id, warn)
        ref_found = {
            "cashline": (reference_raw or {}).get("cashline") is not None,
            "cardholder": (reference_raw or {}).get("customer") is not None,
        }

        # AGENT REFERENCE DATA (18 September 2026): NAME ONLINE roster disuntikkan
        # sebagai referensi eksplisit supaya SC_CL_2 (Greeting — agent menyebutkan
        # nama agent) dinilai LLM sendiri, sama seperti item scorecard lainnya —
        # BUKAN lagi ditimpa deteksi regex Python (`call_ownership.
        # apply_agent_name_verdict`, dihapus). Regex ekstraksi nama dari ucapan bebas
        # terbukti rapuh (tiket 020455CL3A: "saya Eveline, Ibu dari Bank Mega" gagal
        # tertangkap karena vokatif sisipan), sementara LLM yang membaca transkrip
        # utuh sudah benar mengutip nama itu — ia hanya tidak pernah diberi tahu
        # NAME ONLINE-nya. `None` ditulis bila agent tidak ada di roster (data tidak
        # tersedia, bukan berarti kosong) supaya LLM tidak menyimpulkan apa pun.
        if name_online:
            reference_text = (
                (reference_text or "").rstrip()
                + "\n\n=== AGENT REFERENCE DATA ===\n"
                + json.dumps({"name_online": name_online}, ensure_ascii=False, indent=2)
            )
        tahap.catat("campaign_dan_acuan")

        # Task B/C/D keys are only mandatory when a matching reference row was
        # actually found — a ticket without a tms_cashline/ascend_custp match has
        # nothing to verify, and the LLM correctly marks those sections SKIPPED or
        # leaves them out. When a row WAS found but the keys are still missing,
        # that is an LLM output bug (see evaluator.evaluate docstring / ticket
        # 030226vUJS, 18 September 2026) and worth a retry.
        required_keys = []
        if ref_found.get("cashline"):
            required_keys += ["cashline_data_extraction", "cashline_data_verification"]
        if ref_found.get("cardholder"):
            required_keys += ["card_holder_extraction", "card_holder_verification"]

        # 4c. GERBANG DATA ACUAN (18 September 2026) — tiket yang TIDAK punya baris
        # ``tms_cashline`` dan/atau ``ascend_custp`` TIDAK dinilai sama sekali:
        # langsung PENDING, tanpa memanggil LLM.
        #
        # Sebelum gerbang ini tiket semacam itu tetap diproses penuh, lalu tetap
        # berakhir PENDING di layar lewat aturan ``data_gap`` yang berjalan saat BACA
        # (``stats_aggregate._result_ai_status`` langkah 5). Jadi panggilan LLM-nya
        # memang tidak pernah mengubah vonis apa pun — hanya membakar waktu dan biaya.
        # Terukur pada tiket ``150125GJW6`` (18 September 2026): ~40 menit panggilan
        # LLM untuk hasil yang sudah pasti PENDING sejak awal. Lebih buruk lagi, tanpa
        # acuan pembanding LLM menandai baris verifikasi ``MISMATCH`` (bukan
        # ``SKIPPED_NULL``) sehingga skor tersimpannya anjlok ke −1,65 — angka yang
        # menyesatkan bila dibaca lepas dari status kanoniknya.
        #
        # Bentuk keluarannya SENGAJA sama dengan tiket data_gap yang diproses sebelum
        # gerbang ini ada: ``ai_status = "PENDING"`` di evaluasi, dan aturan
        # ``data_gap`` tetap menghasilkan PENDING + alasan "Data TMS Kosong · Data
        # Ascend Kosong" di kolom Results. Satu definisi PENDING, bukan dua.
        kurang = []
        if not ref_found.get("cashline"):
            kurang.append("TMS")
        if not ref_found.get("cardholder"):
            kurang.append("Ascend")

        if kurang:
            # [MERGE 4-service 21092026] Bundle kosong karena DWH TIDAK BISA DIHUBUNGI
            # bukan bukti data acuannya tidak ada. Gagalkan tiketnya (status
            # ``failed``, bisa di-reprocess) alih-alih mencapnya PENDING tanpa dinilai.
            _, dwh_pasti = data_dwh.fetch_bundle_checked(customer_id)
            if not dwh_pasti:
                raise RuntimeError(
                    f"DWH API tidak dapat dihubungi untuk id={customer_id} — "
                    "data acuan tidak bisa dipastikan; reprocess tiket setelah DWH pulih."
                )
            logger.warning(
                "result %s (id=%s): data acuan %s kosong — TIDAK dinilai, langsung PENDING",
                result_id, customer_id, " & ".join(kurang),
            )
            evaluation = {
                "ai_status": "PENDING",
                "ai_summary": (
                    f"Tiket belum dinilai: data acuan {' & '.join(kurang)} kosong. "
                    "Penilaian dijalankan setelah data acuannya tersedia."
                ),
                "scorecard_result": [],
                "category_summary": [],
                "error_codes": [],
                # Jejak mesin — dibaca saat menelusuri kenapa tiket ini tidak dinilai.
                "data_acuan_kurang": kurang,
            }
            rincian_isian, rincian_cashline = [], []
            paralel = False
            tahap.catat("penilaian_llm")
        else:
            # Teks campaign disalin ke variabel BIASA di thread utama. ``campaign`` adalah
            # objek ORM yang atributnya kedaluwarsa setiap ``tahap.catat`` (commit), dan
            # membacanya memuat ulang lewat ``db`` — session yang TIDAK boleh disentuh
            # beberapa thread sekaligus. Dibaca dari dalam ``_nilai`` yang berjalan
            # bersamaan, tiket multi-rekaman gagal dengan "This session is provisioning a
            # new connection; concurrent operations are not permitted" (reproses
            # 010445fAVG, 21 September 2026). Yang boleh masuk thread hanya nilai murni.
            _prompt_text = campaign.prompt_text
            _kb_text = campaign.kb_text
            _scorecard_text = campaign.scorecard_text

            def _nilai(msgs, files_):
                """Satu panggilan LLM. Dipakai jalur tunggal MAUPUN tiap rekaman paralel.

                Aman dipanggil dari beberapa thread: tidak menyentuh ``db``/objek ORM."""
                return evaluate(
                    prompt_text=_prompt_text,
                    messages=msgs,
                    kb_text=_kb_text,
                    scorecard_text=_scorecard_text,
                    reference_text=reference_text,
                    llm_client=_llm_client(),
                    model=settings.llm_model,
                    source_files=files_,
                    temperature=settings.llm_temperature,
                    seed=settings.llm_seed,
                    reasoning_effort=settings.llm_reasoning_effort,
                    return_usage=True,
                    required_keys=required_keys,
                )

            paralel = settings.parallel_recording_eval and len(ordered_paths) > 1
            if paralel:
                # 5-PARALEL. Tiap rekaman dinilai SENDIRI, lalu digabung dengan rekaman
                # utama sebagai DASAR (``parallel_pass.merge_parallel``). Model tidak
                # pernah melihat rekaman lain dalam satu panggilan, jadi penjaga
                # provenance-nya STRUKTURAL — bukan bergantung kepatuhan model pada
                # instruksi. Baris yang gagal di utama baru dicari penggantinya di
                # rekaman lain dan ditandai ``pass2``, sehingga "diselamatkan rekaman
                # perbaikan" tertulis eksplisit di kolom Reason, bukan tersamar.
                #
                # Definisi bisnisnya: recording perbaikan berfungsi MEMPERBAIKI item
                # scorecard yang belum sesuai pada recording utama — jadi bukti dari
                # rekaman perbaikan hanya sah untuk item yang memang gagal di utama.
                # Panggilannya BERSAMAAN (sejak 21 September 2026; sebelumnya berurutan,
                # ~1012 dtk untuk dua rekaman). Transkrip tiap rekaman disusun dulu di
                # sini supaya yang berjalan di thread hanya panggilan LLM-nya.
                _siap = [build_transcript([p]) for p in ordered_paths]
                _hasil = map_concurrently(
                    lambda s_: _nilai(s_[1], s_[0]), _siap,
                    settings.parallel_recording_max_workers,
                )
                evaluations = [ev_i for ev_i, _u in _hasil]
                _tokens_in = sum((u_.get("input_token") or 0) for _e, u_ in _hasil)
                _tokens_out = sum((u_.get("output_token") or 0) for _e, u_ in _hasil)

                utama_idx = ordered_paths.index(seleksi["utama"]) if seleksi.get("utama") in ordered_paths else 0
                evaluation, rincian_isian, rincian_cashline = merge_parallel(
                    evaluations,
                    [os.path.basename(p) for p in ordered_paths],
                    [parse_filename_timestamp(p) for p in ordered_paths],
                    utama_idx,
                )
                tahap.catat("penilaian_llm")
                logger.info(
                    "penilaian PARALEL %d rekaman (utama=%s): token masuk=%s keluar=%s | "
                    "%d baris scorecard & %d field cashline diisi dari rekaman perbaikan",
                    len(ordered_paths), os.path.basename(ordered_paths[utama_idx]),
                    _tokens_in, _tokens_out, len(rincian_isian), len(rincian_cashline),
                )
                for r in rincian_isian:
                    logger.info(
                        "  isian: %s %s -> %s dari %s%s",
                        r.get("item_code"), r.get("dari_status"), r.get("ke_status"),
                        r.get("sumber_file"),
                        f" ({r['alasan']})" if r.get("alasan") else "",
                    )
            else:
                # 5. Penilaian LLM — satu panggilan atas seluruh transkrip gabungan.
                # Jalur tiket SATU rekaman (dan saat sakelar paralel dimatikan).
                evaluation, _usage = _nilai(messages, sorted_filenames)
                rincian_isian, rincian_cashline = [], []
                tahap.catat("penilaian_llm")
                logger.info(
                    "token penilaian: masuk=%s (ter-cache=%s) keluar=%s (penalaran=%s)",
                    _usage.get("input_token"), _usage.get("cached_token"),
                    _usage.get("output_token"), _usage.get("reasoning_token"),
                )

        # SELURUH berkas tiket ini disebut di sini — yang dinilai MAUPUN yang dicoret
        # (jaring pengaman 3: tidak boleh ada PDF yang lenyap dari layar). Yang dicoret
        # membawa tag & alasannya sendiri supaya kolom Call Duration bisa menjelaskan
        # kenapa ia tidak ikut dinilai.
        # Tag-nya dibaca dari ``recording_tags`` (sumber yang SAMA dengan yang dipakai
        # ``stamp_reason_provenance``), bukan dipatok TAG_UTAMA. Kalau dipatok, badge di
        # kolom Call Duration akan menyebut "Recording utama" untuk rekaman pendamping
        # sementara kalimat provenance di kolom Reason menyebut "recording perbaikan" —
        # dua permukaan yang membicarakan berkas yang sama dengan jawaban berbeda.
        recording_types = [
            {"file": os.path.basename(p),
             "tag": (recording_tags.get(os.path.basename(p)) or {}).get("tag") or TAG_UTAMA,
             "tag_label": TAG_LABELS.get(
                 (recording_tags.get(os.path.basename(p)) or {}).get("tag") or TAG_UTAMA),
             "reason": "",
             "excluded": False,
             "out_of_sla": False}
            for p in ordered_paths
        ] + [
            {"file": d["filename"],
             "tag": d["tag"],
             "tag_label": d["tag_label"],
             "reason": d["reason"],
             "excluded": True,
             "out_of_sla": False}
            for d in seleksi.get("dibuang") or []
        ]

        # Langkah 5b-5d SELURUHNYA dilewati untuk tiket yang tidak dinilai (gerbang 4c):
        # tidak ada scorecard yang bisa dicap, tidak ada evidence yang bisa ditandai
        # asal rekamannya, dan ``resync_scores`` justru akan MENGARANG skor untuk
        # scorecard kosong — persis angka menyesatkan yang ingin dihindari gerbang itu.
        if not kurang:
            # 5b. Cap versi aturan verifikasi statik ke dalam evaluasi (revamp 21 Agustus
            # 2026), supaya seluruh permukaan pembaca memakai ambang yang benar tanpa perlu
            # menebak dari waktu upload — dan tiket lama tidak ikut dinilai ulang.
            evaluation = stamp_static_rules(evaluation)

            # 5c. Tempelkan JENIS REKAMAN ke tiap blok evidence. Tanpa ini evidence hanya
            # menyebut nama berkas, dan pembacanya harus mencocokkan sendiri ke kolom Call
            # Duration untuk tahu asal sebuah bukti.
            evaluation = stamp_evidence_tags(evaluation, recording_tags)
            # Alasan SC_CL_2 untuk B29 (nama disebut tapi beda dengan NAME ONLINE) ditulis
            # SEBELUM provenance, karena yang belakangan menempelkan "- Evidence diambil
            # dari ...". Lihat ``call_ownership.stamp_agent_name_reason``.
            evaluation = stamp_agent_name_reason(evaluation, name_online)
            evaluation = stamp_reason_provenance(evaluation, recording_tags)

            # 5f. Pengecualian Mega Ultima Shield diselesaikan SEBELUM skor dihitung —
            # skor maksimal tiket bergantung padanya (108.75 bila dikecualikan, 150 bila
            # MUS wajib tetapi tidak dipenuhi). Menggabungkan bacaan LLM atas transkrip
            # dengan daftar tetap Bank Mega; lihat ``compliance.mus_exemption``.
            evaluation = stamp_mus_exemption(evaluation, customer_id)

            # 5d. Skor disetel ulang dari scorecard memakai rumus yang sama dengan pembaca
            # — SELALU. Tanpa ini JSON tersimpan kadang membawa angka mentah LLM yang tidak
            # mengenal iris 10% item non-tolerable; lihat ``two_pass.resync_scores``.
            evaluation = resync_scores(evaluation)

            # [DIPERTAHANKAN dari produksi — A@HEAD menjatuhkannya saat f672425
            # menghidupkan lagi PARALEL] Mode paralel: ``critical_compliance_check``
            # diambil UTUH dari rekaman utama, tetapi ``merge_parallel`` sudah menaikkan
            # sebagian ``scorecard_result`` dari rekaman lain. Tanpa ini item kritis yang
            # sudah SESUAI di rekaman perbaikan tetap ditagih ``-(maximum_score / 4)``.
            if paralel:
                evaluation = resync_scores(_resync_critical(evaluation))

        tahap.catat("gabung_dan_skor")

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
            # PDF yang dicoret validasi rekaman (``kind="validasi_rekaman"``); kosong
            # untuk tiket satu PDF. Dibaca ResultsView.callDurations.
            "excluded_calls": excluded_calls,
            # Jenis tiap rekaman: utama / perbaikan (pendamping) / yang dicoret.
            # Lihat compliance/recording_validation.tags_by_file.
            "recording_types": recording_types,
            # Lama tiap tahap dalam detik — lihat ``_Tahap``.
            "timings": tahap.data,
            "assigned_agent": {"agent_id": agent_id, "name_online": name_online,
                               "nama_karyawan": nama_karyawan},
            # Hasil pemeriksaan nama on-air — lihat call_ownership.agent_name_verdict.
            "agent_name_check": name_verdict,
            # [ADAPTASI] Baris cashline + customer MENTAH dari build_reference_data()
            # di atas. Dibaca LANGSUNG dari result_data oleh stats.py, agent_error.py,
            # crud.cashline_agent_index() dan crud.reference_snapshot_index() — tidak
            # ada tabel atau bucket baru, hanya field tambahan di JSON yang memang
            # sudah disimpan ke Postgres + MinIO.
            "reference_data": reference_raw,
            # Jejak penggabungan PARALEL (18 September 2026): baris scorecard & field
            # cashline yang gagal di rekaman utama lalu diisi dari rekaman perbaikan.
            # Kosong untuk tiket satu rekaman. Dipakai menelusuri "kenapa item ini
            # SESUAI padahal utamanya gagal" tanpa membandingkan dua JSON.
            "scorecard_pass": rincian_isian,
            "cashline_pass": rincian_cashline,
            "processed_at": completed_at.isoformat(),
            "processing_sec": round(processing_sec, 2),
            "evaluation": evaluation,
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

        tahap.catat("simpan_hasil")

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

        tahap.catat("tandai_selesai")
        logger.info("waktu per tahap %s: %s", result_id, tahap.ringkas())
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
