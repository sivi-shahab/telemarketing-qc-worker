"""Normalisasi ucapan -> nilai terstruktur yang DIPAKAI BERSAMA oleh Cashline dan NTB.

Aturan yang bersinggungan antar campaign diletakkan di sini supaya tidak bercabang:
satu perubahan (mis. feedback QC Bank Mega 12 Oktober 2026 — email yang dieja) langsung
berlaku di kedua campaign.
"""
import re

_DIGIT_WORDS = {
    "nol": "0", "kosong": "0", "zero": "0", "satu": "1", "dua": "2", "tiga": "3",
    "empat": "4", "lima": "5", "enam": "6", "tujuh": "7", "delapan": "8", "sembilan": "9",
}
_REPEAT_WORDS = {"double": 2, "dobel": 2, "triple": 3, "tripel": 3}


def normalize_email(value) -> str:
    """Email dari ucapan/TMS dinormalkan jadi satu alamat huruf kecil tanpa spasi.

    "Hidayat Rizki75 at gmail dot com"          -> "hidayatrizki75@gmail.com"
    "DHI LAH titik AS tujuh at gmail dot com"   -> "dhilah.as7@gmail.com"
    "erdiwansya lima at gmail dot com"          -> "erdiwansya5@gmail.com"
    "kentung r-i-i z-a ky at gmail dot com"     -> "kentungriizaky@gmail.com"

    Hanya FORMAT yang diubah: simbol (at/dot/titik/underscore/strip), kata angka -> digit,
    "double/triple X", huruf yang dieja dengan tanda hubung, dan spasi. Isi tidak dikoreksi.
    """
    s = str(value or "").casefold().strip()
    s = re.sub(r"\s+(?:at|\(at\)|\[at\]|et|keong)\s+", "@", s)
    s = re.sub(r"\s*(?:dot|titik|\(dot\)|\[dot\])\s*", ".", s)
    s = re.sub(r"\s*(?:underscore|garis bawah|under score)\s*", "_", s)
    s = re.sub(r"\s*(?:dash|strip|minus|garis tengah)\s*", "-", s)
    # huruf/angka tunggal yang dieja dengan tanda hubung: "r-i-i" -> "rii"
    s = re.sub(r"(?<![a-z0-9])(?:[a-z0-9]-)+[a-z0-9](?![a-z0-9])", lambda m: m.group(0).replace("-", ""), s)
    out, repeat = [], 1
    for tok in re.findall(r"[a-z0-9]+|[^a-z0-9\s]", s):
        if tok in _REPEAT_WORDS:
            repeat = _REPEAT_WORDS[tok]
            continue
        out.append(_DIGIT_WORDS.get(tok, tok) * repeat)
        repeat = 1
    return "".join(out)
