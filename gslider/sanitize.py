"""Make a deck safe to open in Chrome: every temp HTML gslider hands to Chrome goes through here.

safe_html(html, base_dir, refused=None) -> str. base_dir is the HTML file's folder.
safe_deck(path) -> (html, refused): safe_html of a file, cached per file version (a deck's
slides are measured one Chrome run each; the deck is read and its images fetched once).
refusals(path) -> the refused list of an earlier safe_deck of that file, else [].

What it does:
- Removes `<script>` (also inside SVG), `iframe`, `frame`, `frameset`, `object`, `embed`,
  `portal`, `applet`, `base`, `<meta http-equiv=refresh>`, every `on*` event attribute, and
  SVG `<set>` / `<animate>` that change an href or src. gslider's own measure script is
  added after this.
- `<link rel=stylesheet>`: a local file is inlined as `<style>` when it passes the same
  rules as a local image (inside base_dir after symlinks, at most 50 MB), else dropped;
  a remote one is kept only from https://fonts.googleapis.com. Every other `<link>` is dropped.
- Images: `img[src]`, `img[srcset]`, `source[srcset]`, `input[type=image][src]`, SVG
  `image` / `feImage` `href` and `xlink:href`, `video[poster]`, the `background` attribute
  and every CSS `url(...)` (style attributes, SVG presentation attributes, `<style>`
  blocks, inlined stylesheets, `image-set()` strings) go through `fetch.resolve_image`
  (template-style rules). Accepted bytes become `data:` URIs. A refused one becomes
  nothing: a transparent pixel for `img`/`input`, `none` in CSS, `local()` in a font src;
  each refusal is recorded once as {"src", "reason"}.
- Kept as they are: `data:` URIs (self-contained), `url(#id)` and `href="#id"` (in-page
  references), `<a>`/`<area>` links (never loaded), `@import` and `@font-face` urls from
  https://fonts.googleapis.com and https://fonts.gstatic.com (fonts only).
- A local font in `@font-face` (inside base_dir, WOFF/WOFF2/TTF/OTF bytes) is inlined.
- Other URL attributes (media `src`, `use` to another file, form actions, ...) are removed.
- CSS escapes that spell a letter (`\\75 rl(`) are decoded first, so they cannot
  hide a `url(`.
- The deck is parsed with html5lib (a WHATWG HTML5 parser, much closer to Chrome than
  html.parser), so `<!-->` and similar cannot hide markup. Comments, CDATA, processing
  instructions, every `<meta http-equiv>`, the MathML `mglyph` / `malignmark`, and `noscript`,
  `noframes`, `fencedframe` (Chrome runs with scripts on, so it never shows noscript) are
  removed. `xmp` and `plaintext` become `pre` (their raw text cannot round-trip).
- Style text: bs4 writes it raw, and inside SVG or MathML a `<` there would be read back as
  a tag. So a `<` that could open a tag (before a letter, `/`, `!` or `?`) becomes the CSS
  escape `\\3c ` inside a string or url(), and `< ` (a space added, same CSS) elsewhere, as
  in `1px< width`. The CSS-ignored `<!--` marker is removed.
- html5lib drops the first newline after `<pre>`, `<textarea>` and `<listing>`; it is written
  back so the text Chrome shows does not change.
- Parse and serialise is not always a fixed point, so the output is parsed and sanitised
  again until it no longer changes (UnsafePage after MAX_ROUNDS).

parse(html) -> BeautifulSoup: the same html5lib parse, for every caller that edits a page
Chrome will open.
page(soup, script_nonce=None) -> str: the final page text. Puts a Content-Security-Policy
`<meta>` first in `<head>` (call it last): loads only from `data:` and the two Google Fonts
hosts, no frames or objects, scripts only the one carrying script_nonce (none without it).
Then reads the text back as Chrome will and raises UnsafePage if it holds any
`<meta http-equiv>` but the CSP, or any other script. CSP does not cover navigation, so the
meta check is what stops a redirect to a local file.

CHROME_FLAGS: extra Chrome flags for every run (belt and braces): host names other than
the two Google Fonts hosts do not resolve.
"""
from __future__ import annotations

import base64
import os
import posixpath
import re
import threading
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup, CData, Comment, Declaration, Doctype, ProcessingInstruction

from gslider import fetch

CHROME_FLAGS = ("--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE fonts.googleapis.com, "
                "EXCLUDE fonts.gstatic.com",)
BLANK_GIF = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
FONT_CSS_HOST = "fonts.googleapis.com"
FONT_FILE_HOST = "fonts.gstatic.com"
MAX_IMPORT_DEPTH = 4
MAX_ROUNDS = 4

_DROP_TAGS = ("script", "iframe", "frame", "frameset", "object", "embed", "portal", "applet", "base", "noembed",
              "mglyph", "malignmark", "noscript", "noframes", "fencedframe")
_PRE_TAGS = ("pre", "textarea", "listing")
# in order: a CSS comment, a string or unquoted url(...), the `<!--` marker, a `<` that could open a tag
_STYLE_LT = re.compile(r"""(?P<c>/\*.*?\*/)|(?P<s>"(?:\\.|[^"\\\n])*(?:"|\Z)|'(?:\\.|[^'\\\n])*(?:'|\Z)"""
                       r"""|url\(\s*(?!['"])[^)]*\))|(?P<cdo><!--)|<(?=[A-Za-z/!?])""", re.I | re.S)


def _style_text(css: str) -> str:
    """css with no `<` that could open a tag (see the module notes)."""
    def one(m: re.Match) -> str:
        if m.group("c"):
            return _TAG_LT.sub("< ", m.group("c"))
        if m.group("s"):
            return m.group("s").replace("<", "\\3c ")
        return "" if m.group("cdo") else "< "
    return _STYLE_LT.sub(one, css)


_TAG_LT = re.compile(r"<(?=[A-Za-z/!?])")


_URL_ATTRS = ("src", "srcset", "href", "xlink:href", "poster", "data", "background", "lowsrc", "dynsrc",
              "action", "formaction", "manifest", "codebase", "longdesc", "ping", "imagesrcset", "srcdoc",
              "archive", "classid", "profile", "cite", "icon", "attributionsrc")
_IMAGE_HREF_TAGS = ("image", "feimage")
_FONT_MAGIC = (b"wOF2", b"wOFF", b"\x00\x01\x00\x00", b"OTTO", b"true")
_FONT_MIME = {b"wOF2": "font/woff2", b"wOFF": "font/woff", b"OTTO": "font/otf"}

# CSS: an escape (\hex{1,6} plus one optional space, or \char)
_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n\r\f]?|([^\n0-9a-fA-F]))")
# one of: a comment or string (left as they are), an @font-face block, an @import, a url(...) token
_CSS_TOKEN = re.compile(
    r"(?P<skip>/\*.*?(?:\*/|\Z)|\"(?:\\.|[^\"\\\n])*(?:\"|\Z)|'(?:\\.|[^'\\\n])*(?:'|\Z))"
    r"|(?P<face>@font-face\s*\{[^}]*\})"
    r"|(?P<import>@import\s+(?:url\(\s*(?P<iq>['\"]?)(?P<iurl>.*?)(?P=iq)\s*\)|(?P<sq>['\"])(?P<surl>.*?)(?P=sq))[^;]*;?)"
    r"|(?P<url>url\(\s*(?:(?P<q>['\"])(?P<u>.*?)(?P=q)|(?P<uu>(?:\\.|[^)\\\s'\"])*))\s*\))",
    re.I | re.S)
_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)
_IMAGE_SET_ITEM = re.compile(r"url\([^)]*\)|(['\"])(.*?)\1", re.I | re.S)


class _Ctx:
    """One sanitise run: base folder, refusals, and a cache of resolved sources."""

    def __init__(self, base_dir: Path, refused: list | None, images: bool = True):
        self.base = Path(base_dir)
        self.images = images
        self.refused = refused if refused is not None else []
        self.cache: dict[tuple[str, str], str | None] = {}

    def refuse(self, src: str, reason: str) -> None:
        if not any(r["src"] == src for r in self.refused):
            self.refused.append({"src": src, "reason": reason})

    def image(self, src: str) -> str | None:
        """data: URI for an image source, or None (refused and recorded)."""
        src = (src or "").strip()
        if src.lower().startswith("data:image/"):
            return src
        if not self.images:
            return None
        key = ("img", src)
        if key not in self.cache:
            try:
                data, mime = fetch.resolve_image(src, self.base)
                self.cache[key] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
            except fetch.FetchError as e:
                self.cache[key] = None
                self.refuse(src, e.reason)
            except Exception as e:  # noqa: BLE001 (one image must not stop the deck)
                self.cache[key] = None
                self.refuse(src, str(e) or repr(e))
        return self.cache[key]

    def font(self, src: str) -> str | None:
        """A font url that may stay or be inlined, or None (refused and recorded)."""
        src = (src or "").strip()
        low = src.lower()
        if low.startswith("data:"):
            return src
        parts = urllib.parse.urlsplit(src)
        if parts.scheme.lower() == "https" and (parts.hostname or "").lower() == FONT_FILE_HOST:
            return src
        if parts.scheme.lower() in ("", "file") and not src.startswith("//"):
            if parts.scheme and parts.netloc not in ("", "localhost"):
                self.refuse(src, "a file URL with a host is not supported")
                return None
            key = ("font", src)
            if key not in self.cache:
                try:
                    data = fetch.read_local(urllib.parse.unquote(parts.path), self.base)
                    magic = next((m for m in _FONT_MAGIC if data.startswith(m)), None)
                    if magic is None:
                        raise fetch.FetchError("not a WOFF, WOFF2, TTF or OTF font")
                    mime = _FONT_MIME.get(magic, "font/ttf")
                    self.cache[key] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
                except fetch.FetchError as e:
                    self.cache[key] = None
                    self.refuse(src, e.reason)
            return self.cache[key]
        self.refuse(src, f"font from {parts.hostname or parts.scheme or 'an unknown place'} not loaded "
                         f"(fonts only from {FONT_FILE_HOST} or the HTML file's folder)")
        return None


# --------------------------------------------------------------------- CSS

def _unescape(css: str) -> str:
    """Decode CSS escapes that spell an ASCII letter (the same text to CSS). Not digits:
    `.\\31 0` is a valid class, `.10` is not; the words to unmask (url, import, ...) have none."""
    def one(m: re.Match) -> str:
        try:
            ch = chr(int(m.group(1), 16)) if m.group(1) else m.group(2)
        except (ValueError, OverflowError):
            return m.group(0)
        return ch if ch.isascii() and ch.isalpha() else m.group(0)
    return _ESCAPE.sub(one, css)


def _rebase(url: str, rel_dir: str) -> str:
    """A url from a stylesheet in rel_dir (relative to the HTML folder), relative to the HTML folder."""
    if not rel_dir:
        return url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme or url.startswith(("/", "#")) or url.lower().startswith("data:"):
        return url
    joined = posixpath.normpath(posixpath.join(rel_dir, urllib.parse.unquote(parts.path)))
    return urllib.parse.quote(joined)


def _is_font_css(url: str) -> bool:
    parts = urllib.parse.urlsplit(url.strip())
    return parts.scheme.lower() == "https" and (parts.hostname or "").lower() == FONT_CSS_HOST


_ANY_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n\r\f]?|(\n)|(.))", re.S)


def _css_value(text: str) -> str:
    """The value of a CSS string or url body: every escape decoded (what Chrome loads)."""
    def one(m: re.Match) -> str:
        if m.group(1):
            cp = int(m.group(1), 16)
            return chr(cp) if 0 < cp <= 0x10FFFF and not 0xD800 <= cp <= 0xDFFF else "\ufffd"
        return "" if m.group(2) else m.group(3)
    return _ANY_ESCAPE.sub(one, text)


def _css(css: str, ctx: _Ctx, rel_dir: str = "", depth: int = 0) -> str:
    """CSS with every url() rewritten (see the module doc)."""
    css = _unescape(css)

    def quoted(u: str) -> str:
        return '"' + u.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\a ") + '"'

    def url_in(value: str, font: bool) -> str:
        v = _css_value(value.strip())
        if v.startswith("#"):
            return f"url({quoted(v)})"  # an in-page reference
        if font:
            got = ctx.font(_rebase(v, rel_dir))
            return f"url({quoted(got)})" if got else 'local("gslider-not-loaded")'
        got = ctx.image(_rebase(v, rel_dir))
        return f"url({quoted(got)})" if got else "none"

    def token(m: re.Match) -> str:
        if m.group("skip"):
            return m.group("skip")
        if m.group("face"):
            return _URL.sub(lambda u: url_in(u.group(2), True), m.group("face"))
        if m.group("import"):
            target = _css_value((m.group("iurl") if m.group("iurl") is not None else m.group("surl")) or "")
            if _is_font_css(target):
                return f"@import url({quoted(target.strip())});"
            return _stylesheet(target, ctx, rel_dir, depth) or ""
        return url_in(m.group("u") if m.group("u") is not None else m.group("uu"), False)

    return _CSS_TOKEN.sub(token, _image_set_strings(css))


def _image_set_strings(css: str) -> str:
    """image-set("a.png" 1x) -> image-set(url("a.png") 1x): bare strings there load images too."""
    out, last = [], 0
    for m in re.finditer(r"(?:-webkit-)?image-set\(", css, re.I):
        if m.start() < last:
            continue
        j, depth = m.end(), 1
        while j < len(css) and depth:
            depth += {"(": 1, ")": -1}.get(css[j], 0)
            j += 1
        inner = _IMAGE_SET_ITEM.sub(
            lambda s: f"url({s.group(1)}{s.group(2)}{s.group(1)})" if s.group(1) else s.group(0), css[m.end():j])
        out.append(css[last:m.end()] + inner)
        last = j
    return "".join(out) + css[last:]


def _stylesheet(href: str, ctx: _Ctx, rel_dir: str = "", depth: int = 0) -> str | None:
    """CSS text of a local stylesheet (rewritten), or None (refused and recorded)."""
    href = (href or "").strip()
    if depth >= MAX_IMPORT_DEPTH:
        ctx.refuse(href, "stylesheet imports nested too deep")
        return None
    href = _rebase(href, rel_dir)
    parts = urllib.parse.urlsplit(href)
    scheme = parts.scheme.lower()
    if href.startswith("//") or scheme not in ("", "file") or (scheme == "file" and parts.netloc not in ("", "localhost")):
        ctx.refuse(href, f"stylesheet not loaded (only from the HTML file's folder or {FONT_CSS_HOST})")
        return None
    path_text = urllib.parse.unquote(parts.path)
    try:
        data = fetch.read_local(path_text, ctx.base)
    except fetch.FetchError as e:
        ctx.refuse(href, e.reason)
        return None
    target = (ctx.base / path_text).resolve()
    sub = posixpath.dirname(Path(os.path.relpath(target, ctx.base.resolve())).as_posix())
    return _css(data.decode("utf-8", "replace"), ctx, "" if sub == "." else sub, depth + 1)


# --------------------------------------------------------------------- HTML

def _srcset(value: str, ctx: _Ctx) -> str:
    """srcset with each candidate resolved; refused candidates are left out."""
    out, i, n = [], 0, len(value)
    while i < n:
        while i < n and (value[i].isspace() or value[i] == ","):
            i += 1
        j = i
        while j < n and not value[j].isspace():
            j += 1
        url = value[i:j]
        desc = ""
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            k = value.find(",", j)
            k = n if k < 0 else k
            desc = value[j:k].strip()
            j = k
        i = j
        if url:
            got = ctx.image(url)
            if got:
                out.append(f"{got} {desc}".strip())
    return ", ".join(out)


class UnsafePage(ValueError):
    """A page that still holds a redirect or a foreign script after sanitising."""


def parse(html: str) -> BeautifulSoup:
    """html5lib parse (see the module notes); use it for every page Chrome will open."""
    try:
        return BeautifulSoup(html, "html5lib")
    except AssertionError as e:  # html5lib asserts on some malformed nesting
        raise UnsafePage(f"the HTML parser failed on the deck: {e}") from e


def _lock_down(soup: BeautifulSoup, script_nonce: str | None) -> None:
    scripts = f"'nonce-{script_nonce}'" if script_nonce else "'none'"
    policy = ("default-src 'none'; img-src data:; media-src data:; "
              f"font-src data: https://{FONT_FILE_HOST}; style-src 'unsafe-inline' https://{FONT_CSS_HOST}; "
              f"script-src {scripts}; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'")
    meta = soup.new_tag("meta")
    meta["http-equiv"] = "Content-Security-Policy"
    meta["content"] = policy
    head = soup.head
    if head is None:
        head = soup.new_tag("head")
        (soup.html or soup).insert(0, head)
    head.insert(0, meta)


def page(soup: BeautifulSoup, script_nonce: str | None = None) -> str:
    """The page text for Chrome: CSP first in head, then checked as Chrome will read it."""
    _lock_down(soup, script_nonce)
    text = str(soup)
    back = parse(text)
    metas = back.find_all("meta", attrs={"http-equiv": True})
    if [m["http-equiv"] for m in metas] != ["Content-Security-Policy"] or metas[0].parent is not back.head:
        raise UnsafePage("the page still holds a <meta http-equiv> after sanitising")
    if any(s.get("nonce") != script_nonce or not script_nonce for s in back.find_all("script")):
        raise UnsafePage("the page still holds a script after sanitising")
    return text


def _sanitize_soup(soup: BeautifulSoup, ctx: _Ctx) -> None:
    for node in [n for n in soup.descendants  # find_all(string=) skips empty comments (`<!---->`)
                 if isinstance(n, (Comment, CData, ProcessingInstruction, Declaration)) and not isinstance(n, Doctype)]:
        node.extract()
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    for tag in soup.find_all(["xmp", "plaintext"]):
        tag.name = "pre"
        tag.string = tag.get_text()
    for tag in soup.find_all(_PRE_TAGS):
        first = tag.contents[0] if tag.contents else None
        if isinstance(first, str) and first.startswith("\n"):
            first.replace_with("\n" + first)
    for tag in soup.find_all("meta"):
        if any(a.lower() == "http-equiv" for a in tag.attrs):
            tag.decompose()
    for tag in soup.find_all(["set", "animate"]):
        if (tag.get("attributename") or tag.get("attributeName") or "").lower() in ("href", "xlink:href", "src"):
            tag.decompose()

    for link in soup.find_all("link"):
        rel = [r.lower() for r in (link.get("rel") or [])]
        href = (link.get("href") or "").strip()
        if "stylesheet" in rel and _is_font_css(href):
            for a in list(link.attrs):
                if a.lower().startswith("on"):
                    del link[a]
            continue
        if "stylesheet" in rel and href:
            css = _stylesheet(href, ctx)
            if css is not None:
                style = soup.new_tag("style")
                if link.get("media"):
                    style["media"] = link["media"]
                style.string = css
                link.replace_with(style)
                continue
        link.decompose()

    for style in soup.find_all("style"):
        style.string = _css("".join(str(c) for c in style.contents), ctx)
        style.string = _style_text(style.string)

    for tag in soup.find_all(True):
        name = tag.name.lower()
        for attr in list(tag.attrs):
            low = attr.lower()
            if low.startswith("on"):
                del tag[attr]
                continue
            value = tag.get(attr)
            if isinstance(value, list):
                value = " ".join(value)
            if not isinstance(value, str):
                continue
            if low not in _URL_ATTRS:
                if re.search(r"url\(|image-set\(", _unescape(value), re.I):
                    tag[attr] = _css(value, ctx)
                continue
            v = value.strip()
            if low in ("src", "lowsrc") and name in ("img", "input"):
                tag[attr] = (ctx.image(v) if v else None) or BLANK_GIF
            elif low in ("srcset", "imagesrcset") and name in ("img", "source"):
                got = _srcset(v, ctx)
                if got:
                    tag[attr] = got
                else:
                    del tag[attr]
            elif low in ("href", "xlink:href") and name in _IMAGE_HREF_TAGS:
                got = ctx.image(v) if v and not v.startswith("#") else (v or None)
                if got:
                    tag[attr] = got
                else:
                    del tag[attr]
            elif low in ("poster", "background"):
                got = ctx.image(v) if v else None
                if got:
                    tag[attr] = got
                else:
                    del tag[attr]
            elif low == "href" and name == "link":
                pass  # only a Google Fonts stylesheet link is left (see above)
            elif low == "href" and name in ("a", "area"):
                if v.lower().startswith("javascript:"):
                    del tag[attr]
            elif low in ("href", "xlink:href") and v.startswith("#"):
                pass  # an in-page reference (SVG use, gradients)
            else:
                del tag[attr]


def safe_html(html: str, base_dir: Path, refused: list | None = None, images: bool = True) -> str:
    """The deck with everything Chrome could load from outside base_dir removed or inlined.
    Refused sources are appended to `refused` as {"src", "reason"} (each once).
    images=False blanks every image without reading it (same page structure, no network)."""
    ctx = _Ctx(Path(base_dir), refused, images)
    for _ in range(MAX_ROUNDS):
        soup = parse(html)
        _sanitize_soup(soup, ctx)
        out = str(soup)
        if out == html:
            return out
        html = out
    raise UnsafePage(f"the deck does not settle after {MAX_ROUNDS} sanitising rounds")


_cache: dict[str, tuple[tuple, str, list]] = {}
_lock = threading.Lock()


def _version(path: Path) -> tuple:
    st = path.stat()
    return st.st_mtime_ns, st.st_size


def safe_deck(path: Path) -> tuple[str, list[dict]]:
    """(safe_html of the file, refused) cached per file version; thread-safe."""
    path = Path(path).resolve()
    with _lock:
        version = _version(path)
        hit = _cache.get(str(path))
        if hit and hit[0] == version:
            return hit[1], hit[2]
        refused: list[dict] = []
        html = safe_html(path.read_text(encoding="utf-8"), path.parent, refused)
        _cache[str(path)] = (version, html, refused)
        return html, refused


def refusals(path: Path) -> list[dict]:
    """The refused sources of the last safe_deck of this file in this process, else []."""
    hit = _cache.get(str(Path(path).resolve()))
    return list(hit[2]) if hit else []
