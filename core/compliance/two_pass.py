"""Penilaian scorecard DUA TAHAP: rekaman utama dulu, rekaman perbaikan menyusul.

Permintaan bisnis 5 September 2026 (Fase #3). Sampai sekarang seluruh rekaman valid
satu tiket digabung menjadi SATU percakapan lalu dinilai sekali, dan aturan KB
memerintahkan evidence diambil dari "penyebutan PALING BARU sebelum segmen Final
Konfirmasi" — lintas panggilan. Akibatnya kegagalan di rekaman utama tertutup oleh
pengulangan di rekaman perbaikan, dan itu terukur: pada tiket ``030808fLO1`` provisi
tidak disebut SAMA SEKALI di rekaman utama (0 kali) namun tercatat SESUAI karena
evidence-nya diambil dari rekaman perbaikan — berlawanan dengan ground truth Bank Mega
yang menyebut tiket itu gagal provisi.

ALURNYA:

* **Tahap 1** menilai seluruh item HANYA dari ``recording_utama``.
* **Tahap 2** memeriksa ``recording_perbaikan`` untuk (a) item yang tahap 1 tinggalkan
  ``BELUM_SESUAI``/``PENDING``, dan (b) ``MANDATORY_PASS2_CODES`` — kategori Final
  Konfirmasi + Legal Statement Mega Cashline, yang menurut aturan bisnis WAJIB diulang
  di rekaman perbaikan sekalipun tahap 1 sudah meluluskannya.
* Item yang ketemu di tahap 2 menjadi ``SESUAI`` **penuh**. Itu memang guna rekaman
  perbaikan: memperbaiki yang kurang di rekaman utama. Tidak ada potongan nilai.

YANG SENGAJA **TIDAK** DIUBAH — fallback Final Konfirmasi. Prompt campaign menyatakannya
permanen (keputusan bisnis 28 Agustus 2026; pencabutannya pernah diusulkan dan DITOLAK
karena 344 item "Penjelasan" pada 98 tiket bergantung padanya). Di sini fallback hanya
BERPINDAH POSISI ke tahap 2, dan pemakaiannya ditandai ``evidence_source`` sehingga
"benar-benar diperbaiki" bisa dibedakan dari "kebetulan terbaca di recap". Di tahap 1
fallback berlaku seperti biasa — 36 dari 98 tiket hanya punya satu rekaman sehingga
tahap 2 tidak pernah jalan di sana.

TAHAP 2 TIDAK PERNAH MEMPERBURUK. Ia hanya boleh menaikkan status menjadi ``SESUAI``.
Rekaman perbaikan berdurasi 4 menit tidak boleh menjatuhkan item yang sudah benar
dijelaskan panjang lebar di rekaman utama hanya karena tidak diulang di sana.
"""
import logging
from datetime import timedelta

from compliance.error_codes import CRITICAL_ITEM_CODES
from compliance.pdf_parser import parse_filename_timestamp
from compliance.scoring import (
    base_ai_status,
    max_score,
    non_tolerable_bomb,
    passing_grade,
    phase3_score,
    scorecard_score,
)
from prompt.recording_type import TAG_PERBAIKAN, TAG_UTAMA

logger = logging.getLogger(__name__)

# Item yang WAJIB diperiksa ulang di rekaman perbaikan walau tahap 1 sudah SESUAI —
# kategori scorecard "Final Konfirmasi Mega Cashline" (SC_CL_25..32) dan "Legal
# Statement Mega Cashline" (SC_CL_37). Kategori Mega Ultima Shield SENGAJA tidak ikut:
# aturan #7 menyatakan bila yang gagal item Cashline, MUS tidak perlu diulang.
MANDATORY_PASS2_CODES = (
    "SC_CL_25", "SC_CL_26", "SC_CL_27", "SC_CL_28",
    "SC_CL_29", "SC_CL_30", "SC_CL_31", "SC_CL_32",
    "SC_CL_37",
)

# Status tahap 1 yang membuat sebuah item dicari ulang di rekaman perbaikan.
# ``TIDAK_DINILAI`` sengaja TIDAK termasuk: itu artinya produknya memang tidak diambil
# (mis. seluruh item MUS pada tiket yang nasabahnya menolak asuransi), jadi mencarinya
# di rekaman perbaikan hanya membuang tenaga dan mengundang evidence yang dipaksakan.
RETRY_STATUSES = ("BELUM_SESUAI", "PENDING")

# Kategori scorecard yang PUNYA aturan fallback Final Konfirmasi, jadi satu-satunya
# tempat penanda ``evidence_source`` punya arti.
#
# Prompt campaign menyebut dua pengecualian pemilihan evidence: (a) kategori
# "Penjelasan Mega Cashline" — kandidat dibatasi pada posisi SEBELUM segmen Final
# Konfirmasi, dan bila tidak ada, BARU diambil dari dalam segmen itu (inilah
# fallback-nya); (b) kategori "Final Konfirmasi Mega Cashline" — kandidat WAJIB berada
# DI DALAM segmen Final Konfirmasi. Pada (b) evidence dari dalam recap bukan fallback,
# melainkan satu-satunya sumber yang sah; menandainya "fallback" menyatakan hal yang
# tidak benar. Uji coba 5 September 2026 memperlihatkan model memang menandai kedelapan
# item Final Konfirmasi sebagai fallback — angka yang, bila ditampilkan apa adanya, akan
# membuat orang menyimpulkan agent nyaris tidak menjelaskan apa pun.
FALLBACK_AWARE_CATEGORIES = ("Penjelasan Mega Cashline",)

# Jendela SLA rekaman: sebuah tiket boleh dinilai PARTIAL (dua tahap) hanya bila seluruh
# rekaman validnya berada dalam 7 hari terakhir, dihitung mundur dari rekaman PALING
# BARU (permintaan bisnis 5 September 2026). Acuannya rekaman terbaru, BUKAN
# ``submit_time``: submit_time hanya ada bila tiketnya terdaftar di TMS, dan pada data
# yang ada terdapat rekaman yang justru terjadi SESUDAH submit_time — dua keadaan yang
# tidak punya jawaban di matriks.
#
# DIHITUNG PER TANGGAL KALENDER, BUKAN PER MENIT (ditegaskan 5 September 2026). Rekaman
# terbaru 16 Juli -> batasnya tanggal 9 Juli, dan seluruh rekaman tanggal 9 Juli masuk
# berapa pun jamnya. Presisi menit menghasilkan hasil yang tidak bisa dipertanggung-
# jawabkan ke agent: pada tiket ``060526FLPO`` rekaman 9 Juli 16:49 gugur hanya karena
# rekaman terakhir kebetulan jam 17:04 — lewat 15 menit. Dari 7 tiket yang tersaring
# dengan presisi menit, 3 di antaranya lewat kurang dari 6 jam.
SLA_WINDOW = timedelta(days=7)


def split_by_tag(paths, tags_by_file) -> tuple:
    """``(utama, perbaikan, lain)`` — path dikelompokkan menurut tag jenis rekaman.

    ``lain`` menampung rekaman ber-tag ``lainnya`` atau yang tidak berhasil dilabeli.
    Pemanggil menilainya bersama rekaman utama: keranjang sisa tidak boleh diam-diam
    hilang dari penilaian, prinsip yang sama dengan jaring pengaman ``recording_type``.
    """
    import os

    utama, perbaikan, lain = [], [], []
    for p in paths:
        tag = (tags_by_file.get(os.path.basename(p)) or {}).get("tag")
        if tag == TAG_UTAMA:
            utama.append(p)
        elif tag == TAG_PERBAIKAN:
            perbaikan.append(p)
        else:
            lain.append(p)
    return utama, perbaikan, lain


def recordings_within_sla(paths) -> tuple:
    """``(dalam_sla, di_luar)`` — rekaman yang masih di dalam jendela 7 hari kalender.

    Batasnya = TANGGAL rekaman paling baru dikurangi 7 hari; sebuah rekaman masuk bila
    TANGGAL-nya tidak lebih awal dari batas itu, jamnya tidak diperhitungkan (lihat
    ``SLA_WINDOW``). Berkas tanpa timestamp yang bisa dibaca dianggap DI DALAM jendela:
    kita tidak tahu kapan ia terjadi, dan menendangnya keluar berarti menghukum tiket
    karena penamaan berkas, bukan karena isinya.
    """
    stamped = [(parse_filename_timestamp(p), p) for p in paths]
    known = [ts for ts, _ in stamped if ts is not None]
    if not known:
        return list(paths), []
    batas = max(known).date() - SLA_WINDOW
    dalam = [p for ts, p in stamped if ts is None or ts.date() >= batas]
    luar = [p for ts, p in stamped if ts is not None and ts.date() < batas]
    return dalam, luar


def items_needing_pass2(evaluation: dict) -> list:
    """item_code yang perlu diperiksa di rekaman perbaikan, urut seperti di scorecard.

    Dua sumber: item yang tahap 1 tinggalkan gagal/tertunda, dan
    ``MANDATORY_PASS2_CODES`` yang selalu diulang. Item ``TIDAK_DINILAI`` tidak ikut —
    termasuk bila ia kebetulan ada di daftar wajib, karena produknya memang tidak
    diambil dan tidak ada yang perlu diulang.
    """
    out = []
    for row in (evaluation or {}).get("scorecard_result") or []:
        code = (row or {}).get("item_code")
        status = (row or {}).get("status")
        if not code or status == "TIDAK_DINILAI":
            continue
        if status in RETRY_STATUSES or code in MANDATORY_PASS2_CODES:
            out.append(code)
    return out


def _evidence_stem(row) -> str:
    """Nama berkas asal evidence sebuah baris scorecard, tanpa ``.pdf``.

    Keluaran LLM menulisnya di ``evidence.ticket_id`` — isinya nama berkas transkrip
    ("030808fLO1_20260714140803"), bukan ticket id, warisan penamaan lama.
    """
    ev = (row or {}).get("evidence") or {}
    return str(ev.get("ticket_id") or "").strip().removesuffix(".pdf")


def merge_pass2(base: dict, pass2: dict, codes, evidence_files=None) -> tuple:
    """Gabungkan temuan tahap 2 ke evaluasi tahap 1. ``(evaluasi, rincian)``.

    Hanya ``codes`` yang dilihat, dan hanya bila tahap 2 menyatakannya ``SESUAI`` —
    tahap 2 tidak pernah memperburuk (lihat docstring modul). Baris yang diperbarui
    membawa ``evidence`` dari rekaman perbaikan, ``evidence_source`` (penjelasan vs
    fallback recap), dan ``pass2 = True`` supaya jejaknya terbaca tanpa membandingkan
    dua JSON.

    ``evidence_files`` = nama berkas rekaman perbaikan. Bila diisi, rescue yang
    evidence-nya menunjuk berkas LAIN DITOLAK. Ini penegakan yang sesungguhnya atas
    aturan "evidence hanya boleh dari rekaman perbaikan": tahap 2 menerima transkrip
    tiket secara utuh sebagai konteks (tanpa itu kategori Final Konfirmasi tidak bisa
    dinilai sama sekali — lihat ``prompt.pass2``), jadi tanpa penyaring ini pintu yang
    baru saja ditutup Fase #3 akan terbuka lagi lewat jalur lain: item yang gagal di
    rekaman utama bisa "diselamatkan" oleh kutipan dari rekaman utama itu juga.

    ``rincian`` = ``[{item_code, evidence_source, file}]`` untuk dicatat ke log dan
    ke ``result_json``.
    """
    if not base or not pass2:
        return base, []
    sah = {str(f).removesuffix(".pdf") for f in (evidence_files or [])}
    found = {}
    for r in pass2.get("scorecard_result") or []:
        code = (r or {}).get("item_code")
        if code not in set(codes) or (r or {}).get("status") != "SESUAI":
            continue
        if sah and _evidence_stem(r) not in sah:
            logger.warning(
                "tahap 2: %s ditolak — evidence menunjuk %r, di luar rekaman perbaikan",
                code, _evidence_stem(r) or "(kosong)",
            )
            continue
        found[code] = r
    if not found:
        # Tidak ada item scorecard yang tertolong, tetapi blok verifikasi bisa saja
        # tetap membaik — keduanya dinilai terpisah oleh model.
        merged, rincian_verif = _merge_verification(base, pass2, evidence_files)
        if not rincian_verif:
            return base, []
        return resync_scores(_resync_critical(merged)), rincian_verif

    rows, rincian = [], []
    for row in base.get("scorecard_result") or []:
        code = (row or {}).get("item_code")
        hit = found.get(code)
        if hit is None or (row or {}).get("status") == "SESUAI":
            # Sudah SESUAI di tahap 1 -> tidak ada yang perlu diperbaiki. Berlaku juga
            # untuk item wajib (MANDATORY_PASS2_CODES): pemeriksaannya di rekaman
            # perbaikan tetap dilakukan, tetapi hasilnya tidak boleh menurunkan apa pun.
            rows.append(row)
            continue
        # Penanda hanya disimpan untuk kategori yang memang punya aturan fallback;
        # di kategori lain ia tidak punya arti (lihat FALLBACK_AWARE_CATEGORIES).
        src = (hit.get("evidence_source") or None
               if (row or {}).get("category") in FALLBACK_AWARE_CATEGORIES else None)
        rows.append({
            **row,
            "status": "SESUAI",
            # ``item_score`` dikembalikan ke bobot penuh: item ini memang terpenuhi,
            # dan ``scorecard_score`` hanya memotong yang BELUM_SESUAI.
            "item_score": row.get("weight"),
            "reason": hit.get("reason") or row.get("reason"),
            "evidence": hit.get("evidence"),
            "evidence_source": src,
            "pass2": True,
        })
        rincian.append({
            "item_code": code,
            "evidence_source": src,
            "file": ((hit.get("evidence") or {}).get("ticket_id")),
        })

    merged = {**base, "scorecard_result": rows}
    merged, rincian_verif = _merge_verification(merged, pass2, evidence_files)
    merged = _resync_critical(merged)
    return resync_scores(merged), rincian + rincian_verif


# Blok verifikasi yang ikut diperbarui tahap 2. ``cashline_data_verification`` menilai
# apakah tiap nilai komersial TMS (limit, tenor, bunga, provisi, admin, cicilan…) benar
# disebut/dikonfirmasi ke nasabah — pertanyaan yang jawabannya berubah begitu rekaman
# perbaikan ikut dihitung.
#
# ``card_holder_verification`` SENGAJA TIDAK ikut: isinya verifikasi identitas nasabah
# (tanggal lahir, nama ibu kandung) yang punya jalur normalisasi determinstiknya sendiri
# di ``error_codes.normalize_static_verification`` — similarity dihitung ulang di Python
# dari ``extracted_mentions``, lalu ambang band ditegakkan. Menimpanya dengan keluaran
# LLM tahap 2 berarti membuang penyebutan yang terkumpul di tahap 1 dan menyerahkan
# angka yang sudah deterministik kembali ke model.
PASS2_VERIFICATION_BLOCKS = ("cashline_data_verification",)

# Nilai ``match`` yang dianggap "terpenuhi" — hanya ini yang boleh diambil dari tahap 2.
_VERIFICATION_OK = ("MATCH",)


def _skor_match(row) -> float:
    """``item_score`` untuk baris verifikasi yang dinyatakan MATCH.

    Diambil dari tahap 2 bila ia menyebutnya dan angkanya tidak menghukum; selain itu
    nol. Baris yang cocok memang tidak mengurangi apa pun — dan aturan yang berlaku
    menegaskan error code TIDAK memotong poin: yang memotong adalah status item
    scorecard, bukan baris verifikasi maupun kode yang diterbitkannya.
    """
    nilai = (row or {}).get("item_score")
    try:
        angka = float(nilai)
    except (TypeError, ValueError):
        return 0
    return angka if angka >= 0 else 0


def _merge_verification(base: dict, pass2: dict, evidence_files=None) -> tuple:
    """Perbarui blok verifikasi dengan temuan tahap 2. ``(evaluasi, rincian)``.

    Aturannya sama persis dengan scorecard: hanya boleh MEMPERBAIKI (bukan MATCH ->
    MATCH), dicocokkan per ``field``, dan — bila ``evidence_files`` diisi — hanya bila
    evidence-nya menunjuk rekaman perbaikan.

    Tanpa ini satu tiket menampilkan dua kebenaran sekaligus: pada ``030808fLO1``
    scorecard menyatakan provisi & bunga SESUAI dengan evidence dari rekaman perbaikan,
    sementara ``cashline_data_verification`` — yang tidak ikut digabung — masih berbunyi
    "tidak pernah disebut/dikonfirmasi di transkrip" dan menerbitkan dua baris error code
    B03 ber-risk base M. Angka Total Failure ikut terkerek untuk pelanggaran yang menurut
    scorecard sudah tidak ada.
    """
    sah = {str(f).removesuffix(".pdf") for f in (evidence_files or [])}
    out = dict(base)
    rincian = []
    for blok in PASS2_VERIFICATION_BLOCKS:
        rows = base.get(blok)
        if not isinstance(rows, list) or not rows:
            continue
        baru = {}
        for r in (pass2.get(blok) or []):
            field = (r or {}).get("field")
            if not field or str((r or {}).get("match") or "").upper() not in _VERIFICATION_OK:
                continue
            # Penyaring asal-evidence hanya bisa ditegakkan bila barisnya MEMBAWA
            # evidence. Baris ``cashline_data_verification`` tidak punya: bentuknya
            # ``{field, match, extracted_value, reference_value, similarity_percent,
            # reason, ...}`` tanpa kutipan maupun nama berkas — prompt campaign memang
            # tidak memintanya di sana. Percobaan pertama tetap memberlakukan
            # penyaringnya, sehingga SETIAP baris verifikasi tertolak diam-diam dan
            # penggabungan ini tidak pernah menyala sama sekali.
            #
            # Yang tersisa sebagai penjaga: aturan "tidak pernah memperburuk" di bawah,
            # ditambah kenyataan bahwa tahap 1 sudah menilai rekaman utama. Sebuah baris
            # hanya berubah bila tahap 1 menyebutnya MISMATCH dan tahap 2 menyebutnya
            # MATCH — perbedaan pendapat yang, tanpa nama berkas, tidak bisa kita
            # sempitkan lebih jauh dari ini.
            if sah and "evidence" in (r or {}) and _evidence_stem(r) not in sah:
                continue
            baru[field] = r
        # Field yang tahap 1 sebut MISMATCH tetapi tahap 2 tidak sebut sama sekali:
        # verdict tahap 1 dipertahankan (kita tidak boleh mengarang), tapi keadaan itu
        # harus terbaca — ia sumber pertentangan antara tabel Scorecard dan tabel Error
        # Code, dan tidak ada tempat lain yang bisa menceritakannya.
        disebut = set(baru) | {
            (r or {}).get("field") for r in (pass2.get(blok) or []) if (r or {}).get("field")
        }
        for r in rows:
            f = (r or {}).get("field")
            if f and f not in disebut and str((r or {}).get("match") or "").upper() \
                    not in _VERIFICATION_OK:
                logger.warning(
                    "tahap 2 tidak menyebut %s.%s — verdict tahap 1 (%s) dipertahankan",
                    blok, f, (r or {}).get("match"),
                )
        if not baru:
            continue
        hasil = []
        for r in rows:
            field = (r or {}).get("field")
            hit = baru.get(field)
            if hit is None or str((r or {}).get("match") or "").upper() in _VERIFICATION_OK:
                hasil.append(r)
                continue
            hasil.append({**r, "match": hit.get("match"),
                          "extracted_value": hit.get("extracted_value"),
                          "similarity_percent": hit.get("similarity_percent"),
                          "reason": hit.get("reason") or r.get("reason"),
                          "evidence": hit.get("evidence"),
                          # ``item_score`` WAJIB ikut disetel ulang. Baris yang naik ke
                          # MATCH tidak boleh membawa potongan dari vonis tahap 1: tabel
                          # "Ringkasan Penilaian AI" memungut baris verifikasi ber-
                          # ``item_score`` negatif ke bagian "Pengurangan – Verifikasi
                          # data", sehingga tiket 030808fLO1 sempat menampilkan tiga
                          # baris pengurangan (provisi -4, bunga -1, cicilan -1) yang
                          # alasannya sendiri berbunyi "sesuai TMS".
                          "item_score": _skor_match(hit),
                          "pass2": True})
            rincian.append({"block": blok, "field": field,
                            "file": _evidence_stem(hit) or None})
        out[blok] = hasil
    return out, rincian


def _resync_critical(evaluation: dict) -> dict:
    """Selaraskan ``critical_compliance_check`` dengan scorecard hasil gabungan.

    ``error_codes._sync_critical_compliance`` hanya menjatuhkan (PASS -> FAIL); di sini
    yang dibutuhkan justru arah sebaliknya — ``SC_CL_37`` termasuk item wajib tahap 2,
    jadi ia BISA naik dari gagal menjadi lulus, dan irisan penaltinya harus ikut hilang.
    Membiarkannya berarti tiket kehilangan seperempat skor maksimal untuk pelanggaran
    yang sudah tidak ada.

    Penaltinya dihitung ulang dari nol dengan rumus yang sama dengan prompt campaign:
    ``-(maximum_score / 4)`` per item kritis yang gagal.
    """
    ccc = evaluation.get("critical_compliance_check")
    if not isinstance(ccc, dict) or not ccc.get("checked_items"):
        return evaluation
    status_by_code = {
        (r or {}).get("item_code"): (r or {}).get("status")
        for r in evaluation.get("scorecard_result") or []
    }
    items, gagal = [], 0
    for it in ccc["checked_items"]:
        code = (it or {}).get("item_code")
        if code in CRITICAL_ITEM_CODES and code in status_by_code:
            baru = "FAIL" if status_by_code[code] == "BELUM_SESUAI" else "PASS"
        else:
            baru = (it or {}).get("status")
        if baru == "FAIL":
            gagal += 1
        items.append({**it, "status": baru})
    out = {
        **evaluation,
        "critical_compliance_check": {
            **ccc,
            "status": "FAIL" if gagal else "PASS",
            "checked_items": items,
        },
    }
    maks = evaluation.get("maximum_score")
    try:
        maks = float(maks)
    except (TypeError, ValueError):
        return out
    penalti = -(maks / 4) * gagal
    out["ai_score_critical_compliance_check"] = (
        int(penalti) if penalti == int(penalti) else penalti
    )
    return out


def resync_scores(evaluation: dict) -> dict:
    """Hitung ulang skor & status dari scorecard, memakai rumus yang sama dengan pembaca.

    Dipanggil worker untuk SETIAP tiket sebelum menyimpan — full maupun partial, ada
    perbaikan tahap 2 maupun tidak. Sebelumnya hanya jalan lewat ``merge_pass2``, jadi
    JSON tersimpan kadang membawa angka deterministik dan kadang angka mentah LLM:
    ``180107uT48`` tersimpan ``ai_score_phase_3 = 94`` sementara yang berlaku 19, sebab
    LLM tidak mengenal iris 10% item non-tolerable (aturan Python, 31 Agustus 2026).
    Layarnya selalu benar — pembaca menghitung ulang — tetapi JSON itu ikut diarsipkan
    ke MinIO, diekspor ke XLSX, dan dibaca manusia saat menelusuri sengketa.

    Yang TIDAK disentuh di sini: ``critical_compliance_check``. Menyelaraskannya butuh
    keputusan arah (``_resync_critical`` boleh menaikkan PASS, sedangkan
    ``error_codes._sync_critical_compliance`` di sisi pembaca hanya menjatuhkan), dan
    itu hanya sah ketika sebuah item memang tertolong tahap 2. Untuk tiket biasa blok
    kritisnya dibiarkan apa adanya.

    Angka yang disimpan tetap bisa berbeda dari layar untuk sebab yang SAH: pembaca
    menambahkan keadaan yang bergantung waktu — tenggat dokumen H+2, banding yang
    di-approve, propagasi verifikasi. Yang dihapus di sini hanyalah selisih yang tidak
    punya alasan sama sekali.
    """
    out = dict(evaluation)
    # Penyebutnya lebih dulu: sejak aturan MUS 8 September 2026 skor maksimal bisa
    # berubah setelah LLM menulis hasilnya (tiket cashline-saja tanpa pengecualian naik
    # 108.75 -> 150), dan batas lulus bawaan LLM ikut basi bersamanya. Membiarkan
    # keduanya apa adanya membuat JSON tersimpan membandingkan skor baru ke batas lama.
    ms = max_score(out)
    if ms is not None:
        out["maximum_score"] = ms
    pg = passing_grade(out)
    if pg is not None:
        out["passing_grade"] = pg
    p2 = scorecard_score(out)
    if p2 is not None:
        out["ai_score_phase_2"] = p2
    # Komponennya ikut disimpan supaya JSON-nya bisa dibaca tanpa menghitung sendiri —
    # inilah suku yang selama ini hilang dan membuat angka tersimpan meleset.
    out["ai_score_non_tolerable"] = non_tolerable_bomb(out)
    p3 = phase3_score(out)
    if p3 is not None:
        out["ai_score_phase_3"] = p3
    st = base_ai_status(out)
    if st is not None:
        out["ai_status"] = st
    return out
