"""HTML deck -> deck dict (slides made of blocks). Spec section 1 (Parse).

Block kinds (fields):
  section{text} title{text} lead{text} note{text} quote{text}
  cells{cells:[{num, header, body:[str], bullets:[str], value}]}
  rows{header:[str]|None, rows:[[str]], style:"table"|"timeline", widths:[float|None],
       bold:[[[[start, length]]]] (row -> cell -> ranges), header_bold:[[[start, length]]]|None}
  band{label, text}
  image{sha256, mime, data:bytes}
  unknown{cls, text}            (images that cannot be placed natively are unknown blocks:
                                 cls "external-image <src>" with text = src, or
                                 cls "bad-image: malformed data URI" with that text, or
                                 cls "inline-image <mime>" for a data image inside a cell)
  field{role, text}            (cover / closing slides only)
Optional extras: `width` (0.6 for a `w60` class) on any block; `bold` on a text
block or band: [(start, length)] ranges on its `text`; on a cell: {field: ranges}
where field is num/header/value/body/bullets and ranges index "\n".join(field).
Multi-line text uses "\n"; <br> and block-element boundaries start a new line.
Slide extras: `html_index` (1-based .slide-wrap position, internal slides counted),
`raw_text` (every visible string) and `dropped_text` (strings inside drop selectors).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sys
import unicodedata
import urllib.parse
from itertools import zip_longest

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .layout import load_layout

INTERNAL_IDS = {"version-note"}

CELL_CLS = {"col", "panel", "tcard", "fee", "lp", "stat", "mini-stat", "st", "bs", "vn"}
WRAPPER_CLS = {"cols-2x", "cols-3x", "cols-4x", "loop", "strip", "stats", "fees", "bar", "vn-grid"}
GRID_CLS = {"stack", "row2"}
LEAD_CLS = {"lead"}
NOTE_CLS = {"small", "src", "note", "loop-back"}
QUOTE_CLS = {"quote", "qq"}
IMAGE_CLS = {"side-img", "cv-img"}
SKIP_CLS = {"lp-arrow", "title-rule", "tl-dot"}

NUM_CLS = ("c-num", "st-k", "tl-k")
HEADER_CLS = ("c-title", "stat-label", "st-t")
VALUE_CLS = ("stat-val", "fee-v")
MAX_CELLS = 4

_URL = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^'")\s]*))\s*\)""")
_DATA = re.compile(r"data:([^;,]+)((?:;[^;,]+)*),(.*)", re.S | re.I)
BAD_IMAGE = "malformed data URI"


# ---------------------------------------------------------------- text

def norm(s: str) -> str:
    """NFKC, &nbsp; -> space, curly quotes kept, whitespace collapsed, stripped."""
    s = s.replace("&nbsp;", " ")
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s).strip()


def _classes(el) -> set[str]:
    return set(el.get("class") or []) if isinstance(el, Tag) else set()


BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead",
              "tbody", "tr", "td", "th", "blockquote", "section", "article", "header", "footer",
              "figure", "figcaption", "dl", "dt", "dd", "aside", "nav", "main", "hr", "pre"}
INLINE_TAGS = {"b", "strong", "i", "em", "span", "a", "br", "small", "sup", "sub", "u", "mark",
               "code", "abbr", "cite", "q", "s", "time"}

Rich = tuple[str, list[tuple[int, int]]]  # text ("\n" between lines), bold (start, length) ranges


def _rich(nodes, skip=()) -> Rich:
    """Visible text of nodes as normalised lines plus bold ranges on the joined text.

    <br> and block-element boundaries start a new line; elements in `skip` (and SKIP_CLS
    decorations) are left out.
    """
    chars: list[tuple[str, bool]] = []

    def walk(node, bold: bool) -> None:
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            if node.parent is not None and node.parent.name in ("script", "style"):
                return
            t = unicodedata.normalize("NFKC", str(node).replace("&nbsp;", " "))
            chars.extend((ch, bold) for ch in t)
            return
        if not isinstance(node, Tag) or node.name in ("script", "style"):
            return
        if any(node is k for k in skip) or _classes(node) & SKIP_CLS:
            return
        if node.name == "br":
            chars.append(("\n", False))
            return
        block = node.name in BLOCK_TAGS
        if block:
            chars.append(("\n", False))
        inner = bold or node.name in ("b", "strong")
        for c in node.children:
            walk(c, inner)
        if block:
            chars.append(("\n", False))

    for n in nodes:
        walk(n, False)
    lines: list[list[tuple[str, bool]]] = [[]]
    for ch, b in chars:
        if ch == "\n":
            lines.append([])
        else:
            lines[-1].append((ch, b))
    out: list[tuple[str, bool]] = []
    for ln in lines:
        coll: list[tuple[str, bool]] = []
        for ch, b in ln:
            if ch.isspace():
                if coll and coll[-1][0] == " ":
                    coll[-1] = (" ", coll[-1][1] and b)
                else:
                    coll.append((" ", b))
            else:
                coll.append((ch, b))
        while coll and coll[0][0] == " ":
            coll.pop(0)
        while coll and coll[-1][0] == " ":
            coll.pop()
        if coll:
            if out:
                out.append(("\n", False))
            out += coll
    text = "".join(ch for ch, _ in out)
    ranges: list[tuple[int, int]] = []
    i = 0
    while i < len(out):
        if out[i][1] and out[i][0] not in " \n":
            j = i
            while j < len(out) and out[j][1] and out[j][0] != "\n":
                j += 1
            k = j
            while text[k - 1] == " ":
                k -= 1
            ranges.append((i, k - i))
            i = j
        else:
            i += 1
    return text, ranges


def _cat(parts: list[Rich], sep: str = "\n") -> Rich:
    """Join rich parts (empty ones dropped) with sep, shifting their ranges."""
    text, ranges = "", []
    for t, rs in parts:
        if not t:
            continue
        if text:
            text += sep
        ranges += [(s + len(text), n) for s, n in rs]
        text += t
    return text, ranges


def _lines(el) -> list[str]:
    """Visible text of el as normalised lines; <br> and block boundaries start a new line."""
    t = _rich([el])[0]
    return t.split("\n") if t else []


def _text(el) -> str:
    return _rich([el])[0]


def _text_block(kind: str, el) -> dict:
    return _rich_block(kind, _rich([el]))


def _rich_block(kind: str, rich: Rich) -> dict:
    b = {"kind": kind, "text": rich[0]}
    if rich[1]:
        b["bold"] = rich[1]
    return b


def _unknown(el) -> dict:
    """Unrecognised container: its text nodes joined by spaces (separate elements stay apart)."""
    parts = [norm(str(s)) for s in el.find_all(string=True) if not isinstance(s, Comment)]
    return {"kind": "unknown", "cls": " ".join(el.get("class") or [el.name]),
            "text": " ".join(p for p in parts if p)}


def _has_content(el) -> bool:
    return bool(_lines(el)) or bool(_images(el))


# ---------------------------------------------------------------- images

def _image_from_src(src: str) -> dict | None:
    """Image block for a data URI; an `unknown` block for an external or malformed source
    (compile cannot place those, so the slide falls back with the src in its reason)."""
    src = src.strip()
    if not src or src.startswith("#"):
        return None
    if not src.lower().startswith("data:"):
        return {"kind": "unknown", "cls": f"external-image {src}", "text": src}
    m = _DATA.fullmatch(src)
    try:
        if not m:
            raise ValueError(src[:40])
        mime, params, payload = m.groups()
        if ";base64" in params.lower():
            b64 = re.sub(r"\s+", "", payload).rstrip("=")
            data = base64.b64decode(b64 + "=" * (-len(b64) % 4), validate=True)
        else:
            data = urllib.parse.unquote_to_bytes(payload)
    except (binascii.Error, ValueError):
        return {"kind": "unknown", "cls": f"bad-image: {BAD_IMAGE}", "text": BAD_IMAGE}
    return {"kind": "image", "sha256": hashlib.sha256(data).hexdigest(), "mime": mime, "data": data}


def _image_from_style(style: str) -> dict | None:
    """The first image url(...) in an inline style, as _image_from_src reads it."""
    for m in _URL.finditer(style or ""):
        img = _image_from_src(next(g for g in m.groups() if g is not None))
        if img:
            return img
    return None


def _image_of(el) -> dict | None:
    if el.name == "img":
        return _image_from_src(el.get("src", ""))
    return _image_from_style(el.get("style", ""))


def _images(el) -> list[dict]:
    """Image blocks (or external / malformed image `unknown` blocks) for el and its descendants."""
    found = []
    for e in [el, *el.find_all(True)]:
        if e.name == "img" or _classes(e) & IMAGE_CLS:
            img = _image_of(e)
            if img:
                found.append(img)
    return found


def _image_key(b: dict) -> tuple:
    """What identifies a placed image: extras such as `width` do not."""
    return b["kind"], b.get("sha256"), b.get("cls"), b.get("text")


def _unplaced_images(blocks: list[dict], slide, exclude: list[Tag]) -> list[dict]:
    """Unknown blocks for images on the slide (outside the `exclude` elements) that no block
    carries: an <img> inside a cell or table cell, or beside .content, is not a block of its
    own; it must make the slide fall back, not vanish.

    Each block places one image: found images are matched one-for-one against the slide's
    image / unknown blocks, so a second copy of a placed image inside a cell still counts."""
    pool = [_image_key(b) for b in blocks if b["kind"] in ("image", "unknown")]
    out = []
    for el in [slide, *slide.find_all(True)]:
        if not (el.name == "img" or _classes(el) & IMAGE_CLS):
            continue
        if any(el is x or any(p is x for p in el.parents) for x in exclude):
            continue
        img = _image_of(el)
        if img:
            key = _image_key(img)
            hit = next((i for i, k in enumerate(pool) if k == key), None)
            if hit is not None:
                pool.pop(hit)
            elif img["kind"] == "image":
                out.append({"kind": "unknown", "cls": f"inline-image {img['mime']}",
                            "text": f"image {img['mime']}"})
            else:
                out.append(img)
    return out


# ---------------------------------------------------------------- cells

def _first(el, classes) -> Tag | None:
    for c in classes:
        hit = el.find(class_=c)
        if hit is not None:
            return hit
    return None


def _cell(el) -> dict:
    """One cell from a cell container (or a loose .c-p / bare leaf div)."""
    captured: list[Tag] = []
    cell = {"num": None, "header": None, "body": [], "bullets": [], "value": None}
    rich: dict[str, Rich] = {}
    if _classes(el) & {"c-p"}:  # loose paragraph used as a cell
        rich["body"] = _rich([el])
        captured.append(el)
    else:
        for key, classes in (("num", NUM_CLS), ("header", HEADER_CLS), ("value", VALUE_CLS)):
            hit = _first(el, classes)
            if hit is not None:
                rich[key] = _rich([hit])
                captured.append(hit)
        body: list[Rich] = []
        for p in el.find_all(class_="c-p"):
            if p.find_parent("li") is None:
                body.append(_rich([p]))
                captured.append(p)
        bullets: list[Rich] = []
        for li in el.find_all("li"):
            # each item once: a nested list's items are their own bullets
            bullets.append(_rich([li], skip=li.find_all(["ul", "ol"])))
            captured.append(li)
        # text not claimed by any part is kept, never dropped
        loose = _rich([el], skip=captured)
        if loose[0]:
            if not captured:
                rich["header"] = loose
            else:
                body.append((loose[0].replace("\n", " "), loose[1]))
        rich["body"] = _cat(body)
        rich["bullets"] = _cat(bullets)
    for key in ("num", "header", "value"):
        if key in rich:
            cell[key] = rich[key][0] or None
    for key in ("body", "bullets"):
        if key in rich and rich[key][0]:
            cell[key] = rich[key][0].split("\n")
    bold = {k: r[1] for k, r in rich.items() if r[1]}
    if bold:
        cell["bold"] = bold
    return cell


def _is_leaf(el) -> bool:
    """A bare div with no nested cell, wrapper or grid containers."""
    nested = CELL_CLS | WRAPPER_CLS | GRID_CLS | {"timeline", "tbl", "band"}
    return not any(_classes(d) & nested for d in el.find_all(True))


def _is_bare(el) -> bool:
    return el.name == "div" and not _classes(el)


def _cells_blocks(cells: list[dict]) -> list[dict]:
    """1-4 cells per block; longer runs split into balanced rows."""
    if not cells:
        return []
    nrows = -(-len(cells) // MAX_CELLS)
    per = -(-len(cells) // nrows)
    return [{"kind": "cells", "cells": cells[i:i + per]} for i in range(0, len(cells), per)]


def _children(el) -> list[Tag]:
    return [c for c in el.children if isinstance(c, Tag)
            and not (_classes(c) & SKIP_CLS) and _has_content(c)]


def _runs(el, skip=()) -> list[Tag | list]:
    """el's children in order: block Tags as they are, and runs of bare text / plain inline
    tags (no class) grouped into lists, so loose text in a mixed container is not dropped."""
    out: list[Tag | list] = []
    run: list = []
    for c in el.children:
        if isinstance(c, Comment) or any(c is k for k in skip):
            continue
        if isinstance(c, NavigableString) or (c.name in INLINE_TAGS and not _classes(c)):
            run.append(c)
            continue
        if not isinstance(c, Tag):
            continue
        if run:
            out.append(run)
            run = []
        if not (_classes(c) & SKIP_CLS) and _has_content(c):
            out.append(c)
    if run:
        out.append(run)
    return [r for r in out if not isinstance(r, list) or _rich(r)[0]]


def _run_blocks(el, handle, skip=()) -> list[dict]:
    """Blocks for el's children: Tags through handle, loose text runs as lead blocks."""
    out: list[dict] = []
    for r in _runs(el, skip):
        out += [_rich_block("lead", _rich(r))] if isinstance(r, list) else handle(r)
    return out


def _grid_cell(el) -> dict | None:
    if _classes(el) & (CELL_CLS | {"c-p"}) or (_is_bare(el) and _is_leaf(el)):
        return _cell(el)
    return None


def _wrapper(el) -> list[dict]:
    kids = _runs(el)
    if kids and all(isinstance(k, Tag) and _classes(k) & {"stack"} for k in kids):
        cols = [[_grid_cell(c) or _cell(c) for c in _children(k)] for k in kids]
        out: list[dict] = []
        for row in zip_longest(*cols):  # one row per stack level, split like any cell run
            out += _cells_blocks([c for c in row if c is not None])
        return out
    blocks: list[dict] = []
    run: list[dict] = []
    for k in kids:
        cell = _grid_cell(k) if isinstance(k, Tag) else None
        if cell is not None:
            run.append(cell)
            continue
        blocks += _cells_blocks(run)
        run = []
        if isinstance(k, list):
            blocks.append(_rich_block("lead", _rich(k)))
        elif _classes(k) & {"stack"} or _is_bare(k):
            blocks += _run_blocks(k, _blocks)
        else:
            blocks += _blocks(k)
    return blocks + _cells_blocks(run)


def _ranges(rich: Rich) -> list[list[int]]:
    """JSON-safe bold ranges of a rich text: [[start, length], ...]."""
    return [[a, n] for a, n in rich[1]]


def _rows_table(el) -> dict:
    head = el.find("thead")
    trs = [tr for tr in el.find_all("tr") if head is None or tr.find_parent("thead") is not head]
    ths = None
    if head is not None:
        ths = head.find_all(["th", "td"])
    elif trs and not trs[0].find_all("td") and trs[0].find_all("th"):
        ths = trs.pop(0).find_all("th")  # an all-<th> first row is the header
    header, header_bold, widths = None, None, []
    if ths is not None:
        hr = [_rich([th]) for th in ths]
        header, header_bold = [t for t, _ in hr], [_ranges(r) for r in hr]
        for th in ths:
            m = re.search(r"width\s*:\s*([\d.]+)%", th.get("style", ""))
            widths.append(round(float(m.group(1)) / 100, 4) if m else None)
    rows, bold = [], []
    for tr in trs:
        cells = [_rich([td]) for td in tr.find_all(["td", "th"])]
        if any(t for t, _ in cells):
            rows.append([t for t, _ in cells])
            bold.append([_ranges(r) for r in cells])
    return {"kind": "rows", "header": header, "rows": rows, "style": "table", "widths": widths,
            "bold": bold, "header_bold": header_bold}


def _rows_timeline(el) -> dict:
    rows, bold = [], []
    for tl in el.find_all(class_="tl"):
        k = tl.find(class_="tl-k")
        key = _rich([k]) if k is not None else ("", [])
        text = _rich([tl], skip=[k] if k is not None else [])
        rows.append([key[0], text[0]])
        bold.append([_ranges(key), _ranges(text)])
    return {"kind": "rows", "header": None, "rows": rows, "style": "timeline", "widths": [],
            "bold": bold, "header_bold": None}


def _band(el) -> dict:
    lab = el.find(class_="st-k")
    label = _text(lab) if lab is not None else None
    text, bold = _rich([el], skip=[lab] if lab is not None else [])
    b = {"kind": "band", "label": label, "text": text}
    if bold:
        b["bold"] = bold
    return b


def _blocks(el) -> list[dict]:
    """Blocks for one element inside .content, in document order."""
    cls = _classes(el)
    width = None
    for c in cls:
        m = re.fullmatch(r"w(\d{2})", c)
        if m:
            width = int(m.group(1)) / 100
    out = _blocks_inner(el, cls)
    if width is not None:
        for b in out:
            b["width"] = width
    return out


def _blocks_inner(el, cls: set[str]) -> list[dict]:
    if cls & SKIP_CLS or not _has_content(el):
        return []
    if cls & IMAGE_CLS or el.name == "img":
        img = _image_of(el)
        return [img] if img else []  # an unknown block for an external / malformed source
    if "content-header" in cls:
        return _run_blocks(el, lambda c: [_text_block("title", c)] if c.name in ("h1", "h2") else _blocks(c))
    if el.name in ("h1", "h2"):
        return [_text_block("title", el)]
    if cls & NOTE_CLS:
        return [_text_block("note", el)]
    if cls & QUOTE_CLS:
        return [_text_block("quote", el)]
    if cls & LEAD_CLS or "c-p" in cls:
        return [_text_block("lead", el)]
    if "band" in cls:
        return [_band(el)]
    if el.name == "table" or "tbl" in cls:
        return [_rows_table(el)]
    if "timeline" in cls:
        return [_rows_timeline(el)]
    if cls & GRID_CLS:
        return _wrapper(el)
    if cls & WRAPPER_CLS:
        return _wrapper(el)
    if cls & CELL_CLS:
        return _cells_blocks([_cell(el)])
    if el.name in ("ul", "ol"):
        return _cells_blocks([_cell(el)])
    if _is_bare(el):
        if _is_leaf(el) and not _children(el):
            return [_text_block("lead", el)]
        return _run_blocks(el, _blocks)
    return [_unknown(el)]


# ---------------------------------------------------------------- slides

FIELD_SEL = (("kicker", ".cv-kicker"), ("title", ".cv-title, .cl-title"),
             ("sub", ".cv-sub"), ("meta", ".cv-meta"), ("contact", ".cl-contact"))


def _cover_blocks(slide) -> list[dict]:
    blocks, used = [], []
    for role, sel in FIELD_SEL:
        el = slide.select_one(sel)
        if el is not None:
            blocks.append({"kind": "field", "role": role, "text": _text(el)})
            used.append(el)
    box = slide.select_one(".cv-text, .cl-text")
    if box is not None:
        for c in _runs(box):
            if isinstance(c, list):  # bare text in the text box
                blocks.append({"kind": "unknown", "cls": "text", "text": _rich(c)[0]})
            elif not any(u is c or any(d is u for d in c.descendants) for u in used):
                blocks.append(_unknown(c))
            else:  # a child holding a field: anything else in it is kept, not dropped
                extra = _rich([c], skip=used)[0]
                if extra:
                    blocks.append({"kind": "unknown", "cls": " ".join(c.get("class") or [c.name]),
                                   "text": extra.replace("\n", " ")})
    blocks += _images(slide)  # external / malformed sources arrive as unknown blocks
    return blocks


CHROME_CLS = {"header-bar", "footer"}


def _content_blocks(slide, drop: list[Tag]) -> list[dict]:
    blocks = []
    sec = slide.select_one(".header-sec")
    if sec is not None and _text(sec):
        blocks.append(_text_block("section", sec))
    content = slide.select_one(".content")
    chrome = [c for c in slide.children if _classes(c) & CHROME_CLS] + drop
    if content is not None:
        blocks += _run_blocks(content, _blocks)
    else:  # no .content: everything on the slide but its chrome
        blocks += _run_blocks(slide, _blocks, skip=chrome)
    # images anywhere on the slide but its chrome, .content's siblings included
    return blocks + _unplaced_images(blocks, slide, chrome)


def _drop_elements(slide, selectors: list[str]) -> list[Tag]:
    """Drop-selector elements, each once, outermost only (a .sub inside .brand counts once).

    Identity, not bs4's structural ==: two identical <span class="wm-top">ACME</span> are two
    elements and both count."""
    found, seen = [], set()
    for sel in selectors:
        for el in slide.select(sel):
            if id(el) not in seen:
                seen.add(id(el))
                found.append(el)
    return [el for el in found if not any(id(a) in seen for a in el.parents)]


def _raw_text(slide) -> list[str]:
    out = []
    for s in slide.find_all(string=True):
        if isinstance(s, Comment) or s.parent.name in ("script", "style"):
            continue
        t = norm(str(s))
        if t:
            out.append(t)
    return out


def parse(html: str, include_internal: bool = False, source: str | None = None,
          drop_selectors: list[str] | None = None) -> dict:
    """Deck dict for html. drop_selectors (default: layouts.yaml) name chrome whose text
    the slide may leave out; their strings are listed in each slide's `dropped_text`."""
    if drop_selectors is None:
        drop_selectors = load_layout().drop_selectors
    soup = BeautifulSoup(html, "html.parser")
    title = norm(soup.title.get_text()) if soup.title else None
    slides = []
    for html_index, wrap in enumerate(soup.select(".slide-wrap"), start=1):
        sid = wrap.get("data-slide-id")
        internal = sid in INTERNAL_IDS
        if internal and not include_internal:
            continue
        slide = wrap.select_one(".slide") or wrap
        cls = _classes(slide)
        if "close" in cls:
            kind = "closing"
        elif slide.select_one(".cv-text, .cv-title, .cv-img"):
            kind = "cover"
        else:
            kind = "content"
        drop = _drop_elements(slide, drop_selectors)
        blocks = _cover_blocks(slide) if kind != "content" else _content_blocks(slide, drop)
        background = _image_from_style(slide.get("style", ""))
        if background is not None and background["kind"] != "image":
            blocks.append(background)  # external / malformed background: the slide falls back
            background = None
        slides.append({
            "n": len(slides) + 1,
            "html_index": html_index,
            "id": sid,
            "kind": kind,
            "ground": "dark" if "dk" in cls else "ivory",
            "internal": internal,
            "background": background,
            "blocks": blocks,
            "raw_text": _raw_text(slide),
            "dropped_text": [t for el in drop for t in _raw_text(el)],  # one entry per occurrence
        })
    return {"title": title, "source": source, "slides": slides}


def to_jsonable(obj):
    """Deck dict with image bytes replaced by their sha256 (for deck.json)."""
    if isinstance(obj, dict):
        if obj.get("kind") == "image" and isinstance(obj.get("data"), bytes):
            return {**{k: to_jsonable(v) for k, v in obj.items() if k != "data"},
                    "data": f"sha256:{obj['sha256']}", "bytes": len(obj["data"])}
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, bytes):
        return f"sha256:{hashlib.sha256(obj).hexdigest()}"
    return obj


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    inc = "--include-internal" in argv
    paths = [a for a in argv if not a.startswith("--")]
    if len(paths) != 1:
        print("usage: python -m gslider.parse <deck.html> [--include-internal]", file=sys.stderr)
        return 2
    with open(paths[0], encoding="utf-8") as f:
        deck = parse(f.read(), include_internal=inc, source=paths[0])
    json.dump(to_jsonable(deck), sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
