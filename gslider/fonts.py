"""TTF loading and text measurement (Pillow widths, fontTools hhea line heights)."""

from __future__ import annotations

import math
import subprocess
from functools import lru_cache
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import ImageFont

FONT_URLS = {
    "Playfair Display": "https://github.com/google/fonts/raw/main/ofl/playfairdisplay/PlayfairDisplay%5Bwght%5D.ttf",
    "Libre Franklin": "https://github.com/google/fonts/raw/main/ofl/librefranklin/LibreFranklin%5Bwght%5D.ttf",
}
CACHE = Path.home() / ".cache" / "gslider"
INSET_PT = 7.2  # default Slides text-box inset, each side
_SCALE = 10  # load fonts at 10x the point size so Pillow's integer rounding stays sub-0.1pt


def font_path(family: str) -> Path:
    """Google Fonts variable TTF for the family (the files Slides renders with), cached once."""
    if family not in FONT_URLS:
        raise ValueError(f"unknown font family: {family!r}")
    p = CACHE / f"{family.replace(' ', '')}.ttf"
    if not p.exists() or p.stat().st_size < 10_000:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        subprocess.run(["curl", "-sfL", "-o", str(tmp), FONT_URLS[family]], check=True)
        tmp.replace(p)
    return p


@lru_cache(maxsize=None)
def _ratio(family: str) -> float:
    """(ascent + |descent| + lineGap) / unitsPerEm from the hhea table."""
    f = TTFont(font_path(family), lazy=True)
    h = f["hhea"]
    return (h.ascent + abs(h.descent) + h.lineGap) / f["head"].unitsPerEm


@lru_cache(maxsize=None)
def _font(family: str, size_pt: float) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(font_path(family)), size_pt * _SCALE)
    # Variable fonts: pin the 400 instance (Libre Franklin's default instance is 100).
    font.set_variation_by_axes([400])
    return font


def line_height(family: str, size_pt: float, line_spacing_pct: int = 100) -> float:
    return size_pt * _ratio(family) * line_spacing_pct / 100


def text_width(text: str, family: str, size_pt: float) -> float:
    return _font(family, size_pt).getlength(text) / _SCALE


# Small safety margin: wrap against a 1% narrower width than the text area.
WRAP_SLACK = 1.01


def _wrap_count(paragraph: str, family: str, size_pt: float, avail: float) -> int:
    avail = avail / WRAP_SLACK
    words = paragraph.split()
    if not words:
        return 1  # an empty paragraph still takes one line
    lines, current = 1, words[0]
    for w in words[1:]:
        candidate = f"{current} {w}"
        if text_width(candidate, family, size_pt) <= avail:
            current = candidate
        else:
            lines += 1
            current = w
    # a word wider than the column is broken across lines by Slides
    for w in words:
        width = text_width(w, family, size_pt)
        if width > avail > 0:
            lines += math.ceil(width / avail) - 1
    return lines


def measure(
    text: str,
    family: str,
    size_pt: float,
    width_pt: float,
    line_spacing_pct: int = 100,
) -> tuple[int, float]:
    """Return (lines, height_pt) for text in a box of width_pt (insets removed)."""
    avail = width_pt - 2 * INSET_PT
    lh = line_height(family, size_pt, line_spacing_pct)
    lines = sum(_wrap_count(p, family, size_pt, avail) for p in text.split("\n"))
    return lines, lines * lh
