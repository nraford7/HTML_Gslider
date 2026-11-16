"""Deck dict -> Slides batchUpdate requests, one SlidePlan per slide (pure, no network).

Geometry is in EMU (ints) on every element; type sizes, rule weights and paragraph
spacing are in PT. Box x/y/w are the text-box element edges (text edge - inset);
Box.h is the text-area height without insets (the element is h + 2 * inset tall).

SlidePlan = {n, slide_id, html_id, ground, notes_text, status, reason, requests,
             boxes, missing_assets}
Box = {object_id, role, ground, x, y, w, h, text, lines}
Table Box (role "table") adds rows, cols; x/y/w/h are the table element's edges (h is the
full table height) and text is every non-empty cell, "\n"-joined.
`missing_assets` lists asset keys (sha256, "logo", "logo_dark", "fallback:<n>") the
plan needed but `assets` did not hold; those images are left out of `requests`.
"""
from __future__ import annotations

import io

from PIL import Image

from .fonts import line_height, measure
from .layout import Layout

EMU_PER_PT = 12700
FIELD_ROLE = {"kicker": "cover_kicker", "title": "cover_title", "sub": "cover_sub",
              "meta": "cover_meta", "contact": "contact"}


class Overflow(Exception):
    pass


def emu(pt: float) -> int:
    return round(pt * EMU_PER_PT)


def rgb(hexstr: str) -> dict:
    h = hexstr.lstrip("#")
    return {k: int(h[i:i + 2], 16) / 255 for k, i in (("red", 0), ("green", 2), ("blue", 4))}


def _blend(fg: str, bg: str, alpha: float) -> dict:
    """rgbColor of fg drawn at alpha over bg."""
    f, g = rgb(fg), rgb(bg)
    return {k: f[k] * alpha + g[k] * (1 - alpha) for k in f}


def u16(s: str) -> int:
    """Slides text indexes count UTF-16 code units."""
    return len(s.encode("utf-16-le")) // 2


IMAGE_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/jpg": "JPEG", "image/gif": "GIF"}
MAX_IMAGE_BYTES = 50 * 1024 * 1024  # Slides createImage limits
MAX_IMAGE_PIXELS = 25_000_000
MIN_COL_EMU = 406400  # Slides refuses table columns under 32pt
CELL_PAD = 7.2  # Slides' default table cell padding (0.1in each side), pt


def _unresolved(img: dict) -> None:
    """ValueError for a linked image build did not resolve (or was not asked to)."""
    if img.get("src") and img.get("data") is None:
        raise ValueError(f"linked image {img['src']} not resolved")


def _aspect(img: dict) -> float:
    """Width / height of an image Slides can insert; ValueError (slide falls back) otherwise."""
    _unresolved(img)
    mime, data = img.get("mime"), img.get("data")
    fmt = IMAGE_FORMATS.get(mime)
    if fmt is None:
        raise ValueError(f"unsupported image type {mime}")
    if not isinstance(data, bytes):
        raise ValueError(f"unreadable image ({mime}): no bytes")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"image is {len(data) / 1e6:.0f} MB, over the 50 MB limit")
    try:
        with Image.open(io.BytesIO(data)) as im:
            (w, h), real = im.size, im.format
    except Exception as e:
        raise ValueError(f"unreadable image ({mime}): {e}") from e
    if real != fmt:
        raise ValueError(f"unsupported image type {mime} (bytes are {real})")
    if w * h > MAX_IMAGE_PIXELS:
        raise ValueError(f"image is {w}x{h} pixels, over the 25 Mpx limit")
    if not w or not h:
        raise ValueError(f"unreadable image ({mime}): empty")
    return w / h


class _Slide:
    """Request and box collector for one slide."""

    def __init__(self, n: int, ground: str, layout: Layout, assets: dict, logo: bool = True):
        self.n, self.ground, self.L, self.assets, self.use_logo = n, ground, layout, assets, logo
        self.slide_id = f"s{n:02d}_slide"
        self.reqs: list[dict] = []
        self.boxes: list[dict] = []
        self.missing: list[str] = []
        self.count = 0
        self.reqs.append({"createSlide": {"objectId": self.slide_id,
                                          "slideLayoutReference": {"predefinedLayout": "BLANK"}}})
        self.reqs.append({"updatePageProperties": {
            "objectId": self.slide_id,
            "pageProperties": {"pageBackgroundFill": {"solidFill": {
                "color": {"rgbColor": rgb(layout.grounds[ground])}}}},
            "fields": "pageBackgroundFill.solidFill.color"}})

    # -- helpers
    def oid(self, role: str) -> str:
        i = self.count
        self.count += 1
        return f"s{self.n:02d}_{role}{i}"

    def props(self, x: float, y: float, w: float, h: float) -> dict:
        return {"pageObjectId": self.slide_id,
                "size": {"width": {"magnitude": emu(w), "unit": "EMU"},
                         "height": {"magnitude": emu(h), "unit": "EMU"}},
                "transform": {"scaleX": 1, "scaleY": 1, "translateX": emu(x),
                              "translateY": emu(y), "unit": "EMU"}}

    def style(self, role: str) -> dict:
        return self.L.styles[role][self.ground]

    def measure(self, role: str, paras: list[str], w: float, bullets: list[str] = ()) -> tuple[int, float]:
        """Lines and text height of paragraphs in a text column w pt wide."""
        st = self.style(role)
        ins = 2 * self.L.inset
        lines = 0
        if paras:
            lines += measure("\n".join(paras), st["font"], st["size"], w + ins, st["line_spacing"])[0]
        if bullets:
            lines += measure("\n".join(bullets), st["font"], st["size"],
                             w + ins - self.L.bullet_indent, st["line_spacing"])[0]
        lh = line_height(st["font"], st["size"], st["line_spacing"])
        return lines, lines * lh + (len(paras) + len(bullets)) * st["space_after"]

    def one_line(self, role: str) -> float:
        st = self.style(role)
        return line_height(st["font"], st["size"], st["line_spacing"]) + st["space_after"]

    # -- elements
    def text(self, role: str, x: float, y: float, w: float, paras: list[str] | str,
             bullets: list[str] = (), bold=()) -> dict | None:
        """Text box whose text edge is (x, y) and text column w; returns the Box.

        bold: (start, length) ranges on the box text (paragraphs, then bullets, "\n"-joined).
        """
        paras = [paras] if isinstance(paras, str) else list(paras)
        paras = [p for part in paras for p in part.split("\n")]
        bullets = list(bullets)
        text = "\n".join(paras + bullets)
        if not text.strip():
            return None
        lines, h = self.measure(role, paras, w, bullets)
        ins = self.L.inset
        oid = self.oid(role)
        bx, by, bw = x - ins, y - ins, w + 2 * ins
        st = self.style(role)
        self.reqs += [
            {"createShape": {"objectId": oid, "shapeType": "TEXT_BOX",
                             "elementProperties": self.props(bx, by, bw, h + 2 * ins)}},
            {"insertText": {"objectId": oid, "insertionIndex": 0, "text": text}},
            {"updateTextStyle": {
                "objectId": oid, "textRange": {"type": "ALL"},
                "style": {"fontFamily": st["font"],
                          "weightedFontFamily": {"fontFamily": st["font"], "weight": st["weight"]},
                          "fontSize": {"magnitude": st["size"], "unit": "PT"},
                          "foregroundColor": {"opaqueColor": {"rgbColor": rgb(st["color"])}},
                          "bold": False, "italic": False},
                "fields": "fontFamily,weightedFontFamily,fontSize,foregroundColor,bold,italic"}},
            {"updateParagraphStyle": {
                "objectId": oid, "textRange": {"type": "ALL"},
                "style": {"lineSpacing": st["line_spacing"],
                          "spaceAbove": {"magnitude": 0, "unit": "PT"},
                          "spaceBelow": {"magnitude": st["space_after"], "unit": "PT"}},
                "fields": "lineSpacing,spaceAbove,spaceBelow"}},
        ]
        if bullets:
            start = u16("\n".join(paras) + "\n") if paras else 0
            rng = {"type": "FIXED_RANGE", "startIndex": start, "endIndex": u16(text)}
            self.reqs += [
                {"createParagraphBullets": {"objectId": oid, "textRange": rng,
                                            "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}},
                {"updateParagraphStyle": {
                    "objectId": oid, "textRange": rng,
                    "style": {"indentStart": {"magnitude": self.L.bullet_indent, "unit": "PT"},
                              "indentFirstLine": {"magnitude": 0, "unit": "PT"}},
                    "fields": "indentStart,indentFirstLine"}},
            ]
        for start, length in bold or ():
            if length <= 0 or start < 0 or start + length > len(text):
                raise ValueError(f"bold range {start},{length} outside {role} text")
            self.reqs.append({"updateTextStyle": {
                "objectId": oid,
                "textRange": {"type": "FIXED_RANGE", "startIndex": u16(text[:start]),
                              "endIndex": u16(text[:start + length])},
                "style": {"bold": True}, "fields": "bold"}})
        box = {"object_id": oid, "role": role, "ground": self.ground, "x": bx, "y": by,
               "w": bw, "h": h, "text": text, "lines": lines}
        self.boxes.append(box)
        return box

    def rule(self, x: float, y: float, w: float) -> None:
        oid = self.oid("rule")
        r = self.L.rule
        self.reqs += [
            {"createLine": {"objectId": oid, "category": "STRAIGHT",
                            "elementProperties": {**self.props(x, y, w, 0),
                                                  "size": {"width": {"magnitude": emu(w), "unit": "EMU"},
                                                           "height": {"magnitude": 1, "unit": "EMU"}}}}},
            {"updateLineProperties": {"objectId": oid, "lineProperties": {
                "lineFill": {"solidFill": {"color": {"rgbColor": rgb(r["color"])}, "alpha": r["alpha"]}},
                "weight": {"magnitude": r["weight"], "unit": "PT"}},
                "fields": "lineFill,weight"}},
        ]

    def image(self, key: str, role: str, x: float, y: float, w: float, h: float) -> None:
        url = self.assets.get(key)
        if not url:
            self.missing.append(key)
            return
        self.reqs.append({"createImage": {"objectId": self.oid(role), "url": url,
                                          "elementProperties": self.props(x, y, w, h)}})

    def shade(self, x: float, y: float, w: float, h: float, alpha: float) -> None:
        oid = self.oid("shade")
        self.reqs += [
            {"createShape": {"objectId": oid, "shapeType": "RECTANGLE",
                             "elementProperties": self.props(x, y, w, h)}},
            {"updateShapeProperties": {"objectId": oid, "shapeProperties": {
                "shapeBackgroundFill": {"solidFill": {
                    "color": {"rgbColor": rgb(self.L.grounds[self.ground])}, "alpha": alpha}},
                "outline": {"propertyState": "NOT_RENDERED"}},
                "fields": "shapeBackgroundFill.solidFill.color,shapeBackgroundFill.solidFill.alpha,"
                          "outline.propertyState"}},
        ]

    def background(self, img: dict) -> None:
        """Slide background image, full height or full width, under a ground shade."""
        pw, ph = self.L.page["w"], self.L.page["h"]
        a = _aspect(img)
        if a >= pw / ph:  # wide: full width, bottom-aligned
            w, h = pw, pw / a
            x, y = 0, ph - h
        else:  # tall: full height, right-aligned
            w, h = ph * a, ph
            x, y = pw - w, 0
        self.image(img["sha256"], "bgimage", x, y, w, h)
        self.shade(x, y, w, h, self.L.background.get("shade_alpha", 0.78))

    def logo(self) -> None:
        if not self.use_logo:  # no brand logo configured
            return
        lg = self.L.chrome["logo"]
        self.image("logo" if self.ground == "dark" else "logo_dark", "logo",
                   lg["x"], lg["y"], lg["w"], lg["h"])

    def pageno(self) -> None:
        p = self.L.chrome["pageno"]
        self.text("pageno", p["x"], p["y"], 40, f"{self.n:02d}")


# ------------------------------------------------------------------ recipes

def _bottom(box: dict | None, layout: Layout) -> float:
    return box["y"] + layout.inset + box["h"] if box else 0.0


def _grid(layout: Layout, n: int, frac: float | None) -> tuple[float, float]:
    """(pitch, width) for n cells over the full measure or a fraction of it."""
    g = layout.grid[n]
    if not frac:
        return g["pitch"], g["w"]
    gutter = g["pitch"] - g["w"]
    total = layout.title["w"] * frac
    w = (total - (n - 1) * gutter) / n
    return w + gutter, w


def _bold(cell: dict, field: str) -> list:
    """A cell's bold ranges for one field ({field: [(start, length)]} from parse)."""
    bold = cell.get("bold") or {}
    return list(bold.get(field, ())) if isinstance(bold, dict) else []


def _cells(s: _Slide, b: dict, y: float) -> float:
    L, cells = s.L, b["cells"]
    n = len(cells)
    pitch, w = _grid(L, n, b.get("width"))
    xs = [L.margin + i * pitch for i in range(n)]
    c = L.cells
    if any(cl["num"] for cl in cells):
        hs = [_bottom(s.text("number", x, y, w, cl["num"], bold=_bold(cl, "num")), L) - y
              if cl["num"] else 0 for x, cl in zip(xs, cells)]
        rule_y = y + max(hs) + (c["number_to_rule"] - s.one_line("number"))
    else:
        rule_y = y
    for x in xs:
        s.rule(x, rule_y, w)
    head_y = rule_y + c["rule_to_header"]
    bottom = head_y
    if any(cl["header"] for cl in cells):
        hs = []
        for x, cl in zip(xs, cells):
            bx = s.text("header", x, head_y, w, cl["header"], bold=_bold(cl, "header")) if cl["header"] else None
            hs.append(_bottom(bx, L) - head_y if bx else 0)
        body_y = head_y + max(hs) + (c["header_to_body"] - s.one_line("header"))
        bottom = head_y + max(hs)
    else:
        body_y = head_y
    for x, cl in zip(xs, cells):
        cy = body_y
        if cl["value"]:
            vb = s.text("value", x, cy, w, cl["value"], bold=_bold(cl, "value"))
            cy = _bottom(vb, L) + 4
            bottom = max(bottom, cy - 4)
        if cl["body"] or cl["bullets"]:
            body = "\n".join(cl["body"])
            shift = len(body) + 1 if body else 0
            rng = _bold(cl, "body") + [(st + shift, n) for st, n in _bold(cl, "bullets")]
            bb = s.text("body", x, cy, w, cl["body"], cl["bullets"], bold=rng)
            bottom = max(bottom, _bottom(bb, L))
    return bottom


def _ratios(widths: list, ncols: int) -> list[float]:
    """Column shares summing to 1: declared widths (scaled down when over 100%), the rest split evenly."""
    ws = list(widths or [])[:ncols] + [None] * max(0, ncols - len(widths or []))
    known = sum(w for w in ws if w)
    if known > 1:  # declared widths over 100%: scale them to fit
        ws = [w / known if w else w for w in ws]
        known = 1.0
    free = [i for i, w in enumerate(ws) if not w]
    if free:
        share = (1 - known) / len(free)
        ws = [w if w else share for w in ws]
    total = sum(ws)
    return [w / total for w in ws] if total > 0 else [1 / ncols] * ncols


def _table_widths(widths: list, ncols: int, total: int) -> list[int]:
    """Column widths in EMU from width ratios: each at least 32pt, summing to `total` exactly."""
    if ncols * MIN_COL_EMU > total:
        raise ValueError(f"table of {ncols} columns does not fit: {ncols} x 32pt > {total / EMU_PER_PT:.0f}pt")
    r = _ratios(widths, ncols)
    fixed: set[int] = set()
    while True:
        room = total - MIN_COL_EMU * len(fixed)
        share = sum(r[i] for i in range(ncols) if i not in fixed)
        ws = [MIN_COL_EMU if i in fixed else (r[i] / share * room if share else room / (ncols - len(fixed)))
              for i in range(ncols)]
        narrow = {i for i in range(ncols) if i not in fixed and ws[i] < MIN_COL_EMU}
        if not narrow:
            break
        fixed |= narrow
    out = [int(w) for w in ws]
    out[max(range(ncols), key=lambda i: out[i])] += total - sum(out)
    return out


def _cell_bold(bold: list, i: int, j: int) -> list:
    """Bold ranges of row i, cell j from a rows block's `bold` (row -> cell -> [start, length])."""
    row = bold[i] if i < len(bold) else ()
    return [tuple(r) for r in row[j]] if j < len(row) else []


def _rows(s: _Slide, b: dict, y: float, compact: bool) -> float:
    L = s.L
    pad = L.rows_compact["pad"] if compact else L.row_pad
    pmin = L.rows_compact["pitch_min"] if compact else L.rows["pitch_min"]
    if b.get("style") != "timeline":  # tables (and rows blocks with no style) become native tables
        return _table(s, b, y, pmin)
    bold = b.get("bold") or []
    r = L.rows
    cols = [(r["label_x"], r["text_x"] - r["label_x"] - 12),
            (r["text_x"], L.margin + L.title["w"] - r["text_x"])]
    rows = []
    for i, row in enumerate(b["rows"]):
        rows.append([("number", r["num_x"], r["label_x"] - r["num_x"] - 8, str(i + 1), ())]
                    + [(("header", "body")[j], x, w, t, _cell_bold(bold, i, j))
                       for j, ((x, w), t) in enumerate(zip(cols, row))])
    bottom = y
    for i, row in enumerate(rows):
        if i:
            s.rule(L.margin, y - pad / 2, L.title["w"])
        tallest = 0.0
        for role, x, w, t, rng in row:
            if t:
                tallest = max(tallest, _bottom(s.text(role, x, y, w, t, bold=rng), L) - y)
        bottom = y + tallest
        y += max(tallest + pad, pmin)
    return bottom


def _table(s: _Slide, b: dict, y: float, pmin: float) -> float:
    """Native Slides table whose first-row text top is y and whose cell text lines up with the
    margin; returns the last row's text bottom (the table edge less the cell padding)."""
    L, pad = s.L, CELL_PAD
    bold = b.get("bold") or []
    header = b.get("header") or []
    ncols = max([len(header)] + [len(rw) for rw in b["rows"]])
    grid = ([[(t, "colhead", list(hb) if hb else []) for t, hb in
              zip(header, list(b.get("header_bold") or []) + [()] * len(header))]] if header else [])
    grid += [[(t, "body", _cell_bold(bold, i, j)) for j, t in enumerate(rw)] for i, rw in enumerate(b["rows"])]
    grid = [row + [("", "body", [])] * (ncols - len(row)) for row in grid]
    x, w = L.margin - pad, L.title["w"] + 2 * pad
    cols = _table_widths(b.get("widths"), ncols, emu(w))
    heights = []
    for i, row in enumerate(grid):
        text_h = max([s.measure(role, t.split("\n"), cw / EMU_PER_PT - 2 * pad)[1]
                      for (t, role, _), cw in zip(row, cols) if t] + [0])
        rh = text_h + 2 * pad
        heights.append(rh if header and i == 0 else max(rh, pmin))
    oid = s.oid("table")
    top = y - pad
    total_h = sum(heights)
    r = L.rule
    reqs = [{"createTable": {"objectId": oid, "rows": len(grid), "columns": ncols,
                             "elementProperties": s.props(x, top, w, total_h)}}]
    reqs += [{"updateTableColumnProperties": {
        "objectId": oid, "columnIndices": [j],
        "tableColumnProperties": {"columnWidth": {"magnitude": cw, "unit": "EMU"}},
        "fields": "columnWidth"}} for j, cw in enumerate(cols)]
    reqs += [{"updateTableRowProperties": {
        "objectId": oid, "rowIndices": [i],
        "tableRowProperties": {"minRowHeight": {"magnitude": emu(h), "unit": "EMU"}},
        "fields": "minRowHeight"}} for i, h in enumerate(heights)]
    line = {"color": {"rgbColor": rgb(r["color"])}}
    # Slides accepts table-border alpha 0 or 1 only: the inner rules are opaque, in the rule colour
    # pre-blended at its alpha over the slide's ground (what a translucent rule would show)
    inner = {"color": {"rgbColor": _blend(r["color"], L.grounds[s.ground], r["alpha"])}}
    border_fields = "tableBorderFill.solidFill.color,tableBorderFill.solidFill.alpha,weight,dashStyle"
    reqs += [
        {"updateTableBorderProperties": {"objectId": oid, "borderPosition": "ALL", "tableBorderProperties": {
            "tableBorderFill": {"solidFill": {**inner, "alpha": 1}},
            "weight": {"magnitude": r["weight"], "unit": "PT"}, "dashStyle": "SOLID"},
            "fields": border_fields}},
        {"updateTableBorderProperties": {"objectId": oid, "borderPosition": "OUTER", "tableBorderProperties": {
            "tableBorderFill": {"solidFill": {**line, "alpha": 0}}},
            "fields": "tableBorderFill.solidFill.color,tableBorderFill.solidFill.alpha"}},
        {"updateTableBorderProperties": {"objectId": oid, "borderPosition": "INNER_VERTICAL",
                                         "tableBorderProperties": {
                                             "tableBorderFill": {"solidFill": {**line, "alpha": 0}}},
                                         "fields": "tableBorderFill.solidFill.color,tableBorderFill.solidFill.alpha"}},
        {"updateTableCellProperties": {
            "objectId": oid,
            "tableCellProperties": {"tableCellBackgroundFill": {"propertyState": "NOT_RENDERED"}},
            "fields": "tableCellBackgroundFill.propertyState"}},
    ]
    texts = []
    for i, row in enumerate(grid):
        for j, (t, role, rng) in enumerate(row):
            if not t:
                continue
            texts.append(t)
            loc = {"rowIndex": i, "columnIndex": j}
            st = s.style(role)
            reqs += [
                {"insertText": {"objectId": oid, "cellLocation": loc, "insertionIndex": 0, "text": t}},
                {"updateTextStyle": {
                    "objectId": oid, "cellLocation": loc, "textRange": {"type": "ALL"},
                    "style": {"fontFamily": st["font"],
                              "weightedFontFamily": {"fontFamily": st["font"], "weight": st["weight"]},
                              "fontSize": {"magnitude": st["size"], "unit": "PT"},
                              "foregroundColor": {"opaqueColor": {"rgbColor": rgb(st["color"])}},
                              "bold": False, "italic": False},
                    "fields": "fontFamily,weightedFontFamily,fontSize,foregroundColor,bold,italic"}},
                {"updateParagraphStyle": {
                    "objectId": oid, "cellLocation": loc, "textRange": {"type": "ALL"},
                    "style": {"lineSpacing": st["line_spacing"],
                              "spaceAbove": {"magnitude": 0, "unit": "PT"},
                              "spaceBelow": {"magnitude": st["space_after"], "unit": "PT"}},
                    "fields": "lineSpacing,spaceAbove,spaceBelow"}},
            ]
            for start, length in rng:
                if length <= 0 or start < 0 or start + length > len(t):
                    raise ValueError(f"bold range {start},{length} outside table cell text")
                reqs.append({"updateTextStyle": {
                    "objectId": oid, "cellLocation": loc,
                    "textRange": {"type": "FIXED_RANGE", "startIndex": u16(t[:start]),
                                  "endIndex": u16(t[:start + length])},
                    "style": {"bold": True}, "fields": "bold"}})
    s.reqs += reqs
    s.boxes.append({"object_id": oid, "role": "table", "ground": s.ground, "x": x, "y": top, "w": w,
                    "h": total_h, "text": "\n".join(texts), "lines": len(grid), "rows": len(grid), "cols": ncols})
    return top + total_h - pad


def _band(s: _Slide, b: dict, y: float) -> float:
    L, bd = s.L, s.L.band
    right = L.margin + L.title["w"] * (b.get("width") or 1)
    lw = bd["text_x"] - bd["label_x"] - 12
    tw = right - bd["text_x"]
    lab_h = s.measure("band_label", b["label"].split("\n"), lw)[1] if b.get("label") else 0
    txt_h = s.measure("body", b["text"].split("\n"), tw)[1] if b.get("text") else 0
    need = max(bd["rule_to_label"] + lab_h, bd["rule_to_text"] + txt_h)
    rule_y = max(y, min(L.band_y, L.floor - need))  # pinned near the foot like DESIGN-SYSTEM
    s.rule(L.margin, rule_y, right - L.margin)
    bottom = rule_y
    if b.get("label"):
        bottom = max(bottom, _bottom(s.text("band_label", bd["label_x"], rule_y + bd["rule_to_label"], lw,
                                            b["label"].split("\n")), L))
    if b.get("text"):
        bottom = max(bottom, _bottom(s.text("body", bd["text_x"], rule_y + bd["rule_to_text"], tw,
                                            b["text"], bold=b.get("bold", ())), L))
    return bottom


def _full_image(s: _Slide, b: dict, y: float) -> float:
    """Image over the full measure from y, shrunk to fit above the floor; returns its bottom."""
    L = s.L
    if L.floor - y < L.min_image_h:
        raise Overflow(f"overflow at image: top {y:.0f}pt leaves < {L.min_image_h:g}pt above floor {L.floor:g}pt")
    a = _aspect(b)
    w = L.title["w"]
    h = min(w / a, L.floor - y)
    w = h * a
    s.image(b["sha256"], "image", L.margin, y, w, h)
    return y + h


TEXT_ROLE = {"title": "title", "lead": "lead", "note": "note", "quote": "quote"}


def _content(s: _Slide, slide: dict, compact: bool) -> None:
    L = s.L
    if slide.get("background"):
        s.background(slide["background"])
    s.logo()
    bottom, prev = None, None
    group_top = group_bottom = None  # the run of 60%-width blocks an image sits beside
    side_bottom = 0.0  # lowest side-image edge; a full-width block starts below it
    for k, b in enumerate(slide["blocks"]):
        kind = b["kind"]
        if kind == "section":
            x, y, w = L.section_pos
            s.text("section", x, y, w, b["text"], bold=b.get("bold", ()))
            continue
        if kind in ("unknown", "field"):
            raise ValueError(f"unknown block {b.get('cls') or kind}")
        if kind == "image" and group_top is None:
            nxt = next((n for n in slide["blocks"][k + 1:] if n["kind"] != "section"), None)
            if nxt is not None and nxt.get("width"):
                raise ValueError("side image before group")
        if kind == "image" and group_top is not None:
            si = L.side_image
            span = max(group_bottom - group_top, 120)
            a = _aspect(b)
            w = min(si["w"], span * a)
            h = w / a
            if group_top + h > L.floor:
                raise Overflow(f"overflow at side image {k}: bottom {group_top + h:.0f}pt > floor {L.floor:g}pt")
            s.image(b["sha256"], "image", si["x"] + si["w"] - w, group_top, w, h)
            side_bottom = max(side_bottom, group_top + h)
            continue
        if not b.get("width") and side_bottom > (bottom or 0):
            bottom = side_bottom
        if bottom is None:
            y = L.title["y"]
        else:
            y = bottom + (L.lead_gap if prev == "title" else L.block_gap)
        if kind in TEXT_ROLE:
            frac = b.get("width") or 1
            box = s.text(TEXT_ROLE[kind], L.margin, y, L.title["w"] * frac, b["text"], bold=b.get("bold", ()))
            new_bottom = _bottom(box, L) if box else y
        elif kind == "cells":
            new_bottom = _cells(s, b, y)
        elif kind == "rows":
            new_bottom = _rows(s, b, y, compact)
        elif kind == "band":
            new_bottom = _band(s, b, y)
        elif kind == "image":
            new_bottom = _full_image(s, b, y)
        else:
            raise ValueError(f"unknown block {kind}")
        if b.get("width"):
            group_top = y if group_top is None else group_top
            group_bottom = new_bottom
        else:  # a full-width block ends the 60% group a side image may sit beside
            group_top = group_bottom = None
        if new_bottom > L.floor:
            raise Overflow(f"overflow at block {k} ({kind}): bottom {new_bottom:.0f}pt > floor {L.floor:g}pt")
        bottom, prev = new_bottom, kind
    s.pageno()


def _cover(s: _Slide, slide: dict) -> None:
    L = s.L
    spec = L.cover if slide["kind"] == "cover" else L.closing
    if slide.get("background"):
        s.background(slide["background"])
    for b in slide["blocks"]:
        if b["kind"] == "image":
            im = spec.get("image")
            if not im:
                raise ValueError("image on a closing slide")
            a = _aspect(b)
            w, h = min(im["w"], im["h"] * a), min(im["h"], im["w"] / a)
            s.image(b["sha256"], "image", im["x"] + (im["w"] - w) / 2, im["y"] + (im["h"] - h) / 2, w, h)
        elif b["kind"] != "field" or b["role"] not in FIELD_ROLE:
            raise ValueError(f"unknown block {b.get('cls') or b['kind']}")
    if slide["kind"] == "cover":
        s.logo()
    fields = [b for b in slide["blocks"] if b["kind"] == "field"]
    x, w = spec["x"], spec["w"]
    hs = [s.measure(FIELD_ROLE[f["role"]], f["text"].split("\n"), w)[1] for f in fields]
    gaps = [spec["gaps"].get(f["role"], 24) if i else 0 for i, f in enumerate(fields)]
    total = sum(hs) + sum(gaps)
    y = max(spec["top"], (spec["top"] + L.floor) / 2 - total / 2)
    for f, gap in zip(fields, gaps):
        y += gap
        box = s.text(FIELD_ROLE[f["role"]], x, y, w, f["text"])
        y = _bottom(box, L)
    if y > L.floor:
        raise Overflow(f"overflow at cover text: bottom {y:.0f}pt > floor {L.floor:g}pt")


# ------------------------------------------------------------------ entry point

def _check_placed(slide: dict, boxes: list[dict], layout: Layout) -> None:
    """Every visible string of the slide (bar drop-list chrome) must sit in some box."""
    placed = _norm(" ".join(b["text"] for b in boxes))
    skip = set(slide.get("dropped_text") or ())
    for t in slide.get("raw_text") or ():
        if t in skip or t in layout.drop_glyphs:
            continue
        if _norm(t) not in placed:
            raise ValueError(f"text not placed: {t[:40]}")


def _norm(t: str) -> str:
    return " ".join(t.split())


def _plan(s: _Slide, slide: dict, status: str, reason: str | None, notes: str | None) -> dict:
    return {"n": slide["n"], "slide_id": s.slide_id, "html_id": slide.get("id"), "ground": s.ground,
            "notes_text": notes, "status": status, "reason": reason, "requests": s.reqs,
            "boxes": s.boxes, "missing_assets": s.missing}


def compile_slide(slide: dict, layout: Layout, assets: dict, ground: str, logo: bool = True) -> dict:
    n = slide["n"]
    reason = None
    for compact in (False, True):
        s = _Slide(n, ground, layout, assets, logo)
        try:
            for img in [*slide["blocks"], slide.get("background") or {}]:
                if img.get("kind") == "image":
                    _unresolved(img)
            if slide["kind"] in ("cover", "closing"):
                _cover(s, slide)
            else:
                _content(s, slide, compact)
            _check_placed(slide, s.boxes, layout)
            return _plan(s, slide, "native", None, None)
        except Overflow as e:
            reason = str(e)
            if not any(b["kind"] == "rows" for b in slide["blocks"]):
                break
        except ValueError as e:
            reason = str(e)
            break
        except Exception as e:  # never let one slide crash the deck
            reason = repr(e)
            break
    s = _Slide(n, ground, layout, assets)
    s.image(f"fallback:{n}", "fallback", 0, 0, layout.page["w"], layout.page["h"])
    return _plan(s, slide, "fallback", reason, "\n".join(slide.get("raw_text") or []))


def compile_deck(deck: dict, layout: Layout, assets: dict[str, str], ivory: set[int],
                 logo: bool = True) -> list[dict]:
    """One SlidePlan per slide; `ivory` holds 1-based built-deck positions forced to the ivory ground.
    `logo` False leaves the brand logo off every slide."""
    return [compile_slide(sl, layout, assets, "ivory" if sl["n"] in ivory else sl["ground"], logo)
            for sl in deck["slides"]]


def main(argv: list[str] | None = None) -> int:
    import sys

    from .layout import load_layout
    from .parse import parse

    argv = sys.argv[1:] if argv is None else argv
    with open(argv[0], encoding="utf-8") as f:
        deck = parse(f.read())
    for p in compile_deck(deck, load_layout(), {}, set()):
        print(p["n"], p["status"], p["reason"] or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
