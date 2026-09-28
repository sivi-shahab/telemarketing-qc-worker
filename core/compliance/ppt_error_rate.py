"""Generator deck "Error Rate Update" (.pptx) — menu Stats > Generate PPT.

Meniru STRUKTUR deck bulanan yang selama ini dibuat manual di Canva
(``docs/csv_bank/25 September 2025/PPT Error Rate Telemarketing as of Agustus
2026.pdf``, 24 slide, 3 section): Trend Error Rate -> Error Rate Area
Manager/SPV -> Top 10 TLO -> Return Reason Details per campaign -> Complaint.

Styling MENIRU palet & font asli PDF acuan — diukur langsung dari pixel/font
embed-nya (25 September 2026): kuning ``#FFDE59``, navy ``#144778``, header
tabel ``#0070C0``, baris Total ``#00B0F0``, legend Risk Rate hijau/kuning/merah
(``#92D050``/``#FFFF00``/``#FF0000``), font judul "Paytone One", font tabel
"Montserrat", plus logo Bank Mega asli (``core/compliance/assets/``). YANG
TIDAK ditiru: elemen dekoratif hasil desain tangan Canva (kertas sobek,
selotip washi, ikon coretan/lampu/koin) — itu aset grafis kustom, di luar
cakupan yang masuk akal untuk generator otomatis.

Modul ini murni PRESENTASI: seluruh data (termasuk cell mana yang harus jadi
placeholder) sudah dirakit pemanggil (``api/routers/stats.py``) dari fungsi
agregasi di ``core/compliance/stats_aggregate.py``. Lihat ``PLACEHOLDER`` di
bawah — konstanta yang sama dipakai kedua sisi supaya sel yang datanya tidak
ada di sistem QC ini (Submission/Sampling asli, %KPI, status U/A/S/E1-E3,
Complaint, campaign Credit Shield/Personal Loan) selalu tampil abu-abu dengan
catatan "Isi manual", bukan diam-diam kosong atau menyesatkan sebagai nol.
"""
from __future__ import annotations

import io
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
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

_TITLE_FONT = "Paytone One"
_BODY_FONT = "Montserrat"

_SLIDE_W = Inches(13.333)
_SLIDE_H = Inches(7.5)
_MARGIN = Inches(0.4)

_ASSET_DIR = Path(__file__).resolve().parent / "assets"
_LOGO_PATH = _ASSET_DIR / "bank-mega-logo.png"
_LOGO_ASPECT = 1200 / 701  # width/height asli bank-mega-logo.png


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


def _add_logo(slide, top=Inches(0.25), height=Inches(0.45)):
    """Logo Bank Mega asli, pojok kanan atas — muncul di SETIAP slide di PDF
    acuan. Aman dipakai di atas background apa pun (PNG transparan)."""
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


def _title_slide(prs, title, subtitle=None):
    slide = _blank_slide(prs)
    bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, _SLIDE_W, _SLIDE_H)
    bg.fill.solid()
    bg.fill.fore_color.rgb = _YELLOW
    bg.line.fill.background()
    bg.shadow.inherit = False
    _add_logo(slide)
    _add_textbox(slide, Inches(0.8), Inches(2.7), Inches(11.7), Inches(1.2),
                 title, size=40, bold=True, color=_NAVY, font=_TITLE_FONT)
    if subtitle:
        _add_textbox(slide, Inches(0.8), Inches(3.8), Inches(11.7), Inches(0.7),
                     subtitle, size=20, bold=True, color=_NAVY)
    return slide


def _section_slide(prs, title, show_legend=False):
    slide = _blank_slide(prs)
    bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, _SLIDE_W, _SLIDE_H)
    bg.fill.solid()
    bg.fill.fore_color.rgb = _YELLOW
    bg.line.fill.background()
    bg.shadow.inherit = False
    _add_logo(slide)
    _add_textbox(slide, Inches(0.8), Inches(3.2), Inches(11.7), Inches(1.2),
                 title, size=34, bold=True, color=_NAVY, font=_TITLE_FONT)
    if show_legend:
        # Legend ambang Risk Rate ditaruh di slide divider (kosong, tinggi
        # tetap) — bukan menumpuk di bawah tabel Trend Error Rate: tinggi
        # tabel itu tergantung wrap teks header/kolom, jadi posisi "di bawah
        # tabel" tidak bisa dipastikan aman dari overlap (lihat catatan di
        # _build_error_reason_slides untuk kasus serupa).
        _add_textbox(slide, Inches(0.8), Inches(4.3), Inches(6), Inches(0.4),
                     "Ambang Error Rate:", size=14, bold=True, color=_NAVY)
        _add_legend(slide, Inches(0.8), Inches(4.75))
    return slide


def _table_slide(prs, title, subtitle=None):
    slide = _blank_slide(prs)
    _add_logo(slide)
    _add_textbox(slide, _MARGIN, Inches(0.2), Inches(10.5), Inches(0.55),
                 title, size=22, bold=True, color=_NAVY, font=_TITLE_FONT)
    # Garis aksen kuning tipis di bawah judul — jejak warna brand tanpa
    # menirukan bentuk sobekan kertas Canva.
    rule_top = Inches(0.78)
    rule = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, _MARGIN, rule_top, Inches(1.6), Pt(4))
    rule.fill.solid()
    rule.fill.fore_color.rgb = _YELLOW
    rule.line.fill.background()
    rule.shadow.inherit = False
    top = Inches(0.95)
    if subtitle:
        _add_textbox(slide, _MARGIN, Inches(0.85), Inches(12.5), Inches(0.4),
                     subtitle, size=12, color=_GRAY_FG)
        top = Inches(1.25)
    return slide, top


def _add_legend(slide, left, top):
    """Legend ambang Risk Rate (hijau/kuning/merah) — sekali saja, di slide
    Trend Error Rate pertama, sama seperti posisinya di PDF acuan."""
    items = [("0% - 3%", _GREEN), ("3% - 6%", _RATE_YELLOW), ("> 6%", _RED)]
    x = left
    w, h = Inches(1.35), Inches(0.4)
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
        run.font.size = Pt(12)
        run.font.bold = True
        run.font.name = _BODY_FONT
        run.font.color.rgb = _BLACK
        x += w + Inches(0.2)


def _style_header_cell(cell, text):
    cell.text = text
    cell.fill.solid()
    cell.fill.fore_color.rgb = _HEADER_BG
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
    cell.fill.solid()
    cell.fill.fore_color.rgb = fill if fill is not None else _WHITE
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
        f"Submission ({period_curr})", f"Sampling (%) ({period_curr})",
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
            PLACEHOLDER,
            _fmt_int(r["curr"]["h"]) if r["has_data"] else PLACEHOLDER,
            _fmt_int(r["curr"]["m"]) if r["has_data"] else PLACEHOLDER,
            _fmt_int(r["curr"]["l"]) if r["has_data"] else PLACEHOLDER,
            _fmt_pct(r["curr"]["error_rate"]) if r["has_data"] else PLACEHOLDER,
        ])
    slide, top = _table_slide(
        prs, "Trend Error Rate per Campaign",
        f"{period_prev} vs {period_curr} — kolom abu-abu = data tidak tersedia di sistem QC, isi manual",
    )
    _add_table(slide, _MARGIN, top, Inches(12.5), Inches(5.8), headers, rows,
               rate_cols={2, 8}, bold_rows=bold_idx)


def _build_hierarchy_slides(prs, title_prefix, table_rows, rows_per_slide=16):
    """``table_rows`` = list of [name, campaign, grand_total, error_rate, h, m, l, approved]
    dengan baris Total (per AM/SPV) ditandai lewat elemen ke-9 boolean."""
    headers = ["Nama", "Campaign", "Grand Total", "Error Rate (%)", "H", "M", "L", "Approved"]
    plain_rows = [r[:8] for r in table_rows]
    bold_flags = [bool(r[8]) if len(r) > 8 else False for r in table_rows]
    for chunk_i, chunk in enumerate(_chunk(list(zip(plain_rows, bold_flags)), rows_per_slide)):
        subtitle = None if chunk_i == 0 and len(plain_rows) <= rows_per_slide else \
            f"(lanjutan {chunk_i + 1})" if chunk_i else None
        slide, top = _table_slide(prs, title_prefix, subtitle)
        rows = [r for r, _ in chunk]
        bold_idx = {i for i, (_, b) in enumerate(chunk) if b}
        _add_table(slide, _MARGIN, top, Inches(12.5), Inches(6.0), headers, rows,
                   rate_cols={3}, bold_rows=bold_idx)


def _build_top_tlo_slides(prs, data):
    headers = ["Agent ID", "Nama TLO", "Dedicate", "SPV", "Join Date",
               "Grand Total", "Error Rate (%)", "H", "M", "L", "Approved",
               "%KPI Bln Lalu", "%KPI Bln Ini"]
    for bucket, rows_in in data["top_tlo"].items():
        rows = [
            [
                r["agent_id"] or "-", r["name"] or "-", r["dedicate"] or "-",
                r["spv"] or "-", r["join_date"] or "-",
                _fmt_int(r["ticket_count"]), _fmt_pct(r["error_rate"]),
                _fmt_int(r["risk_high"]), _fmt_int(r["risk_medium"]), _fmt_int(r["risk_low"]),
                _fmt_int(r["approved"]), PLACEHOLDER, PLACEHOLDER,
            ]
            for r in rows_in
        ]
        slide, top = _table_slide(
            prs, "Error Rate — Top 10 TLO", f"Join Date {bucket} — kolom %KPI belum tersedia di sistem, isi manual",
        )
        if rows:
            _add_table(slide, _MARGIN, top, Inches(12.5), Inches(5.8), headers, rows, rate_cols={6})
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
        slide, top = _table_slide(prs, f"Detail Error Reason — {camp['label']}",
                                   f"Periode {data['period_current']['label']}")
        if not camp["has_data"]:
            _add_textbox(slide, _MARGIN, top, Inches(12), Inches(1),
                         "Campaign ini belum terdaftar di sistem QC — tidak ada data untuk diagregasi.",
                         size=14, color=_GRAY_FG)
            continue
        cat_headers = ["Kategori", "Contoh Alasan Teratas", "Fail Count"]
        cat_rows = [
            [c["category"], _truncate(c["example"], 110), _fmt_int(c["fail_count"])]
            for c in camp["categories"][:8]
        ]
        if cat_rows:
            _add_table(slide, _MARGIN, top, Inches(12.5), Inches(5.8), cat_headers, cat_rows)
        else:
            _add_textbox(slide, _MARGIN, top, Inches(12), Inches(0.4),
                         "Tidak ada kategori error pada periode ini.", size=12, color=_GRAY_FG)

        slide2, top2 = _table_slide(prs, f"Top 10 TLO Terburuk — {camp['label']}",
                                     f"Periode {data['period_current']['label']}")
        agent_headers = ["Agent ID", "Nama", "Fail Tickets", "Evaluated", "Fail Rate (%)"]
        agent_rows = [
            [a["agent_id"] or "-", a["name"] or "-", _fmt_int(a["fail_tickets"]),
             _fmt_int(a["evaluated"]), _fmt_pct(a["fail_rate"])]
            for a in camp["top_agents"]
        ]
        if agent_rows:
            _add_table(slide2, _MARGIN, top2, Inches(12.5), Inches(5.8), agent_headers, agent_rows, rate_cols={4})
        else:
            _add_textbox(slide2, _MARGIN, top2, Inches(12), Inches(0.4),
                         "Tidak ada TLO dengan tiket Not Qualified pada periode ini.", size=12, color=_GRAY_FG)


def _build_complaint_slides(prs, data):
    _section_slide(prs, f"3. Complaint {data['period_current']['label']}")
    slide, top = _table_slide(prs, "Complaint", data["period_current"]["label"])
    _add_textbox(
        slide, _MARGIN, top, Inches(12), Inches(2),
        "Data Complaint (Type of Customer Commentary, Fault Category: Customer Fault vs "
        "Customer Inquiry) tidak tersedia di sistem QC ini — isi manual dari sumber data Complaint.",
        size=14, color=_GRAY_FG,
    )


def build_error_rate_pptx(data: dict) -> io.BytesIO:
    """Rakit seluruh deck dari ``data`` (lihat kontrak field di
    ``api/routers/stats.py::export_error_rate_pptx``) dan kembalikan buffer siap
    di-stream sebagai ``.pptx``."""
    prs = _new_deck()

    _title_slide(prs, "Error Rate Update",
                 f"as of {data['period_current']['label']}")

    _section_slide(prs, "1. Trend Error Rate", show_legend=True)
    _build_trend_slides(prs, data)
    _build_hierarchy_slides(prs, "Error Rate Area Manager", data["am_table"])
    _build_hierarchy_slides(prs, "Error Rate SPV", data["spv_table"])
    _build_top_tlo_slides(prs, data)

    _section_slide(prs, "2. Return Reason Details")
    _build_error_reason_slides(prs, data)

    _build_complaint_slides(prs, data)

    _title_slide(prs, "Terima Kasih")

    buffer = io.BytesIO()
    prs.save(buffer)
    buffer.seek(0)
    return buffer
