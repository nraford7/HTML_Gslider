"""Image assets on Drive (upload, dedupe, share) and Chrome renders of one HTML slide.

Assets live in the Drive folder `HTML_Gslider assets`. Each file carries its
SHA-256 in `appProperties.sha256`; a second publish of the same bytes reuses the
file and does not upload again. A file gets `appProperties.shared = "1"` only
after its `{type: anyone, role: reader}` permission was created; a reused file
without that mark is shared then (a share that failed on an earlier run).
The folder is looked up among folders the signed-in user owns.

Chrome: env GSLIDER_CHROME if set (a missing path gives None, never the default),
else the macOS app, else `google-chrome` / `google-chrome-stable` / `chromium` /
`chromium-browser` on PATH, else None.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from bs4 import BeautifulSoup

from gslider import gws

FOLDER_NAME = "HTML_Gslider assets"
FOLDER_MIME = "application/vnd.google-apps.folder"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SLIDE_W, SLIDE_H = 1920, 1080

_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif"}
_folder_id: str | None = None


def find_chrome() -> str | None:
    """Chrome binary for fallback renders, or None when there is none."""
    env = os.environ.get("GSLIDER_CHROME")
    if env:
        return env if Path(env).exists() else None
    if Path(MAC_CHROME).exists():
        return MAC_CHROME
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


CHROME: str | None = find_chrome()


def uc_url(file_id: str) -> str:
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def ensure_folder() -> str:
    """Id of the Drive folder `HTML_Gslider assets`, found by name or created."""
    global _folder_id
    if _folder_id:
        return _folder_id
    found = gws.drive_find(  # own folders only: a same-named folder shared by someone else is not ours
        f"name = '{FOLDER_NAME}' and mimeType = '{FOLDER_MIME}' and 'me' in owners and trashed = false")
    if found:
        _folder_id = found[0]["id"]
    else:
        _folder_id = gws.created_id(gws.call("drive", "files", "create", "--json",
                                             gws._j({"name": FOLDER_NAME, "mimeType": FOLDER_MIME})),
                                    "folder create")
    return _folder_id


def _share(file_id: str) -> None:
    gws.drive_share_anyone(file_id)
    gws.drive_update_props(file_id, {"shared": "1"})  # only after the permission exists


def publish_image(data: bytes, mime: str, name: str) -> str:
    """Upload image bytes once (dedupe by SHA-256), share by link, return the public uc URL."""
    sha = hashlib.sha256(data).hexdigest()
    folder = ensure_folder()
    existing = gws.drive_find(
        f"appProperties has {{ key='sha256' and value='{sha}' }} "
        f"and '{folder}' in parents and trashed = false")
    if existing:
        hit = existing[0]
        if (hit.get("appProperties") or {}).get("shared") != "1":
            _share(hit["id"])
        return uc_url(hit["id"])
    tmp_root = OUT / "assets_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="up-", dir=tmp_root))  # unique per call: parallel builds
    path = tmp_dir / f"{sha[:16]}{_EXT.get(mime, '')}"
    path.write_bytes(data)
    try:
        file_id = gws.drive_upload(path, name, folder, {"sha256": sha, "gslider": "1"})
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    _share(file_id)
    return uc_url(file_id)


# --- fallback renders ---------------------------------------------------------

def _print_rules(css: str) -> str:
    """Inner rules of every `@media print { ... }` block, minus `@page` rules."""
    rules = []
    for m in re.finditer(r"@media\s+print\s*\{", css):
        depth, i = 1, m.end()
        while i < len(css) and depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        inner = css[m.end():i - 1]
        rules.append(re.sub(r"@page\s*\{[^}]*\}", "", inner))
    return "\n".join(rules)


def _render_css(print_css: str) -> str:
    return f"""
{print_css}
:root{{--fit:1 !important;}}
html,body{{margin:0 !important;padding:0 !important;overflow:hidden !important;
  width:{SLIDE_W}px;height:{SLIDE_H}px;}}
.slide-wrap{{display:none !important;}}
.slide-wrap.gslider-render{{display:block !important;zoom:1 !important;margin:0 !important;
  box-shadow:none !important;transform:none !important;position:absolute !important;
  left:0 !important;top:0 !important;width:{SLIDE_W}px !important;height:{SLIDE_H}px !important;}}
.gslider-render > .slide{{width:{SLIDE_W}px !important;height:{SLIDE_H}px !important;}}
"""


def render_slide_png(html_path: Path, n: int, out: Path) -> Path:
    """Screenshot slide n (1-based `.slide-wrap` order) of html_path to a 1920×1080 PNG."""
    if not CHROME:
        raise RuntimeError("Chrome not found for the fallback render; set GSLIDER_CHROME to its path")
    html_path = Path(html_path).resolve()
    out = Path(out).resolve()
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    wraps = soup.select(".slide-wrap")
    if not 1 <= n <= len(wraps):
        raise ValueError(f"slide {n} not in {html_path.name} ({len(wraps)} slides)")
    wraps[n - 1]["class"] = [*wraps[n - 1].get("class", []), "gslider-render"]

    css = "\n".join(s.get_text() for s in soup.find_all("style"))
    head = soup.head or soup.new_tag("head")
    if soup.head is None:
        (soup.html or soup).insert(0, head)
    base = soup.new_tag("base", href=html_path.parent.as_uri() + "/")
    head.insert(0, base)
    style = soup.new_tag("style")
    style.string = _render_css(_print_rules(css))
    head.append(style)  # last in head, after the deck's own styles

    work = OUT / "render"
    work.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    fd, name = tempfile.mkstemp(prefix=f"{html_path.stem}_s{n:02d}_", suffix=".html", dir=work)
    tmp_html = Path(name)  # unique per call: parallel builds of one deck must not share it
    with open(fd, "w", encoding="utf-8") as f:
        f.write(str(soup))
    profile = Path(tempfile.mkdtemp(prefix="chrome-", dir=work))
    try:
        r = subprocess.run(
            [CHROME, "--headless=new", f"--screenshot={out}",
             f"--window-size={SLIDE_W},{SLIDE_H}", "--force-device-scale-factor=1",
             "--hide-scrollbars", "--virtual-time-budget=3000",
             f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
             tmp_html.as_uri()],
            capture_output=True, text=True, timeout=120)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        tmp_html.unlink(missing_ok=True)
    if r.returncode or not out.exists():
        raise RuntimeError(f"Chrome render of slide {n} failed: {r.stderr.strip()[-500:]}")
    return out
