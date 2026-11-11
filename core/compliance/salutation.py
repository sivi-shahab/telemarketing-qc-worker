"""Peringatan sapaan di pembuka panggilan (10 November 2026, tiket 030202vlpW).

Agent membuka dengan "selamat siang Ibu Yemi" lalu detik berikutnya beralih ke "Pak"
dan "Bapak" sampai akhir, padahal nasabahnya (TMS: YERMI ADITAMA, pria, data
verifikasi cocok) memang laki-laki. Sapaan pembuka yang salah itu kesalahan agent,
tetapi BUKAN item scorecard dan atas keputusan user TIDAK mengurangi skor maupun
mengubah status: hanya diperingatkan di dashboard supaya QC bisa meninjau.

Sumbernya field top-level ``salutation_check`` yang diisi LLM (prompt v86, bagian
STRUCTURED DATA EXTRACTION nomor 6). Modul ini hanya MEMBACA field itu; tidak ada
fungsi di sini yang menyentuh ``scorecard_result`` atau skor.
"""

# Satu-satunya nilai status yang memunculkan peringatan. NOT_STATED / OK / kosong
# (termasuk tiket lama tanpa field ini) tidak menghasilkan apa pun.
STATUS_BEDA = "MISMATCH"


def salutation_warnings(evaluation) -> list:
    """Daftar kalimat peringatan sapaan untuk satu evaluasi; kosong bila tidak ada."""
    check = (evaluation or {}).get("salutation_check") if isinstance(evaluation, dict) else None
    if not isinstance(check, dict):
        return []
    if str(check.get("status") or "").strip().upper() != STATUS_BEDA:
        return []
    awal = str(check.get("opening_salutation") or "").strip()
    berikut = str(check.get("later_salutation") or "").strip()
    ev = check.get("evidence") if isinstance(check.get("evidence"), dict) else {}
    ts = str(ev.get("timestamp") or "").strip()
    if awal and berikut:
        teks = f'Sapaan pembuka "{awal}" berbeda dengan sapaan selanjutnya "{berikut}"'
    else:
        teks = "Sapaan pembuka berbeda dengan sapaan selanjutnya"
    if ts:
        teks += f" ({ts})"
    return [teks + ". Tidak mengurangi skor."]
