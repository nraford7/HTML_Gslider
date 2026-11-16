"""Measure the text of one HTML slide in Chrome, and render its text-free plate.

One headless Chrome run per slide opens a temp copy of the deck where only that
`.slide-wrap` is visible at 1920x1080 (the same isolation CSS as fallback
renders in `assets.render_slide_png`), with `iframe`, `object` and `embed`
removed. The injected `measure.js` waits for the page and its fonts, writes the
text blocks as JSON into `<script id="gslider-measure">`, then tags the measured
blocks so `PLATE_CSS` makes their text transparent. Chrome runs with both
`--dump-dom` (Python reads the JSON from the dumped DOM) and `--screenshot` (the
plate: everything that is not editable text). Text that cannot become a text
box (rotated, zoomed, clipped, inside SVG) is not hidden: it stays in the plate
and is listed under `skipped`.

SlideMeasure = {"n", "background": "#rrggbb", "fonts_status", "unloaded": [family],
                "blocks": [Block], "skipped": [{"text", "reason"}], "plate": Path}
Block = {"x", "y", "w", "h" (px, slide origin), "lines", "align", "line_height_px",
         "letter_spacing_px", "runs": [{"text", "family", "generic", "size_px",
         "bold", "italic", "underline", "color"}], "transform": text-transform or None}
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bs4 import BeautifulSoup
from PIL import Image

from gslider import assets

MEASURE_JS = Path(__file__).with_name("measure.js")
OUT_ID = "gslider-measure"
VIRTUAL_TIME_MS = 10000
CHROME_TIMEOUT_S = 120

# Text of tagged blocks is made invisible; pseudo-elements (bullets, ::before
# labels) keep the colours the script saved in custom properties.
PLATE_CSS = """
[data-gslider-hide], [data-gslider-hide] *:not(svg):not(svg *) {
  color: transparent !important;
  -webkit-text-fill-color: transparent !important;
  -webkit-text-stroke: 0 !important;
  text-shadow: none !important;
  caret-color: transparent !important;
  text-decoration-color: transparent !important;
  text-emphasis-color: transparent !important;
}
[data-gslider-hide]::before, [data-gslider-hide] *::before {
  color: var(--gs-b-c) !important; -webkit-text-fill-color: var(--gs-b-f) !important;
  text-shadow: var(--gs-b-s) !important;
}
[data-gslider-hide]::after, [data-gslider-hide] *::after {
  color: var(--gs-a-c) !important; -webkit-text-fill-color: var(--gs-a-f) !important;
  text-shadow: var(--gs-a-s) !important;
}
[data-gslider-hide]::marker, [data-gslider-hide] *::marker {
  color: var(--gs-m-c) !important; -webkit-text-fill-color: var(--gs-m-f) !important;
}
[data-gslider-gradient] {
  background-image: none !important;
  background-color: transparent !important;
  background-clip: border-box !important;
  -webkit-background-clip: border-box !important;
}
"""

_JSON_RE = re.compile(
    r"<script\b[^>]*\bid=\"" + OUT_ID + r"\"[^>]*>(.*?)</script>", re.DOTALL)


class MeasureError(RuntimeError):
    """Chrome or the measure script failed for one slide."""


def read_measure_json(dump: str) -> dict:
    """The measure script's JSON from Chrome's `--dump-dom` output."""
    found = _JSON_RE.findall(dump)
    if not found:
        raise MeasureError("no measure output in the dumped page (script blocked or Chrome failed)")
    data = json.loads(found[-1])  # the element the script appended last in <body>
    status = data.get("status")
    if status == "pending":
        raise MeasureError("measure script still pending when Chrome stopped (page or fonts too slow)")
    if status != "done":
        raise MeasureError(f"measure script failed: {data.get('error', status)}")
    return data


def prepare_html(html_path: Path, n: int) -> str:
    """Temp-copy HTML for slide n: isolation CSS, plate CSS, measure script; no iframes."""
    html_path = Path(html_path).resolve()
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    wraps = soup.select(".slide-wrap")
    if not 1 <= n <= len(wraps):
        raise ValueError(f"slide {n} not in {html_path.name} ({len(wraps)} slides)")
    wraps[n - 1]["class"] = [*wraps[n - 1].get("class", []), "gslider-render"]
    for tag in soup.find_all(["iframe", "object", "embed"]):
        tag.decompose()

    css = "\n".join(s.get_text() for s in soup.find_all("style"))
    head = soup.head
    if head is None:
        head = soup.new_tag("head")
        (soup.html or soup).insert(0, head)
    head.insert(0, soup.new_tag("base", href=html_path.parent.as_uri() + "/"))
    render = soup.new_tag("style", id="gslider-render-css")
    render.string = assets._render_css(assets._print_rules(css))
    head.append(render)  # last in head, after the deck's own styles
    plate = soup.new_tag("style", id="gslider-plate-css")
    plate.string = PLATE_CSS
    head.append(plate)

    script = soup.new_tag("script", id="gslider-measure-js")
    script.string = MEASURE_JS.read_text(encoding="utf-8")
    (soup.body or soup.html or soup).append(script)
    return str(soup)


def _plate_background(plate: Path) -> str:
    """Most common colour of the plate (slides with a gradient or image ground)."""
    with Image.open(plate) as im:
        small = im.convert("RGB").resize((96, 54))
    count, rgb = max(small.getcolors(96 * 54))
    return "#%02x%02x%02x" % rgb


def measure_slide(html_path: Path, n: int, workdir: Path) -> dict:
    """Measure slide n (1-based `.slide-wrap` order); the plate PNG goes to workdir."""
    chrome = assets.CHROME
    if not chrome:
        raise RuntimeError("Chrome not found for measuring; set GSLIDER_CHROME to its path")
    html_path = Path(html_path).resolve()
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    plate = workdir / f"plate_{html_path.stem}_s{n:02d}.png"
    plate.unlink(missing_ok=True)

    fd, name = tempfile.mkstemp(prefix=f"{html_path.stem}_s{n:02d}_", suffix=".html", dir=workdir)
    tmp_html = Path(name)  # unique per call: slides measured in parallel
    with open(fd, "w", encoding="utf-8") as f:
        f.write(prepare_html(html_path, n))
    profile = Path(tempfile.mkdtemp(prefix="chrome-", dir=workdir))
    try:
        r = subprocess.run(
            [chrome, "--headless=new", "--dump-dom", f"--screenshot={plate}",
             f"--window-size={assets.SLIDE_W},{assets.SLIDE_H}", "--force-device-scale-factor=1",
             "--hide-scrollbars", f"--virtual-time-budget={VIRTUAL_TIME_MS}",
             f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
             tmp_html.as_uri()],
            capture_output=True, text=True, timeout=CHROME_TIMEOUT_S)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        tmp_html.unlink(missing_ok=True)
    if r.returncode or not plate.exists():
        raise MeasureError(f"Chrome measure of slide {n} failed: {r.stderr.strip()[-500:]}")
    try:
        data = read_measure_json(r.stdout)
    except MeasureError as exc:
        raise MeasureError(f"slide {n}: {exc}") from exc

    return {
        "n": n,
        "background": data.get("background") or _plate_background(plate),
        "fonts_status": data.get("fonts_status", ""),
        "unloaded": data.get("unloaded", []),
        "blocks": data.get("blocks", []),
        "skipped": data.get("skipped", []),
        "plate": plate,
    }


def measure_deck(html_path: Path, slide_ns: list[int], workdir: Path, workers: int = 3) -> dict[int, dict]:
    """measure_slide for each n, `workers` Chrome processes at a time; keyed by n."""
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {n: pool.submit(measure_slide, html_path, n, workdir) for n in slide_ns}
        return {n: f.result() for n, f in futures.items()}
