"""Jenis recording: rekaman mana pada satu tiket yang boleh dinilai.

Pasangan runtime dari ``prompt.recording_type`` — modul itu memegang prompt &
daftar tag, modul ini yang memanggil LLM dan memutuskan rekaman mana yang tersisa.

Alurnya menumpang pola ``compliance.call_ownership`` (penyaring pra-LLM yang sudah ada
sejak 31 Agustus 2026), termasuk kontrak keluarannya: ``(kept, dropped)``, dan yang
dibuang tetap dilaporkan lengkap supaya bisa ditampilkan dicoret di kolom Call Duration
alih-alih lenyap dari layar.

TIGA JARING PENGAMAN, semuanya mengembalikan seluruh rekaman:

1. **Panggilan LLM gagal** (timeout, JSON tidak terbaca, kuota habis). Tiket tidak boleh
   ikut gagal hanya karena langkah pemilahan ini — ia hanya menyaring, bukan menilai.
2. **Model tidak menyebut sebuah berkas**, atau menyebut nama yang tidak dikenal. Berkas
   yang tidak punya label dianggap layak dinilai; menganggapnya tidak layak berarti
   membuang transkrip karena kelalaian model.
3. **Semua rekaman ter-tag dicoret.** Tiket tanpa satu pun transkrip pasti salah, dan
   lebih baik menilai berlebih daripada menerbitkan evaluasi kosong — persis alasan
   yang sama dengan jaring pengaman ``filter_calls_by_agent``.

Semua keputusan ditulis ke log dengan alasannya, karena inilah satu-satunya tempat
yang tahu MENGAPA sebuah PDF tidak ikut dinilai.
"""
import collections
import logging
import os
import re

from prompt.recording_type import (
    EXCLUDED_TAGS,
    SYSTEM_PROMPT,
    TAG_LABELS,
    TAGS,
    build_user_content,
)

logger = logging.getLogger(__name__)

# Batas waktu SATU panggilan klasifikasi, detik. Klien LLM dipakai bersama seluruh
# worker dengan ``LLM_TIMEOUT`` 1800 detik — ukuran yang pas untuk panggilan PENILAIAN
# yang memang berjalan ~5 menit, tetapi keliru total untuk panggilan yang normalnya
# selesai dalam 8 detik. Pengukuran 5 September 2026 menangkap dua panggilan klasifikasi
# yang tersendat ~1.795 detik (bukan gagal — benar-benar selesai setelah 30 menit),
# dan dengan tiga suara paparan itu ikut berlipat.
#
# 180 detik = ~20x waktu normal, cukup longgar untuk lonjakan beban 24 worker paralel,
# tetapi memastikan satu panggilan yang tersendat dibuang dalam 3 menit alih-alih
# menahan tiketnya setengah jam. Suara yang habis waktunya dilewati seperti kegagalan
# lain; bila ketiganya habis, tidak ada yang disaring.
CLASSIFY_TIMEOUT = 180


def _log_usage(label: str, response) -> None:
    """Catat pemakaian token satu panggilan ke log.

    Satu-satunya cara mengetahui ongkos sebenarnya: porsi ter-cache ditagih lebih murah
    dan token penalaran tidak terlihat di hasil, jadi keduanya tidak bisa diperkirakan
    dari luar. Diam-diam gagal bila server tidak melaporkannya — ini catatan, bukan
    bagian dari penilaian.
    """
    try:
        u = getattr(response, "usage", None)
        if u is None:
            return
        pd = getattr(u, "prompt_tokens_details", None)
        cd = getattr(u, "completion_tokens_details", None)
        logger.info(
            "token %s: masuk=%s (ter-cache=%s) keluar=%s (penalaran=%s)",
            label, getattr(u, "prompt_tokens", None),
            getattr(pd, "cached_tokens", None) if pd else None,
            getattr(u, "completion_tokens", None),
            getattr(cd, "reasoning_tokens", None) if cd else None,
        )
    except Exception:  # noqa: BLE001 — pencatatan tidak boleh menjatuhkan apa pun
        pass


def _parse_json_object(content):
    """Objek JSON dari balasan model. ``None`` bila tidak ada yang bisa dibaca.

    Sengaja tidak mengimpor helper di ``compliance.evaluator``: yang di sana privat dan
    melempar ``ValueError`` untuk jalur penilaian yang memang harus gagal keras,
    sedangkan di sini balasan yang tidak terbaca cukup berarti "tidak menyaring apa pun".
    """
    import json
    import re

    if not content:
        return None
    text = str(content).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    return None


def classify_recordings(items, llm_client, model, temperature=1.0, seed=None,
                        reasoning_effort=None, submit_time=None, votes=1) -> dict:
    """``{nama_berkas: {"tag", "reason"}}`` untuk seluruh rekaman satu tiket.

    ``items`` = ``[{"file", "duration", "text"}, ...]`` urut kronologis. SATU panggilan
    LLM untuk seluruh tiket, bukan satu per rekaman: label "perbaikan" hanya punya arti
    bila model bisa melihat rekaman yang diperbaikinya, dan satu panggilan jauh lebih
    murah daripada N panggilan berisi teks yang saling tumpang tindih.

    ``submit_time`` (``tms_cashline.submit_time``) ikut dikirim sebagai jangkar bila
    tiketnya terdaftar di TMS — lihat ``prompt.recording_type.build_user_content``.

    Berkas yang tidak dilabeli model tidak muncul di hasil — pemanggil memperlakukannya
    sebagai layak dinilai (jaring pengaman 2). Mengembalikan ``{}`` bila panggilannya
    gagal; ``items`` kosong tidak memanggil LLM sama sekali.
    """
    if not items:
        return {}
    if votes and int(votes) > 1:
        return _classify_by_vote(items, llm_client, model, temperature, seed,
                                 reasoning_effort, submit_time, int(votes))
    create_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_content(items, submit_time)},
        ],
        "temperature": temperature,
        "timeout": CLASSIFY_TIMEOUT,
    }
    if seed is not None:
        create_kwargs["seed"] = seed
    if reasoning_effort:
        create_kwargs["extra_body"] = {"reasoning_effort": reasoning_effort}

    try:
        response = llm_client.chat.completions.create(**create_kwargs)
        _log_usage("klasifikasi", response)
        parsed = _parse_json_object(response.choices[0].message.content)
    except Exception:  # noqa: BLE001 — penyaring tidak boleh menjatuhkan tiket
        logger.exception("klasifikasi jenis recording gagal; seluruh rekaman dinilai")
        return {}
    if not isinstance(parsed, dict):
        logger.warning("klasifikasi jenis recording: balasan bukan objek JSON")
        return {}

    known = {item.get("file") for item in items}
    out = {}
    for row in parsed.get("recordings") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("file") or "").strip()
        tag = str(row.get("tag") or "").strip().casefold()
        if name not in known:
            # Nama berkas yang dikarang model. Tidak bisa dipetakan ke PDF mana pun,
            # jadi diabaikan — memaksakannya berarti mencoret berkas yang salah.
            logger.warning("klasifikasi menyebut berkas asing: %r", name)
            continue
        if tag not in TAGS:
            logger.warning("klasifikasi memberi tag di luar daftar: %r (%s)", tag, name)
            continue
        out[name] = {"tag": tag, "reason": str(row.get("reason") or "").strip()}
    return out


def _classify_by_vote(items, llm_client, model, temperature, seed, reasoning_effort,
                      submit_time, votes) -> dict:
    """Klasifikasi diulang ``votes`` kali; tag terbanyak per berkas yang menang.

    Kenapa perlu: label sebuah rekaman menentukan apakah ia dinilai sama sekali, dan
    satu salah tag merambat jauh — pada tiket ``030808fLO1`` satu rekaman utama yang
    keliru dibaca sebagai pembatalan menjatuhkan tiga rekaman sekaligus, memaksa tiket
    ke mode full, dan menggeser skornya dari 31,875 ke −56,44. Tidak ada jaring pengaman
    yang bisa menangkap itu: dari sudut pandang sistem, tiket dengan satu rekaman valid
    adalah keadaan yang sah.

    Kenapa voting, bukan menurunkan suhu: model ini MENOLAK ``temperature`` selain 1
    pada jalur reasoning ("Only the default (1) value is supported"), jadi variasi
    sampling tidak bisa dimatikan di sumbernya. Voting menyerangnya langsung — dengan
    tingkat keliru ~1 dari 8 per percobaan, dua dari tiga percobaan harus keliru dengan
    cara yang SAMA sebelum hasilnya ikut keliru.

    Murah: satu panggilan klasifikasi ~8 detik dan ~69 ribu karakter, sementara satu
    panggilan penilaian ~5 menit dan ~300 ribu karakter. Tiga suara menambah ~16 detik
    pada tiket yang butuh ~11 menit.

    Seri dimenangkan tag yang MEMPERTAHANKAN rekaman (bukan tag yang mencoret), sejalan
    dengan prinsip modul ini: lebih baik menilai berlebih daripada diam-diam membuang
    transkrip. Percobaan yang gagal dilewati; bila SELURUHNYA gagal hasilnya ``{}`` dan
    pemanggil tidak menyaring apa pun.
    """
    suara = collections.defaultdict(list)
    for _ in range(max(2, votes)):
        hasil = classify_recordings(items, llm_client, model, temperature, seed,
                                    reasoning_effort, submit_time, votes=1)
        for nama, info in (hasil or {}).items():
            suara[nama].append(info)
    out = {}
    for nama, daftar in suara.items():
        hitung = collections.Counter(i["tag"] for i in daftar)
        tertinggi = max(hitung.values())
        unggul = [t for t, n in hitung.items() if n == tertinggi]
        menang = (unggul[0] if len(unggul) == 1
                  else next((t for t in unggul if t not in EXCLUDED_TAGS), unggul[0]))
        if len(unggul) > 1 or tertinggi < len(daftar):
            logger.warning(
                "klasifikasi %s tidak bulat: %s -> dipakai %r",
                nama, dict(hitung), menang,
            )
        out[nama] = next(i for i in daftar if i["tag"] == menang)
    return out


def split_by_recording_type(pdf_paths, tags_by_file) -> tuple:
    """``(kept, dropped)`` — path yang dinilai vs yang dicoret, berikut alasannya.

    ``dropped`` berisi ``{"path", "filename", "tag", "tag_label", "reason"}`` supaya
    pemanggil bisa menuliskannya ke ``excluded_calls`` tanpa membaca ulang apa pun.

    Berkas tanpa label DIPERTAHANKAN (jaring pengaman 2), dan bila penyaringan
    menyisakan nol berkas seluruhnya dikembalikan (jaring pengaman 3).
    """
    kept, dropped = [], []
    for path in pdf_paths:
        info = tags_by_file.get(os.path.basename(path)) or {}
        if info.get("tag") in EXCLUDED_TAGS:
            dropped.append({
                "path": path,
                "filename": os.path.basename(path),
                "tag": info["tag"],
                "tag_label": TAG_LABELS.get(info["tag"], info["tag"]),
                "reason": info.get("reason") or "",
            })
        else:
            kept.append(path)
    if not kept:
        logger.warning(
            "seluruh rekaman ter-tag tidak dinilai (%s) — semuanya dikembalikan",
            ", ".join(d["tag"] for d in dropped),
        )
        return list(pdf_paths), []
    return kept, dropped


def needs_classification_review(kept, dropped) -> bool:
    """Apakah hasil klasifikasi tiket ini layak dilihat manusia?

    True bila rekaman yang DICORET lebih banyak daripada yang tersisa. Itu bukan
    kesalahan dengan sendirinya — tiket yang benar-benar berisi tiga panggilan batal
    memang seperti itu — tetapi ia bentuk yang sama dengan satu-satunya kegagalan berat
    yang pernah terjadi: pada ``030808fLO1`` satu rekaman utama 19 menit keliru terbaca
    sebagai pembatalan, tiga dari empat rekaman tercoret, tiket jatuh ke mode full, dan
    skornya bergerak dari 31,875 ke −56,44. Tidak ada jaring pengaman yang bisa
    menangkapnya: dari sudut pandang sistem, tiket dengan satu rekaman valid adalah
    keadaan yang sah.

    Sengaja hanya MENANDAI, tidak mengubah apa pun. Menahan tiket atau membatalkan
    penyaringan akan menukar satu kegagalan diam-diam dengan kegagalan diam-diam yang
    lain; yang dibutuhkan di sini adalah seseorang yang melihat.

    Voting tiga suara (``_classify_by_vote``) sudah memperkecil peluangnya jauh —
    penanda ini lapisan kedua, untuk sisa yang lolos.
    """
    return len(dropped or []) > len(kept or [])


def stamp_evidence_tags(evaluation, tags_by_file):
    """Sisipkan jenis rekaman ke SETIAP blok evidence di dalam evaluasi. Non-destruktif.

    Keluaran LLM menyebut asal evidence hanya lewat ``evidence.ticket_id``, yang isinya
    nama berkas transkrip ("030808fLO1_20260714140803" — bukan ticket id, warisan
    penamaan lama). Nama berkas saja memaksa pembacanya mencocokkan sendiri ke daftar
    tag di kolom Call Duration untuk tahu apakah sebuah bukti diambil dari rekaman utama
    atau rekaman perbaikan — padahal justru itu yang perlu terlihat sejak Fase #3.

    Karena itu tiap blok yang membawa ``ticket_id`` ikut diberi ``recording_tag`` dan
    ``recording_tag_label``. Ditempel di sini, DETERMINISTIK dari hasil klasifikasi,
    bukan diminta ke LLM: model sudah punya cukup pekerjaan, dan tag yang ditebak ulang
    per blok evidence pasti akan berbeda dengan tag berkasnya sendiri di suatu tempat.

    Berlaku untuk seluruh isi evaluasi — scorecard, minat produk, verifikasi — karena
    penelusurannya rekursif; blok yang ``ticket_id``-nya kosong atau tidak cocok dengan
    berkas mana pun dibiarkan apa adanya.
    """
    lookup = {}
    for name, info in (tags_by_file or {}).items():
        tag = (info or {}).get("tag")
        if not tag:
            continue
        lookup[str(name).removesuffix(".pdf")] = tag

    def walk(node):
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        out = {k: walk(v) for k, v in node.items()}
        if "ticket_id" in out:
            tag = lookup.get(str(out.get("ticket_id") or "").removesuffix(".pdf"))
            if tag:
                out["recording_tag"] = tag
                out["recording_tag_label"] = TAG_LABELS.get(tag, tag)
        return out

    return walk(evaluation)


# Ekor kalimat yang ditulis LLM: "... - Evidence diambil dari Final Konfirmasi Mega
# Cashline". Isinya FASE percakapan — informasi yang sudah ada di kolom kategori item
# itu sendiri, dan kalimatnya berbenturan dengan keterangan asal REKAMAN yang dipasang
# di bawah ("Evidence diambil dari recording utama"). Dua kalimat berawalan sama persis
# di satu sel membuat pembacanya harus menebak mana yang menerangkan apa, jadi ekor
# lama dibuang lebih dulu.
_REASON_PHASE_TAIL = re.compile(
    r"\s*[-–—]\s*Evidence\s+diambil\s+dari\s+[^.]*\.?\s*$", re.IGNORECASE
)


def stamp_reason_provenance(evaluation, tags_by_file):
    """Tulis ASAL REKAMAN sebuah evidence ke dalam ``reason`` tiap baris scorecard.

    Permintaan bisnis 5 September 2026: kolom Evidence cukup memuat timestamp + kutipan,
    sedangkan "dari rekaman mana bukti ini datang" pindah ke kolom Reason. Sebelumnya
    keterangan itu hanya ada sebagai tag kecil di kolom Ticket ID — terbaca saat
    seseorang memang mencarinya, tidak saat ia sedang membaca alasan vonisnya.

    Tiga bentuk kalimat, dipilih dari keadaan barisnya sendiri:

    * baris biasa            -> "Evidence diambil dari recording utama."
    * baris yang tertolong
      tahap 2 (``pass2``)    -> "Evidence tidak disebutkan pada recording utama tetapi
                                disebutkan pada recording perbaikan."
    * baris tahap 2 yang
      lolos lewat recap      -> menyebutkan bahwa yang menyelamatkannya adalah pembacaan
                                ulang di segmen Final Konfirmasi, BUKAN penjelasan
                                tersendiri — perbedaan yang justru ingin dilihat
                                Bank Mega (lihat ``prompt.pass2``).

    Ditulis DETERMINISTIK dari hasil klasifikasi + penanda ``pass2``/``evidence_source``,
    bukan diminta ke LLM: kalimat yang ditebak model akan berbeda dengan tag berkasnya
    sendiri di suatu tempat, dan justru kalimat inilah yang dibaca QC saat bersengketa.

    Baris tanpa evidence (mis. ``TIDAK_DINILAI``) tidak disentuh — tidak ada asal yang
    bisa disebutkan. Non-destruktif.
    """
    from prompt.pass2 import SOURCE_FALLBACK

    lookup = {}
    for name, info in (tags_by_file or {}).items():
        tag = (info or {}).get("tag")
        if tag:
            lookup[str(name).removesuffix(".pdf")] = tag

    rows = (evaluation or {}).get("scorecard_result")
    if not isinstance(rows, list):
        return evaluation

    out, berubah = [], False
    for row in rows:
        r = row if isinstance(row, dict) else {}
        stem = str(((r.get("evidence") or {}).get("ticket_id")) or "").strip().removesuffix(".pdf")
        tag = lookup.get(stem)
        if not tag:
            out.append(row)
            continue
        if r.get("pass2"):
            if r.get("evidence_source") == SOURCE_FALLBACK:
                ekor = ("Evidence tidak disebutkan pada recording utama; item ini "
                        "terpenuhi dari pembacaan ulang pada segmen Final Konfirmasi "
                        "di recording perbaikan, bukan dari penjelasan tersendiri.")
            else:
                ekor = ("Evidence tidak disebutkan pada recording utama tetapi "
                        "disebutkan pada recording perbaikan.")
        else:
            ekor = f"Evidence diambil dari {TAG_LABELS.get(tag, tag).lower()}."
        pokok = _REASON_PHASE_TAIL.sub("", str(r.get("reason") or "")).strip().rstrip(".;,- ")
        baru = f"{pokok}. - {ekor}" if pokok else ekor
        if baru == r.get("reason"):
            out.append(row)
            continue
        out.append({**r, "reason": baru})
        berubah = True
    if not berubah:
        return evaluation
    return {**evaluation, "scorecard_result": out}
