"""Prompt klasifikasi JENIS RECORDING satu ticket id (permintaan bisnis 4 September 2026).

Satu ticket id dikirim sebagai kumpulan PDF, dan sampai 5 September 2026 seluruhnya
dinilai tanpa memeriksa APA peran masing-masing rekaman. Padahal isinya bermacam-macam:
ada rekaman utama, rekaman perbaikan, rekaman yang batal karena nasabah menolak, dan
rekaman yang ditutup nasabah karena sedang sibuk. Mengambil evidence dari rekaman yang
batal berarti menilai percakapan yang tidak jadi apa-apa — pada tiket ``030808fLO1``
dua rekaman 10 Juli berisi pembatalan ("saya nggak jadi dah", "berarti kita batal Pak
ya") sementara pengajuan yang benar-benar disubmit terjadi 14 Juli.

**Kenapa LLM, bukan pola kata seperti ``compliance.call_ownership``?** Gaya bicara agent
berbeda-beda dan pembatalan hampir tidak pernah diucapkan dengan kalimat baku; kata
"batal" juga muncul pada kalimat yang justru BUKAN pembatalan ("kalau dananya di bawah
limit, apakah bisa dibatalkan?"). Yang membedakan adalah bagaimana percakapan itu
BERAKHIR dan apa akibatnya — penilaian konteks, bukan pencocokan kata.

DUA DIMENSI YANG SENGAJA DIJADIKAN SATU LABEL. ``recording_utama``/``recording_perbaikan``
menjawab "apa peran panggilan ini", sedangkan ``ditunda_oleh_customer``/``tidak_terhubung``
menjawab "bagaimana panggilan ini berakhir" — dan satu panggilan bisa keduanya. Pada
``030808fLO1`` hal itu benar-benar terjadi: ``..._20260714131827`` adalah rekaman utama
tetapi ditutup nasabah di tengah ("sebentar saya dipanggil dokter... nanti mohon diangkat
lima belas menit kemudian"), persis pola akhir yang sama dengan ``..._20260710111836``
yang justru harus di-tag ``ditunda_oleh_customer``. Karena itu prompt di bawah memakai
satu aturan tegas: **PERAN MENANG selama isi pokoknya sempat tersampaikan**; label
"ditunda"/"tidak terhubung" hanya untuk panggilan yang berakhir SEBELUM ada isi yang
layak dinilai.
"""

# Daftar TERTUTUP. Model wajib memilih salah satu — "dsb" tidak bisa diimplementasikan,
# dan label bebas akan membuat penyaringnya tidak bisa diuji.
TAG_UTAMA = "recording_utama"
TAG_PERBAIKAN = "recording_perbaikan"
TAG_PEMBATALAN = "pembatalan_oleh_customer"
TAG_DITUNDA = "ditunda_oleh_customer"
TAG_TIDAK_TERHUBUNG = "tidak_terhubung"
TAG_LAINNYA = "lainnya"

TAGS = (
    TAG_UTAMA,
    TAG_PERBAIKAN,
    TAG_PEMBATALAN,
    TAG_DITUNDA,
    TAG_TIDAK_TERHUBUNG,
    TAG_LAINNYA,
)

# Tag yang TIDAK ikut dinilai — panggilannya tidak dikirim ke LLM penilai, tapi tetap
# tampil di layar dalam keadaan dicoret berikut alasannya (lihat ``compliance.
# recording_type.split_by_recording_type`` dan kolom Call Duration).
#
# ``lainnya`` sengaja TIDAK termasuk: ia keranjang sisa, dan mencoret keranjang sisa
# berarti membuang isi yang gagal dikenali model tanpa ada yang menyadarinya. Prinsip
# yang sama dipakai jaring pengaman ``call_ownership``: lebih baik menilai berlebih
# daripada diam-diam menilai kurang.
EXCLUDED_TAGS = frozenset({TAG_PEMBATALAN, TAG_DITUNDA, TAG_TIDAK_TERHUBUNG})

# Label yang dibaca manusia di dashboard.
TAG_LABELS = {
    TAG_UTAMA: "Recording utama",
    TAG_PERBAIKAN: "Recording perbaikan",
    TAG_PEMBATALAN: "Pembatalan oleh customer",
    TAG_DITUNDA: "Ditunda oleh customer",
    TAG_TIDAK_TERHUBUNG: "Tidak terhubung",
    TAG_LAINNYA: "Lainnya",
}

# Batas panjang satu transkrip yang dikirim, dalam karakter. Sengaja dipasang jauh di
# atas transkrip terpanjang yang pernah ada (33 ribu karakter / 41 menit) sehingga pada
# praktiknya TIDAK ADA yang terpangkas — ini rem darurat untuk berkas yang tidak wajar,
# bukan penghematan.
#
# Versi pertama mengirim 3.000 karakter awal + 2.500 karakter akhir saja, dengan alasan
# jenis rekaman terbaca dari pembuka dan penutup. Alasan itu SALAH dan terbukti di
# playground: pada ``030808fLO1_20260710160335`` nasabah membatalkan di TENGAH
# percakapan ("saya nggak jadi dah", "berarti kita batal") lalu percakapannya berlanjut
# panjang soal hal lain, sehingga potongan pembuka+penutup justru membuang satu-satunya
# bukti pembatalan — labelnya berubah-ubah antar percobaan. Transkrip utuh membuatnya
# konsisten. Biayanya wajar: panggilan penilaian utama toh sudah mengirim seluruh
# transkrip tiket yang sama.
MAX_CHARS = 60000
SYSTEM_PROMPT = (
    "Anda adalah pemeriksa rekaman panggilan telemarketing Bank Mega. Anda menerima "
    "SELURUH rekaman (transkrip) milik SATU ticket id, urut kronologis, lalu memberi "
    "TEPAT SATU label jenis untuk setiap rekaman.\n\n"
    "Anda TIDAK menilai kepatuhan, tidak memberi skor, dan tidak mencari pelanggaran. "
    "Tugas Anda hanya menentukan PERAN tiap rekaman di dalam tiket ini.\n\n"
    "LABEL YANG BOLEH DIPAKAI (pilih tepat satu per rekaman, tulis persis seperti ini):\n"
    f"1. {TAG_UTAMA} — panggilan yang benar-benar MENGGERAKKAN pengajuan: data "
    "nasabah dikumpulkan atau dikonfirmasi, limit/tenor disepakati, legal statement "
    "atau final confirmation dibacakan, atau pengajuan diproses. Dalam satu tiket "
    f"PALING BANYAK ADA SATU {TAG_UTAMA}, yaitu rekaman PALING AWAL yang melakukan "
    "hal itu. Rekaman sah sesudahnya — termasuk yang membacakan legal statement atau "
    f"menuntaskan pengajuan — adalah {TAG_PERBAIKAN}, karena ia melanjutkan yang "
    "sudah dimulai.\n"
    f"2. {TAG_PERBAIKAN} — panggilan yang MELANJUTKAN, MENGULANG, atau MELENGKAPI "
    "rekaman lain pada tiket yang sama: membetulkan data yang keliru, membacakan ulang "
    "legal statement/final confirmation, atau meneruskan bagian yang tadi tertunda. "
    "Rekaman kedua dan seterusnya yang isinya sah hampir selalu masuk sini.\n"
    f"3. {TAG_PEMBATALAN} — pada panggilan itu nasabah MENYATAKAN TIDAK JADI, "
    "membatalkan, atau menolak melanjutkan **pengajuan MEGA CASHLINE-nya** "
    "(\"saya nggak jadi\", \"batal saja\", \"berarti kita batal\", \"kalau begitu "
    "tidak usah\"). BERLAKU walaupun panggilannya panjang dan sempat membahas pengajuan "
    "jauh, dan TIDAK perlu menunggu pembatalan yang resmi atau tertulis — pernyataan "
    "nasabah sudah cukup.\n"
    "   DUA HAL YANG BUKAN PEMBATALAN, dan keduanya sering tertukar:\n"
    "   - Menolak PRODUK TAMBAHAN — asuransi Mega Ultima Shield, proteksi, perlindungan "
    "— sementara pengajuan Mega Cashline-nya TETAP BERJALAN. Nasabah yang berkata "
    "\"asuransinya tidak usah\" lalu tetap melanjutkan ke data rekening, verifikasi, "
    "atau legal statement TIDAK membatalkan apa pun; panggilan itu tetap "
    f"{TAG_UTAMA} atau {TAG_PERBAIKAN}.\n"
    "   - Sekadar munculnya kata \"batal\": pertanyaan seperti \"kalau nanti tidak "
    "sesuai, apakah bisa dibatalkan?\" adalah pertanyaan, bukan keputusan.\n"
    f"4. {TAG_DITUNDA} — panggilan berakhir atas kemauan/kendala nasabah (sedang "
    "sibuk, sinyal buruk, ada urusan lain, minta ditelepon lagi) SEBELUM pengajuannya "
    "sempat maju. Penjelasan produk atau penawaran yang belum sampai pada pengumpulan "
    f"data, kesepakatan, atau konfirmasi termasuk di sini — BUKAN {TAG_UTAMA}.\n"
    f"5. {TAG_TIDAK_TERHUBUNG} — tidak tersambung, salah sambung, bukan orang yang "
    "dituju, atau terputus sebelum ada percakapan yang berarti.\n"
    f"6. {TAG_LAINNYA} — sungguh-sungguh tidak cocok dengan mana pun di atas. Jangan "
    "dipakai hanya karena ragu antara dua label; pilih yang paling mendekati.\n\n"
    "URUTAN MEMUTUSKAN — periksa dari atas:\n"
    "a. Apakah nasabah menyatakan tidak jadi/batal atas PENGAJUAN MEGA CASHLINE pada "
    f"panggilan ini? -> {TAG_PEMBATALAN}. (Menolak asuransi/produk tambahan saja: BUKAN.)\n"
    f"b. Apakah panggilan tidak pernah tersambung dengan orang yang dituju? -> {TAG_TIDAK_TERHUBUNG}.\n"
    "c. Apakah panggilan berakhir karena kendala nasabah SEBELUM pengajuannya maju "
    f"(hanya perkenalan/penawaran, lalu minta dihubungi lagi)? -> {TAG_DITUNDA}.\n"
    "d. Sisanya adalah rekaman yang sah: yang PALING AWAL menggerakkan pengajuan "
    f"-> {TAG_UTAMA}; semua yang sesudahnya -> {TAG_PERBAIKAN}.\n\n"
    "PEMBATALAN TETAP PEMBATALAN. Bila di dalam sebuah panggilan nasabah pernah "
    "menyatakan tidak jadi / batal / menolak melanjutkan PENGAJUAN MEGA CASHLINE-nya, "
    f"panggilan itu {TAG_PEMBATALAN} — walaupun pembicaraan masih berlanjut panjang "
    "sesudahnya, "
    "walaupun agent tetap mencoba membujuk, dan walaupun pada rekaman BERIKUTNYA "
    "pengajuannya dihidupkan kembali dan akhirnya berhasil. Yang dinilai adalah apa "
    "yang terjadi PADA panggilan itu, bukan nasib tiketnya secara keseluruhan. Sekali "
    "lagi: yang dibatalkan harus PENGAJUAN MEGA CASHLINE — penolakan asuransi tidak "
    "pernah cukup.\n\n"
    "CARA BERAKHIR TIDAK MENGALAHKAN ISI. Panggilan yang SUDAH mengumpulkan atau "
    "mengonfirmasi data pengajuan tetap sah walaupun di akhir nasabah terburu-buru, "
    "minta ditelepon lagi, atau menutup telepon di tengah pembacaan — itu bukan "
    f"{TAG_DITUNDA}. Sebaliknya, panggilan yang hanya sempat menjelaskan produk lalu "
    f"ditutup nasabah memang {TAG_DITUNDA} sekalipun berlangsung lama.\n\n"
    "Nilai tiap rekaman DALAM KONTEKS seluruh tiket dan urutan kronologisnya.\n\n"
    "WAKTU PENGAJUAN DISUBMIT (bila disebutkan di bawah) adalah jangkar yang kuat: "
    "pengajuan yang berhasil masuk sistem pasti dihasilkan oleh rekaman menjelang waktu "
    "itu. Pakai untuk memutuskan saat isi percakapan sendiri ambigu — mis. nasabah "
    "berdebat panjang soal kemungkinan membatalkan tanpa jelas apakah ia benar-benar "
    "membatalkan. Rekaman jauh SEBELUM waktu submit yang tidak berlanjut pada hari yang "
    "sama hampir selalu berakhir batal atau tertunda; rekaman pada hari submit adalah "
    "yang menggerakkan pengajuan sampai jadi. Ini PEMBANTU, bukan pengganti isi: "
    "rekaman pada hari submit yang isinya jelas-jelas pembatalan tetap pembatalan.\n\n"
    "OUTPUT: SATU objek JSON, tanpa teks lain, tanpa pagar kode, berbentuk:\n"
    "{\"recordings\": [{\"file\": \"<nama berkas persis seperti diberikan>\", "
    "\"tag\": \"<salah satu label di atas>\", \"reason\": \"<alasan singkat, "
    "maksimal 20 kata, bahasa Indonesia>\"}]}\n"
    "Wajib ada satu entri untuk SETIAP berkas yang diberikan, dengan nama berkas "
    "disalin persis."
)


def build_user_content(items, submit_time=None) -> str:
    """Pesan user: transkrip tiap rekaman, urut kronologis.

    ``submit_time`` (``tms_cashline.submit_time``, boleh kosong) ikut disebutkan sebagai
    jangkar. Tanpa itu model harus membedakan pembatalan sungguhan dari pengandaian
    hanya dari kata-kata, dan pada tiket ``030808fLO1`` itu terbukti tidak stabil: di
    rekaman 41 menit nasabah berdebat hipotetis soal prosedur bank ("kalau persetujuannya
    tidak sesuai... berarti kita nggak match ya, batal"), dan label rekaman itu
    berubah-ubah antar percobaan. Waktu submit menyelesaikannya tanpa menebak: pengajuan
    yang masuk sistem pasti dihasilkan rekaman menjelang waktu itu.

    ``items`` = ``[{"file", "duration", "text"}, ...]``. Transkrip yang lebih panjang
    dari ``MAX_CHARS`` dipotong di tengah dan diberi penanda, supaya model tahu ada
    bagian yang tidak diperlihatkan dan tidak menyimpulkan panggilan berakhir mendadak.
    """
    blocks = []
    for i, item in enumerate(items, start=1):
        text = (item.get("text") or "").strip()
        if len(text) > MAX_CHARS:
            half = MAX_CHARS // 2
            omitted = len(text) - MAX_CHARS
            body = (
                text[:half]
                + f"\n\n[... {omitted} karakter bagian tengah tidak ditampilkan ...]\n\n"
                + text[-half:]
            )
        else:
            body = text or "(transkrip kosong)"
        durasi = item.get("duration") or "tidak diketahui"
        blocks.append(
            f"=== Rekaman ke-{i} (file: {item.get('file')}, durasi: {durasi}) ===\n{body}"
        )
    kepala = f"Ticket ini punya {len(items)} rekaman, urut dari yang paling awal."
    if str(submit_time or "").strip():
        kepala += (f"\nPengajuan tiket ini tercatat DISUBMIT ke sistem pada "
                   f"{str(submit_time).strip()}.")
    return (
        f"{kepala}\n\n"
        + "\n\n".join(blocks)
        + "\n\nBeri label untuk setiap rekaman di atas."
    )
