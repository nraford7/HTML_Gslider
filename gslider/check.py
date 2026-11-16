"""Check a built deck (spec section 3, Check): coverage, overflow, style audit,
contact sheet and report.md.

check(pid, deck, plans, out_dir) reads the live deck once (`gws.get`), fetches one
LARGE thumbnail per slide, and writes into out_dir:
  thumbs/sNN.png   the thumbnails
  contact.png      4 per row, 400x225 each
  report.md        URL, slide table, coverage misses, style violations, shared assets

Coverage: every `raw_text` string of a slide, less the strings the layout drops on
purpose (the slide's `dropped_text` when parse provides it, else the
layouts.yaml drop selectors applied to the source HTML; lone arrow glyphs always),
must be found in the built slide's text, both sides through `parse.norm`. Strings
are matched longest first, one shape (or the speaker notes) at a time, and each
match is consumed, so a string the source has twice must be built twice; strings
of 3 characters or fewer must match as whole words. A string found in no single
shape is looked for across the remaining text joined (one string split over boxes).
Native table cells count as text, one cell at a time (key `<table id>:<row>:<col>`).
Live slides that no plan names, and planned slides missing from the deck, fail.
A build.json whose status is not "done" fails.

Overflow: each plan box is re-measured with the fonts it was built with
(est_h). est_h > box h flags it. est_h >= 90% of box h sends it to the thumbnail, except a one-line box
(h <= 1.2 line heights) whose line fills less than 90% of the wrap width:
ink (luminance differs from the ground by > 60) in the band just below the
box's text area, inside its x span and clear of other elements, flags it.
Tables are read back instead: the live `tableRows[].rowHeight` total, if taller than
planned, flags a table whose last-row text passes the floor or that runs into an
element planned below it.

Style audit: from the live deck, each text run gives a combo (font, size, colour,
line spacing, space below); role is the objectId less `sNN_` and its trailing
digits, ground is the ground the slide was built on. More than one combo per
(role, ground), or a combo that differs from layouts.yaml, is a violation.
Tables are not audited.

html style (plans with style "html"): coverage ignores case (text-transform is applied
to the built text) and the slide's `dropped_text` is the text left in the plate. The
style audit is skipped (there are no roles). Overflow: every box goes to the
thumbnail, and the band below it is compared with the plate PNG at the same place
(ink = a thumbnail pixel outside the plate's local 3x3 luminance range by more than
INK_DELTA), so the plate's own rules and shapes are not ink. Information only (not
pass/fail): font substitutions, dropped letter-spacing, text left in the plate,
position read-back (live element x/y from `get` against the plan, POS_TOL pt), and a
per-slide image difference between the Slides thumbnail and a Chrome render of the
HTML slide (mean absolute difference, 400x225 greyscale) with `compare.png` beside it.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from gslider import assets, gws
from gslider.compile import CELL_PAD
from gslider.fonts import INSET_PT, WRAP_SLACK, line_height, measure, text_width
from gslider.layout import Layout, load_layout
from gslider.parse import norm

EMU_PER_PT = 12700
ROLE_RE = re.compile(r"^s\d{2,}_(.+?)\d+$")
FULL = 0.9           # boxes at least this full get the pixel check
EST_TOL = 0.5        # pt: estimate above box h by more than this is overflow on its own
INK_DELTA = 60       # luminance difference from the ground that counts as ink
INK_MIN_PX = 4       # ink pixels needed in the scan band
BAND_PAD = 2.0       # pt between the text-area bottom and the scan band
THUMB = (400, 225)
PER_ROW = 4
GUTTER = 8
POS_TOL = 4.0        # pt: html-style boxes whose live x/y moved more than this are listed


@dataclass
class CheckResult:
    coverage_missing: dict = field(default_factory=dict)   # n -> [strings]
    overflow: list = field(default_factory=list)            # (n, object_id, est_h, box_h)
    style_violations: list = field(default_factory=list)    # str
    contact: Path | None = None
    report: Path | None = None
    ok: bool = False
    unconfirmed: list = field(default_factory=list)         # (n, object_id, why) boxes the pixel scan could not see
    dropped: dict = field(default_factory=dict)             # n -> [strings] not required by design
    notes: list = field(default_factory=list)               # caveats for the report
    extra_slides: list = field(default_factory=list)        # live slide ids no plan names
    missing_slides: list = field(default_factory=list)      # planned slide ids not in the deck
    build_status: str | None = None                         # build.json status (None: no build.json)
    style: str = "template"                                 # "html" when the plans are html style
    positions: dict = field(default_factory=dict)           # n -> {boxes, max_dx, max_dy, off, missing}
    image_diff: dict = field(default_factory=dict)          # n -> mean abs difference (0-255)
    compare: Path | None = None                             # thumbnail | Chrome render, per slide


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ live text

def role_of(object_id: str) -> str | None:
    m = ROLE_RE.match(object_id or "")
    return m.group(1) if m else None


def _runs(shape: dict) -> list[dict]:
    """[{content, style, para}] for every text run; para = the paragraph style it sits in."""
    out, para = [], {}
    for el in ((shape.get("text") or {}).get("textElements") or []):
        if "paragraphMarker" in el:
            para = el["paragraphMarker"].get("style") or {}
        elif "textRun" in el:
            out.append({"content": el["textRun"].get("content", ""),
                        "style": el["textRun"].get("style") or {}, "para": para})
    return out


def shape_text(shape: dict) -> str:
    return "".join(r["content"] for r in _runs(shape))


def _text_elements(elements: list[dict]):
    """(objectId, shape) for every shape with text, groups flattened."""
    for el in elements or []:
        if "elementGroup" in el:
            yield from _text_elements(el["elementGroup"].get("children"))
        elif "shape" in el and (el["shape"].get("text") or {}).get("textElements"):
            yield el.get("objectId", ""), el["shape"]


def _table_cells(elements: list[dict]):
    """("<table id>:<row>:<col>", cell) for every table cell with text, groups flattened."""
    for el in elements or []:
        if "elementGroup" in el:
            yield from _table_cells(el["elementGroup"].get("children"))
        elif "table" in el:
            for i, row in enumerate(el["table"].get("tableRows") or []):
                for j, cell in enumerate(row.get("tableCells") or []):
                    loc = cell.get("location") or {}
                    if (cell.get("text") or {}).get("textElements"):
                        yield (f"{el.get('objectId', '')}:{loc.get('rowIndex', i)}:"
                               f"{loc.get('columnIndex', j)}", cell)


def slide_texts(presentation: dict) -> dict[str, dict]:
    """slide objectId -> {"shapes": {objectId (or table cell key): text}, "notes": text}."""
    out = {}
    for sl in presentation.get("slides") or []:
        notes = ((sl.get("slideProperties") or {}).get("notesPage") or {}).get("pageElements")
        els = sl.get("pageElements")
        out[sl.get("objectId")] = {
            "shapes": {**{oid: shape_text(sh) for oid, sh in _text_elements(els)},
                       **{key: shape_text(cell) for key, cell in _table_cells(els)}},
            "notes": "\n".join(shape_text(sh) for _, sh in _text_elements(notes)),
        }
    return out


# ------------------------------------------------------------------ coverage

def _resolve_source(src: str | None) -> Path | None:
    if not src:
        return None
    p = Path(src).expanduser()
    for cand in (p, Path(__file__).resolve().parents[1] / p):
        if cand.is_file():
            return cand
    return None


def _drop_from_html(deck: dict, layout: Layout) -> dict[int, list[str]] | None:
    """n -> strings under the layouts.yaml drop selectors, read from the source HTML."""
    path = _resolve_source(deck.get("source"))
    if path is None:
        return None
    from bs4 import BeautifulSoup, Comment

    from gslider.build import wrap_indexes

    html = path.read_text(encoding="utf-8")
    wraps = BeautifulSoup(html, "html.parser").select(".slide-wrap")
    include_internal = any(s.get("internal") for s in deck.get("slides", []))
    index = wrap_indexes(html, include_internal)
    out = {}
    for s in deck.get("slides", []):
        i = s.get("html_index") or index.get(s["n"])
        if not i or i > len(wraps):
            continue
        slide = wraps[i - 1].select_one(".slide") or wraps[i - 1]
        seen, strings = set(), []
        for sel in layout.drop_selectors:
            for el in slide.select(sel):
                for node in el.find_all(string=True):
                    if isinstance(node, Comment) or id(node) in seen:
                        continue
                    seen.add(id(node))
                    t = norm(str(node))
                    if t:
                        strings.append(t)
        out[s["n"]] = strings
    return out


def dropped_strings(deck: dict, layout: Layout, notes: list | None = None) -> dict[int, list[str]]:
    """n -> raw_text strings the build is not required to carry."""
    from_html = None
    out = {}
    for s in deck.get("slides", []):
        if "dropped_text" in s:
            drop = [norm(t) for t in s.get("dropped_text") or []]
        else:
            if from_html is None:
                from_html = _drop_from_html(deck, layout) or {}
                if not from_html and notes is not None:
                    notes.append("drop selectors not applied: no `dropped_text` in deck.json and the "
                                 "source HTML was not found")
            drop = list(from_html.get(s["n"], []))
        drop += [t for t in s.get("raw_text") or [] if t in layout.drop_glyphs]
        out[s["n"]] = drop
    return out


def coverage(deck: dict, plans: list, presentation: dict, layout: Layout,
             notes: list | None = None, fold: bool = False) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    """(missing, dropped): n -> strings absent from the built slide; n -> strings not required.
    fold: compare case-insensitively (html style applies CSS text-transform)."""
    texts = slide_texts(presentation)
    slide_id = {p["n"]: p["slide_id"] for p in plans}
    dropped = dropped_strings(deck, layout, notes)
    f = (lambda t: norm(t).casefold()) if fold else norm
    missing = {}
    for s in deck.get("slides", []):
        n = s["n"]
        built = texts.get(slide_id.get(n, f"s{n:02d}_slide"))
        original = {}
        for t in s.get("raw_text") or []:
            if norm(t):
                original.setdefault(f(t), norm(t))
        need = Counter(f(t) for t in s.get("raw_text") or [] if norm(t))
        need -= Counter(f(t) for t in dropped.get(n, []))
        if built is None:
            miss = list(need.elements())
        else:
            miss = _match_all(list(need.elements()), [f(x) for x in (*built["shapes"].values(), built["notes"])])
        if miss:
            missing[n] = [original.get(t, t) for t in miss]
    return missing, dropped


def _pattern(t: str) -> re.Pattern:
    """Strings of 3 chars or fewer match as whole words; longer ones as substrings."""
    esc = re.escape(t)
    return re.compile(rf"(?<!\w){esc}(?!\w)" if len(t) <= 3 else esc)


def _match_all(need: list[str], texts: list[str]) -> list[str]:
    """Strings of `need` not found in `texts`; each match is consumed (blanked) once used."""
    pool = [norm(t) for t in texts]
    miss = []
    for t in sorted(need, key=len, reverse=True):
        pat = _pattern(t)
        for i, text in enumerate(pool):
            m = pat.search(text)
            if m:
                pool[i] = text[:m.start()] + "\x00" + text[m.end():]
                break
        else:
            joined = " ".join(pool)
            m = pat.search(joined)
            if m is None:
                miss.append(t)
                continue
            pool = [joined[:m.start()] + "\x00" + joined[m.end():]]
    order = {t: i for i, t in enumerate(need)}
    return sorted(miss, key=lambda t: order[t])


# ------------------------------------------------------------------ overflow

def _bullet_start(plan: dict, object_id: str) -> int | None:
    for r in plan.get("requests", []):
        b = r.get("createParagraphBullets")
        if b and b.get("objectId") == object_id:
            return b["textRange"].get("startIndex", 0)
    return None


def _split_u16(text: str, index: int) -> tuple[str, str]:
    units = 0
    for i, ch in enumerate(text):
        if units >= index:
            return text[:i], text[i:]
        units += len(ch.encode("utf-16-le")) // 2
    return text, ""


def estimate_h(box: dict, text: str, layout: Layout, bullet_start: int | None = None) -> float:
    """Text-area height of `text` in `box`, measured the way compile sizes boxes."""
    st = layout.styles.get(box["role"], {}).get(box.get("ground", "dark"))
    if st is None:
        return box["h"]
    text = text.rstrip("\n")
    if bullet_start is None:
        paras, bullets = text.split("\n"), []
    else:
        head, tail = _split_u16(text, bullet_start)
        paras = head.rstrip("\n").split("\n") if head.strip("\n") else []
        bullets = tail.split("\n") if tail else []
    lines = 0
    if paras:
        lines += measure("\n".join(paras), st["font"], st["size"], box["w"], st["line_spacing"])[0]
    if bullets:
        lines += measure("\n".join(bullets), st["font"], st["size"], box["w"] - layout.bullet_indent,
                         st["line_spacing"])[0]
    lh = line_height(st["font"], st["size"], st["line_spacing"])
    return lines * lh + (len(paras) + len(bullets)) * st["space_after"]


def _short_single_line(box: dict, text: str, layout: Layout) -> bool:
    """A one-line box (h <= 1.2 line heights) whose line fills < FULL of the wrap width."""
    st = layout.styles.get(box["role"], {}).get(box.get("ground", "dark"))
    if st is None or "\n" in text.strip("\n"):
        return False
    if box["h"] > 1.2 * line_height(st["font"], st["size"], st["line_spacing"]) + st["space_after"]:
        return False
    avail = (box["w"] - 2 * INSET_PT) / WRAP_SLACK
    return text_width(text.strip(), st["font"], st["size"]) < FULL * avail


def _pt(v) -> float:
    return (v or 0) / EMU_PER_PT


def obstacles(plan: dict, layout: Layout, skip: str) -> list[tuple[float, float, float, float]]:
    """(x0, y0, x1, y1) in pt of everything drawn on the slide except `skip`.

    Text boxes count by their text area (insets are blank), tables by their whole
    rectangle; full-page fallback images are left out (the fallback slide has no boxes
    to scan)."""
    boxes = {b["object_id"]: b for b in plan.get("boxes", [])}
    ins = layout.inset
    out = []
    for r in plan.get("requests", []):
        (kind, body), = r.items()
        if kind not in ("createShape", "createLine", "createImage", "createTable") or body.get("objectId") == skip:
            continue
        oid = body["objectId"]
        if oid.endswith("_plate"):
            continue  # html style: the full-page plate is compared against, not avoided
        if oid in boxes and boxes[oid]["role"] == "table":
            b = boxes[oid]
            out.append((b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]))
            continue
        if oid in boxes:
            b = boxes[oid]
            out.append((b["x"] + ins, b["y"] + ins, b["x"] + b["w"] - ins, b["y"] + ins + b["h"]))
            continue
        ep = body.get("elementProperties") or {}
        tr, sz = ep.get("transform") or {}, ep.get("size") or {}
        x, y = _pt(tr.get("translateX")), _pt(tr.get("translateY"))
        w = _pt((sz.get("width") or {}).get("magnitude")) * (tr.get("scaleX") or 1)
        h = _pt((sz.get("height") or {}).get("magnitude")) * (tr.get("scaleY") or 1)
        if kind == "createLine":
            out.append((x, y - 1.5, x + w, y + 1.5))
        else:
            out.append((x, y, x + w, y + h))
    return out


def scan_band(box: dict, plan: dict, layout: Layout) -> tuple[float, float, float, float] | None:
    """The clear strip (x0, y0, x1, y1) pt just below the box's text area, or None."""
    ins = layout.inset
    st = layout.styles.get(box["role"], {}).get(box.get("ground", "dark"))
    depth = box.get("line_pt") or (line_height(st["font"], st["size"], st["line_spacing"]) if st else 14.0)
    x0, x1 = box["x"] + ins, box["x"] + box["w"] - ins
    y0 = box["y"] + ins + box["h"] + BAND_PAD
    y1 = min(y0 + depth, layout.page["h"])
    for ox0, oy0, ox1, oy1 in obstacles(plan, layout, box["object_id"]):
        if ox1 <= x0 or ox0 >= x1 or oy1 <= y0:
            continue
        if oy0 <= y0:
            return None  # something already sits at the band's top edge
        y1 = min(y1, oy0 - 1)
    return (x0, y0, x1, y1) if y1 - y0 >= 2 else None


def _lum(rgb) -> float:
    r, g, b = rgb[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


def ink_below(thumb: Image.Image, band: tuple, ground_hex: str, page_w: float) -> int:
    """Pixels in band (pt) whose luminance differs from the ground by more than INK_DELTA."""
    s = thumb.width / page_w
    h = ground_hex.lstrip("#")
    g = _lum(tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)))
    x0, y0, x1, y1 = (round(v * s) for v in band)
    region = thumb.convert("RGB").crop((x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)))
    return sum(1 for px in region.getdata() if abs(_lum(px) - g) > INK_DELTA)


def _lum_image(img: Image.Image) -> Image.Image:
    return img.convert("RGB").convert("L", (0.299, 0.587, 0.114, 0))


def ink_vs_plate(thumb: Image.Image, plate: Image.Image, band: tuple, page_w: float) -> int:
    """Pixels in band (pt) whose luminance lies outside the plate's 3x3 neighbourhood range
    (at the thumbnail's scale) by more than INK_DELTA: drawn on the slide, not in the plate."""
    s = thumb.width / page_w
    x0, y0, x1, y1 = (round(v * s) for v in band)
    box = (x0, y0, max(x1, x0 + 1), max(y1, y0 + 1))
    t = _lum_image(thumb).crop(box)
    pl = _lum_image(plate).resize(thumb.size, Image.BILINEAR)
    lo = pl.filter(ImageFilter.MinFilter(3)).crop(box)
    hi = pl.filter(ImageFilter.MaxFilter(3)).crop(box)
    return sum(1 for v, a, b in zip(t.getdata(), lo.getdata(), hi.getdata())
               if v < a - INK_DELTA or v > b + INK_DELTA)


def _overflow_html(p: dict, layout: Layout, thumbs: dict, flags: list, unconfirmed: list) -> None:
    path = (thumbs or {}).get(p["n"])
    if path is None:
        return  # no thumbnail: html boxes have no estimate to fall back on
    plate = p.get("plate_png")
    if not plate or not Path(plate).exists():
        unconfirmed += [(p["n"], b["object_id"], "no plate image to compare") for b in p.get("boxes", [])]
        return
    thumb, plate_img = Image.open(path), Image.open(plate)
    for box in p.get("boxes", []):
        band = scan_band(box, p, layout)
        if band is None:
            unconfirmed.append((p["n"], box["object_id"], "no clear strip below the box"))
            continue
        if ink_vs_plate(thumb, plate_img, band, layout.page["w"]) >= INK_MIN_PX:
            flags.append((p["n"], box["object_id"], round(box["h"], 1), round(box["h"], 1)))


def overflow(plans: list, layout: Layout, live: dict | None = None,
             thumbs: dict[int, Path] | None = None) -> tuple[list, list]:
    """(flags, unconfirmed). flags: (n, object_id, est_h, box_h)."""
    flags, unconfirmed = [], []
    for p in plans:
        if p.get("status") != "native":
            continue
        if p.get("style") == "html":
            _overflow_html(p, layout, thumbs, flags, unconfirmed)
            continue
        shapes = ((live or {}).get(p["slide_id"]) or {}).get("shapes", {})
        img = None
        for box in p.get("boxes", []):
            if box["role"] == "table":
                continue  # read back from live row heights (table_overflow)
            oid = box["object_id"]
            text = shapes.get(oid, box["text"])
            est = estimate_h(box, text, layout, _bullet_start(p, oid))
            h = box["h"]
            if est > h + EST_TOL:
                flags.append((p["n"], oid, round(est, 1), round(h, 1)))
                continue
            if h <= 0 or est < FULL * h:
                continue
            if _short_single_line(box, text, layout):
                continue  # one line well inside its width: Slides will not wrap it
            path = (thumbs or {}).get(p["n"])
            if path is None:
                continue  # no thumbnail (offline): the estimate is all there is
            band = scan_band(box, p, layout)
            if band is None:
                unconfirmed.append((p["n"], oid, "no clear strip below the box"))
                continue
            if img is None:
                img = Image.open(path)
            ink = ink_below(img, band, layout.grounds[box.get("ground") or p["ground"]], layout.page["w"])
            if ink >= INK_MIN_PX:
                flags.append((p["n"], oid, round(est, 1), round(h, 1)))
    return flags, unconfirmed


def _dim_pt(d: dict | None) -> float:
    d = d or {}
    return (d.get("magnitude") or 0) * (1 if d.get("unit") == "PT" else 1 / EMU_PER_PT)


def _live_tables(elements: list[dict]):
    for el in elements or []:
        if "elementGroup" in el:
            yield from _live_tables(el["elementGroup"].get("children"))
        elif "table" in el:
            yield el.get("objectId"), el


def table_overflow(plans: list, presentation: dict, layout: Layout) -> list:
    """(n, object_id, live_h, planned_h) for tables that grew on the live slide past the floor
    (last-row text below it) or into an element planned below the table."""
    live = {sl.get("objectId"): dict(_live_tables(sl.get("pageElements")))
            for sl in presentation.get("slides") or []}
    flags = []
    for p in plans:
        if p.get("status") != "native":
            continue
        tables = live.get(p["slide_id"]) or {}
        for box in p.get("boxes", []):
            el = tables.get(box["object_id"]) if box["role"] == "table" else None
            if el is None:
                continue
            h = sum(_dim_pt(r.get("rowHeight")) for r in el["table"].get("tableRows") or [])
            if h <= box["h"] + EST_TOL:
                continue
            x0, y0, x1 = box["x"], box["y"], box["x"] + box["w"]
            grown = y0 + h
            past_floor = grown - CELL_PAD > layout.floor + EST_TOL
            hits = [o for o in obstacles(p, layout, box["object_id"])
                    if o[0] < x1 and o[2] > x0 and o[1] >= y0 + box["h"] - EST_TOL and o[1] < grown]
            if past_floor or hits:
                flags.append((p["n"], box["object_id"], round(h, 1), round(box["h"], 1)))
    return flags


# ------------------------------------------------------------------ style audit

def _hex(rgb: dict) -> str:
    return "#" + "".join(f"{round((rgb.get(k) or 0) * 255):02X}" for k in ("red", "green", "blue"))


def _combo(run: dict) -> tuple:
    st, para = run["style"], run["para"]
    font = st.get("fontFamily") or (st.get("weightedFontFamily") or {}).get("fontFamily")
    size = (st.get("fontSize") or {}).get("magnitude")
    rgb = (((st.get("foregroundColor") or {}).get("opaqueColor") or {}).get("rgbColor"))
    colour = _hex(rgb) if rgb is not None else None
    below = (para.get("spaceBelow") or {}).get("magnitude", 0)
    return (font, size, colour, para.get("lineSpacing"), below)


def _fmt(c: tuple) -> str:
    font, size, colour, spacing, below = c
    return f"{font} {size}pt {colour} {spacing}% +{below}pt"


def style_audit(presentation: dict, plans: list, deck: dict, layout: Layout) -> list[str]:
    ground = {s["n"]: s.get("ground") for s in deck.get("slides", [])}
    by_slide = {p["slide_id"]: (p["n"], p.get("ground") or ground.get(p["n"])) for p in plans}
    seen: dict[tuple, dict[tuple, list[str]]] = {}
    for sl in presentation.get("slides") or []:
        n, gr = by_slide.get(sl.get("objectId"), (None, None))
        if n is None:
            m = re.match(r"^s(\d{2,})_", sl.get("objectId", ""))
            n = int(m.group(1)) if m else None
            gr = ground.get(n)
        for oid, shape in _text_elements(sl.get("pageElements")):
            role = role_of(oid)
            if role is None:
                continue
            for run in _runs(shape):
                if not run["content"].strip():
                    continue
                seen.setdefault((role, gr), {}).setdefault(_combo(run), []).append(oid)
    out = []
    for (role, gr), combos in sorted(seen.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        want = (layout.styles.get(role) or {}).get(gr)
        exp = (want["font"], want["size"], want["color"].upper(), want["line_spacing"], want["space_after"]) \
            if want else None
        if len(combos) > 1:
            parts = "; ".join(f"{_fmt(c)} on {', '.join(sorted(set(o))[:4])}" for c, o in combos.items())
            out.append(f"{role}/{gr}: {len(combos)} combos ({parts})")
        if exp:
            for c, o in combos.items():
                if (c[0], c[1], c[2], c[3], c[4]) != exp:
                    out.append(f"{role}/{gr}: {_fmt(c)} differs from layouts.yaml {_fmt(exp)} "
                               f"on {', '.join(sorted(set(o))[:4])}")
    return out


# ------------------------------------------------------------------ thumbnails

def fetch_thumbs(pid: str, page_ids: list[tuple[int, str]], out_dir: Path) -> dict[int, Path]:
    """n -> LARGE thumbnail PNG (gws backs off on 429). Failures are logged and skipped."""
    tdir = out_dir / "thumbs"
    tdir.mkdir(parents=True, exist_ok=True)

    def one(item):
        n, page_id = item
        path = tdir / f"s{n:02d}.png"
        try:
            gws.thumbnail(pid, page_id, path)
            Image.open(path).verify()
            return n, path
        except Exception as e:  # noqa: BLE001 (one bad slide must not stop the sheet)
            _log(f"thumbnail slide {n} failed: {e}")
            return n, None

    with ThreadPoolExecutor(max_workers=2) as ex:
        return {n: p for n, p in ex.map(one, page_ids) if p is not None}


def contact_sheet(thumbs: dict[int, Path], out: Path, total: int | None = None) -> Path:
    ns = list(range(1, (total or max(thumbs, default=0)) + 1)) or sorted(thumbs)
    rows = max(1, -(-len(ns) // PER_ROW))
    W = PER_ROW * THUMB[0] + (PER_ROW + 1) * GUTTER
    H = rows * THUMB[1] + (rows + 1) * GUTTER
    sheet = Image.new("RGB", (W, H), (96, 96, 96))
    draw = ImageDraw.Draw(sheet)
    for i, n in enumerate(ns):
        x = GUTTER + (i % PER_ROW) * (THUMB[0] + GUTTER)
        y = GUTTER + (i // PER_ROW) * (THUMB[1] + GUTTER)
        if n in thumbs:
            sheet.paste(Image.open(thumbs[n]).convert("RGB").resize(THUMB, Image.LANCZOS), (x, y))
        else:
            draw.rectangle((x, y, x + THUMB[0] - 1, y + THUMB[1] - 1), fill=(160, 40, 40))
            draw.text((x + 8, y + THUMB[1] // 2), "no thumbnail", fill=(255, 255, 255))
        by = y + THUMB[1] - 14  # bottom-left: the top-left corner holds the section kicker
        draw.rectangle((x, by, x + 26, by + 14), fill=(0, 0, 0))
        draw.text((x + 4, by + 2), f"{n:02d}", fill=(255, 255, 255))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


# ------------------------------------------------------------------ html style: fidelity

def _live_positions(presentation: dict) -> dict[str, dict[str, tuple[float, float]]]:
    """slide id -> {objectId: (x, y) pt} for every page element, groups flattened."""
    def walk(elements, out):
        for el in elements or []:
            if "elementGroup" in el:
                walk(el["elementGroup"].get("children"), out)
            tr = el.get("transform") or {}
            k = 1 if tr.get("unit") == "PT" else 1 / EMU_PER_PT
            out[el.get("objectId")] = ((tr.get("translateX") or 0) * k, (tr.get("translateY") or 0) * k)
        return out
    return {sl.get("objectId"): walk(sl.get("pageElements"), {}) for sl in presentation.get("slides") or []}


def position_readback(plans: list, presentation: dict) -> dict[int, dict]:
    """n -> {boxes, max_dx, max_dy, off: [ids moved > POS_TOL pt], missing: [ids not in the deck]}
    for native html-style slides: live element x/y against the planned (measured) x/y."""
    live = _live_positions(presentation)
    out = {}
    for p in plans:
        if p.get("style") != "html" or p.get("status") != "native" or not p.get("boxes"):
            continue
        els = live.get(p["slide_id"]) or {}
        dx = dy = 0.0
        off, missing = [], []
        for b in p["boxes"]:
            if b["object_id"] not in els:
                missing.append(b["object_id"])
                continue
            x, y = els[b["object_id"]]
            ddx, ddy = abs(x - b["x"]), abs(y - b["y"])
            dx, dy = max(dx, ddx), max(dy, ddy)
            if max(ddx, ddy) > POS_TOL:
                off.append(b["object_id"])
        out[p["n"]] = {"boxes": len(p["boxes"]), "max_dx": round(dx, 1), "max_dy": round(dy, 1),
                       "off": off, "missing": missing}
    return out


def image_diff(a: Path, b: Path) -> float:
    """Mean absolute difference of two slide images at 400x225 greyscale (0-255)."""
    ga = Image.open(a).convert("L").resize(THUMB, Image.LANCZOS)
    gb = Image.open(b).convert("L").resize(THUMB, Image.LANCZOS)
    return round(sum(abs(x - y) for x, y in zip(ga.getdata(), gb.getdata())) / (THUMB[0] * THUMB[1]), 1)


def render_html_slides(deck: dict, plans: list, out_dir: Path, notes: list) -> dict[int, Path]:
    """n -> Chrome render of the HTML slide (3 at a time), for the image difference."""
    src = _resolve_source(deck.get("source"))
    if src is None or not assets.CHROME:
        notes.append("image difference not measured: " + ("source HTML not found" if src is None
                                                          else "Chrome not found"))
        return {}
    index = {s["n"]: s.get("html_index") or s["n"] for s in deck.get("slides", [])}
    rdir = out_dir / "renders"

    def one(n):
        try:
            return n, assets.render_slide_png(src, index.get(n, n), rdir / f"s{n:02d}.png")
        except Exception as e:  # noqa: BLE001 (one slide must not stop the check)
            _log(f"render slide {n} failed: {e}")
            return n, None

    with ThreadPoolExecutor(max_workers=3) as ex:
        return {n: p for n, p in ex.map(one, [p["n"] for p in plans]) if p is not None}


def compare_sheet(thumbs: dict[int, Path], renders: dict[int, Path], out: Path, total: int) -> Path:
    """One row per slide: the Slides thumbnail (left) beside the Chrome render (right)."""
    W = 2 * THUMB[0] + 3 * GUTTER
    head = 18
    H = head + total * (THUMB[1] + GUTTER) + GUTTER
    sheet = Image.new("RGB", (W, H), (96, 96, 96))
    draw = ImageDraw.Draw(sheet)
    draw.text((GUTTER, 4), "Google Slides", fill=(255, 255, 255))
    draw.text((2 * GUTTER + THUMB[0], 4), "HTML (Chrome)", fill=(255, 255, 255))
    for i, n in enumerate(range(1, total + 1)):
        y = head + GUTTER + i * (THUMB[1] + GUTTER)
        for j, src in enumerate((thumbs, renders)):
            x = GUTTER + j * (THUMB[0] + GUTTER)
            if n in src:
                sheet.paste(Image.open(src[n]).convert("RGB").resize(THUMB, Image.LANCZOS), (x, y))
            else:
                draw.rectangle((x, y, x + THUMB[0] - 1, y + THUMB[1] - 1), fill=(160, 40, 40))
                draw.text((x + 8, y + THUMB[1] // 2), "missing", fill=(255, 255, 255))
        by = y + THUMB[1] - 14
        draw.rectangle((GUTTER, by, GUTTER + 26, by + 14), fill=(0, 0, 0))
        draw.text((GUTTER + 4, by + 2), f"{n:02d}", fill=(255, 255, 255))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


def _html_report(plans: list, res: CheckResult) -> list[str]:
    L = []
    subs: dict[tuple, list[int]] = {}
    spacing, plate, hidden = [], [], []
    for p in plans:
        info = p.get("html") or {}
        for sub in info.get("substitutions") or []:
            subs.setdefault((sub["family"], sub["used"]), []).append(p["n"])
        spacing += [(p["n"], x) for x in info.get("letter_spacing") or []]
        plate += [(p["n"], x) for x in info.get("left_in_plate") or []]
        hidden += [(p["n"], x) for x in info.get("hidden") or []]
    L += ["", "## Font substitutions", ""]
    L += [f"- `{fam}` -> {used} (slides {', '.join(map(str, ns))}): Google Fonts does not have it"
          for (fam, used), ns in subs.items()] or ["None."]
    L += ["", "## Letter-spacing dropped", "", "Slides has no letter-spacing; these boxes are set without it.", ""]
    L += [f"- slide {n} `{x['object_id']}` {x['px']}px: {_cell(x['text'])}" for n, x in spacing] or ["None."]
    L += ["", "## Left in plate", "", "This text is part of the slide picture, not editable "
          "(rotated, clipped, or inside SVG).", ""]
    L += [f"- slide {n}: `{_cell(x['text'][:80])}` ({_cell(x['reason'])})" for n, x in plate] or ["None."]
    L += ["", "## Hidden in the HTML", "", "In the HTML source but not drawn by Chrome (for example "
          "`display:none`), so not built and not required.", ""]
    by_text: dict[str, list[int]] = {}
    for n, t in hidden:
        by_text.setdefault(t, []).append(n)
    L += [f"- `{_cell(t[:80])}` (slides {', '.join(map(str, ns))})" for t, ns in by_text.items()] or ["None."]
    L += ["", "## Position read-back", "",
          f"Live text-box x/y against the measured position (information; boxes over {POS_TOL:g}pt are named).",
          "", "| n | boxes | max dx pt | max dy pt | over tolerance | missing |", "|---|---|---|---|---|---|"]
    for n, r in sorted(res.positions.items()):
        L.append(f"| {n} | {r['boxes']} | {r['max_dx']} | {r['max_dy']} | {_cell(', '.join(r['off']))} | "
                 f"{_cell(', '.join(r['missing']))} |")
    L += ["", "## Image difference", "",
          "Mean absolute difference between the Slides thumbnail and the Chrome render, 400x225 greyscale, "
          "0-255 (information, not pass/fail). Side by side: "
          f"{res.compare.name if res.compare else 'not made'}.", ""]
    if res.image_diff:
        L += ["| n | difference |", "|---|---|", *[f"| {n} | {d} |" for n, d in sorted(res.image_diff.items())]]
    else:
        L.append("Not measured.")
    return L


# ------------------------------------------------------------------ report

def _shared_assets(meta: dict) -> list[str]:
    v = meta.get("shared_assets")
    return [str(x) for x in v] if isinstance(v, list) else []


def _sharing_report(meta: dict) -> list[str]:
    """Asset files shared during the build, and whether the link was removed afterwards."""
    shared = _shared_assets(meta)
    L = ["", "## Asset files shared during the build", ""]
    revoked = meta.get("revoked")
    failed = [f for f in (meta.get("revoke_failed") or []) if isinstance(f, dict)]
    if not isinstance(revoked, list):
        L.append("build.json does not record removing the links (an older build): these files may still be "
                 "shared with anyone who has the link. Run `gslider unshare-assets`.")
        revoked = []
    elif shared:
        L.append(f"Link sharing was removed after the build from {len(revoked)} of {len(shared)} files.")
    gone = set(map(str, revoked))
    bad = {str(f.get("file")) for f in failed}
    L += [f"- {s}" + (" (link removed)" if s in gone else " (STILL SHARED by link)" if s in bad or revoked else "")
          for s in shared] or ["Not listed in build.json."]
    plates = meta.get("plates") if isinstance(meta.get("plates"), list) else []
    if plates:
        L += ["", f"Plate pictures among them: {', '.join(map(str, plates))}"]
    if failed:
        L += ["", "### Sharing not removed", "",
              *[f"- {f.get('file')}: {_cell(str(f.get('error') or ''))}" for f in failed],
              "", "Run `gslider unshare-assets` to remove these links."]
    return L


def _cell(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def write_report(path: Path, pid: str, deck: dict, plans: list, res: CheckResult, meta: dict) -> Path:
    url = meta.get("url") or f"https://docs.google.com/presentation/d/{pid}/edit"
    kinds = {s["n"]: (s.get("kind"), s.get("id")) for s in deck.get("slides", [])}
    flags: dict[int, list[str]] = {}
    for n, oid, est, h in res.overflow:
        flags.setdefault(n, []).append(f"{oid} {est}>{h}pt" if est > h else f"{oid} ink below")
    native = sum(p["status"] == "native" for p in plans)
    L = [f"# gslider check: {meta.get('title') or deck.get('title') or pid}", "",
         f"- URL: {url}",
         f"- Source: {deck.get('source') or meta.get('source') or 'n/a'}",
         f"- Slides: {len(plans)}, native {native}, fallback {len(plans) - native}",
         f"- Build status: {res.build_status or 'no build.json'}",
         f"- Result: {'PASS' if res.ok else 'FAIL'} (coverage misses {sum(map(len, res.coverage_missing.values()))}, "
         f"overflow {len(res.overflow)}, style violations {len(res.style_violations)}, "
         f"extra slides {len(res.extra_slides)}, missing slides {len(res.missing_slides)})",
         f"- Contact sheet: {res.contact.name if res.contact else 'none'}", "",
         "## Slides", "", "| n | type | build | reason | overflow |", "|---|---|---|---|---|"]
    for p in plans:
        kind, hid = kinds.get(p["n"], (None, None))
        typ = f"{kind or ''}{f' ({hid})' if hid else ''}"
        L.append(f"| {p['n']} | {_cell(typ)} | {p['status']} | {_cell(p.get('reason') or '')} | "
                 f"{_cell('; '.join(flags.get(p['n'], [])))} |")
    if res.build_status not in (None, "done"):
        L += ["", "## Build not finished", "",
              f"build.json status is `{res.build_status}`: {_cell(str(meta.get('error') or 'no error recorded'))}"]
    if res.extra_slides or res.missing_slides:
        L += ["", "## Slide count", ""]
        L += [f"- live slide not in plans.json: `{x}`" for x in res.extra_slides]
        L += [f"- planned slide missing from the deck: `{x}`" for x in res.missing_slides]
    L += ["", "## Coverage misses", ""]
    L += [f"- slide {n}: " + "; ".join(f"`{t}`" for t in miss) for n, miss in sorted(res.coverage_missing.items())] \
        or ["None."]
    L += ["", "## Style violations", ""]
    if res.style == "html":
        L.append("Style audit skipped: html style keeps the HTML's own styles (no roles).")
    else:
        L += [f"- {v}" for v in res.style_violations] or ["None."]
    if res.unconfirmed:
        L += ["", "## Overflow not pixel-checked", "",
              *[f"- slide {n} {oid}: {why}" for n, oid, why in res.unconfirmed]]
    fb = [p["n"] for p in plans if p["status"] == "fallback"]
    if fb:
        L += ["", "## Fallback slides", "",
              f"Slides {', '.join(map(str, fb))} are images of the HTML slide, not native Slides objects:",
              *([] if res.style == "html" else [
                  "- They look like the HTML, not like the native slides around them (kicker case, rules, "
                  "page-number position and fonts follow the HTML)."]),
              "- Their text is not editable in Slides; it is in each slide's speaker notes.",
              "- Coverage for them is checked against the speaker notes; overflow is not checked."]
    if res.style == "html":
        L += _html_report(plans, res)
    else:
        drop = sorted({t for v in res.dropped.values() for t in v})
        L += ["", "## Dropped by design", "", (", ".join(f"`{t}`" for t in drop) if drop else "None.")]
    L += _sharing_report(meta)
    if res.notes:
        L += ["", "## Notes", "", *[f"- {x}" for x in res.notes]]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------ entry point

def check(pid: str, deck: dict, plans: list, out_dir: Path, *, thumbnails: bool = True,
          layout: Layout | None = None) -> CheckResult:
    out_dir = Path(out_dir)
    layout = layout or load_layout()
    res = CheckResult()
    presentation = gws.get(pid)
    live = slide_texts(presentation)

    html = any(p.get("style") == "html" for p in plans)
    res.style = "html" if html else "template"
    res.coverage_missing, res.dropped = coverage(deck, plans, presentation, layout, res.notes, fold=html)
    res.style_violations = [] if html else style_audit(presentation, plans, deck, layout)

    live_ids = [sl.get("objectId") for sl in presentation.get("slides") or []]
    planned = [p["slide_id"] for p in plans]
    res.extra_slides = [i for i in live_ids if i not in planned]
    res.missing_slides = [i for i in planned if i not in live_ids]

    thumbs: dict[int, Path] = {}
    if thumbnails:
        ids = {sl.get("objectId") for sl in presentation.get("slides") or []}
        pages = [(p["n"], p["slide_id"]) for p in plans if p["slide_id"] in ids]
        thumbs = fetch_thumbs(pid, pages, out_dir)
        if len(thumbs) < len(plans):
            res.notes.append(f"{len(plans) - len(thumbs)} slide thumbnail(s) missing; "
                             "their boxes were checked by estimate only")
        res.contact = contact_sheet(thumbs, out_dir / "contact.png", len(plans))
    res.overflow, res.unconfirmed = overflow(plans, layout, live, thumbs)
    res.overflow += table_overflow(plans, presentation, layout)
    if html:
        res.positions = position_readback(plans, presentation)
        if thumbnails:
            renders = render_html_slides(deck, plans, out_dir, res.notes)
            res.image_diff = {n: image_diff(thumbs[n], renders[n]) for n in sorted(thumbs) if n in renders}
            if renders:
                res.compare = compare_sheet(thumbs, renders, out_dir / "compare.png", len(plans))

    meta, has_meta = {}, (out_dir / "build.json").exists()
    try:
        meta = json.loads((out_dir / "build.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    if has_meta:
        res.build_status = str(meta.get("status") or "unknown")
    res.ok = (not res.coverage_missing and not res.overflow and not res.style_violations
              and not res.extra_slides and not res.missing_slides and res.build_status in (None, "done"))
    res.report = write_report(out_dir / "report.md", pid, deck, plans, res, meta)
    return res
