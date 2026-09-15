"""Tahap pipeline pemrosesan satu tiket — urutan, label, dan tabel progresnya.

Permintaan 14 September 2026: saat status masih pending/processing, dashboard hanya
menampilkan "Status: processing — hasil belum tersedia" tanpa rincian, padahal worker
sudah punya checkpoint per tahap. Worker menulis checkpoint TERAKHIR yang selesai ke
``results.current_stage`` (lihat ``db.crud.set_result_stage``), dan API menyusun tabel
progresnya dari daftar di bawah.

KENAPA DI SINI, BUKAN DI WORKER. Di repo monolit daftar ini tinggal di
``worker/tasks/process_transcript.py`` dan API mengimpornya dari sana. Sesudah repo
dipisah, image API tidak memuat ``worker/`` sama sekali, jadi impor itu mustahil.
Menduplikasinya di API akan menghasilkan DUA daftar tahap yang bisa berbeda diam-diam —
dan perbedaannya baru ketahuan sebagai baris progres yang salah label di layar QC.
Daftar ini dibaca kedua sisi, dan itu persis definisi kode bersama.

``stage_table`` ikut tinggal di sini karena alasan yang sama: aturan "``current_stage``
adalah checkpoint yang SUDAH selesai, jadi yang berjalan adalah satu sesudahnya"
sebaiknya berdiri tepat di sebelah daftar yang mendefinisikannya, bukan tersebar di
router yang kebetulan menampilkannya.
"""

#: Urutan tahap, dari pertama sampai terakhir: ``(key, label)``.
#: ``key`` HARUS sama persis dengan yang ditulis worker lewat ``tahap.catat(...)``.
PROCESSING_STAGES = [
    ("unduh_pdf", "Mengunduh rekaman dari penyimpanan"),
    ("baca_teks_pdf", "Membaca teks transkrip"),
    ("klasifikasi_llm", "Mengklasifikasi jenis rekaman (AI)"),
    ("rangkai_transkrip", "Menyusun transkrip gabungan"),
    ("campaign_dan_acuan", "Memuat konfigurasi campaign & data acuan"),
    ("penilaian_llm", "Penilaian AI (scorecard)"),
    ("gabung_dan_skor", "Menggabungkan hasil & menghitung skor"),
    ("simpan_hasil", "Menyimpan hasil"),
    ("tandai_selesai", "Selesai"),
]

#: Kunci tahap yang sah — dipakai worker untuk menjaga salah ketik ``catat(...)``.
STAGE_KEYS = tuple(key for key, _ in PROCESSING_STAGES)

STATE_SELESAI = "selesai"
STATE_BERJALAN = "berjalan"
STATE_MENUNGGU = "menunggu"


def stage_table(current_stage) -> list[dict]:
    """Tabel progres untuk satu tiket: ``[{"key", "label", "state"}, ...]``.

    ``current_stage`` adalah checkpoint TERAKHIR yang SUDAH SELESAI (lihat
    ``_Tahap.catat`` di worker), BUKAN yang sedang berjalan — jadi tahap "berjalan"
    adalah SATU SESUDAHNYA.

    ``None`` (belum ada checkpoint sama sekali) membuat tahap PERTAMA yang ditandai
    berjalan, bukan selesai. Nilai yang tidak dikenal katalog diperlakukan sama dengan
    ``None``: hasil lama atau nama tahap yang sudah tidak ada tidak boleh membuat
    seluruh tabel tampak selesai.
    """
    idx = next((i for i, (k, _) in enumerate(PROCESSING_STAGES) if k == current_stage), -1)
    return [
        {
            "key": key,
            "label": label,
            "state": STATE_SELESAI if i <= idx
            else (STATE_BERJALAN if i == idx + 1 else STATE_MENUNGGU),
        }
        for i, (key, label) in enumerate(PROCESSING_STAGES)
    ]
