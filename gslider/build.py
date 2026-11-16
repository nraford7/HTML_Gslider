"""Base deck, build pipeline and the `--into` guard (spec section 3, Push).

init_base(): one-slide 13.333x7.5in PPTX (python-pptx) imported through Drive, so
the base deck is 960x540pt (the Slides API cannot set page size). It then writes
one BLANK probe slide to the new, unmarked base and deletes it again (the only
write to a deck without appProperties.gslider). Its id goes to
~/.config/gslider/config.json.

build(): guard `--into` -> check the base (or target) pageSize -> lock out/<slug>/
-> build.json (status building) -> parse -> compile (pass 1 learns which asset keys
are needed and which slides fall back) -> refuse if a fallback needs Chrome and
there is none -> publish images, logos and fallback renders (each shared id
appended to build.json) -> compile again with URLs -> refuse if any asset is
still missing -> copy the base (or re-check comments and clear an `--into` deck)
-> verify pageSize -> one batchUpdate per slide, in order (createSlide appends)
-> delete the placeholder -> one read and one batch for the speaker notes of all
fallback slides -> build.json done.
Any failure after the guard writes build.json status "failed" (error, last good
slide, deck URL) and is raised as BuildError. An `--into` deck loses its old
slides only at the clear step; a failure before it leaves the deck untouched.

Nothing is written to a presentation unless its Drive appProperties carry
gslider == "1". `--into` also refuses decks with any comment thread. gws refuses
every write to gws.PROTECTED_IDS.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup

from gslider import assets, gws
from gslider.compile import compile_deck
from gslider.layout import load_layout
from gslider.parse import INTERNAL_IDS, parse, to_jsonable

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
CONFIG_PATH = Path.home() / ".config" / "gslider" / "config.json"
BASE_NAME = "HTML_Gslider base (do not edit)"
PAGE_EMU = (12192000, 6858000)
PROTECTED_IDS = gws.PROTECTED_IDS  # from config/env; gws refuses writes to these too
LOCK_NAME = ".gslider.lock"
PLACEHOLDER = "gslider_clear"  # keeps the deck non-empty while old slides are removed
def logo_files() -> dict[str, Path]:
    """Brand logo from config `"brand": {"logo": <png for dark slides>, "logo_dark": <png for light
    slides>}`. One file is used for both grounds when only one is set; none means no logo."""
    brand = load_config().get("brand", {})
    light = brand.get("logo") or brand.get("logo_dark")
    dark = brand.get("logo_dark") or brand.get("logo")
    files = {"logo": light, "logo_dark": dark} if light else {}
    return {k: Path(v).expanduser() for k, v in files.items()}
NOTES_FIELDS = "slides(objectId,slideProperties/notesPage/notesProperties)"


class BuildError(Exception):
    pass


@dataclass
class BuildResult:
    pid: str
    url: str
    plans: list = field(default_factory=list)
    out_dir: Path | None = None


def deck_url(pid: str) -> str:
    return f"https://docs.google.com/presentation/d/{pid}/edit"


def slugify(name: str) -> str:
    """ASCII folder name: accents stripped (NFKD); a name with no ASCII letters gets a sha8."""
    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")
    return slug or hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------- config

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_config(**values) -> None:
    cfg = {**load_config(), **values}
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def base_id() -> str:
    bid = load_config().get("base_id")
    if not bid:
        raise BuildError(f"no base deck configured in {CONFIG_PATH}; run `gslider init-base` once")
    return bid


# --------------------------------------------------------------------- checks

def _page_size(pid: str) -> tuple[int | None, int | None]:
    ps = gws.get(pid, "pageSize").get("pageSize", {})
    return ps.get("width", {}).get("magnitude"), ps.get("height", {}).get("magnitude")


def _check_page_size(pid: str) -> None:
    try:
        got = _page_size(pid)
    except gws.GwsError as e:
        if e.status == 404:
            raise BuildError(f"base deck missing; run init-base ({pid} not found: {e.message})") from e
        raise
    if got == (None, None):
        raise BuildError(f"{deck_url(pid)} returned no pageSize (missing or not a presentation); "
                         "if it is the base deck, run init-base")
    if tuple(got) != PAGE_EMU:
        raise BuildError(f"pageSize of {deck_url(pid)} is {got[0]}x{got[1]} EMU, "
                         f"expected {PAGE_EMU[0]}x{PAGE_EMU[1]}; nothing written")


def _is_marked(pid: str) -> bool:
    props = gws.drive_get(pid, "id,appProperties").get("appProperties") or {}
    return props.get("gslider") == "1"


def guard_into(pid: str) -> None:
    """Refuse `--into` targets the tool must not write to. Read-only calls only."""
    if pid in PROTECTED_IDS:
        raise BuildError(f"refusing --into {pid}: protected deck")
    if pid == load_config().get("base_id"):
        raise BuildError(f"refusing --into {pid}: that is the base deck")
    if not _is_marked(pid):
        raise BuildError(f"refusing --into {pid}: not created by gslider (no appProperties.gslider=1)")
    n = gws.comments_count(pid)
    if n:
        raise BuildError(f"refusing --into {pid}: it has {n} comment thread(s); build a new deck instead")


# --------------------------------------------------------------------- assets

def wrap_indexes(html: str, include_internal: bool) -> dict[int, int]:
    """Deck slide n -> 1-based `.slide-wrap` index in the HTML (what render_slide_png takes)."""
    out, n = {}, 0
    for i, wrap in enumerate(BeautifulSoup(html, "html.parser").select(".slide-wrap"), 1):
        if wrap.get("data-slide-id") in INTERNAL_IDS and not include_internal:
            continue
        n += 1
        out[n] = i
    return out


def _images(obj, found: dict) -> dict:
    """sha256 -> image dict for every image block / background in the deck."""
    if isinstance(obj, dict):
        if obj.get("kind") == "image" and isinstance(obj.get("data"), bytes):
            found.setdefault(obj["sha256"], obj)
        for v in obj.values():
            _images(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _images(v, found)
    return found


def publish_assets(keys: set[str], deck: dict, html_path: Path, html: str, slug: str,
                   out_dir: Path, include_internal: bool, on_publish=None) -> dict[str, str]:
    """Publish every asset key compile asked for; keys it cannot resolve are left out.

    on_publish(url) runs after each key is published (build records the shared id)."""
    logos = logo_files()
    images = _images(deck["slides"], {})
    wraps = None
    urls: dict[str, str] = {}
    for key in sorted(keys):
        before = urls.get(key)
        if key in logos:
            urls[key] = assets.publish_image(logos[key].read_bytes(), "image/png", logos[key].name)
        elif key.startswith("fallback:"):
            n = int(key.split(":", 1)[1])
            slide = next((s for s in deck["slides"] if s["n"] == n), {})
            idx = slide.get("html_index")
            if idx is None:  # older parse output: derive from the .slide-wrap order
                wraps = wraps or wrap_indexes(html, include_internal)
                idx = wraps[n]
            png = out_dir / "fallback" / f"s{n:02d}.png"
            _log(f"render fallback slide {n} (html slide-wrap {idx})")
            assets.render_slide_png(html_path, idx, png)
            urls[key] = assets.publish_image(png.read_bytes(), "image/png", f"{slug} slide {n:02d} fallback.png")
        elif key in images:
            img = images[key]
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif"}.get(img["mime"], "bin")
            urls[key] = assets.publish_image(img["data"], img["mime"], f"{key[:16]}.{ext}")
        if on_publish and urls.get(key) and urls[key] != before:
            on_publish(urls[key])
    return urls


# --------------------------------------------------------------------- base

def _base_pptx(path: Path) -> Path:
    from pptx import Presentation
    from pptx.util import Emu

    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(PAGE_EMU[0]), Emu(PAGE_EMU[1])
    blank = next((lo for lo in prs.slide_layouts if lo.name == "Blank"), prs.slide_layouts[6])
    prs.slides.add_slide(blank)
    prs.save(str(path))
    return path


def init_base(force: bool = False) -> str:
    """Create the 960x540pt base deck once and store its id in the config."""
    old = load_config().get("base_id")
    if old and not force:
        raise BuildError(f"base deck already configured: {old} ({CONFIG_PATH}); use --force to make another")
    with tempfile.TemporaryDirectory() as tmp:
        pid = gws.drive_import_pptx(_base_pptx(Path(tmp) / "base.pptx"), BASE_NAME)
    got = _page_size(pid)
    if tuple(got) != PAGE_EMU:
        raise BuildError(f"imported base {pid} has pageSize {got}, expected {PAGE_EMU}; config not saved")
    try:
        gws.batch(pid, [{"createSlide": {"objectId": "gslider_probe",
                                         "slideLayoutReference": {"predefinedLayout": "BLANK"}}},
                        {"deleteObject": {"objectId": "gslider_probe"}}])
    except gws.GwsError as e:
        raise BuildError(f"base {pid}: BLANK createSlide probe failed: {e.message}; config not saved") from e
    save_config(base_id=pid)
    return pid


# --------------------------------------------------------------------- push

def _clear(pid: str) -> None:
    """Remove every slide but a blank placeholder (deleted after the last slide)."""
    existing = [s["objectId"] for s in gws.get(pid, "slides(objectId)").get("slides", [])]
    reqs = [] if PLACEHOLDER in existing else [
        {"createSlide": {"objectId": PLACEHOLDER, "slideLayoutReference": {"predefinedLayout": "BLANK"}}}]
    reqs += [{"deleteObject": {"objectId": s}} for s in existing if s != PLACEHOLDER]
    if reqs:
        try:
            gws.batch(pid, reqs)
        except gws.GwsError as e:
            raise BuildError(f"clearing {deck_url(pid)} failed: {e.message}") from e


def _notes(pid: str, plans: list[dict]) -> None:
    """Speaker notes for these plans: one read of the deck, one batch."""
    slides = gws.get(pid, NOTES_FIELDS).get("slides", [])
    nids = {s.get("objectId"): (((s.get("slideProperties") or {}).get("notesPage") or {})
                                .get("notesProperties") or {}).get("speakerNotesObjectId") for s in slides}
    lost = [p["slide_id"] for p in plans if not nids.get(p["slide_id"])]
    if lost:
        raise gws.GwsError(None, f"no speaker notes shape on {', '.join(lost)}")
    gws.batch(pid, [{"insertText": {"objectId": nids[p["slide_id"]], "insertionIndex": 0,
                                    "text": p["notes_text"]}} for p in plans])


def _in_deck(k: int, url: str) -> str:
    if k <= 0:
        return f"No slides are in the deck yet: {url}"
    return f"Slide 1 is in the deck: {url}" if k == 1 else f"Slides 1-{k} are in the deck: {url}"


# Slides fetches each image URL itself and sometimes refuses a Drive link it fetched a moment
# earlier (live build, 2026-11-16: the logo worked on slides 1, 2, 6 and failed on 7). Google
# documents batchUpdate as atomic, and that failed build confirmed it: the check found no trace
# of slide 7 afterwards. So retrying the slide after a pause cannot duplicate objects.
IMAGE_FETCH_ERROR = "problem retrieving the image"
IMAGE_FETCH_DELAYS = (5, 15, 30)


def _batch_retrying_image_fetch(pid: str, requests: list[dict], n: int = 0) -> None:
    attempts = len(IMAGE_FETCH_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        try:
            gws.batch(pid, requests)
            return
        except gws.GwsError as e:
            fetch_error = e.status == 400 and IMAGE_FETCH_ERROR in (e.message or "")
            if not fetch_error or attempt == attempts:
                raise
            delay = IMAGE_FETCH_DELAYS[attempt - 1]
            _log(f"slide {n}: Slides could not fetch an image (attempt {attempt}/{attempts}); retrying in {delay}s")
            time.sleep(delay)


def push(pid: str, plans: list[dict], progress: dict | None = None) -> None:
    """One batchUpdate per slide; progress["last_good_slide"] tracks the last slide fully built."""
    url = deck_url(pid)
    total = len(plans)
    progress = progress if progress is not None else {}
    for plan in plans:
        n = plan["n"]
        name = plan.get("html_id") or plan["slide_id"]
        try:
            _batch_retrying_image_fetch(pid, plan["requests"], n)
        except gws.GwsError as e:
            raise BuildError(f"slide {n} ({name}) failed: {e.message}\n{_in_deck(n - 1, url)}") from e
        progress["last_good_slide"] = n
        _log(f"slide {n}/{total} {plan['status']}")
    try:
        gws.batch(pid, [{"deleteObject": {"objectId": PLACEHOLDER}}])
    except gws.GwsError as e:
        raise BuildError(f"all slides built but placeholder delete failed: {e.message}\n{url}") from e
    noted = [p for p in plans if p["status"] == "fallback" and p.get("notes_text")]
    if noted:
        try:
            _notes(pid, noted)
        except gws.GwsError as e:
            raise BuildError(f"all {total} slides are in the deck; speaker notes failed for slides "
                             f"{', '.join(str(p['n']) for p in noted)}: {e.message}; "
                             f"rerun with --into {pid} (rebuilds the whole deck)\n{url}") from e


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_holder(lock: Path) -> int | None:
    """PID in the lock file; None if it is gone; 0 if empty or unreadable (treated as busy)."""
    try:
        text = lock.read_text().strip()
    except FileNotFoundError:
        return None
    except OSError:
        return 0
    return int(text) if text.isascii() and text.isdigit() and int(text) > 0 else 0


@contextlib.contextmanager
def _out_lock(out_dir: Path):
    """Hold out_dir/.gslider.lock for one build; a lock left by a dead process is taken over.

    The PID is written to a temp file first and hard-linked to the lock name, so the
    lock never exists without its PID. An empty or unreadable lock counts as busy."""
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = out_dir / LOCK_NAME
    fd, tmp_name = tempfile.mkstemp(prefix=f"{LOCK_NAME}.", suffix=".tmp", dir=out_dir)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        for _ in range(2):
            try:
                os.link(tmp, lock)
                break
            except FileExistsError:
                holder = _lock_holder(lock)
                if holder == 0:
                    raise BuildError(f"{lock} is empty or unreadable, so another build may be starting; "
                                     f"if no gslider build is running, delete {lock} by hand, "
                                     f"or pass --out <another folder>") from None
                if holder and _alive(holder):
                    raise BuildError(f"another gslider build (process {holder}) is using {out_dir}; "
                                     f"wait for it, or pass --out <another folder>") from None
                if _lock_holder(lock) == holder:  # stale: its process is gone, and no one took it over
                    lock.unlink(missing_ok=True)
            except OSError as e:  # e.g. a filesystem without hard links
                raise BuildError(f"cannot create {lock}: {e}; pass --out <another folder>") from None
        else:
            raise BuildError(f"could not lock {out_dir}; pass --out <another folder>")
    finally:
        tmp.unlink(missing_ok=True)
    try:
        yield
    finally:
        if _lock_holder(lock) == os.getpid():  # never remove a lock another build holds
            lock.unlink(missing_ok=True)


def _uc_id(url: str) -> str:
    return url.rsplit("id=", 1)[-1]


def build(html_path, title: str, ivory=frozenset(), include_internal: bool = False,
          into: str | None = None, out_dir: Path | None = None) -> BuildResult:
    html_path = Path(html_path)
    if into:
        guard_into(into)
    src = into or base_id()
    if not into and src in PROTECTED_IDS:
        raise BuildError(f"configured base {src} is a protected deck")
    _check_page_size(src)  # a stale or wrong base is found before minutes of rendering

    html = html_path.read_text(encoding="utf-8")
    slug = slugify(html_path.stem)
    out_dir = Path(out_dir) if out_dir else OUT / slug
    ivory = set(ivory)
    meta = {"pid": into, "url": deck_url(into) if into else None, "title": title, "source": str(html_path),
            "base_id": None if into else src, "into": bool(into), "ivory": sorted(ivory),
            "include_internal": include_internal, "status": "building", "shared_assets": [],
            "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    progress = {"last_good_slide": 0}
    with _out_lock(out_dir):
        _write(out_dir / "build.json", meta)
        try:
            plans = _build(html_path, html, slug, out_dir, title, ivory, include_internal, into, src,
                           meta, progress)
        except BaseException as e:
            pid = meta.get("pid")
            meta.update(status="failed", error=str(e) or repr(e), last_good_slide=progress["last_good_slide"],
                        finished=time.strftime("%Y-%m-%dT%H:%M:%S"))
            _write(out_dir / "build.json", meta)
            if not isinstance(e, Exception):
                raise
            msg = str(e) or repr(e)
            if pid:
                msg += f"\nbuild.json: {out_dir / 'build.json'}; retry with --into {pid}"
            raise BuildError(msg) from e

    meta.update(status="done", finished=time.strftime("%Y-%m-%dT%H:%M:%S"),
                last_good_slide=progress["last_good_slide"],
                fallbacks={p["n"]: p["reason"] for p in plans if p["status"] == "fallback"})
    _write(out_dir / "build.json", meta)
    return BuildResult(meta["pid"], deck_url(meta["pid"]), plans, out_dir)


def _build(html_path: Path, html: str, slug: str, out_dir: Path, title: str, ivory: set[int],
           include_internal: bool, into: str | None, src: str, meta: dict, progress: dict) -> list[dict]:
    deck = parse(html, include_internal=include_internal, source=str(html_path))
    layout = load_layout()

    logos = logo_files()
    plans = compile_deck(deck, layout, {}, ivory, logo=bool(logos))
    bad = sorted(n for n in ivory if not 1 <= n <= len(plans))
    if bad:
        _log(f"warning: --ivory {', '.join(map(str, bad))} not in 1-{len(plans)}; ignored")

    def shared(url: str) -> None:
        fid = _uc_id(url)
        if fid not in meta["shared_assets"]:
            meta["shared_assets"].append(fid)
            _write(out_dir / "build.json", meta)

    needed = {k for p in plans for k in p["missing_assets"]}
    if any(k.startswith("fallback:") for k in needed) and not assets.CHROME:
        env = os.environ.get("GSLIDER_CHROME")
        why = (f"GSLIDER_CHROME={env} does not exist" if env else
               "Chrome not found (no GSLIDER_CHROME, no Google Chrome app, no google-chrome or chromium on PATH)")
        slides = sorted(int(k.split(":", 1)[1]) for k in needed if k.startswith("fallback:"))
        raise BuildError(f"{why}; slides {', '.join(map(str, slides))} need a Chrome render. "
                         "Set GSLIDER_CHROME to the Chrome or Chromium binary; nothing published")
    urls = publish_assets(needed, deck, html_path, html, slug, out_dir, include_internal,
                          on_publish=shared) if needed else {}
    if urls:
        plans = compile_deck(deck, layout, urls, ivory, logo=bool(logos))
    missing = {p["n"]: p["missing_assets"] for p in plans if p["missing_assets"]}
    if missing:
        detail = "; ".join(f"slide {n}: {', '.join(k)}" for n, k in missing.items())
        raise BuildError(f"assets missing, nothing pushed ({detail})")

    built = to_jsonable(deck)
    for sl, p in zip(built["slides"], plans):
        sl["ground"] = p["ground"]  # deck.json carries the ground as built
    _write(out_dir / "deck.json", built)
    _write(out_dir / "plans.json", plans)

    if into:
        pid = into
        n = gws.comments_count(pid)  # again: a comment may have arrived while assets published
        if n:
            raise BuildError(f"refusing --into {pid}: it has {n} comment thread(s) now; nothing cleared")
    else:
        try:
            pid = gws.drive_copy(src, title, {"gslider": "1"})
        except gws.GwsError as e:
            if e.status == 404:
                raise BuildError(f"base deck missing; run init-base ({src} not found: {e.message})") from e
            raise
        meta.update(pid=pid, url=deck_url(pid))
        _write(out_dir / "build.json", meta)
        _log(f"copied base -> {deck_url(pid)}")

    if not _is_marked(pid):
        raise BuildError(f"{deck_url(pid)} lacks appProperties.gslider=1; nothing written")
    _check_page_size(pid)
    _clear(pid)
    push(pid, plans, progress)
    return plans


def find_build(pid: str, root: Path | None = None) -> Path | None:
    """The out/<slug>/ folder whose build.json names pid (newest first)."""
    hits = []
    for f in (root or OUT).glob("*/build.json"):
        try:
            if json.loads(f.read_text(encoding="utf-8")).get("pid") == pid:
                hits.append(f)
        except (OSError, json.JSONDecodeError):
            continue
    return max(hits, key=lambda f: f.stat().st_mtime).parent if hits else None
