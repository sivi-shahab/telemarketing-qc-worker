"""Parser deterministik script campaign Bank Mega (template 7 kolom).

Template: FASE | BAGIAN | : | NO | SCRIPT | SYARAT | KETERANGAN.
Keluaran: daftar blok (satu per baris berisi SCRIPT/SYARAT/KETERANGAN) dengan FASE
diwariskan ke baris di bawahnya bila kosong. Tanpa LLM. Sheet tersembunyi dilewati
kecuali --hidden.

Gambar tersemat (tabel field, skema) diekstrak ke ``img_dir`` dan ditempel ke blok pemilik lewat kunci ``gambar``.
"""
import os
import posixpath
import re
import sys
import zipfile

import openpyxl

HEADER = {"FASE", "BAGIAN", "SCRIPT", "SYARAT", "KETERANGAN"}


def _txt(v):
    return re.sub(r"[ \t]+", " ", str(v)).strip() if v is not None else ""


def _fill(c):
    """Warna fill sel: RGB hex, 'theme:N' untuk warna tema, None bila tanpa fill."""
    if not c.fill or not c.fill.fill_type:
        return None
    col = c.fill.fgColor
    if col.type == "rgb":
        return col.rgb
    if col.type == "theme":
        return f"theme:{col.theme}"
    return f"{col.type}:{col.value}"


def parse_sheet(ws):
    blocks, findings = [], []
    header_row = None
    for r in range(1, min(ws.max_row, 15) + 1):
        vals = {_txt(c.value).upper() for c in ws[r]}
        if len(HEADER & vals) >= 4:
            header_row = r
            break
    if header_row is None:
        return blocks, [f"sheet '{ws.title}': baris header FASE/BAGIAN/SCRIPT tidak ditemukan"]
    fase = ""
    for r in range(header_row + 1, ws.max_row + 1):
        cells = {k: ws[f"{k}{r}"] for k in "ABCDEFG"}
        a, b, e, f, g = (_txt(cells[k].value) for k in "ABEFG")
        if not (a or b or e or f or g):
            continue
        if a:
            fase = a
        if not (e or f or g):
            # baris judul fase tanpa isi (mis. 'Health Declaration')
            continue
        if not e:
            findings.append(f"{ws.title}!{r}: bagian '{b[:40]}' tanpa SCRIPT (hanya SYARAT/KETERANGAN)")
        blocks.append({
            "sheet": ws.title, "row": r, "fase": fase, "fase_eksplisit": bool(a),
            "bagian": b, "script": e, "syarat": f, "keterangan": g,
            "warna": {k: _fill(cells[k]) for k in "BEF" if _fill(cells[k])},
            "alternatif": [x.strip() for x in re.split(r"\n\s*(?:ATAU|atau)\s*\n", e) if x.strip()] if e else [],
        })
    return blocks, findings


def sheet_images(path, img_dir):
    """Gambar tersemat per sheet: {judul_sheet: [(baris, kolom, berkas_png)]} (baris/kolom 1-based).

    Tabel dan skema di script sering berupa gambar (mis. tabel field pengisian data NTB),
    jadi teks sel saja tidak cukup. Gambar disalin ke img_dir.
    """
    ns = {"r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    out = {}
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())

        def rels(part):
            rp = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
            if rp not in names:
                return {}
            return {m.group(1): posixpath.normpath(posixpath.join(posixpath.dirname(part), m.group(2)))
                    for m in re.finditer(r'Id="(\w+)"[^>]*?Target="([^"]+)"', z.read(rp).decode())
                    } | {m.group(2): posixpath.normpath(posixpath.join(posixpath.dirname(part), m.group(1)))
                         for m in re.finditer(r'Target="([^"]+)"[^>]*?Id="(\w+)"', z.read(rp).decode())}

        wbx = z.read("xl/workbook.xml").decode()
        wrel = rels("xl/workbook.xml")
        os.makedirs(img_dir, exist_ok=True)
        for m in re.finditer(r"<sheet [^>]*?name=\"([^\"]+)\"[^>]*?r:id=\"(\w+)\"", wbx):
            title, sheet_part = m.group(1), wrel.get(m.group(2))
            if not sheet_part:
                continue
            for target in rels(sheet_part).values():
                if "/drawings/drawing" not in target:
                    continue
                dx, drel = z.read(target).decode(), rels(target)
                for a in re.finditer(r"<xdr:(?:two|one)CellAnchor.*?</xdr:(?:two|one)CellAnchor>", dx, re.S):
                    pos = re.search(r"<xdr:from><xdr:col>(\d+)</xdr:col>.*?<xdr:row>(\d+)</xdr:row>", a.group(0), re.S)
                    emb = re.search(r'r:embed="(\w+)"', a.group(0))
                    if not (pos and emb and drel.get(emb.group(1)) in names):
                        continue
                    media = drel[emb.group(1)]
                    dest = os.path.join(img_dir, f"{re.sub(r'[^A-Za-z0-9]+', '_', title)}_{posixpath.basename(media)}")
                    with open(dest, "wb") as fh:
                        fh.write(z.read(media))
                    out.setdefault(title, []).append((int(pos.group(2)) + 1, int(pos.group(1)) + 1, dest))
    return out


def parse_file(path, hidden=False, img_dir=None):
    wb = openpyxl.load_workbook(path)
    imgs = sheet_images(path, img_dir) if img_dir else {}
    blocks, findings, skipped = [], [], []
    for ws in wb.worksheets:
        if ws.sheet_state != "visible" and not hidden:
            skipped.append(ws.title)
            continue
        b, f = parse_sheet(ws)
        # gambar menempel ke blok terdekat di atas/sama dengan baris jangkarnya
        for row, col, file in sorted(imgs.get(ws.title, [])):
            owner = [x for x in b if x["row"] <= row]
            if owner:
                owner[-1].setdefault("gambar", []).append({"file": file, "baris": row, "kolom": "ABCDEFG"[col - 1]})
            else:
                f.append(f"{ws.title}!{row}: gambar tanpa blok pemilik")
        blocks += b
        findings += f
    return blocks, findings, skipped
