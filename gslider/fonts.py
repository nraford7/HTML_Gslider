"""TTF loading and text measurement (Pillow widths, fontTools hhea line heights).

Any Google Fonts family works: its TTF comes from the Google Fonts CSS API, which
lists TTF files when fetched with a non-browser user agent. The two families the
template style uses keep their pinned variable TTFs.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
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


CSS_API = "https://fonts.googleapis.com/css2"
USER_AGENT = "gslider"  # a non-browser agent makes the CSS API list TTF (not WOFF2) files
GENERIC_FALLBACK = {"sans-serif": "Arial", "serif": "Georgia", "monospace": "Courier New"}


class FontUnavailable(ValueError):
    """The family is not on Google Fonts (or its TTF could not be found)."""


def css_url(family: str) -> str:
    return f"{CSS_API}?family={urllib.parse.quote_plus(family)}:wght@400;700"


def _fetch_css(url: str) -> tuple[int, str]:
    """GET a CSS API url. Returns (status, body); HTTP errors return their status.
    Network errors raise OSError."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


_FACE = re.compile(r"@font-face\s*\{(.*?)\}", re.S)


def ttf_url_from_css(css: str, weight: int = 400) -> str | None:
    """The TTF url of the normal face whose weight is nearest `weight`, or None."""
    faces = []
    for block in _FACE.findall(css):
        m = re.search(r"src:\s*url\(([^)]+\.ttf)\)", block)
        if not m:
            continue
        if re.search(r"font-style:\s*italic", block):
            continue
        w = re.search(r"font-weight:\s*(\d+)", block)
        faces.append((abs((int(w.group(1)) if w else 400) - weight), m.group(1).strip("'\"")))
    return min(faces)[1] if faces else None


def _css_for(family: str) -> tuple[int, str]:
    """CSS for the family: 400/700 first, then the bare family (for families without
    one of those weights)."""
    status, body = _fetch_css(css_url(family))
    if status == 400:
        status, body = _fetch_css(f"{CSS_API}?family={urllib.parse.quote_plus(family)}")
    return status, body


def _availability_file() -> Path:
    return CACHE / "fonts.json"


def _read_availability() -> dict[str, bool]:
    try:
        return json.loads(_availability_file().read_text())
    except (OSError, ValueError):
        return {}


def google_font_available(family: str) -> bool:
    """True if the Google Fonts CSS API knows the family (HTTP 200). Answers from the
    API are cached in ~/.cache/gslider/fonts.json; network failures are not cached."""
    if family in FONT_URLS:
        return True
    known = _read_availability()
    if family in known:
        return known[family]
    try:
        status, _ = _css_for(family)
    except OSError:
        return False
    if status >= 500:
        return False
    ok = status == 200
    known[family] = ok
    f = _availability_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(known, indent=1, sort_keys=True))
    return ok


def _download(url: str, dest: Path) -> None:
    subprocess.run(["curl", "-sfL", "--max-time", "60", "--max-filesize", "20M", "-o", str(dest), url],
                   check=True)


def _cache_name(family: str) -> str:
    """File stem for a family's cached TTF: only [A-Za-z0-9_-] (the family name comes from the
    deck's CSS). Raises FontUnavailable when nothing is left."""
    stem = re.sub(r"[^A-Za-z0-9_-]", "", family)
    if not stem:
        raise FontUnavailable(f"font {family!r}: no usable characters for a cache file name")
    return stem


def font_path(family: str) -> Path:
    """TTF for the family (the files Slides renders with), cached once. The two template
    families use pinned variable TTFs; any other family comes from the CSS API (400 face).
    Raises FontUnavailable if Google Fonts does not know the family."""
    p = CACHE / f"{_cache_name(family)}.ttf"
    if p.exists() and p.stat().st_size >= 10_000:
        return p
    url = FONT_URLS.get(family)
    if url is None:
        if not google_font_available(family):
            raise FontUnavailable(f"unknown font family: {family!r}")
        try:
            status, css = _css_for(family)
        except OSError as e:
            raise FontUnavailable(f"font {family!r}: CSS API unreachable: {e}") from e
        url = ttf_url_from_css(css) if status == 200 else None
        if url is None:
            raise FontUnavailable(f"font {family!r}: no TTF in the CSS API response")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".part")
    _download(url, tmp)
    tmp.replace(p)
    return p


def normal_line_ratio(family: str) -> float | None:
    """Slides' normal line height per point of font size (hhea), or None if the family's
    TTF cannot be had."""
    try:
        return _ratio(family)
    except (ValueError, OSError, subprocess.CalledProcessError):
        return None


def slides_family(css_family: str, generic: str) -> tuple[str, bool]:
    """(family to use in Slides, substituted?). A family Google Fonts knows is used by
    name; any other maps by its CSS generic: Arial (sans-serif, and the default),
    Georgia (serif), Courier New (monospace). A bare generic name is mapped, not
    counted as a substitution."""
    name = css_family.strip().strip("'\"").strip()
    if name.lower() in GENERIC_FALLBACK:
        return GENERIC_FALLBACK[name.lower()], False
    if name and google_font_available(name):
        return name, False
    return GENERIC_FALLBACK.get(generic.strip().lower(), "Arial"), True


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
    # Static TTFs from the CSS API are already the 400 face and have no axes.
    try:
        font.set_variation_by_axes([400])
    except OSError:
        pass
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
