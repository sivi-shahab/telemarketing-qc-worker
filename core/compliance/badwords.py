"""Badword agent — ucapan agent yang membawa SENTIMEN NEGATIF kepada nasabah.

Latar belakang (data komplain Bank Mega, "Data Komplain penawaran tidak sopan.xlsx",
13 Agustus 2026): komplain nasabah bersumber dari CCBM maupun temuan QC sendiri, dan
yang dikeluhkan hampir tidak pernah kata kasar baku. Yang membuat nasabah komplain
adalah kalimat agent yang MENYINGGUNG — merendahkan, menyindir, menyalahkan, atau
menggerutu tentang nasabah. Contoh nyata dari file itu:

    "gembel"                                        (REV22311985)
    "ikh bapaknya blo'on deh ini"                   (Other/QC, Megapay)
    "yah ibunya padahal bunganya kecil loh"          (REV17026289)
    "tidak niat lo namanya"                          (REV19805640)
    "ulang-ulang terus"                              (REV17307793)
    "ibu keberatan kita sudah berikan solusi tapi kalo masih ngeluh juga ya bingung
     juga tuh"                                       (REV21368535)

Karena penentuannya soal SENTIMEN dan bukan pencocokan daftar kata, deteksinya
dikerjakan LLM (prompt v53, blok ``badword_check``) — modul ini hanya membaca,
menormalkan, dan menjadikannya vonis:

  - ``badword_rows``   : baris siap tampil untuk tabel "Badword Summary"
                         (Ticket ID / Evidence / Reason) di dropdown Results.
  - ``has_badword``    : ada minimal satu temuan berbukti -> AI Status Not Qualified.
  - ``BADWORD_REASON`` : teks komentar yang mendampingi status itu.

Hasil evaluasi LAMA (sebelum prompt v53) tidak punya blok ``badword_check`` sama
sekali; semua helper di sini mengembalikan kosong/False untuk hasil seperti itu,
jadi status tiket lama tidak berubah.
"""

# Komentar yang menemani AI Status = Not Qualified karena badword — satu-satunya
# sebab yang masih ditulis di kolom AI Status. Sebab yang TIDAK terbaca dari skor
# perlu tertulis di sana supaya QC tahu alasannya tanpa membuka baris. (Kegagalan
# konsistensi verifikasi statik dulu punya komentar serupa; sejak 21 Agustus 2026
# komentarnya dihapus — aturannya tetap, sebabnya dibaca dari kolom Critical Failure.)
BADWORD_REASON = "Terindikasi Badword"


def _clean(value) -> str:
    """Trim a value to a display string; non-strings become ''."""
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    return str(value).strip()


def _first(*values) -> str:
    """First non-empty cleaned value."""
    for v in values:
        text = _clean(v)
        if text:
            return text
    return ""


def _findings(evaluation: dict) -> list:
    """Raw findings list from ``evaluation.badword_check`` (tolerant to shape).

    Menerima ``badword_check`` berupa dict ({status, findings}) maupun langsung
    berupa list temuan — output LLM tidak selalu setia pada skema."""
    block = (evaluation or {}).get("badword_check")
    if isinstance(block, list):
        return [f for f in block if isinstance(f, dict)]
    if not isinstance(block, dict):
        return []
    for key in ("findings", "items", "checked_items"):
        found = block.get(key)
        if isinstance(found, list):
            return [f for f in found if isinstance(f, dict)]
    return []


def badword_rows(evaluation: dict) -> list:
    """Temuan badword sebagai baris tabel, urut sesuai output LLM.

    Tiap baris: ``ticket_id`` (nama PDF panggilan), ``timestamp``, ``quote``,
    ``evidence`` (gabungan timestamp + kutipan, untuk pemakai yang butuh satu teks),
    dan ``reason``.

    Sebuah temuan HARUS membawa kutipan transkrip. Temuan tanpa kutipan dibuang:
    tabel ini menuduh seorang agent berucap tidak pantas dan vonisnya mengunci AI
    Status — tuduhan tanpa bukti yang bisa dibaca QC tidak boleh berdiri. Baris
    kembar (timestamp + kutipan sama, mis. temuan yang sama dilaporkan dua kali)
    dilipat jadi satu."""
    rows = []
    seen = set()
    for f in _findings(evaluation):
        ev = f.get("evidence")
        ev = ev if isinstance(ev, dict) else {}
        quote = _first(ev.get("quote"), ev.get("text"), f.get("quote"), f.get("text"))
        if not quote:
            continue
        timestamp = _first(ev.get("timestamp"), f.get("timestamp"))
        ticket_id = _first(ev.get("ticket_id"), f.get("ticket_id"))
        reason = _first(f.get("reason"), f.get("category"), ev.get("reason"))
        key = (timestamp, quote)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "ticket_id": ticket_id,
                "timestamp": timestamp,
                "quote": quote,
                "evidence": f"{timestamp} — {quote}" if timestamp else quote,
                "reason": reason,
            }
        )
    return rows


def has_badword(evaluation: dict) -> bool:
    """True bila ada temuan badword berbukti — memaksa AI Status FAIL.

    Sengaja dihitung dari ``badword_rows`` dan BUKAN dari ``badword_check.status``:
    status "FAIL" tanpa satu pun kutipan adalah vonis tanpa bukti."""
    return bool(badword_rows(evaluation))


def badword_fail_reason(evaluation: dict) -> "str | None":
    """Komentar AI Status untuk tiket yang gugur karena badword, atau None."""
    rows = badword_rows(evaluation)
    if not rows:
        return None
    return f"{BADWORD_REASON} — {len(rows)} ucapan bersentimen negatif kepada nasabah"
