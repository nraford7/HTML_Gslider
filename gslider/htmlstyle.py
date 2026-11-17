"""html style: Slides requests that recreate the HTML's own look (pure, no network).

Input is one SlideMeasure per slide (`gslider.measure`): the text blocks Chrome drew,
in px on a 1920x1080 slide, and a text-free plate PNG of everything else. Each slide
becomes: the page background colour, the plate as a full-bleed picture at the bottom
(object `s{NN}_plate`, asset key `plate:<n>`), then one TEXT_BOX per text block in
DOM order (objects `s{NN}_t{i}`), so overlapping text keeps its stacking order.

Geometry: 1920px = 960pt, so 1px = 0.5pt. A block at (left, top, width, height) px
becomes a box whose element edge is x = left*0.5 - 7.2, y = top*0.5 - 7.2, width
w = width*0.5*1.03 + 14.4 (3% slack so Slides does not wrap before Chrome did).
Centred text moves left by half the slack and right-aligned text by all of it, so
the text keeps its place. Box h is the text-area height (height*0.5); the element is
h + 14.4 tall (the template-style Box convention).

Text: per run font family (`fonts.slides_family`; a family Google Fonts does not know
is substituted by its CSS generic and listed) sent as `weightedFontFamily` with the CSS
weight rounded to the nearest 100 (100-900; `bold` is also set for 600 and up), size px*0.5 pt, italic,
underline and colour; block alignment; line spacing css_line_height /
(font size * normal line ratio) * 100, the ratio from the family's hhea metrics
(1.2 when unknown). `text-transform` is applied to the text. `letter-spacing` has no
Slides equivalent: dropped and listed.

A slide falls back to a picture of the HTML slide (`fallback:<n>`, like template
style) when Chrome could not measure it, or when a font it uses did not load.

SlidePlan (html) = template SlidePlan keys plus style "html", background "#rrggbb",
plate_png (path of the plate PNG), dropped_text (raw_text strings the build does not
carry as text: left in the plate, or not drawn by Chrome at all) and html =
{substitutions: [{family, used}], letter_spacing: [{object_id, px, text}],
left_in_plate: [{text, reason}], hidden: [strings Chrome did not draw]}. Box = {object_id, role "text", ground, x, y, w, h,
text, lines, line_pt}.
"""
from __future__ import annotations

import re

from .compile import emu, rgb, u16
from .fonts import INSET_PT, normal_line_ratio, slides_family
from .parse import norm

PX = 0.5            # pt per CSS px (1920px slide = 960pt page)
SLACK = 1.03        # width slack against earlier wrapping in Slides
PAGE_W, PAGE_H = 960.0, 540.0
DEFAULT_RATIO = 1.2
# Families Slides draws by name although Google Fonts does not serve them, with their
# hhea normal line ratio (Slides' 100% line height).
SLIDES_NATIVE = {"Arial": 1.149, "Georgia": 1.136, "Courier New": 1.133, "Times New Roman": 1.149,
                 "Verdana": 1.215, "Trebuchet MS": 1.161, "Comic Sans MS": 1.394, "Impact": 1.219}
ALIGN = {"left": "START", "center": "CENTER", "right": "END", "justify": "JUSTIFIED"}


def _native_name(family: str) -> str | None:
    low = family.strip().strip("'\"").lower()
    return next((f for f in SLIDES_NATIVE if f.lower() == low), None)


def resolve_family(css_family: str, generic: str) -> tuple[str, bool]:
    """(family used in Slides, substituted?). Slides' own fonts are kept by name."""
    native = _native_name(css_family)
    if native:
        return native, False
    used, sub = slides_family(css_family, generic)
    return used, sub and used.lower() != css_family.strip().strip("'\"").lower()


def line_ratio(family: str) -> float:
    native = _native_name(family)
    if native:
        return SLIDES_NATIVE[native]
    return normal_line_ratio(family) or DEFAULT_RATIO


def css_weight(run: dict) -> int:
    """The run's CSS font weight rounded to the nearest 100 (100-900); 700 or 400 from `bold`
    when the measure has no weight."""
    w = run.get("weight")
    if not isinstance(w, (int, float)) or w <= 0:
        w = 700 if run.get("bold") else 400
    return min(900, max(100, int(w / 100 + 0.5) * 100))


def ground_of(background: str) -> str:
    """"dark" when the background's relative luminance is under 0.5, else "ivory"."""
    c = rgb(background or "#ffffff")
    lum = 0.2126 * c["red"] + 0.7152 * c["green"] + 0.0722 * c["blue"]
    return "dark" if lum < 0.5 else "ivory"


def _transform(texts: list[str], how: str | None) -> list[str]:
    if how == "uppercase":
        return [t.upper() for t in texts]
    if how == "lowercase":
        return [t.lower() for t in texts]
    if how == "capitalize":
        out, prev_space = [], True
        for t in texts:
            chars = []
            for ch in t:
                chars.append(ch.upper() if prev_space and not ch.isspace() else ch)
                prev_space = ch.isspace()
            out.append("".join(chars))
        return out
    return list(texts)


def _consume(pool: list[str], t: str) -> bool:
    """Find t in one pool string (strings of 3 chars or fewer as whole words) and blank it."""
    pat = re.compile(rf"(?<!\w){re.escape(t)}(?!\w)" if len(t) <= 3 else re.escape(t))
    for i, text in enumerate(pool):
        m = pat.search(text)
        if m:
            pool[i] = text[:m.start()] + "\x00" + text[m.end():]
            return True
    return False


def sort_raw_text(raw: list[str], drawn: list[str], plate: list[str]) -> tuple[list[str], list[str]]:
    """(left_in_plate, hidden): raw_text strings found in text left in the plate, and strings
    Chrome did not draw at all (display:none and the like). Each drawn or plate occurrence
    is used once, longest strings first, case-insensitive (text-transform)."""
    key = [norm(t).casefold() for t in raw]
    drawn_pool = [norm(t).casefold() for t in drawn]
    plate_pool = [norm(t).casefold() for t in plate]
    left, hidden = set(), set()
    for i in sorted(range(len(raw)), key=lambda i: len(key[i]), reverse=True):
        if not key[i] or _consume(drawn_pool, key[i]):
            continue
        (left if _consume(plate_pool, key[i]) else hidden).add(i)
    return [raw[i] for i in sorted(left)], [raw[i] for i in sorted(hidden)]


class _HtmlSlide:
    def __init__(self, slide: dict, background: str, assets: dict):
        self.n = slide["n"]
        self.slide = slide
        self.assets = assets
        self.slide_id = f"s{self.n:02d}_slide"
        self.background = background
        self.ground = ground_of(background)
        self.reqs: list[dict] = [
            {"createSlide": {"objectId": self.slide_id, "slideLayoutReference": {"predefinedLayout": "BLANK"}}},
            {"updatePageProperties": {
                "objectId": self.slide_id,
                "pageProperties": {"pageBackgroundFill": {"solidFill": {"color": {"rgbColor": rgb(background)}}}},
                "fields": "pageBackgroundFill.solidFill.color"}},
        ]
        self.boxes: list[dict] = []
        self.missing: list[str] = []
        self.info = {"substitutions": [], "letter_spacing": [], "left_in_plate": [], "hidden": []}

    def props(self, x: float, y: float, w: float, h: float) -> dict:
        return {"pageObjectId": self.slide_id,
                "size": {"width": {"magnitude": emu(w), "unit": "EMU"},
                         "height": {"magnitude": emu(h), "unit": "EMU"}},
                "transform": {"scaleX": 1, "scaleY": 1, "translateX": emu(x), "translateY": emu(y),
                              "unit": "EMU"}}

    def image(self, key: str, oid: str) -> None:
        url = self.assets.get(key)
        if not url:
            self.missing.append(key)
            return
        self.reqs.append({"createImage": {"objectId": oid, "url": url,
                                          "elementProperties": self.props(0, 0, PAGE_W, PAGE_H)}})

    def _family(self, run: dict) -> str:
        used, sub = resolve_family(run.get("family") or "", run.get("generic") or "sans-serif")
        entry = {"family": run.get("family"), "used": used}
        if sub and entry not in self.info["substitutions"]:
            self.info["substitutions"].append(entry)
        return used

    def text_block(self, block: dict) -> None:
        runs = [r for r in block.get("runs") or [] if r.get("text")]
        texts = _transform([r["text"] for r in runs], block.get("transform"))
        text = "".join(texts).rstrip()
        if not text.strip():
            return
        oid = f"s{self.n:02d}_t{len(self.boxes)}"
        align = block.get("align") or "left"
        slack = block["w"] * PX * (SLACK - 1)
        x = block["x"] * PX - INSET_PT - {"center": slack / 2, "right": slack}.get(align, 0)
        y = block["y"] * PX - INSET_PT
        w = block["w"] * PX * SLACK + 2 * INSET_PT
        h = block["h"] * PX
        self.reqs += [
            {"createShape": {"objectId": oid, "shapeType": "TEXT_BOX",
                             "elementProperties": self.props(x, y, w, h + 2 * INSET_PT)}},
            {"insertText": {"objectId": oid, "insertionIndex": 0, "text": text}},
        ]
        families, start = [], 0
        for run, t in zip(runs, texts):
            t = t[:max(0, len(text) - start)]  # the trailing space trimmed above
            if not t:
                break
            fam = self._family(run)
            families.append((len(t), fam, run))
            self.reqs.append({"updateTextStyle": {
                "objectId": oid,
                "textRange": {"type": "FIXED_RANGE", "startIndex": u16(text[:start]),
                              "endIndex": u16(text[:start + len(t)])},
                "style": {"fontFamily": fam,
                          "weightedFontFamily": {"fontFamily": fam, "weight": css_weight(run)},
                          "fontSize": {"magnitude": round(run["size_px"] * PX, 2), "unit": "PT"},
                          "foregroundColor": {"opaqueColor": {"rgbColor": rgb(run.get("color") or "#000000")}},
                          "bold": css_weight(run) >= 600, "italic": bool(run.get("italic")),
                          "underline": bool(run.get("underline"))},
                "fields": "fontFamily,weightedFontFamily,fontSize,foregroundColor,bold,italic,underline"}})
            start += len(t)
        _, fam, main = max(families, key=lambda f: f[0])  # the run with the most text sets the spacing
        lh_px = block.get("line_height_px") or main["size_px"] * DEFAULT_RATIO
        spacing = lh_px / (main["size_px"] * line_ratio(fam)) * 100
        self.reqs.append({"updateParagraphStyle": {
            "objectId": oid, "textRange": {"type": "ALL"},
            "style": {"alignment": ALIGN.get(align, "START"), "lineSpacing": round(spacing, 1),
                      "spaceAbove": {"magnitude": 0, "unit": "PT"},
                      "spaceBelow": {"magnitude": 0, "unit": "PT"}},
            "fields": "alignment,lineSpacing,spaceAbove,spaceBelow"}})
        ls = block.get("letter_spacing_px") or 0
        if ls:
            self.info["letter_spacing"].append({"object_id": oid, "px": ls, "text": text[:60]})
        self.boxes.append({"object_id": oid, "role": "text", "ground": self.ground, "x": x, "y": y, "w": w,
                           "h": h, "text": text, "lines": block.get("lines") or 1, "line_pt": lh_px * PX})

    def plan(self, status: str, reason: str | None, notes: str | None, plate_png=None,
             dropped: list[str] = ()) -> dict:
        return {"n": self.n, "slide_id": self.slide_id, "html_id": self.slide.get("id"), "ground": self.ground,
                "background": self.background, "style": "html", "notes_text": notes, "status": status,
                "reason": reason, "requests": self.reqs, "boxes": self.boxes, "missing_assets": self.missing,
                "plate_png": str(plate_png) if plate_png else None, "dropped_text": list(dropped),
                "html": self.info}


def _fallback(slide: dict, background: str, assets: dict, reason: str) -> dict:
    s = _HtmlSlide(slide, background, assets)
    s.image(f"fallback:{slide['n']}", f"s{slide['n']:02d}_fallback0")
    return s.plan("fallback", reason, "\n".join(slide.get("raw_text") or []))


_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def compile_html_slide(slide: dict, m: dict | None, assets: dict) -> dict:
    n = slide["n"]
    if not m or m.get("error"):
        why = (m or {}).get("error") or "not measured"
        return _fallback(slide, "#ffffff", assets, f"measure failed: {why}")
    background = m.get("background") if _HEX.match(m.get("background") or "") else "#ffffff"
    if m.get("unloaded"):
        fams = ", ".join(m["unloaded"])
        return _fallback(slide, background, assets,
                         f"font {fams} did not load" if len(m["unloaded"]) == 1 else f"fonts {fams} did not load")
    s = _HtmlSlide(slide, background, assets)
    try:
        s.image(f"plate:{n}", f"s{n:02d}_plate")
        for block in m.get("blocks") or []:
            s.text_block(block)
    except Exception as e:  # never let one slide crash the deck
        return _fallback(slide, background, assets, repr(e))
    skipped = list(m.get("skipped") or [])
    s.info["left_in_plate"] = [{"text": k.get("text", ""), "reason": k.get("reason", "")} for k in skipped]
    raw = slide.get("raw_text") or []
    left, hidden = sort_raw_text(raw, [b["text"] for b in s.boxes], [k.get("text", "") for k in skipped])
    s.info["hidden"] = sorted({norm(t) for t in hidden})
    # keep each string as many times as it was dropped
    counts = {t: left.count(t) + hidden.count(t) for t in set(left) | set(hidden)}
    dropped = []
    for t in raw:
        if counts.get(t):
            dropped.append(t)
            counts[t] -= 1
    return s.plan("native", None, None, m.get("plate"), dropped)


def compile_html_deck(deck: dict, measures: dict[int, dict], assets: dict[str, str]) -> list[dict]:
    """One SlidePlan per deck slide. `measures` is keyed by deck slide n (not the
    .slide-wrap index); `assets` maps `plate:<n>` and `fallback:<n>` to image URLs.
    Keys it needs and lacks are listed in each plan's `missing_assets`."""
    return [compile_html_slide(sl, measures.get(sl["n"]), assets) for sl in deck["slides"]]
