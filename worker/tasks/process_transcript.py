"""Celery task: evaluate a submitted transcript (set of PDFs) for one result.

Pipeline (per result_id):
  1. status -> processing, set started_at
  2. download all PDFs from MinIO transcripts/{result_id}/ to /tmp
  2b. buang panggilan milik agent LAIN (compliance.call_ownership): tiket ini
      di-assign TMS kepada satu agent, dan hanya panggilan agent itu yang dinilai
  2c. tandai JENIS tiap rekaman lewat LLM (compliance.recording_type) dan coret yang
      batal / ditunda / tidak terhubung — evidence tidak boleh diambil dari sana
  3. build_transcript(pdf_paths) -> messages + sorted_filenames
  4. load active campaign config from DB (prompt/scorecard/kb)
  5. evaluate(...) -> LLM output. PARALEL (compliance.parallel_pass): bila >= 2
     rekaman valid & semua di dalam SLA, tiap rekaman dinilai lewat panggilan
     terpisah yang ditembakkan bersamaan; pick_utama memilih rekaman utama
     (rekaman TERTUA) dan merge_parallel mengisi item non-SESUAI dari rekaman
     lain, PLUS menggabung cashline_data_extraction/_verification field demi
     field (rekaman terbaru yang punya nilai valid menang — lihat
     parallel_pass._merge_cashline_data). Selain itu: FULL, satu panggilan atas
     seluruh transkrip gabungan.
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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import lru_cache

from openai import OpenAI, AzureOpenAI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services.s3_buckets import build_minio_client
from compliance.processing_stages import PROCESSING_STAGES, STAGE_KEYS

from compliance.call_ownership import (
    agent_name_verdict,
    apply_agent_name_verdict,
    filter_calls_by_agent,
)
from compliance.evaluator import evaluate
from compliance.parallel_pass import merge_parallel, pick_utama, sesuai_count
from compliance.pdf_parser import (
    build_transcript,
    call_duration,
    latest_generated_timestamp,
    parse_filename_timestamp,
    sort_pdf_paths,
    transcript_plain_text,
)
from compliance.recording_type import (
    classify_recordings,
    needs_classification_review,
    split_by_recording_type,
    stamp_evidence_tags,
    stamp_reason_provenance,
)
from compliance.mus_exemption import stamp as stamp_mus_exemption
from compliance.static_similarity import stamp_static_rules
from compliance.two_pass import (
    _resync_critical,
    recordings_within_sla,
    resync_scores,
)
from compliance.reference_data import (
    build_reference_data,
    customer_id_from_filenames,
)
from compliance.sales_roster import parse_roster
from prompt.recording_type import TAG_LABELS, TAG_PERBAIKAN, TAG_UTAMA
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


def _assigned_name_online(db, customer_id: str) -> "tuple[str | None, str | None, tuple]":
    """``(agent_id, NAME ONLINE, seluruh NAME ONLINE roster)`` untuk tiket ini.

    ``agent_id`` dari ``tms_cashline.agent_id`` (satu agent per tiket), NAME ONLINE
    dari kolom E roster sales yang aktif. Daftar NAME ONLINE seluruh roster ikut
    dikembalikan: ``compliance.call_ownership`` memakainya untuk memastikan nama yang
    terbaca di transkrip benar-benar nama agent, bukan kata biasa. Semuanya kosong
    bila tiketnya tidak ada di TMS atau agent-nya tidak ada di roster — pemanggil lalu
    TIDAK menyaring apa pun.
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

        # 2b. Buang panggilan milik agent LAIN. Satu ticket id bisa berisi panggilan
        # dari beberapa agent, sementara TMS hanya meng-assign tiket ini kepada SATU
        # orang dan scorecard-nya menilai orang itu — evidence dari panggilan agent
        # lain berarti menilai pekerjaan yang bukan miliknya. Lihat
        # ``compliance.call_ownership`` untuk cara pemiliknya dikenali dan untuk
        # jaring pengamannya (tanpa NAME ONLINE / tanpa nama terdeteksi -> tidak ada
        # yang dibuang).
        customer_id_early = customer_id_from_filenames(
            [os.path.basename(p) for p in pdf_paths]
        )
        agent_id, name_online, roster_names, nama_karyawan = _assigned_name_online(
            db, customer_id_early)
        # Teks polos tiap PDF dibaca SEKALI di sini: dipakai penyaring pemilik panggilan
        # (2b) dan klasifikasi jenis recording (2c). Keduanya membutuhkan teks yang sama.
        texts = {p: transcript_plain_text(p) for p in pdf_paths}
        tahap.catat("baca_teks_pdf")
        excluded_calls = []
        if name_online:
            kept, dropped = filter_calls_by_agent(texts, name_online, roster_names)
            for d in dropped:
                logger.warning(
                    "result %s: panggilan %s dibuang — agent terdeteksi %s "
                    "(cocok NAME ONLINE %s di roster), tiket ini milik %s (%s)",
                    result_id, d["filename"], d["names"], d.get("matched_agent"),
                    name_online, agent_id,
                )
            # Durasinya ikut dihitung walau panggilannya tidak dinilai: kolom Call
            # Duration menyebut SEMUA panggilan tiket ini, yang dibuang pun, supaya
            # QC melihat satu PDF sengaja ditinggalkan dan bukan hilang diam-diam.
            excluded_calls = [
                {"filename": d["filename"], "detected_agent": d["names"],
                 "matched_agent": d.get("matched_agent"),
                 "similarity_percent": d["score"],
                 "duration": call_duration(d["path"]),
                 # Alasan sebuah PDF dicoret. Sejak 5 September 2026 ada dua sebab yang
                 # berbeda (agent lain / jenis recording) dan layarnya harus bisa
                 # menyebut yang mana — tanpa penanda ini keduanya terbaca sama.
                 "kind": "agent_lain"}
                for d in dropped
            ]
            pdf_paths = kept

        # 2c. Tandai JENIS tiap rekaman, lalu coret yang tidak layak dinilai (permintaan
        # bisnis 4 September 2026). Satu ticket id berisi bermacam rekaman — utama,
        # perbaikan, pembatalan oleh nasabah, panggilan yang ditutup dini — dan evidence
        # scorecard tidak boleh diambil dari rekaman yang batal atau tidak jadi apa-apa.
        # Berbeda dengan 2b yang deterministik, langkah ini memakai LLM: gaya bicara
        # agent berbeda-beda dan yang membedakan adalah konteks, bukan pola kata. Lihat
        # ``compliance.recording_type`` untuk ketiga jaring pengamannya — gagalnya
        # langkah ini TIDAK PERNAH menjatuhkan tiket, hanya membuatnya tidak menyaring.
        # Dijalankan SESUDAH 2b: panggilan milik agent lain sudah tidak relevan.
        ordered_paths = sort_pdf_paths(pdf_paths)
        # ``submit_time`` TMS dikirim sebagai jangkar: pengajuan yang berhasil masuk
        # sistem pasti dihasilkan rekaman menjelang waktu itu, dan itulah yang
        # menyelesaikan rekaman yang isinya ambigu (nasabah berdebat soal kemungkinan
        # membatalkan tanpa benar-benar membatalkan). Kosong bila tiketnya tidak
        # terdaftar di TMS — prompt lalu tidak menyebut waktu apa pun.
        _cashline_row = (
            crud.get_tms_cashline_by_result_id(db, customer_id_early) or {}
            if customer_id_early else {}
        )
        recording_tags = classify_recordings(
            [{"file": os.path.basename(p), "duration": call_duration(p),
              "text": texts.get(p, "")} for p in ordered_paths],
            llm_client=_llm_client(),
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            seed=settings.llm_seed,
            reasoning_effort=settings.llm_reasoning_effort,
            submit_time=(_cashline_row.get("submit_time") or None),
            # 14 September 2026: diturunkan dari votes=3 ke votes=1 (keputusan bisnis,
            # demi kecepatan) — SEBELUMNYA tiga suara dipasang justru karena tiket
            # 030808fLO1 sendiri: satu salah tag merambat jauh (lihat
            # ``_classify_by_vote``, contoh nyata skor bergeser dari 31,875 ke -56,44
            # akibat SATU rekaman utama yang keliru dibaca sebagai pembatalan), dan
            # suhu tidak bisa diturunkan pada model ini sehingga voting adalah satu-
            # satunya mitigasi ketidakstabilan itu. Risiko itu KEMBALI TERBUKA dengan
            # votes=1 — bukan dihapus, hanya diterima sebagai trade-off kecepatan.
            votes=1,
        )
        tahap.catat("klasifikasi_llm")
        kept_paths, skipped = split_by_recording_type(ordered_paths, recording_tags)
        for sk in skipped:
            logger.warning(
                "result %s: rekaman %s tidak dinilai — %s (%s)",
                result_id, sk["filename"], sk["tag_label"], sk["reason"] or "tanpa alasan",
            )
        excluded_calls += [
            {"filename": sk["filename"], "duration": call_duration(sk["path"]),
             "tag": sk["tag"], "tag_label": sk["tag_label"], "reason": sk["reason"],
             "kind": "jenis_recording"}
            for sk in skipped
        ]
        pdf_paths = kept_paths
        kept_set = set(kept_paths)
        # Lebih banyak rekaman dicoret daripada yang tersisa: bentuk yang sama dengan
        # satu-satunya kegagalan berat yang pernah terjadi (lihat
        # ``needs_classification_review``). Hanya ditandai — tidak mengubah apa pun.
        classification_review = needs_classification_review(kept_paths, skipped)
        if classification_review:
            logger.warning(
                "result %s: %d dari %d rekaman dicoret klasifikasi — hasilnya layak "
                "ditinjau manusia",
                result_id, len(skipped), len(skipped) + len(kept_paths),
            )

        # 2d. Nama on-air: perkenalan agent WAJIB memakai NAME ONLINE (aturan bisnis
        # 7 September 2026, dikonfirmasi Bank Mega). Dinilai atas rekaman yang DINILAI
        # saja — rekaman milik agent lain sudah dibuang di 2b, dan memasukkannya kembali
        # akan menjamin kegagalan. Vonisnya diterapkan sesudah penilaian, lihat 5e.
        name_verdict = agent_name_verdict(
            {p: texts.get(p, "") for p in pdf_paths}, name_online or "",
            roster_names, nama_karyawan,
        )
        if name_verdict.get("match") is False:
            logger.warning("result %s: %s", result_id, name_verdict.get("reason"))

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
        # [ADAPTASI] Tiga nilai, bukan dua: sumber reference data di repo ini adalah
        # DWH API Aplikasi A, dan ``reference_raw`` (baris cashline + customer MENTAH)
        # ikut disimpan ke result_json di bawah. Snapshot itulah yang dibaca hierarki
        # Statistics, scope Team Leader, timer SLA H+2 dan Agent Error Summary — tanpa
        # menembak App A lagi setiap dashboard dibuka.
        reference_text, ref_warnings, reference_raw = build_reference_data(
            customer_id, db, riplay_extraction=campaign.riplay_extraction
        )
        for warn in ref_warnings:
            logger.warning("reference data (%s / id=%s): %s", result_id, customer_id, warn)
        tahap.catat("campaign_dan_acuan")

        # 5. Penilaian LLM. Dua bentuk, dipilih di sini:
        #
        #   PARALEL (10 September 2026) — tiket punya >= 2 rekaman valid dan SEMUANYA
        #   masih di dalam jendela SLA 7 hari. Tiap rekaman dinilai lewat panggilan LLM
        #   TERPISAH — prompt + KB + scorecard + reference identik, hanya transkrip
        #   rekaman itu — dan panggilannya ditembakkan bersamaan. Setelah semua kembali,
        #   ``parallel_pass.pick_utama`` memilih rekaman utama = rekaman TERTUA (14
        #   September 2026, keputusan Bank Mega — MENGGANTIKAN aturan lama "SESUAI
        #   terbanyak" yang bias ke rekaman penutup singkat, lihat
        #   docs/csv_bank/12 September 2026/recording_utama_perbaikan_ambiguity.md),
        #   lalu ``merge_parallel`` mengisi tiap item non-SESUAI di rekaman utama dari
        #   rekaman lain (timestamp paling baru bila lebih dari satu). Karena model
        #   tidak pernah melihat rekaman lain, ia tidak bisa salah ambil bukti lintas
        #   rekaman — penjaga provenance tidak lagi diperlukan.
        #
        #   FULL (perilaku sejak awal) — semua keadaan lain: tiket satu rekaman, atau
        #   tiket yang punya rekaman di luar jendela SLA. Seluruh rekaman valid digabung
        #   dan dinilai satu panggilan.
        _dalam_sla, luar_sla = recordings_within_sla(pdf_paths)
        luar_sla_files = {os.path.basename(p) for p in luar_sla}
        # ``recording_types`` (tag per PDF untuk dashboard) dibangun BELAKANGAN — di mode
        # paralel ``pick_utama`` bisa menetapkan rekaman utama yang berbeda dari tebakan
        # klasifikasi, dan tag yang tampil harus mengikuti keputusan itu.

        # Kandidat penilaian = seluruh rekaman valid (rekaman batal/ditunda/tidak
        # terhubung sudah dicoret di 2c), diurutkan kronologis supaya ``pick_utama``
        # bisa memakai index 0 = rekaman tertua.
        kandidat_paths = sorted(
            pdf_paths,
            key=lambda p: (parse_filename_timestamp(p) or datetime.min, os.path.basename(p)),
        )
        parallel = len(kandidat_paths) >= 2 and not luar_sla
        if luar_sla:
            logger.warning(
                "result %s: %d rekaman di luar jendela SLA 7 hari (%s) — tiket dinilai "
                "FULL, bukan paralel; perlu ditinjau/diproses ulang",
                result_id, len(luar_sla), ", ".join(sorted(luar_sla_files)),
            )

        scorecard_pass = {
            "mode": "parallel" if parallel else "full",
            "out_of_sla": [os.path.basename(p) for p in luar_sla],
            "rekaman": [],
            "utama_terpilih": None,
            "isian_dari_perbaikan": [],
            "isian_cashline_dari_perbaikan": [],
        }

        if not parallel:
            # `evaluate(return_usage=True)` returns (evaluation, usage); the token
            # usage is logged below but not persisted to MinIO/DB or the API.
            evaluation, _usage = evaluate(
                prompt_text=campaign.prompt_text,
                messages=messages,
                kb_text=campaign.kb_text,
                scorecard_text=campaign.scorecard_text,
                reference_text=reference_text,
                llm_client=_llm_client(),
                model=settings.llm_model,
                source_files=sorted_filenames,
                temperature=settings.llm_temperature,
                seed=settings.llm_seed,
                reasoning_effort=settings.llm_reasoning_effort,
                return_usage=True,
            )
            tahap.catat("penilaian_llm")
            logger.info(
                "token penilaian (full): masuk=%s (ter-cache=%s) keluar=%s (penalaran=%s)",
                _usage.get("input_token"), _usage.get("cached_token"),
                _usage.get("output_token"), _usage.get("reasoning_token"),
            )
        else:
            def _nilai_satu(path):
                f_names, f_msgs, _d, _ds = build_transcript([path])
                ev, usage = evaluate(
                    prompt_text=campaign.prompt_text,
                    messages=f_msgs,
                    kb_text=campaign.kb_text,
                    scorecard_text=campaign.scorecard_text,
                    reference_text=reference_text,
                    llm_client=_llm_client(),
                    model=settings.llm_model,
                    source_files=f_names,
                    temperature=settings.llm_temperature,
                    seed=settings.llm_seed,
                    reasoning_effort=settings.llm_reasoning_effort,
                    return_usage=True,
                )
                return path, ev, usage

            # Satu panggilan LLM per rekaman, ditembakkan bersamaan — tiap panggilan
            # I/O-bound murni ke endpoint, jadi thread cukup. Konkurensi dibatasi 3:
            # 5 panggilan reasoning-berat serentak pernah membuat endpoint melambat
            # cukup jauh sampai task menembus batas waktu Celery (10 September 2026).
            # Tiket dengan > 3 rekaman valid berjalan dalam beberapa gelombang.
            hasil: dict = {}
            with ThreadPoolExecutor(max_workers=min(len(kandidat_paths), 3)) as ex:
                futs = [ex.submit(_nilai_satu, p) for p in kandidat_paths]
                for fut in as_completed(futs):
                    path, ev, usage = fut.result()
                    hasil[path] = (ev, usage)
            tahap.catat("penilaian_llm")

            evals, files, tss = [], [], []
            for p in kandidat_paths:  # sudah kronologis
                ev, usage = hasil[p]
                evals.append(ev)
                files.append(os.path.basename(p))
                tss.append(parse_filename_timestamp(p))
                logger.info(
                    "token penilaian (paralel %s): masuk=%s (ter-cache=%s) keluar=%s "
                    "(penalaran=%s) SESUAI=%d",
                    os.path.basename(p), usage.get("input_token"),
                    usage.get("cached_token"), usage.get("output_token"),
                    usage.get("reasoning_token"), sesuai_count(ev),
                )

            utama_idx = pick_utama(evals)
            evaluation, isian, isian_cashline = merge_parallel(evals, files, tss, utama_idx)

            # Rekaman utama versi PENILAIAN (rekaman tertua, 14 September 2026)
            # menggantikan tebakan klasifikasi: berkas terpilih -> recording_utama;
            # berkas lain yang tadinya recording_utama turun jadi recording_perbaikan.
            # Tag inilah yang dibaca ``stamp_evidence_tags`` / ``stamp_reason_provenance``
            # dan yang tampil di dashboard, jadi "rekaman utama" yang terlihat = rekaman
            # yang benar-benar jadi acuan skor. Dalam praktiknya klasifikasi Fase #2 dan
            # aturan tanggal-tertua ini biasanya SUDAH sepakat (recording_utama menurut
            # klasifikasi memang biasanya yang paling awal); blok ini tetap dipertahankan
            # sebagai jaring pengaman supaya tag yang tampil selalu konsisten dengan
            # rekaman yang benar-benar dipakai sebagai basis skor.
            utama_name = files[utama_idx]
            for _name, _info in list(recording_tags.items()):
                if not isinstance(_info, dict):
                    continue
                _tag = _info.get("tag")
                if _name == utama_name and _tag != TAG_UTAMA:
                    recording_tags[_name] = {
                        **_info, "tag": TAG_UTAMA,
                        "reason": ((_info.get("reason") or "").rstrip(". ")
                                   + ". Ditetapkan rekaman utama: rekaman tertua "
                                     "di antara rekaman valid.").lstrip(". "),
                    }
                elif _name != utama_name and _tag == TAG_UTAMA:
                    recording_tags[_name] = {
                        **_info, "tag": TAG_PERBAIKAN,
                        "reason": ((_info.get("reason") or "").rstrip(". ")
                                   + ". Klasifikasi menandainya utama, tetapi rekaman "
                                     "lain lebih tua.").lstrip(". "),
                    }

            scorecard_pass["rekaman"] = [
                {"file": files[i], "sesuai": sesuai_count(evals[i]),
                 "utama": i == utama_idx}
                for i in range(len(evals))
            ]
            scorecard_pass["utama_terpilih"] = files[utama_idx]
            scorecard_pass["isian_dari_perbaikan"] = isian
            scorecard_pass["isian_cashline_dari_perbaikan"] = isian_cashline
            logger.info(
                "result %s: rekaman utama = %s (SESUAI=%d dari %d rekaman); "
                "%d item diisi dari rekaman lain",
                result_id, files[utama_idx], sesuai_count(evals[utama_idx]),
                len(evals), len(isian),
            )
            for it in isian:
                logger.info(
                    "result %s: %s (%s) -> SESUAI diambil dari %s",
                    result_id, it["item_code"], it["dari_status"], it["sumber_file"],
                )

        # Tag SETIAP rekaman tiket ini — yang dinilai maupun yang dicoret — supaya
        # dashboard bisa menyebutkan perannya per PDF. Dibangun SESUDAH ``pick_utama``
        # supaya "recording_utama" yang tampil sudah versi penilaian, bukan tebakan
        # klasifikasi. ``out_of_sla`` menempel di sini, bukan di ``excluded_calls``:
        # rekaman di luar jendela 7 hari TETAP DINILAI (tiket jatuh ke FULL), jadi ia
        # bukan berkas yang dicoret — hanya perlu terbaca bahwa ia yang membuat tiket
        # ini keluar dari jalur paralel.
        recording_types = [
            {"file": os.path.basename(p),
             "tag": (recording_tags.get(os.path.basename(p)) or {}).get("tag"),
             "tag_label": TAG_LABELS.get(
                 (recording_tags.get(os.path.basename(p)) or {}).get("tag")),
             "reason": (recording_tags.get(os.path.basename(p)) or {}).get("reason") or "",
             "excluded": p not in kept_set,
             "out_of_sla": os.path.basename(p) in luar_sla_files}
            for p in ordered_paths
        ]

        # 5b. Cap versi aturan verifikasi statik ke dalam evaluasi (revamp 21 Agustus
        # 2026), supaya seluruh permukaan pembaca memakai ambang yang benar tanpa perlu
        # menebak dari waktu upload — dan tiket lama tidak ikut dinilai ulang.
        evaluation = stamp_static_rules(evaluation)

        # 5c. Tempelkan JENIS REKAMAN ke tiap blok evidence. Tanpa ini evidence hanya
        # menyebut nama berkas, dan pembacanya harus mencocokkan sendiri ke kolom Call
        # Duration untuk tahu sebuah bukti berasal dari rekaman utama atau perbaikan.
        evaluation = stamp_evidence_tags(evaluation, recording_tags)
        # Asal rekaman ikut ditulis ke dalam ``reason`` tiap baris scorecard: kolom
        # Evidence cukup timestamp + kutipan, sedangkan "dari rekaman mana" dibaca orang
        # justru saat ia sedang membaca alasan vonisnya.
        evaluation = stamp_reason_provenance(evaluation, recording_tags)

        # 5e. Nama on-air tidak sesuai -> SC_CL_2 BELUM_SESUAI. Ditegakkan di sini,
        # bukan lewat prompt: LLM tidak pernah melihat roster sales.
        evaluation = apply_agent_name_verdict(evaluation, name_verdict)

        # 5f. Pengecualian Mega Ultima Shield diselesaikan SEBELUM skor dihitung —
        # skor maksimal tiket bergantung padanya (108.75 bila dikecualikan, 150 bila
        # MUS wajib tetapi tidak dipenuhi). Menggabungkan bacaan LLM atas transkrip
        # dengan daftar tetap Bank Mega; lihat ``compliance.mus_exemption``.
        evaluation = stamp_mus_exemption(evaluation, customer_id)

        # 5d. Skor disetel ulang dari scorecard memakai rumus yang sama dengan pembaca
        # — SELALU, bukan hanya saat tahap 2 memperbaiki sesuatu. Tanpa ini JSON
        # tersimpan kadang membawa angka mentah LLM yang tidak mengenal iris 10% item
        # non-tolerable; lihat ``two_pass.resync_scores``.
        evaluation = resync_scores(evaluation)

        # 5d-bis. Mode paralel: ``critical_compliance_check`` diambil UTUH dari rekaman
        # utama, tetapi ``merge_parallel`` sudah menaikkan sebagian ``scorecard_result``
        # dari rekaman lain. Tanpa penyelarasan ini, item kritis yang sudah SESUAI di
        # rekaman perbaikan tetap ditagih ``-(maximum_score / 4)`` di blok kritis.
        # ``resync_scores`` sengaja tidak menyentuh blok itu (butuh keputusan arah),
        # jadi ``_resync_critical`` dijepit di antara dua ``resync_scores``: yang pertama
        # sudah menetapkan ``maximum_score``, yang kedua menghitung ulang phase3 &
        # status dengan penalti kritis yang baru.
        if parallel:
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
            # Panggilan yang TIDAK ikut dinilai karena milik agent lain
            # (kosong pada tiket satu-agent). Disimpan supaya QC bisa melihat bahwa
            # sebuah PDF sengaja ditinggalkan, bukan hilang diam-diam.
            "excluded_calls": excluded_calls,
            # Jenis tiap rekaman tiket ini (utama / perbaikan / pembatalan / ...),
            # termasuk yang tetap dinilai. Lihat compliance/recording_type.py.
            "recording_types": recording_types,
            # Bentuk penilaian scorecard-nya: "parallel" (satu panggilan LLM per
            # rekaman) atau "full" (satu panggilan gabungan). Pada mode paralel:
            # jumlah SESUAI tiap rekaman, mana yang terpilih jadi utama, dan item apa
            # yang diisi dari rekaman lain. Lihat compliance/parallel_pass.py.
            "scorecard_pass": scorecard_pass,
            # Lama tiap tahap dalam detik — lihat ``_Tahap``.
            "timings": tahap.data,
            # True bila klasifikasi mencoret lebih banyak rekaman daripada yang
            # disisakannya — penanda untuk ditinjau, bukan vonis.
            "classification_review": classification_review,
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
