"""Parser scorecard format Bank Mega (CSV atau sheet xlsx) -> daftar item ternormalisasi + temuan.

Pakai:
    python3 -m core.builder.scorecard_parser <berkas.csv|berkas.xlsx> [nama_sheet]

Format yang dibaca (sama untuk Cashline dan NTB): header ``No | Kategori Script | Not Success Category |
Kategori System Error | Tolerable or Not | Score | Weight Score``; kategori hanya di baris pertama grup
(sel gabungan), baris "<Kategori> Score" = subtotal, "Total Score" dan "Minimum score passing Grade"
di bawah, baris grup "Dinamik (pilih N ...)" tanpa Major/Minor. Tidak ada LLM.

Tiap item: category, label, severity (Major/Minor), tolerable (YES/NO), score (angka atau None),
note (teks bebas yang nyasar di kolom angka), group ({"pick": N, "of": M} bila item anggota grup "pilih N").
Temuan (``flags``) dicatat, tidak diperbaiki diam-diam: skor kosong, catatan di kolom angka,
subtotal tidak cocok dengan jumlah item, total tidak cocok, anggota grup.
"""
import csv
import re
import sys

import openpyxl


def _cells(path, sheet=None):
    if path.lower().endswith(".csv"):
        return [list(r) for r in csv.reader(open(path, encoding="utf-8"))]
    ws = openpyxl.load_workbook(path, data_only=True)[sheet] if sheet else openpyxl.load_workbook(path, data_only=True).worksheets[0]
    return [[("" if c is None else c) for c in row] for row in ws.iter_rows(values_only=True)]


def _t(v):
    return re.sub(r"\s+", " ", str(v)).strip() if v not in (None, "") else ""


def _num(v):
    """Angka dari sel (juga '1.5', '5%'); None bila bukan angka."""
    if isinstance(v, (int, float)):
        return float(v)
    s = _t(v).replace(",", ".").rstrip("%").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _tol(v):
    s = _t(v).lower()
    if not s:
        return None
    return "NO" if "not" in s else "YES" if s.startswith("tol") else None


def parse(path, sheet=None):
    rows = _cells(path, sheet)
    head = next(i for i, r in enumerate(rows) if any("kategori script" in _t(c).lower() for c in r))
    col = {}
    for i, c in enumerate(rows[head]):
        s = _t(c).lower()
        for key, pat in (("no", r"^no$"), ("cat", "kategori script"), ("label", "not success"),
                         ("sev", "system error"), ("tol", "tol"), ("score", r"^score$"), ("weight", "weight")):
            if key not in col and re.search(pat, s):
                col[key] = i
    items, flags, subtotals = [], [], {}
    block_start = 0
    total = passing = None
    cat = None
    group = None
    g = lambda r, k: r[col[k]] if k in col and col[k] < len(r) else ""
    for ln, r in enumerate(rows[head + 1:], head + 2):
        if not any(_t(c) for c in r):
            continue
        label = _t(g(r, "label"))
        rowtxt = " ".join(_t(c) for c in r)
        low = (_t(g(r, "cat")) + " " + label).lower()
        first_text = next((_t(c) for c in r if _t(c) and _num(c) is None), "")
        nums = [_num(c) for c in r if _t(c) and _num(c) is not None]
        # baris penutup: teksnya bisa di kolom mana pun
        if re.search(r"total score", rowtxt.lower()):
            total = _num(g(r, "score")) if _num(g(r, "score")) is not None else (nums[0] if nums else None)
            continue
        if re.search(r"passing grade", rowtxt.lower()):
            passing = _num(g(r, "score")) if _num(g(r, "score")) is not None else (nums[0] if nums else None)
            continue
        # subtotal "<kategori> Score" (tanpa Major/Minor)
        # item selalu punya Major/Minor atau tolerable; baris dengan angka tetapi tanpa keduanya = subtotal
        # (juga bila tidak berakhiran kata "Score", mis. "T&C Product Mega Ultima Shiled")
        no_item_marks = not _t(g(r, "sev")) and not _tol(g(r, "tol"))
        if first_text and no_item_marks and (re.search(r"\bscore$", first_text.lower()) or nums):
            val = _num(g(r, "score")) if _num(g(r, "score")) is not None else (nums[0] if nums else None)
            name = re.sub(r"\s*score$", "", first_text, flags=re.I).strip()
            subtotals[name] = (ln, val)
            block = items[block_start:]
            bsum = sum(i["score"] or 0.0 for i in block)
            for i in block:
                i["block"] = name
            if val is not None and abs(bsum - val) > 1e-6:
                cats_in = []
                for i in block:
                    if i["category"] not in cats_in:
                        cats_in.append(i["category"])
                last = sum(i["score"] or 0.0 for i in block if i["category"] == cats_in[-1])
                if len(cats_in) > 1 and abs(last - val) < 1e-6:
                    flags.append(("kategori_tanpa_subtotal", ln, "%s tidak punya baris subtotal sendiri (%.2f); "
                                  "subtotal '%s' hanya menutup kategori terakhir" %
                                  (" + ".join(cats_in[:-1]), bsum - last, name)))
                else:
                    flags.append(("subtotal_tidak_cocok", ln, "%s: jumlah %d item di atasnya %.2f, subtotal %.2f" % (name, len(block), bsum, val)))
            block_start = len(items)
            group = None
            continue
        if not label and not _t(g(r, "cat")) and re.fullmatch(r"[\d.,]+", rowtxt):
            flags.append(("angka_liar", ln, rowtxt))
            continue
        if _num(label) is not None and not _t(g(r, "sev")):
            flags.append(("angka_liar", ln, label))
            continue
        if _t(g(r, "cat")):
            cat = _t(g(r, "cat"))
            group = None
        if not label:
            if len([c for c in r if _t(c)]) == 1 and re.fullmatch(r"[\d.,]+", rowtxt):
                flags.append(("angka_liar", ln, rowtxt))  # mis. 41.25 di bawah tabel
            continue
        sev, tol = _t(g(r, "sev")), _tol(g(r, "tol"))
        m = re.search(r"pilih\s+(\d+)", label.lower())
        if not sev and not tol and m:    # header grup "Dinamik (pilih 2 ...)"
            group = {"label": label, "pick": int(m.group(1)), "of": 0, "start": len(items)}
            continue
        score_cell, weight_cell = g(r, "score"), g(r, "weight")
        score, note = _num(score_cell), []
        for v in (score_cell, weight_cell):
            if _num(v) is None and _t(v):
                note.append(_t(v))
        extra = [_t(c) for c in r[max(col.values()) + 1:] if _t(c)]
        note += extra
        it = dict(line=ln, category=cat, label=label, severity=sev or None, tolerable=tol, score=score,
                  note=" | ".join(note) or None, group=None)
        if group:
            group["of"] += 1
            it["group"] = {"pick": group["pick"], "of": None, "label": group["label"]}
        items.append(it)
        if score is None and tol == "NO":
            flags.append(("skor_kosong_not_tolerable", ln, label))
        if note:
            flags.append(("catatan_di_kolom_angka", ln, "%s: %s" % (label, " | ".join(note))))
    # isi 'of' grup
    for it in items:
        if it["group"]:
            it["group"]["of"] = sum(1 for x in items if x["group"] and x["group"]["label"] == it["group"]["label"])
    sums = {}
    for it in items:
        sums[it["category"]] = sums.get(it["category"], 0.0) + (it["score"] or 0.0)
    all_sum = sum(sums.values())
    if total is not None and abs(all_sum - total) > 1e-6:
        flags.append(("total_tidak_cocok", 0, "jumlah item %.2f, Total Score %.2f" % (all_sum, total)))
    return dict(items=items, flags=flags, subtotals={k: v[1] for k, v in subtotals.items()}, total=total,
                passing=passing, item_sum=all_sum)


def report(res, title=""):
    out = ["== %s: %d item, jumlah skor %.2f, Total Score %s, passing %s" %
           (title, len(res["items"]), res["item_sum"], res["total"], res["passing"])]
    cats = []
    for it in res["items"]:
        if it["category"] not in cats:
            cats.append(it["category"])
    for c in cats:
        its = [i for i in res["items"] if i["category"] == c]
        out.append("  %-40s %2d item  skor %6.2f" % (c[:40], len(its), sum(i["score"] or 0 for i in its)))
    for f in res["flags"]:
        out.append("  ! %s (baris %s): %s" % f)
    return "\n".join(out)


if __name__ == "__main__":
    res = parse(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(report(res, sys.argv[1]))
