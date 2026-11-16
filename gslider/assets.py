"""Image assets on Drive (upload, dedupe, share, revoke) and Chrome renders of one HTML slide.

Assets live in the Drive folder `HTML_Gslider assets`. Each file carries its
SHA-256 in `appProperties.sha256` and `gslider = "1"` (plates also `kind = "plate"`);
a second publish of the same bytes reuses the file and does not upload again.
A file gets `appProperties.shared = "1"` and `perm = <permission id>` only after
its `{type: anyone, role: reader}` permission was created; a reused file without
that mark is shared then (a share that failed, or was revoked, on an earlier run).
Every share is also appended to SHARE_LOG (before the mark), so a build can revoke
a share whose mark failed.

reshare(ids) shares gslider files again (build.py, before retrying a slide whose image
Slides could not fetch). revoke(ids) removes the `anyone` permissions again (build.py calls it after every
build, also a failed one). It marks the file `shared = "0"` first and only then
deletes the permission, so a later build re-shares a file whose delete failed.
It touches only files that carry `gslider = "1"` and are not protected.
unshare_all() does the same for every gslider file in the assets folder.
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
SHARE_LOG: list[tuple[str, str | None]] = []  # (file id, permission id) for every share made by this process


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


def find_folder() -> str | None:
    """Id of the Drive folder `HTML_Gslider assets` the user owns, or None."""
    found = gws.drive_find(  # own folders only: a same-named folder shared by someone else is not ours
        f"name = '{FOLDER_NAME}' and mimeType = '{FOLDER_MIME}' and 'me' in owners and trashed = false")
    return found[0]["id"] if found else None


def ensure_folder() -> str:
    """Id of the Drive folder `HTML_Gslider assets`, found by name or created."""
    global _folder_id
    if _folder_id:
        return _folder_id
    _folder_id = find_folder() or gws.created_id(
        gws.call("drive", "files", "create", "--json", gws._j({"name": FOLDER_NAME, "mimeType": FOLDER_MIME})),
        "folder create")
    return _folder_id


class RevokeError(Exception):
    pass


def _share(file_id: str) -> None:
    perm = gws.drive_share_anyone(file_id)
    SHARE_LOG.append((file_id, perm))  # before the mark: a build revokes this share even if the mark fails
    props = {"shared": "1"}
    if perm:
        props["perm"] = perm
    gws.drive_update_props(file_id, props)  # only after the permission exists


def reshare(ids) -> tuple[list[str], list[dict]]:
    """Share these gslider files by link again (each share goes to SHARE_LOG, so the build's
    final revoke covers it). For a slide Slides could not fetch: another build may have
    revoked the link to a cached file this build still needs. Only files that carry
    `gslider = "1"` and are not protected. Never raises for one file:
    returns (shared ids, [{"file", "error"}])."""
    done, failed = [], []
    for fid in dict.fromkeys(ids):
        try:
            if fid in gws.PROTECTED_IDS:
                raise RevokeError("protected file; not touched")
            props = gws.drive_get(fid, "id,appProperties").get("appProperties") or {}
            if props.get("gslider") != "1":
                raise RevokeError("not uploaded by gslider (no appProperties.gslider=1); not touched")
            _share(fid)
            done.append(fid)
        except Exception as e:  # noqa: BLE001 (one file must not stop the others)
            failed.append({"file": fid, "error": str(e) or repr(e)})
    return done, failed


def publish_image(data: bytes, mime: str, name: str, kind: str | None = None) -> str:
    """Upload image bytes once (dedupe by SHA-256), share by link, return the public uc URL.

    kind (e.g. "plate") is stored in `appProperties.kind` of a new upload."""
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
        props = {"sha256": sha, "gslider": "1", **({"kind": kind} if kind else {})}
        file_id = gws.drive_upload(path, name, folder, props)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    _share(file_id)
    return uc_url(file_id)


# --- revoke sharing -----------------------------------------------------------------

def _revoke_one(file_id: str, perm: str | None = None, props: dict | None = None) -> int:
    """Remove every `anyone` permission from one gslider file; returns how many were removed.

    perm: the permission id gslider created (else `appProperties.perm`, else found by listing).
    props: the file's appProperties when the caller has them (else one read)."""
    if file_id in gws.PROTECTED_IDS:
        raise RevokeError("protected file; not touched")
    if props is None:
        props = gws.drive_get(file_id, "id,appProperties").get("appProperties") or {}
    if props.get("gslider") != "1":
        raise RevokeError("not uploaded by gslider (no appProperties.gslider=1); not touched")
    perm = perm or props.get("perm")
    if props.get("shared") != "0" or props.get("perm"):
        # mark first: if the delete below fails, the next build sees shared=0 and shares again
        gws.drive_update_props(file_id, {"shared": "0", "perm": None})
    removed = 0
    if perm:
        try:
            gws.drive_unshare(file_id, perm)
            removed += 1
        except gws.GwsError as e:
            if e.status != 404:  # 404: already gone (removed by hand); the list below verifies
                raise
    for _ in range(2):  # list to verify; delete any `anyone` permission left; list again
        left = [p for p in gws.drive_permissions(file_id) if p.get("type") == "anyone" and p.get("id")]
        if not left:
            return removed
        for p in left:
            gws.drive_unshare(file_id, p["id"])
            removed += 1
    raise RevokeError("an anyone permission is still there after delete")


def revoke(ids, perms: dict[str, str] | None = None) -> tuple[list[str], list[dict]]:
    """Remove link sharing from these files. Never raises for one file: returns
    (revoked ids, [{"file", "error"}] for files that could not be revoked or were refused)."""
    revoked, failed = [], []
    for fid in dict.fromkeys(ids):
        try:
            _revoke_one(fid, (perms or {}).get(fid))
            revoked.append(fid)
        except Exception as e:  # noqa: BLE001 (one file must not stop the others)
            failed.append({"file": fid, "error": str(e) or repr(e)})
    return revoked, failed


def _is_plate(f: dict) -> bool:
    props = f.get("appProperties") or {}
    return props.get("kind") == "plate" or str(f.get("name") or "").endswith(" plate.png")


def unshare_all(purge_plates: bool = False) -> dict:
    """Remove link sharing from every gslider file in the assets folder the user owns.

    purge_plates also deletes plate pictures (decks keep their own copy of each image).
    Returns counts: files, revoked (had a link), private (had none), purged, failed [{file, error}]."""
    res = {"files": 0, "revoked": 0, "private": 0, "purged": 0, "failed": []}
    folder = _folder_id or find_folder()  # never creates the folder
    if not folder:
        return res
    files = gws.drive_find(f"'{folder}' in parents and 'me' in owners and trashed = false "
                           "and appProperties has { key='gslider' and value='1' }")
    for f in files:
        fid = f.get("id")
        props = f.get("appProperties") or {}
        if not fid or fid in gws.PROTECTED_IDS or props.get("gslider") != "1":
            continue
        res["files"] += 1
        try:
            res["revoked" if _revoke_one(fid, props=props) else "private"] += 1
            if purge_plates and _is_plate(f):
                gws.drive_delete(fid)
                res["purged"] += 1
        except Exception as e:  # noqa: BLE001
            res["failed"].append({"file": fid, "error": str(e) or repr(e)})
    return res


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
