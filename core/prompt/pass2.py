"""Tambahan prompt untuk TAHAP 2 — penilaian ulang di rekaman perbaikan (Fase #3).

Tahap 2 memakai **prompt campaign yang sama persis**, hanya ditambahi teks di bawah.
Itu keputusan yang disengaja: seluruh aturan penilaian (definisi segmen Final
Konfirmasi, syarat sebuah blok disebut recap, urutan pemilihan evidence, larangan
mengambil evidence dari penutup, dst) sudah tertulis rapi di prompt campaign sepanjang
182 ribu karakter dan sudah teruji. Menulis prompt tahap 2 dari nol berarti menyalin
aturan itu dan menunggu keduanya menyimpang diam-diam pada revisi berikutnya.

Yang ditambahkan hanya dua hal yang memang tidak bisa diketahui prompt campaign:

1. **Konteksnya**: transkrip yang diberikan adalah REKAMAN PERBAIKAN dari tiket yang
   sama, bukan tiket baru — supaya model tidak menilai item yang memang bukan urusan
   rekaman itu sebagai kegagalan agent.
2. **``evidence_source``**: penanda apakah sebuah item terpenuhi lewat penjelasan
   sungguhan atau hanya karena terbaca di dalam recap Final Konfirmasi. Tanpa penanda
   ini hasil akhirnya identik dengan perilaku lama dan tidak ada yang bisa membedakan
   "benar-benar diperbaiki" dari "kebetulan terbaca di recap" — padahal justru itu isi
   Fase #3 (keputusan bisnis 5 September 2026).

FALLBACK FINAL KONFIRMASI TIDAK DICABUT. Prompt campaign menyatakannya permanen
(keputusan bisnis 28 Agustus 2026, pencabutan pernah diusulkan dan ditolak: 344 item
"Penjelasan" pada 98 tiket bergantung padanya). Tahap 2 hanya MEMINDAHKAN posisinya —
di rekaman perbaikan, penjelasan sungguhan di luar recap didahulukan, dan recap tetap
menyelamatkan item yang tidak dijelaskan di mana pun, hanya saja kini tertandai.
"""

# Nilai yang boleh diisi model pada ``evidence_source``.
SOURCE_PENJELASAN = "penjelasan"
SOURCE_FALLBACK = "fallback_final_konfirmasi"


def build_pass2_prompt(campaign_prompt: str, item_codes, requirements, evidence_files) -> str:
    """Prompt campaign + instruksi tahap 2 untuk ``item_codes`` yang perlu diperiksa.

    ``requirements`` = ``{item_code: requirement}`` untuk ditulis apa adanya di daftar,
    supaya model tidak perlu mencarinya sendiri di scorecard yang panjang.
    ``evidence_files`` = nama berkas rekaman perbaikan — SATU-SATUNYA sumber evidence
    yang sah di tahap ini.

    KENAPA TRANSKRIPNYA UTUH TAPI EVIDENCE-NYA DIBATASI. Percobaan pertama hanya
    mengirim rekaman perbaikan, dan seluruh kategori "Final Konfirmasi Mega Cashline"
    (8 item) jatuh BELUM_SESUAI pada tiket ``030808fLO1`` — padahal rekaman itu memuat
    recap yang memenuhi syarat, dan evaluasi transkrip-digabung menerimanya. Sebabnya
    struktural: kategori itu menilai keberadaan sebuah SEGMEN di dalam alur panggilan,
    bukan sekadar apakah sebuah kalimat terucap. Pada tiket itu recap dan legal
    statement menyatu dalam satu blok ("Erina lanjutkan untuk legal pengajuan Mega
    Cashline dengan detail sebagai berikut... Ulangi kembali untuk dananya..."), dan KB
    menyatakan legal statement BUKAN recap — jadi tanpa pembanding, model membacanya
    sebagai legal statement lalu menyimpulkan tidak ada recap sama sekali. Jaring
    pengaman KB untuk kasus ini ("bila BANYAK item kategori ini hendak dinilai
    BELUM_SESUAI, periksa ulang panggilan yang LEBIH AWAL") juga mustahil dijalankan
    tanpa panggilan yang lebih awal.

    Karena itu tahap 2 melihat SELURUH rekaman valid tiket, tetapi hanya boleh
    mengambil evidence dari rekaman perbaikan. Batas itu **ditegakkan di kode**
    (``compliance.two_pass.merge_pass2`` menolak rescue yang evidence-nya menunjuk
    berkas lain), bukan digantungkan pada kepatuhan model terhadap instruksi.
    """
    daftar = "\n".join(
        f"  - {code}: {requirements.get(code, '(requirement tidak diketahui)')}"
        for code in item_codes
    )
    berkas = "\n".join(f"  - {f}" for f in evidence_files)
    return f"""{campaign_prompt}

================================================================================
TAHAP 2 — REKAMAN PERBAIKAN (berlaku HANYA untuk permintaan ini)
================================================================================

Transkrip yang Anda terima adalah SELURUH rekaman valid satu tiket, urut kronologis.
Rekaman TERAKHIR di bawah ini adalah **REKAMAN PERBAIKAN** — panggilan susulan yang
dibuat agent untuk melengkapi apa yang kurang pada panggilan sebelumnya:

{berkas}

Rekaman utamanya sudah dinilai pada permintaan terpisah. Tugas Anda sekarang: memeriksa
apakah item berikut — yang BELUM terpenuhi di rekaman utama, atau yang WAJIB diulang di
rekaman perbaikan — terpenuhi **DI DALAM rekaman perbaikan** itu:

{daftar}

ATURAN TAHAP 2:

1. **Evidence WAJIB berasal dari rekaman perbaikan yang disebut di atas.** Rekaman yang
   lebih awal diberikan sebagai KONTEKS — supaya Anda bisa menilai alur percakapan,
   mengenali segmen mana yang berperan sebagai Final Konfirmasi, dan tahu apa yang
   sudah/belum disampaikan — tetapi kutipan dari sana TIDAK SAH sebagai evidence dan
   akan ditolak.
2. Seluruh aturan penilaian di atas berlaku apa adanya, termasuk definisi segmen Final
   Konfirmasi dan syarat sebuah blok disebut recap. Ingat: sebuah blok yang mengulang
   syarat komersial TETAP recap walaupun agent membukanya dengan kata "legal" — periksa
   isinya, bukan kata pembukanya.
3. Item di LUAR daftar itu tetap Anda keluarkan sesuai format (bentuk keluarannya tidak
   berubah), tetapi hasilnya TIDAK dipakai.
4. **Keluarkan SELURUH baris `cashline_data_verification`** — satu baris untuk SETIAP
   field acuan TMS, tanpa ada yang dilewatkan. Baris yang hilang membuat verifikasi
   field itu tertinggal pada penilaian rekaman utama, sehingga tabel Error Code bisa
   menerbitkan kesalahan untuk data yang justru sudah dikonfirmasi di rekaman perbaikan.
5. **WAJIB: tambahkan field `evidence_source` pada SETIAP baris `scorecard_result`,**
   berisi tepat salah satu dari dua nilai berikut:
   - `"{SOURCE_PENJELASAN}"` — evidence berada DI LUAR segmen Final Konfirmasi, yaitu
     agent benar-benar menyampaikan/menjelaskan hal itu kepada nasabah;
   - `"{SOURCE_FALLBACK}"` — evidence hanya ditemukan DI DALAM segmen Final Konfirmasi
     (recap penutup), jadi item itu terpenuhi lewat fallback, bukan lewat penjelasan.
   Untuk item yang statusnya bukan SESUAI, isi `evidence_source` dengan `null`.
   Aturan pemilihan evidence-nya TIDAK berubah — yang di luar recap tetap didahulukan,
   dan recap tetap menjadi fallback. Penanda ini hanya mencatat mana yang terjadi.
"""
