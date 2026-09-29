"""Generator deck "Error Rate Update" (.pptx) — menu Stats > Generate PPT.

Meniru STRUKTUR deck bulanan yang selama ini dibuat manual di Canva
(``docs/csv_bank/25 September 2025/PPT Error Rate Telemarketing as of Agustus
2026.pdf``): Trend Error Rate -> Error Rate Area Manager/SPV -> Top 10 TLO ->
Return Reason Details per campaign. Section "3. Complaint" PDF acuan DIHAPUS
(28 September 2026, atas permintaan user) — datanya tidak ada di dashboard.

Styling MENIRU palet & font asli PDF acuan — diukur langsung dari pixel/font
embed-nya: kuning ``#FFDE59``, navy ``#144778``, header tabel ``#0070C0``,
baris Total ``#00B0F0``, legend Risk Rate hijau/kuning/merah
(``#92D050``/``#FFFF00``/``#FF0000``), font tabel "Montserrat", plus logo
Bank Mega asli (``core/compliance/assets/``).

Font JUDUL (28 September 2026, dikoreksi): PDF acuan sebenarnya memakai
"Handpicked Seashells" untuk semua judul besar — dicek langsung lewat
``pdftohtml -xml`` yang memetakan tiap potongan teks ke font aslinya, bukan
cuma menebak dari daftar font ter-embed. "Paytone One" (dipakai versi
sebelumnya) ternyata cuma font body kecil di 3 slide Top 10 TLO, BUKAN font
judul — itu koreksi dari asumsi awal yang salah. "Handpicked Seashells" tidak
di-embed di sini: subset yang berhasil diekstrak dari PDF (union dari 19
kemunculannya, lihat riwayat ekstraksi) cuma berisi 45 glyph (huruf yang
kebetulan kepakai di PDF-nya) — jauh dari cukup untuk judul dinamis (nama
campaign/AM/SPV/agent yang berubah tiap bulan), jadi men-embed itu apa
adanya justru BERISIKO menampilkan kotak kosong utk huruf yang hilang. Nama
font tetap diset benar (kalau organisasi someday punya lisensi fontnya
ter-install, otomatis kepakai); kalau tidak ada, PowerPoint fallback ke font
default yang tetap terbaca — jauh lebih aman daripada glyph hilang.

Background (28 September 2026, upgrade dari versi tekstur+vector generik ke
TEMPLATE ASLI): keempat slide "kuning" tanpa tabel — cover, divider "1. Trend
Error Rate", divider "2. Return Reason Details", dan penutup — memakai RASTER
PENUH halaman aslinya dari PDF acuan (``tpl_cover.jpg``,
``tpl_divider_trend.jpg``, ``tpl_divider_return.jpg``, ``tpl_thankyou.jpg``;
lihat ``_add_full_bg``), bukan direka ulang — jadi kertas sobek, selotip
washi, dan seluruh ikon coretan tangan (koin, folder, bohlam, kalkulator,
clipboard+gembok, target+panah) SAMA PERSIS dengan aslinya, termasuk yang
sebelumnya tidak bisa ditiru sebagai vektor. Tiga dari empat (divider
Trend/Return, penutup) tekstnya statis tiap bulan sehingga dipakai APA
ADANYA tanpa teks tambahan di atasnya (termasuk penutup: tetap "THANK YOU"
bahasa Inggris, bukan "Terima Kasih", supaya benar-benar sama persis seperti
diminta). Cover satu-satunya yang mengandung teks tanggal yang berubah tiap
bulan — untuk itu teks tanggal ASLI di-hapus dari raster (ditutup pakai warna
kuning yang di-sample langsung dari pixel di sebelahnya, lihat riwayat ekstraksi)
lalu diganti teks dinamis kita sendiri di posisi yang sama.

Modul ini murni PRESENTASI: seluruh data sudah dirakit pemanggil
(``api/routers/stats.py``) dari fungsi agregasi di
``core/compliance/stats_aggregate.py``.

Kolom/section yang datanya TIDAK ADA di dashboard (28 September 2026, atas
permintaan user): Sampling (%), %KPI Bln Lalu/Bln Ini, kolom "Evaluated" di
tabel Top 10 TLO Terburuk per campaign, section 3 "Complaint", dan baris
placeholder campaign Credit Shield/Personal Loan — SEMUA dihapus dari deck
(bukan ditampilkan abu-abu "isi manual" seperti versi sebelumnya). Kalau
datanya tidak ada di sistem QC ini, kolom/section itu tidak perlu tampil sama
sekali. ``PLACEHOLDER`` di bawah masih dipakai murni untuk nilai yang
memang ``None`` (mis. error rate campaign tanpa submission), bukan lagi
untuk kolom yang strukturnya tidak pernah terisi.
"""
from __future__ import annotations

import io
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

PLACEHOLDER = "— (isi manual)"

# --- Palet, font & aset — diukur dari PDF acuan (lihat docstring modul) ----
_YELLOW = RGBColor(0xFF, 0xDE, 0x59)      # background cover/section, aksen
_NAVY = RGBColor(0x14, 0x47, 0x78)        # judul
_HEADER_BG = RGBColor(0x00, 0x70, 0xC0)   # header tabel
_TOTAL_BG = RGBColor(0x00, 0xB0, 0xF0)    # baris Total/Grand Total
_GREEN = RGBColor(0x92, 0xD0, 0x50)       # Risk Rate 0%-3%
_RATE_YELLOW = RGBColor(0xFF, 0xFF, 0x00) # Risk Rate 3%-6%
_RED = RGBColor(0xFF, 0x00, 0x00)         # Risk Rate >6%
_GRAY_FG = RGBColor(0x75, 0x75, 0x75)
_WHITE = RGBColor(0xFF, 0xFF, 0xFF)
_BLACK = RGBColor(0x1A, 0x1A, 0x1A)
_ORANGE = RGBColor(0xE6, 0x58, 0x00)  # aksen "Update" di cover PDF acuan

_TITLE_FONT = "Handpicked Seashells"  # lihat docstring modul — dikoreksi 28 Sep 2026
_BODY_FONT = "Montserrat"

_SLIDE_W = Inches(13.333)
_SLIDE_H = Inches(7.5)
_MARGIN = Inches(0.4)

_ASSET_DIR = Path(__file__).resolve().parent / "assets"
_LOGO_PATH = _ASSET_DIR / "bank-mega-logo.png"
_LOGO_ASPECT = 1200 / 701  # width/height asli bank-mega-logo.png

# Template raster full-slide — diekstrak langsung dari halaman PDF acuan
# (rasio 20x11.25in = 16:9, sama dengan slide, jadi full-bleed tanpa crop).
# Lihat docstring modul untuk detail per file.
_TPL_COVER = _ASSET_DIR / "tpl_cover.jpg"
_TPL_DIVIDER_TREND = _ASSET_DIR / "tpl_divider_trend.jpg"
_TPL_DIVIDER_RETURN = _ASSET_DIR / "tpl_divider_return.jpg"
_TPL_THANKYOU = _ASSET_DIR / "tpl_thankyou.jpg"

# Background tabel "Error Rate ..." (28 September 2026) — sama seperti di
# atas, raster ASLI dari halaman PDF acuan (bukan bikinan sendiri), tapi
# dengan tabel/paragraf lama di-hapus (ditutup warna kuning yang di-sample
# dari pixel sebelahnya) supaya bisa dipakai ulang dengan data bulan
# berjalan. ``_TPL_AM``/``_TPL_SPV`` sudah punya judul & ikon (bohlam/
# kalkulator) ter-bakar di raster — lihat ``_bg_table_slide``, TIDAK
# menggambar judul teks lagi di atasnya (dobel kalau iya). ``_TPL_TREND``
# TIDAK punya judul ter-bakar (halaman tabel Trend di PDF acuan memang
# polos, judulnya cuma ada di slide divider sebelumnya) jadi judulnya masih
# kita gambar sendiri seperti sebelumnya. ``_TPL_TOPTLO`` per bucket masa
# kerja (sama seperti page 7/8/9 acuan) — paragraf "TOP 3 return reason"
# aslinya JUGA dihapus (bukan cuma tabelnya): angka & alasannya spesifik
# bulan Agustus 2026, sistem QC ini tidak punya data itu, jadi menyisakannya
# akan menyesatkan (data basi ditampilkan seolah data bulan berjalan).
_TPL_TREND = _ASSET_DIR / "bg_trend.jpg"
_TPL_AM = _ASSET_DIR / "bg_am.jpg"
_TPL_SPV = _ASSET_DIR / "bg_spv.jpg"
_TPL_TOPTLO = {
    "0-6 Bulan": _ASSET_DIR / "bg_toptlo_0_6.jpg",
    "6-12 Bulan": _ASSET_DIR / "bg_toptlo_6_12.jpg",
    "> 12 Bulan": _ASSET_DIR / "bg_toptlo_gt12.jpg",
}

# Background "Detail Error Reason" / "Top 10 TLO Terburuk" per campaign (28
# September 2026, diambil dari halaman 23 "Complaint" PDF acuan — versi awal
# salah pakai halaman "USAGE - LOC" yang bentuk sobekan kartunya jauh lebih
# tipis/nempel ke tepi, BUKAN yang dipakai slide konten tabel tunggal kayak
# gini). Tabel "Complaint" AM (JEFRI ANSYAH dkk) di halaman asli itu dihapus,
# menyisakan cangkang kartu kuning bersobekan besar di kiri/bawah + grid —
# dipakai ulang untuk kedua jenis slide ini karena satu-satunya konten tabel
# tunggal serupa di PDF acuan cuma ada di halaman itu; campaign kita jumlah/
# namanya beda dari 10 halaman "USAGE-X"/"Card-X" tetap acuan (MUS CC
# terpisah, dst.) jadi tidak bisa dipetakan 1:1 seperti ``_TPL_AM``/
# ``_TPL_SPV`` — lihat ``_table_slide``.
_TPL_DETAIL = _ASSET_DIR / "bg_detail.jpg"


def _truncate(text, limit) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fmt_pct(value) -> str:
    if value is None:
        return PLACEHOLDER
    return f"{value:.2f}".replace(".", ",") + "%"


def _fmt_int(value) -> str:
    if value is None:
        return PLACEHOLDER
    return f"{int(value):,}".replace(",", ".")


def _rate_fill(pct) -> "RGBColor | None":
    if pct is None:
        return None
    if pct > 6:
        return _RED
    if pct >= 3:
        return _RATE_YELLOW
    return _GREEN


def _new_deck() -> Presentation:
    prs = Presentation()
    prs.slide_width = _SLIDE_W
    prs.slide_height = _SLIDE_H
    return prs


def _blank_slide(prs: Presentation):
    return prs.slides.add_slide(prs.slide_layouts[6])


def _add_full_bg(slide, path):
    """Background raster PENUH satu halaman PDF acuan (rasio 16:9 = rasio
    slide, jadi full-bleed apa adanya tanpa crop/distorsi). Lihat docstring
    modul — dipakai untuk kelima slide "kuning" (cover/3 divider/penutup)."""
    if not path.exists():
        return None
    return slide.shapes.add_picture(str(path), 0, 0, width=_SLIDE_W, height=_SLIDE_H)


def _add_logo(slide, top=Inches(0.25), height=Inches(0.45)):
    """Logo Bank Mega asli, pojok kanan atas — muncul di SETIAP slide di PDF
    acuan. Aman dipakai di atas background apa pun (PNG transparan); untuk
    slide yang sudah pakai ``_add_full_bg`` logo aslinya sudah ikut ter-bakar
    di raster juga, jadi ini cuma menegaskan ulang di posisi yang sama."""
    if not _LOGO_PATH.exists():
        return None
    pic = slide.shapes.add_picture(str(_LOGO_PATH), 0, top, height=height)
    pic.left = _SLIDE_W - pic.width - Inches(0.35)
    return pic


def _add_textbox(slide, left, top, width, height, text, *, size=18, bold=False,
                  color=_BLACK, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP,
                  font=_BODY_FONT):
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    run.font.name = font
    return box


def _cover_slide(prs, period_label):
    """Cover — background = raster asli PDF acuan dengan teks tanggal lama
    DIHAPUS (lihat docstring modul), teks kita gambar ulang di posisi yang
    sama: "ERROR RATE" / "Update" (aksen oranye, mengganti font skrip
    HarlowStd asli yang tidak bisa diekstrak sebagai TrueType) / "AS OF
    {periode}"."""
    slide = _blank_slide(prs)
    _add_full_bg(slide, _TPL_COVER)  # logo Bank Mega sudah ikut ter-bakar di raster
    _add_textbox(slide, Inches(0.9), Inches(1.5), Inches(9.6), Inches(1.3),
                 "ERROR RATE", size=44, bold=True, color=_NAVY, font=_TITLE_FONT)
    _add_textbox(slide, Inches(0.9), Inches(3.0), Inches(9.6), Inches(0.9),
                 "Update", size=32, bold=True, color=_ORANGE, font=_TITLE_FONT)
    _add_textbox(slide, Inches(0.9), Inches(4.3), Inches(9.6), Inches(0.7),
                 f"AS OF {period_label.upper()}", size=24, bold=True, color=_NAVY, font=_TITLE_FONT)
    return slide


def _divider_slide(prs, template_path):
    """Divider section — background = raster asli PDF acuan apa adanya. Kedua
    divider yang tersisa (Trend Error Rate/Return Reason Details) teksnya
    statis tiap bulan jadi dipakai langsung dari raster, tanpa teks tambahan
    di atasnya."""
    slide = _blank_slide(prs)
    _add_full_bg(slide, template_path)  # logo Bank Mega sudah ikut ter-bakar di raster
    return slide


def _closing_slide(prs):
    """Penutup — raster asli apa adanya ("THANK YOU", bahasa Inggris seperti
    PDF acuan) — sengaja TIDAK diterjemahkan ke "Terima Kasih" supaya benar-
    benar sama persis seperti diminta, bukan pilihan bahasa kita sendiri."""
    slide = _blank_slide(prs)
    _add_full_bg(slide, _TPL_THANKYOU)  # logo Bank Mega sudah ikut ter-bakar di raster
    return slide


def _table_slide(prs, title, subtitle=None, *, bg_path=None):
    """``bg_path`` (28 September 2026) — background raster ASLI PDF acuan
    (lihat ``_TPL_DETAIL``) untuk slide "Detail Error Reason"/"Top 10 TLO
    Terburuk" per campaign, dipakai sebagai pengganti kanvas putih polos.
    Judul & subtitle campaign-nya SENDIRI TIDAK ter-bakar di raster ini (beda
    dari ``_TPL_AM``/``_TPL_SPV``) — halaman aslinya cuma satu per campaign
    tetap (Cashline, LOC, dst.), sedangkan campaign kita bisa berbeda jumlah/
    nama (MUS CC terpisah, dst.), jadi judul & subtitle masih digambar
    sendiri di sini seperti biasa."""
    slide = _blank_slide(prs)
    if bg_path is not None:
        _add_full_bg(slide, bg_path)  # logo Bank Mega sudah ikut ter-bakar di raster
    else:
        _add_logo(slide)
    # Judul & subtitle di-tengah (28 September 2026, atas permintaan) — cuma
    # untuk slide berbackground (Detail Error Reason/Top 10 TLO Terburuk,
    # satu-satunya pemakai ``bg_path`` saat ini); tanpa background tetap rata
    # kiri seperti semula.
    text_align = PP_ALIGN.CENTER if bg_path is not None else PP_ALIGN.LEFT
    title_left = _MARGIN if bg_path is None else Inches(0)
    title_width = Inches(10.5) if bg_path is None else _SLIDE_W
    _add_textbox(slide, title_left, Inches(0.2), title_width, Inches(0.55),
                 title, size=22, bold=True, color=_NAVY, font=_TITLE_FONT, align=text_align)
    top = Inches(0.95)
    if subtitle:
        sub_color = _NAVY if bg_path is not None else _GRAY_FG
        sub_left = _MARGIN if bg_path is None else Inches(0)
        sub_width = Inches(12.5) if bg_path is None else _SLIDE_W
        _add_textbox(slide, sub_left, Inches(0.85), sub_width, Inches(0.4),
                     subtitle, size=12, bold=bg_path is not None, color=sub_color, align=text_align)
        top = Inches(1.25)
    if bg_path is None:
        # Garis aksen kuning tipis di bawah judul — jejak warna brand tanpa
        # menirukan bentuk sobekan kertas Canva. Dilewati kalau sudah pakai
        # background kuning asli (rule kuning tidak akan kelihatan di atasnya).
        rule = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, _MARGIN, Inches(0.78), Inches(1.6), Pt(4))
        rule.fill.solid()
        rule.fill.fore_color.rgb = _YELLOW
        rule.line.fill.background()
        rule.shadow.inherit = False
    return slide, top


def _bg_table_slide(prs, bg_path, content_top, *, subtitle=None):
    """Slide tabel dengan background ASLI PDF acuan (lihat ``_TPL_TREND``/
    ``_TPL_AM``/``_TPL_SPV``/``_TPL_TOPTLO``) — bukan latar putih/kuning polos
    bikinan sendiri seperti ``_table_slide``. Judulnya TIDAK digambar di sini:
    AM/SPV/TopTLO judulnya sudah ter-bakar di raster; Trend (satu-satunya yang
    belum punya judul ter-bakar) menggambar judulnya sendiri secara khusus di
    ``_build_trend_slides`` karena harus menghindari ikon pesawat kertas di
    pojok kiri atas. ``subtitle`` (mis. "(lanjutan 2)") ditaruh tepat di atas
    ``content_top``, di dalam area yang sudah dikosongkan dari tabel/paragraf
    lama — lihat docstring modul."""
    slide = _blank_slide(prs)
    _add_full_bg(slide, bg_path)
    if subtitle:
        _add_textbox(slide, _MARGIN, content_top - Inches(0.38), Inches(12.5), Inches(0.35),
                     subtitle, size=12, bold=True, color=_NAVY)
    return slide, content_top


def _add_legend(slide, left, top, *, pill_w=Inches(1.35), pill_h=Inches(0.4), font_size=12):
    """Legend ambang Risk Rate (hijau/kuning/merah) — sekali saja, di slide
    Trend Error Rate pertama, sama seperti posisinya di PDF acuan.

    Ditaruh di baris judul (pojok kanan), BUKAN di bawah tabel: jumlah
    campaign aktif berubah-ubah (bisa 8, bisa 14+ kalau ada campaign baru),
    jadi tinggi tabel Trend tidak bisa dipastikan selalu sisa ruang di bawah
    tanpa numpuk sama baris terakhir/GRAND TOTAL — beda dengan posisi lain
    yang tabelnya dipaginasi ke slide baru (lihat ``_build_hierarchy_slides``)
    kalau kepanjangan, tabel Trend sengaja tidak dipaginasi (harus satu slide).
    """
    items = [("0% - 3%", _GREEN), ("3% - 6%", _RATE_YELLOW), ("> 6%", _RED)]
    x = left
    w, h = pill_w, pill_h
    for label, color in items:
        pill = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, top, w, h)
        pill.fill.solid()
        pill.fill.fore_color.rgb = color
        pill.line.fill.background()
        pill.shadow.inherit = False
        tf = pill.text_frame
        tf.word_wrap = False
        tf.margin_left = tf.margin_right = Emu(0)
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        run = p.add_run()
        run.text = label
        run.font.size = Pt(font_size)
        run.font.bold = True
        run.font.name = _BODY_FONT
        run.font.color.rgb = _BLACK
        x += w + Inches(0.15)


def _set_cell_border(cell, color=_BLACK, weight_pt=1.5):
    """python-pptx tidak punya API border sel tabel (masih harus tulis XML
    langsung) — border tabel bawaan mengikuti theme (abu-abu tipis), atas
    permintaan disamakan jadi HITAM solid di semua sisi, semua tabel."""
    tc_pr = cell._tc.get_or_add_tcPr()
    w = str(int(weight_pt * 12700))
    hex_color = "%02X%02X%02X" % (color[0], color[1], color[2])
    for i, tag in enumerate(("a:lnL", "a:lnR", "a:lnT", "a:lnB")):
        existing = tc_pr.find(qn(tag))
        if existing is not None:
            tc_pr.remove(existing)
        ln = tc_pr.makeelement(qn(tag), {"w": w, "cap": "flat", "cmpd": "sng", "algn": "ctr"})
        solid_fill = ln.makeelement(qn("a:solidFill"), {})
        solid_fill.append(solid_fill.makeelement(qn("a:srgbClr"), {"val": hex_color}))
        ln.append(solid_fill)
        ln.append(ln.makeelement(qn("a:prstDash"), {"val": "solid"}))
        tc_pr.insert(i, ln)


def _style_header_cell(cell, text):
    cell.text = text
    cell.fill.solid()
    cell.fill.fore_color.rgb = _HEADER_BG
    _set_cell_border(cell)
    cell.margin_left = cell.margin_right = Emu(45720)
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = cell.text_frame.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    run = p.runs[0]
    run.font.size = Pt(10)
    run.font.bold = True
    run.font.name = _BODY_FONT
    run.font.color.rgb = _WHITE


def _style_body_cell(cell, text, *, bold=False, fill=None, font_color=None,
                      align=PP_ALIGN.CENTER, italic=False):
    cell.text = text
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
    cell.margin_left = cell.margin_right = Emu(45720)
    if fill is not None:
        # Baris Total/Grand Total (_TOTAL_BG) atau sel kolom rate
        # (hijau/kuning/merah) — tetap solid, warnanya harus tegas.
        cell.fill.solid()
        cell.fill.fore_color.rgb = fill
    else:
        # Cell biasa & placeholder "isi manual" TIDAK diisi putih lagi —
        # transparan supaya ikut warna background slide di baliknya (kuning
        # untuk slide yang sudah pakai template asli PDF acuan), sama seperti
        # tabel Excel asli yang dipetakan (bodinya menyatu dengan kartu
        # kuning, bukan kotak putih terpisah).
        cell.fill.background()
    _set_cell_border(cell)
    p = cell.text_frame.paragraphs[0]
    p.alignment = align
    run = p.runs[0] if p.runs else p.add_run()
    run.font.size = Pt(9.5)
    run.font.bold = bold
    run.font.italic = italic
    run.font.name = _BODY_FONT
    run.font.color.rgb = font_color or (_GRAY_FG if text == PLACEHOLDER else _BLACK)


def _add_table(slide, left, top, width, height, headers, rows, *,
                col_widths=None, rate_cols=(), bold_rows=()):
    """Render satu tabel. ``rows`` = list of list-of-str (sudah diformat pemanggil).

    ``rate_cols`` = indeks kolom yang selnya diwarnai sesuai ambang Error Rate
    (hijau/kuning/merah, threshold sama dengan legend PPT acuan).
    """
    n_rows = len(rows) + 1
    n_cols = len(headers)
    shape = slide.shapes.add_table(n_rows, n_cols, left, top, width, height)
    table = shape.table
    if col_widths:
        for c, w in enumerate(col_widths):
            table.columns[c].width = w
    # python-pptx membagi ``height`` rata ke SEMUA baris saat tabel dibuat —
    # untuk tabel berbaris sedikit (mis. 1 TLO di satu bucket masa kerja) itu
    # membuat baris raksasa kosong. Baris tetap bisa tumbuh lebih tinggi dari
    # ini kalau isinya wrap ke banyak baris teks; ini cuma batas BAWAH yang
    # wajar, bukan tinggi tetap.
    table.rows[0].height = Inches(0.45)
    for r in range(1, n_rows):
        table.rows[r].height = Inches(0.4)
    for c, h in enumerate(headers):
        _style_header_cell(table.cell(0, c), h)
    for r, row in enumerate(rows):
        is_bold = r in bold_rows
        for c, val in enumerate(row):
            text = val if isinstance(val, str) else str(val)
            fill = _TOTAL_BG if is_bold else None
            if c in rate_cols and text not in (PLACEHOLDER, ""):
                try:
                    pct = float(text.replace("%", "").replace(",", "."))
                    fill = _rate_fill(pct)
                except ValueError:
                    pass
            _style_body_cell(table.cell(r + 1, c), text, bold=is_bold, fill=fill)
    return table


def _chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# --- Slide builders per section --------------------------------------------

def _build_trend_slides(prs, data):
    period_prev = data["period_previous"]["label"]
    period_curr = data["period_current"]["label"]
    headers = [
        "Campaign",
        f"Submission ({period_prev})", f"Error Rate ({period_prev})",
        f"Submission ({period_curr})",
        "H", "M", "L", f"Error Rate ({period_curr})",
    ]
    rows, bold_idx = [], set()
    for i, r in enumerate(data["trend_rows"]):
        if r.get("is_total"):
            bold_idx.add(len(rows))
        rows.append([
            r["label"],
            _fmt_int(r["prev"]["submission"]) if r["has_data"] else PLACEHOLDER,
            _fmt_pct(r["prev"]["error_rate"]) if r["has_data"] else PLACEHOLDER,
            _fmt_int(r["curr"]["submission"]) if r["has_data"] else PLACEHOLDER,
            _fmt_int(r["curr"]["h"]) if r["has_data"] else PLACEHOLDER,
            _fmt_int(r["curr"]["m"]) if r["has_data"] else PLACEHOLDER,
            _fmt_int(r["curr"]["l"]) if r["has_data"] else PLACEHOLDER,
            _fmt_pct(r["curr"]["error_rate"]) if r["has_data"] else PLACEHOLDER,
        ])
    # Halaman tabel Trend di PDF acuan polos (ikon pesawat kertas pojok kiri
    # atas, x kira-kira 0-3.3in / y 0-1.3in; tanpa judul ter-bakar — judul
    # "1. Trend Error Rate" cuma ada di slide divider sebelumnya), jadi judul
    # & legend masih kita gambar sendiri, TAPI digeser ke KANAN ikonnya
    # (bukan didorong ke bawah) supaya tabelnya tetap dapat tinggi penuh
    # seperti slide tabel lain — lihat docstring ``_TPL_TREND``.
    slide, top = _bg_table_slide(prs, _TPL_TREND, Inches(1.3))
    _add_textbox(slide, Inches(3.6), Inches(0.2), Inches(4.6), Inches(0.5),
                 "Trend Error Rate", size=22, bold=True, color=_NAVY, font=_TITLE_FONT)
    _add_legend(slide, Inches(8.45), Inches(0.2),
                pill_w=Inches(1.1), pill_h=Inches(0.32), font_size=10)
    _add_textbox(slide, Inches(3.6), Inches(0.82), Inches(9.3), Inches(0.35),
                 f"{period_prev} vs {period_curr}", size=11, bold=True, color=_NAVY)
    _add_table(slide, _MARGIN, top, Inches(12.5), Inches(5.8), headers, rows,
               rate_cols={2, 7}, bold_rows=bold_idx)


_HIERARCHY_BG = {
    "Error Rate Area Manager": _TPL_AM,
    "Error Rate SPV": _TPL_SPV,
}


def _build_hierarchy_slides(prs, title_prefix, table_rows, rows_per_slide=13):
    """``table_rows`` = list of [name, campaign, grand_total, error_rate, h, m, l, approved]
    dengan baris Total (per AM/SPV) ditandai lewat elemen ke-9 boolean.

    Background = raster asli PDF acuan (``_TPL_AM``/``_TPL_SPV``) — judul
    "Error Rate Area Manager"/"Error Rate SPV" sudah ter-bakar di situ, jadi
    TIDAK digambar ulang di sini (lihat docstring ``_bg_table_slide``).
    ``rows_per_slide`` diturunkan dari 16 ke 13 (28 September 2026): judul
    yang sekarang ikut ter-bakar di background makan tempat vertikal
    (konten mulai dari 1,4in, bukan 0,95in seperti sebelumnya), jadi baris
    per slide dikurangi supaya tabel tidak pernah melewati tepi bawah slide —
    ``_add_table`` memberi tinggi TETAP tiap baris (lihat catatan di sana),
    bukan menyusut otomatis mengikuti sisa ruang."""
    bg_path = _HIERARCHY_BG[title_prefix]
    # Lebar tabel dipatok maksimal 70% lebar slide (atas permintaan) supaya
    # tidak nyampe ujung kanan — beda dari tabel lain yang full-width.
    table_width = Emu(int(_SLIDE_W * 0.8))
    headers = ["Nama", "Campaign", "Grand Total", "Error Rate (%)", "H", "M", "L", "Approved"]
    plain_rows = [r[:8] for r in table_rows]
    bold_flags = [bool(r[8]) if len(r) > 8 else False for r in table_rows]
    for chunk_i, chunk in enumerate(_chunk(list(zip(plain_rows, bold_flags)), rows_per_slide)):
        subtitle = f"(lanjutan {chunk_i + 1})" if chunk_i else None
        slide, top = _bg_table_slide(prs, bg_path, Inches(1.6), subtitle=subtitle)
        rows = [r for r, _ in chunk]
        bold_idx = {i for i, (_, b) in enumerate(chunk) if b}
        _add_table(slide, _MARGIN, top, table_width, Inches(5.75), headers, rows,
                   rate_cols={3}, bold_rows=bold_idx)


def _build_top_tlo_slides(prs, data):
    """Background = raster asli PDF acuan per bucket masa kerja (``_TPL_TOPTLO``)
    — judul "Error Rate - Top 10 TLO" dan "Join Date {bucket}" sudah ter-bakar
    di situ (jadi tidak digambar ulang), TERMASUK paragraf "TOP 3 return
    reason" asli yang sengaja DIHAPUS karena datanya basi (Agustus 2026) dan
    sistem QC ini tidak memodelkan alasan retur sebagai data terstruktur —
    lihat docstring ``_TPL_TOPTLO``."""
    headers = ["Agent ID", "Nama TLO", "Dedicate", "SPV", "Join Date",
               "Grand Total", "Error Rate (%)", "H", "M", "L", "Approved"]
    for bucket, rows_in in data["top_tlo"].items():
        rows = [
            [
                r["agent_id"] or "-", r["name"] or "-", r["dedicate"] or "-",
                r["spv"] or "-", r["join_date"] or "-",
                _fmt_int(r["ticket_count"]), _fmt_pct(r["error_rate"]),
                _fmt_int(r["risk_high"]), _fmt_int(r["risk_medium"]), _fmt_int(r["risk_low"]),
                _fmt_int(r["approved"]),
            ]
            for r in rows_in
        ]
        bg_path = _TPL_TOPTLO.get(bucket)
        top = Inches(2.6)
        if bg_path and bg_path.exists():
            # Tidak pakai parameter subtitle _bg_table_slide (itu ditaruh DI
            # ATAS content_top) — di sini area itu sudah dipakai judul+"Join
            # Date {bucket}" bakaran.
            slide, top = _bg_table_slide(prs, bg_path, top)
        else:
            slide, top = _table_slide(prs, "Error Rate — Top 10 TLO", f"Join Date {bucket}")
        if rows:
            _add_table(slide, _MARGIN, top, Inches(12.5), Inches(4.7), headers, rows, rate_cols={6})
        else:
            _add_textbox(slide, _MARGIN, top, Inches(12), Inches(1),
                         "Tidak ada TLO dengan submission pada periode ini di bucket masa kerja ini.",
                         size=14, color=_GRAY_FG)


def _build_error_reason_slides(prs, data):
    # Dua slide TERPISAH per campaign (kategori, lalu top TLO terburuk) — bukan
    # digabung satu slide seperti PPT acuan: jumlah baris kategori & agent di
    # sini datang langsung dari data (bisa 0-6 dan 0-10), sehingga tinggi
    # gabungannya tidak bisa dipastikan selalu muat dalam satu slide tanpa
    # saling tumpang tindih. Memisahkannya menjamin tidak pernah overlap
    # berapa pun jumlah barisnya.
    for camp in data["error_reason"]:
        # Campaign tanpa kategori maupun agent sama sekali pada periode ini
        # (28 September 2026, atas permintaan user: "yang datanya tidak ada
        # di dashboard itu tidak perlu ditampilkan") — dilewati SELURUHNYA,
        # bukan dibikinkan slide kosong "tidak ada data". Beda dengan
        # campaign yang cuma salah satu kosong (ada kategori tapi TLO-nya
        # belum ada, atau sebaliknya) — itu tetap tampil apa adanya di bawah.
        if not camp["categories"] and not camp["top_agents"]:
            continue

        slide, top = _table_slide(prs, f"Detail Error Reason — {camp['label']}",
                                   f"Periode {data['period_current']['label']}", bg_path=_TPL_DETAIL)
        # Contoh alasan disederhanakan (28 September 2026, atas permintaan
        # user) — dipotong lebih pendek dari sebelumnya (110 char), cukup
        # cuplikan singkat, bukan alasan lengkap apa adanya.
        cat_headers = ["Kategori", "Contoh Alasan Teratas", "Fail Count"]
        cat_rows = [
            [c["category"], _truncate(c["example"], 50), _fmt_int(c["fail_count"])]
            for c in camp["categories"][:8]
        ]
        if cat_rows:
            _add_table(slide, _MARGIN, top, Inches(12.5), Inches(5.8), cat_headers, cat_rows)
        else:
            _add_textbox(slide, Inches(0), top, _SLIDE_W, Inches(0.4),
                         "Tidak ada kategori error pada periode ini.", size=12, bold=True, color=_NAVY,
                         align=PP_ALIGN.CENTER)

        slide2, top2 = _table_slide(prs, f"Top 10 TLO Terburuk — {camp['label']}",
                                     f"Periode {data['period_current']['label']}", bg_path=_TPL_DETAIL)
        # Cuma kolom Fail Rate (28 September 2026, atas permintaan user) —
        # TANPA kolom "Evaluated" dan tanpa ikut skema risk-based (H/M/L) &
        # kolom lain (Dedicate/SPV/Join Date/Approved) dari tabel Top 10 TLO
        # utama (``_build_top_tlo_slides``); tabel ini murni angka Fail
        # Tickets/Fail Rate dari agregasi Failure Rate apa adanya.
        agent_headers = ["Agent ID", "Nama", "Fail Tickets", "Fail Rate (%)"]
        agent_rows = [
            [a["agent_id"] or "-", a["name"] or "-", _fmt_int(a["fail_tickets"]),
             _fmt_pct(a["fail_rate"])]
            for a in camp["top_agents"]
        ]
        if agent_rows:
            _add_table(slide2, _MARGIN, top2, Inches(12.5), Inches(5.8), agent_headers, agent_rows, rate_cols={3})
        else:
            _add_textbox(slide2, Inches(0), top2, _SLIDE_W, Inches(0.4),
                         "Tidak ada TLO dengan tiket Not Qualified pada periode ini.", size=12, bold=True,
                         color=_NAVY, align=PP_ALIGN.CENTER)


def build_error_rate_pptx(data: dict) -> io.BytesIO:
    """Rakit seluruh deck dari ``data`` (lihat kontrak field di
    ``api/routers/stats.py::export_error_rate_pptx``) dan kembalikan buffer siap
    di-stream sebagai ``.pptx``."""
    prs = _new_deck()

    _cover_slide(prs, data["period_current"]["label"])

    _divider_slide(prs, _TPL_DIVIDER_TREND)
    _build_trend_slides(prs, data)
    _build_hierarchy_slides(prs, "Error Rate Area Manager", data["am_table"])
    _build_hierarchy_slides(prs, "Error Rate SPV", data["spv_table"])
    _build_top_tlo_slides(prs, data)

    _divider_slide(prs, _TPL_DIVIDER_RETURN)
    _build_error_reason_slides(prs, data)

    _closing_slide(prs)

    buffer = io.BytesIO()
    prs.save(buffer)
    buffer.seek(0)
    return buffer
