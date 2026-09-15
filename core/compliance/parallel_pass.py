"""Penilaian PARALEL: satu panggilan LLM per rekaman, lalu digabung (10 September 2026).

Menggantikan dua pendekatan sebelumnya:

* dua tahap (``two_pass.merge_pass2``) — tahap 1 rekaman utama, tahap 2 perbaikan;
  mengirim ulang prompt + KB di setiap tahap.
* satu panggilan (dihapus: ``core/prompt/single_pass.py``) — semua rekaman digabung
  dalam satu panggilan yang diminta mengeluarkan dua scorecard; model malah mengambil
  bukti lintas rekaman dan array keduanya (``scorecard_perbaikan``) tidak pernah keluar.

Alur baru: tiap rekaman valid dinilai SENDIRI — prompt + KB + scorecard + reference
sama, hanya transkrip rekaman itu. Model tidak pernah melihat rekaman lain, jadi tidak
bisa salah ambil bukti lintas rekaman. Penjaga provenance jadi STRUKTURAL, bukan
tambalan kode (dulu ``two_pass.enforce_utama_provenance``, juga dihapus).

Setelah N keluaran terkumpul:

1. **Rekaman utama = rekaman TERTUA (index 0)** — keputusan Bank Mega 14 September
   2026, MENGGANTIKAN aturan lama "SESUAI terbanyak" (10 September 2026). Aturan
   lama terbukti bias ke rekaman PENUTUP yang singkat (lewat fallback Final
   Konfirmasi di ``two_pass.py``, sebuah rekaman 4 menit yang cuma membacakan
   ulang & submit bisa memanen SESUAI lebih banyak daripada rekaman substantif
   19 menit sebelumnya) — lihat kronologi lengkap di
   ``docs/csv_bank/12 September 2026/recording_utama_perbaikan_ambiguity.md``
   (tiket contoh: ``030808fLO1``). ``sesuai_count`` masih dihitung untuk LOGGING
   (``scorecard_pass`` di dashboard), tapi TIDAK LAGI dipakai memilih rekaman
   utama. Pemanggil menjamin ``evaluations`` sudah urut kronologis (rekaman
   paling awal di index 0) — cukup index 0 yang diambil, tanpa perlu menyisir
   timestamp lagi di sini.
2. ``scorecard_result`` rekaman utama jadi DASAR. Tiap baris yang statusnya BUKAN
   ``SESUAI`` di utama dicari penggantinya di rekaman lain yang menilainya ``SESUAI``;
   bila lebih dari satu, ambil rekaman dengan **timestamp paling baru**. Baris
   pengganti diberi ``pass2 = True`` supaya ``recording_type.stamp_reason_provenance``
   menuliskan asal buktinya sebagai rekaman perbaikan.
3. Blok di luar scorecard (``cashline_interest``, ``mus_interest``,
   ``critical_compliance``, badword, ``ai_summary`` — apa pun selain
   ``scorecard_result``) diambil UTUH dari keluaran rekaman utama. Tiap panggilan
   menilainya juga, tetapi yang jadi acuan hanya milik rekaman utama.

   PENGECUALIAN (14 September 2026) — ``cashline_data_extraction`` /
   ``cashline_data_verification``: kedua blok ini TIDAK ikut aturan "utuh dari
   utama" di atas, melainkan digabung FIELD DEMI FIELD lewat
   ``_merge_cashline_data`` — lihat docstring fungsi itu untuk alasannya (tiket
   contoh: ``030808fLO1``, di mana rekaman utama hanya menyebut simulasi awal
   nominal cicilan/bunga sementara rekaman perbaikan menyampaikan angka final
   yang benar, dan scorecard_result-nya sendiri SUDAH dibetulkan oleh mekanisme
   nomor 2 di atas — tanpa pengecualian ini, ``cashline_data_verification``
   tertinggal salah dan lewat ``_propagate_cashline_to_scorecard`` menuliskan
   ulang "reason" scorecard yang sudah benar itu dengan alasan yang salah).
4. **ATURAN BANK MEGA — WAJIB ULANG (14 September 2026, dikonfirmasi Bank Mega,
   BUKAN inferensi sistem)**: bila ADA SATU SAJA item kategori "Penjelasan"
   sebuah produk (Mega Cashline ATAU Mega Ultima Shield) yang BELUM_SESUAI di
   rekaman UTAMA, maka SELURUH item kategori "Final Konfirmasi" DAN "Legal
   Statement" produk YANG SAMA WAJIB DIULANG di rekaman perbaikan — berlaku
   simetris untuk kedua produk (lihat ``WAJIB_ULANG_ATURAN`` dan
   ``_apply_wajib_ulang_final_konfirmasi``). Ini BUKAN sekadar "cari SESUAI di
   rekaman lain" seperti nomor 2: final konfirmasi/legal statement yang dibuat
   berdasarkan penjelasan yang ternyata salah tidak lagi berlaku, jadi KEDUA
   kategori itu diganti UTUH dengan versi rekaman perbaikan — TERMASUK item
   yang kebetulan sudah SESUAI di rekaman utama — bukan hanya item yang gagal.
   Bila rekaman perbaikan sendiri tidak benar-benar mengulanginya, hasilnya
   tetap BELUM_SESUAI, bersumber dari rekaman perbaikan (bukan diam-diam
   mempertahankan status lama rekaman utama). Tiket contoh: ``030808fLO1``
   (SC_CL_7/8 Penjelasan Mega Cashline gagal di utama -> SC_CL_25..32 Final
   Konfirmasi & SC_CL_37 Legal Statement Mega Cashline wajib diulang).
"""
import copy
from datetime import datetime

SESUAI = "SESUAI"

#: Pasangan (kategori PEMICU -> kategori yang WAJIB DIULANG) untuk aturan Bank
#: Mega nomor 4 di atas — satu pasangan per produk, keduanya diperlakukan
#: SIMETRIS: penjelasan produk itu yang gagal membuat final konfirmasi & legal
#: statement produk YANG SAMA (bukan produk lain) tidak berlaku lagi.
WAJIB_ULANG_ATURAN = [
    (
        "penjelasan mega cashline",
        {"final konfirmasi mega cashline", "legal statement mega cashline"},
    ),
    (
        "penjelasan mega ultima shield",
        {"final konfirmasi mega ultima shield", "legal statement mega ultima shield"},
    ),
]


def _kategori(item) -> str:
    return str((item or {}).get("category") or "").strip().casefold()


def _norm(status) -> str:
    return str(status or "").strip().upper()


def sesuai_count(evaluation: dict) -> int:
    """Jumlah baris ``scorecard_result`` berstatus ``SESUAI`` pada satu keluaran LLM."""
    return sum(
        1
        for r in (evaluation.get("scorecard_result") or [])
        if _norm(r.get("status")) == SESUAI
    )


def pick_utama(evaluations: list[dict]) -> int:
    """Index rekaman utama = rekaman TERTUA (index 0) — keputusan Bank Mega 14
    September 2026, menggantikan aturan lama "SESUAI terbanyak" (lihat
    docstring modul).

    ``evaluations`` HARUS sudah urut kronologis (rekaman paling awal di index 0).
    """
    if not evaluations:
        raise ValueError("pick_utama: daftar keluaran kosong")
    return 0


def _kosong(value) -> bool:
    """True bila nilai field ekstraksi dianggap KOSONG (null / string blank)."""
    return value is None or (isinstance(value, str) and not value.strip())


def _merge_cashline_data(
    evaluations: list[dict],
    files: list[str],
    timestamps: "list[datetime | None]",
    utama_idx: int,
) -> "tuple[dict | None, list[dict] | None, list[dict]]":
    """Gabungkan ``cashline_data_extraction``/``cashline_data_verification`` LINTAS
    rekaman, FIELD DEMI FIELD — "rekaman TERBARU yang punya nilai valid menang".

    Ini persis aturan "Extraction priority order" yang SUDAH tertulis di prompt
    Task B sejak awal ("ANTAR panggilan, posisi (call_index, start_timestamp)
    TERBESAR selalu menang" / "hasil verifikasi ULANG pada panggilan terbaru,
    BUKAN yang pertama") — tetapi aturan itu ditulis untuk mode FULL (satu
    panggilan LLM melihat semua rekaman sekaligus) dan TIDAK BISA dipatuhi model
    dalam mode PARALEL, karena setiap panggilan hanya melihat SATU rekaman dan
    tidak tahu apa yang dikatakan rekaman lain. Tanpa fungsi ini, kedua blok itu
    ikut aturan umum "diambil utuh dari rekaman utama" (lihat docstring modul),
    padahal keduanya BUKAN status "sudah dikerjakan atau belum" seperti
    scorecard — melainkan DATA MENTAH yang lazim dikoreksi/diperbarui pada
    panggilan berikutnya. Beda dari merge scorecard (yang HANYA mengganti baris
    non-SESUAI), di sini SEMUA field dibandingkan lintas rekaman, TERMASUK yang
    utama sudah punya nilai bukan-null — nilai utama bisa saja sekadar simulasi
    awal yang kemudian dikoreksi di rekaman perbaikan.

    Tiket nyata yang mengungkap celah ini: ``030808fLO1`` (14 September 2026) —
    rekaman utama cuma menyebut simulasi "kurang lebih 3.128.000" dan preminya
    (bukan bunga Cashline) sebagai bunga, sedangkan rekaman perbaikan
    menyampaikan Final Konfirmasi dengan angka yang benar (bunga 2,09%, provisi
    2% dari limit, cicilan 3.128.333). ``scorecard_result``-nya sendiri SUDAH
    dibetulkan lewat mekanisme SESUAI di atas (evidence-nya benar, dari rekaman
    perbaikan), tapi ``cashline_data_verification`` tertinggal salah dan lewat
    ``_propagate_cashline_to_scorecard`` (read-time, di ``error_codes.py``)
    menuliskan ulang "reason" item scorecard yang sudah benar itu dengan alasan
    yang salah — persis gejala "evidence benar, reason masih dari rekaman
    utama" yang dilaporkan QC.

    Kembalikan ``(None, None, [])`` bila TIDAK SATU PUN evaluasi punya
    ``cashline_data_extraction`` (campaign lain / field ini tidak relevan sama
    sekali) — pemanggil lalu tidak menimpa kunci itu di ``evaluation``.
    Kembalikan ``rincian_cashline``: satu entri per FIELD yang nilai akhirnya
    diambil dari rekaman BUKAN utama, untuk jejak di ``scorecard_pass``
    (sejajar konsepnya dengan ``rincian_isian`` scorecard).
    """
    if not any(isinstance(ev.get("cashline_data_extraction"), dict) for ev in evaluations):
        return None, None, []

    base_extraction = dict(evaluations[utama_idx].get("cashline_data_extraction") or {})
    base_verif_by_field = {
        v.get("field"): v
        for v in (evaluations[utama_idx].get("cashline_data_verification") or [])
        if isinstance(v, dict) and v.get("field")
    }

    fields: list = []
    seen = set()
    for ev in evaluations:
        for f in (ev.get("cashline_data_extraction") or {}):
            if f not in seen:
                seen.add(f)
                fields.append(f)

    merged_extraction: dict = {}
    merged_verif_by_field = dict(base_verif_by_field)
    rincian: list[dict] = []

    for field in fields:
        kandidat = [
            (timestamps[i] or datetime.min, i, val)
            for i, ev in enumerate(evaluations)
            if not _kosong(val := (ev.get("cashline_data_extraction") or {}).get(field))
        ]
        if not kandidat:
            merged_extraction[field] = base_extraction.get(field)
            continue

        # Tuple penuh (BUKAN key=lambda t: t[0]): saat timestamp seri (mis. tidak
        # ada satu pun rekaman berlabel timestamp), pemenangnya index TERBESAR —
        # rekaman PALING BARU dalam urutan kronologis ``evaluations`` — bukan
        # yang pertama ditemukan.
        _, src_idx, src_val = max(kandidat)
        merged_extraction[field] = src_val

        src_verif_row = next(
            (
                v for v in (evaluations[src_idx].get("cashline_data_verification") or [])
                if isinstance(v, dict) and v.get("field") == field
            ),
            None,
        )
        if src_verif_row is not None:
            merged_verif_by_field[field] = copy.deepcopy(src_verif_row)

        if src_idx != utama_idx and src_val != base_extraction.get(field):
            rincian.append({
                "field": field,
                "dari_nilai": base_extraction.get(field),
                "ke_nilai": src_val,
                "sumber_file": files[src_idx],
            })

    merged_verification = [merged_verif_by_field[f] for f in fields if f in merged_verif_by_field]
    # Baris verifikasi yang field-nya tidak muncul di cashline_data_extraction
    # sama sekali (semestinya tidak terjadi) dipertahankan agar tidak diam-diam
    # hilang dari output.
    for f, row in base_verif_by_field.items():
        if f not in seen:
            merged_verification.append(row)

    return merged_extraction, merged_verification, rincian


def _apply_wajib_ulang_final_konfirmasi(
    base: dict,
    evaluations: list[dict],
    files: list[str],
    timestamps: "list[datetime | None]",
    utama_idx: int,
) -> "tuple[dict, list[dict]]":
    """Aturan Bank Mega (14 September 2026, dikonfirmasi langsung — bukan
    inferensi sistem), berlaku SIMETRIS untuk Mega Cashline DAN Mega Ultima
    Shield (lihat ``WAJIB_ULANG_ATURAN``): bila ADA item kategori "Penjelasan"
    sebuah produk yang BELUM_SESUAI di rekaman UTAMA, SELURUH item kategori
    "Final Konfirmasi" DAN "Legal Statement" produk YANG SAMA WAJIB DIULANG di
    rekaman perbaikan. Alasannya: final konfirmasi/legal statement itu
    MENGONFIRMASI penjelasan yang ternyata salah, jadi konfirmasi yang dibuat
    berdasarkan penjelasan yang salah itu tidak berlaku lagi — terlepas dari
    statusnya sendiri saat itu. Bila HANYA salah satu produk yang penjelasannya
    gagal (mis. Cashline gagal tapi MUS tidak), HANYA kategori produk itu yang
    wajib diulang — produk yang lain tidak tersentuh.

    BERBEDA dari merge scorecard umum di ``merge_parallel`` (yang HANYA
    mengganti item BUKAN-SESUAI): di sini SELURUH item kategori yang terpicu
    diganti UTUH dengan versi rekaman perbaikan (rekaman BUKAN utama dengan
    timestamp TERBARU) — TERMASUK yang kebetulan sudah SESUAI di rekaman
    utama — karena kewajibannya adalah MENGULANG, bukan sekadar memperbaiki
    yang gagal. Bila rekaman perbaikan sendiri TIDAK benar-benar mengulanginya
    (item itu BELUM_SESUAI juga di sana, atau tidak dinilai sama sekali),
    hasilnya mengikuti rekaman perbaikan APA ADANYA — termasuk tetap
    BELUM_SESUAI bila memang belum diulang — bukan diam-diam mempertahankan
    status lama rekaman utama.

    Tidak berlaku (``base`` dikembalikan apa adanya, ``rincian`` kosong) bila
    TIDAK ADA satu pun kategori Penjelasan yang gagal di utama, atau bila
    tidak ada rekaman lain selain utama untuk dijadikan acuan "rekaman
    perbaikan".
    """
    utama_items = evaluations[utama_idx].get("scorecard_result") or []
    utama_kategori_gagal = {
        _kategori(it) for it in utama_items if _norm(it.get("status")) == "BELUM_SESUAI"
    }
    target_kategori: set = set()
    for pemicu, target in WAJIB_ULANG_ATURAN:
        if pemicu in utama_kategori_gagal:
            target_kategori |= target
    if not target_kategori:
        return base, []

    kandidat_perbaikan = [
        (timestamps[i] or datetime.min, i) for i in range(len(evaluations)) if i != utama_idx
    ]
    if not kandidat_perbaikan:
        return base, []
    _, perbaikan_idx = max(kandidat_perbaikan)
    perbaikan_items = {
        (it or {}).get("item_code"): it
        for it in (evaluations[perbaikan_idx].get("scorecard_result") or [])
        if (it or {}).get("item_code")
    }

    out_rows: list[dict] = []
    rincian: list[dict] = []
    changed = False
    for row in base.get("scorecard_result") or []:
        if _kategori(row) not in target_kategori:
            out_rows.append(row)
            continue
        code = row.get("item_code")
        sumber_row = perbaikan_items.get(code)
        if sumber_row is None:
            # Rekaman perbaikan tidak menilai item ini sama sekali — tidak ada
            # yang bisa dijadikan pengganti, pertahankan baris sekarang.
            out_rows.append(row)
            continue
        pengganti = copy.deepcopy(sumber_row)
        # Penanda yang sama dengan merge scorecard umum, dibaca
        # ``recording_type.stamp_reason_provenance`` untuk menulis "diulang
        # pada rekaman perbaikan".
        pengganti["pass2"] = True
        out_rows.append(pengganti)
        rincian.append({
            "item_code": code,
            "dari_status": row.get("status"),
            "ke_status": pengganti.get("status"),
            "sumber_file": files[perbaikan_idx],
            "evidence": pengganti.get("evidence") or {},
            "alasan": "wajib_ulang_final_konfirmasi_legal_statement",
        })
        changed = True

    if not changed:
        return base, []
    return {**base, "scorecard_result": out_rows}, rincian


def merge_parallel(
    evaluations: list[dict],
    files: list[str],
    timestamps: "list[datetime | None]",
    utama_idx: int,
) -> "tuple[dict, list[dict], list[dict]]":
    """Gabungkan N keluaran LLM menjadi satu evaluasi.

    Kembalikan ``(evaluation, rincian_isian, rincian_cashline)``:

    * ``evaluation`` — salinan dalam keluaran rekaman utama. ``scorecard_result``-nya
      punya tiap baris non-``SESUAI`` yang berhasil diisi dari rekaman lain ditimpa
      oleh baris ``SESUAI`` milik rekaman itu (utuh: evidence, reason, dst.), LALU
      seluruh item "Final Konfirmasi Mega Cashline"/"Legal Statement Mega Cashline"
      ditimpa UTUH dengan versi rekaman perbaikan bila ada item "Penjelasan Mega
      Cashline" yang gagal di utama (aturan Bank Mega — lihat
      ``_apply_wajib_ulang_final_konfirmasi``), dan
      ``cashline_data_extraction``/``cashline_data_verification``-nya digabung
      field demi field lewat ``_merge_cashline_data`` (lihat docstring fungsi itu).
    * ``rincian_isian`` — satu entri per baris SCORECARD yang diganti (baik oleh
      merge umum maupun oleh aturan "wajib ulang" — beda hanya lewat key
      ``"alasan"``, hanya ada pada baris "wajib ulang"), untuk jejak di
      ``scorecard_pass``.
    * ``rincian_cashline`` — satu entri per FIELD cashline data yang diganti,
      jejak yang sama untuk ``cashline_data_extraction``/``_verification``.

    ``timestamps`` sejajar dengan ``evaluations``/``files``; ``None`` diperlakukan
    sebagai paling lama (``datetime.min``) supaya rekaman bertimestamp selalu menang.
    """
    if not (len(evaluations) == len(files) == len(timestamps)):
        raise ValueError("merge_parallel: panjang evaluations/files/timestamps beda")

    base = copy.deepcopy(evaluations[utama_idx])
    out_rows: list[dict] = []
    rincian: list[dict] = []

    for row in base.get("scorecard_result") or []:
        code = row.get("item_code")
        if not code or _norm(row.get("status")) == SESUAI:
            out_rows.append(row)
            continue

        # Kandidat pengganti: baris SESUAI dengan item_code sama di rekaman LAIN.
        kandidat: list[tuple] = []
        for i, ev in enumerate(evaluations):
            if i == utama_idx:
                continue
            for r in ev.get("scorecard_result") or []:
                if r.get("item_code") == code and _norm(r.get("status")) == SESUAI:
                    kandidat.append((timestamps[i] or datetime.min, i, r))

        if not kandidat:
            out_rows.append(row)
            continue

        _, src_idx, src_row = max(kandidat, key=lambda t: t[0])
        pengganti = copy.deepcopy(src_row)
        # Penanda "baris ini diisi dari rekaman non-utama" — dibaca
        # ``recording_type.stamp_reason_provenance`` untuk menulis kalimat "tidak
        # disebutkan pada recording utama tetapi disebutkan pada recording perbaikan"
        # (atau varian recap-nya bila ``evidence_source`` menunjuk fallback). Nama
        # kunci ``pass2`` dipertahankan agar cabang yang sudah ada di stamp itu dipakai
        # apa adanya; tidak ada kode skor yang membacanya.
        pengganti["pass2"] = True
        out_rows.append(pengganti)
        rincian.append(
            {
                "item_code": code,
                "dari_status": row.get("status"),
                "ke_status": _norm(src_row.get("status")),
                "sumber_file": files[src_idx],
                "evidence": pengganti.get("evidence") or {},
            }
        )

    base["scorecard_result"] = out_rows

    # Aturan Bank Mega "wajib ulang" (14 September 2026, lihat docstring modul
    # nomor 4 dan ``_apply_wajib_ulang_final_konfirmasi``): dijalankan SETELAH
    # merge umum di atas supaya menimpanya untuk kategori yang terpicu, bahkan
    # untuk item yang tadi sudah "menang" sebagai SESUAI dari rekaman utama.
    base, rincian_wajib_ulang = _apply_wajib_ulang_final_konfirmasi(
        base, evaluations, files, timestamps, utama_idx
    )
    rincian.extend(rincian_wajib_ulang)

    merged_extraction, merged_verification, rincian_cashline = _merge_cashline_data(
        evaluations, files, timestamps, utama_idx
    )
    if merged_extraction is not None:
        base["cashline_data_extraction"] = merged_extraction
    if merged_verification is not None:
        base["cashline_data_verification"] = merged_verification

    return base, rincian, rincian_cashline
