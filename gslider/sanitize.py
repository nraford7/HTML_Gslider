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
  and every CSS `url(...)` (`style` and SVG presentation attributes, `<style>`
  blocks, inlined stylesheets, `image-set()` strings) go through `fetch.resolve_image`
  (template-style rules). Accepted bytes become `data:` URIs. A refused one becomes
  nothing: a transparent pixel for `img`/`input`, `none` in CSS, `local()` in a font src;
  each refusal is recorded once as {"src", "reason"}.
- Kept as they are: `data:` URIs (self-contained), `url(#id)` and `href="#id"` (in-page
  references), `<a>`/`<area>` links (never loaded), `@import` and `@font-face` urls from
  https://fonts.googleapis.com and https://fonts.gstatic.com (fonts only).
- A local font in `@font-face` (inside base_dir, WOFF/WOFF2/TTF/OTF bytes) is inlined.
- Other URL attributes (media `src`, `use` to another file, form actions, ...) are removed.
- CSS is read with tinycss2 (the CSS Syntax spec tokenizer Chrome follows), so escapes
  (`\\75 rl(`, `image\\2d set(`), comments, strings and bad urls mean what they mean to
  Chrome. The CSS is written back from the tokens: comment text dropped (`/**/` kept so
  tokens stay apart), `<!--` / `-->` dropped,
  every string and url quoted with `"`, `\\`, newline and `<` escaped, a `<` delimiter
  never directly before a letter, `/`, `!` or `?`. So style text has no `<` that could
  open a tag (bs4 writes style text raw, and inside SVG or MathML Chrome would read one).
  A bad url (`url(a"b)`) loads nothing in Chrome; it becomes the bad url
  `url(gslider bad)`, so its declaration stays invalid. An `image-set()` holding `var()`,
  `env()`, `attr()`, `inherit()` or a custom function becomes `none` (refused; the string it
  would load is not visible here). SVG animations (`set`, `animate`, ...) of an href, a src
  or style are removed; for a CSS url attribute their `to`, `from`, `by` and `values` go
  through the same CSS rewrite.
- The deck is parsed with html5lib (a WHATWG HTML5 parser, much closer to Chrome than
  html.parser), so `<!-->` and similar cannot hide markup. Comments, CDATA, processing
  instructions, every `<meta http-equiv>`, the MathML `mglyph` / `malignmark`, and `noscript`,
  `noframes`, `fencedframe` (Chrome runs with scripts on, so it never shows noscript) are
  removed. `xmp` and `plaintext` become `pre` (their raw text cannot round-trip).
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

import tinycss2
from bs4 import BeautifulSoup, CData, Comment, Declaration, Doctype, ProcessingInstruction

from gslider import fetch

CHROME_FLAGS = ("--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE fonts.googleapis.com, "
                "EXCLUDE fonts.gstatic.com",)
BLANK_GIF = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
FONT_CSS_HOST = "fonts.googleapis.com"
FONT_FILE_HOST = "fonts.gstatic.com"
MAX_IMPORT_DEPTH = 4
MAX_ROUNDS = 4
MAX_CSS_DEPTH = 200

_DROP_TAGS = ("script", "iframe", "frame", "frameset", "object", "embed", "portal", "applet", "base", "noembed",
              "mglyph", "malignmark", "noscript", "noframes", "fencedframe")
_PRE_TAGS = ("pre", "textarea", "listing")
_URL_ATTRS = ("src", "srcset", "href", "xlink:href", "poster", "data", "background", "lowsrc", "dynsrc",
              "action", "formaction", "manifest", "codebase", "longdesc", "ping", "imagesrcset", "srcdoc",
              "archive", "classid", "profile", "cite", "icon", "attributionsrc")
_IMAGE_HREF_TAGS = ("image", "feimage")
# attributes Chrome reads CSS url()s from
_CSS_ATTRS = ("style", "fill", "stroke", "clip-path", "mask", "filter", "marker-start", "marker-mid",
              "marker-end", "cursor")
_TAG_START = re.compile(r"[A-Za-z/!?]")
_IMAGE_SET = ("image-set", "-webkit-image-set")
_ANIMATE_TAGS = ("set", "animate", "animatetransform", "animatemotion", "animatecolor")
_SUBSTITUTED = ("var", "env", "attr", "inherit")  # and every custom function (--name())
_SURROGATE = re.compile("[\ud800-\udfff]")
_CLOSERS = {"}": "}", ")": ")", "]": "]"}
_FONT_MAGIC = (b"wOF2", b"wOFF", b"\x00\x01\x00\x00", b"OTTO", b"true")
_FONT_MIME = {b"wOF2": "font/woff2", b"wOFF": "font/woff", b"OTTO": "font/otf"}

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


def _quoted(text: str) -> str:
    """A CSS string for text: `"`, `\\`, newline, CR, form feed and `<` escaped."""
    return '"' + (text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\a ")
                  .replace("\r", "\\d ").replace("\f", "\\c ").replace("<", "\\3c ")) + '"'


def _leaf(node) -> str:
    """A token as CSS text; an escaped `<` in a name becomes `\\3c `."""
    return node.serialize().replace("\\<", "\\3c ")


def _has_url(nodes, level: int = 0) -> bool:
    if level > MAX_CSS_DEPTH:
        raise UnsafePage(f"CSS nested deeper than {MAX_CSS_DEPTH} levels")
    for n in nodes:
        if n.type == "url" or (n.type == "error" and n.kind == "bad-url"):
            return True
        if n.type == "function" and n.lower_name in ("url", "image-set", "-webkit-image-set"):
            return True
        if n.type in ("function", "() block", "[] block", "{} block") and _has_url(
                n.arguments if n.type == "function" else n.content, level + 1):
            return True
    return False


def _has_fn(nodes, names, level: int = 0) -> bool:
    """A function in names, or a custom function (`--f()`), anywhere in nodes."""
    if level > MAX_CSS_DEPTH:
        raise UnsafePage(f"CSS nested deeper than {MAX_CSS_DEPTH} levels")
    for n in nodes:
        if n.type == "function" and (n.lower_name in names or n.lower_name.startswith("--")
                                     or _has_fn(n.arguments, names, level + 1)):
            return True
        if n.type in ("() block", "[] block", "{} block") and _has_fn(n.content, names, level + 1):
            return True
    return False


def _css(css: str, ctx: _Ctx, rel_dir: str = "", depth: int = 0) -> str:
    """CSS with every url() rewritten, written back from tinycss2 tokens (see the module doc)."""

    def url_in(value: str, font: bool) -> str:
        v = value.strip()
        if v.startswith("#"):
            return f"url({_quoted(v)})"  # an in-page reference
        if font:
            got = ctx.font(_rebase(v, rel_dir))
            return f"url({_quoted(got)})" if got else 'local("gslider-not-loaded")'
        got = ctx.image(_rebase(v, rel_dir))
        return f"url({_quoted(got)})" if got else "none"

    def url_arg(fn) -> str | None:
        """The string of url("..."), else None."""
        args = [a for a in fn.arguments if a.type not in ("whitespace", "comment")]
        return args[0].value if len(args) == 1 and args[0].type == "string" else None

    def import_rule(nodes, i) -> tuple[str, int]:
        """(@import replacement, index after its `;`) for the at-keyword at nodes[i]."""
        j, target = i + 1, None
        while j < len(nodes) and not (nodes[j].type == "literal" and nodes[j].value == ";"):
            n = nodes[j]
            if target is None and n.type in ("string", "url"):
                target = n.value
            elif target is None and n.type == "function" and n.lower_name == "url":
                target = url_arg(n) or ""
            j += 1
        target = (target or "").strip()
        if not target:
            return "", j + 1  # no target: nothing to load
        if _is_font_css(target):
            return f"@import url({_quoted(target)});", j + 1
        return _stylesheet(target, ctx, rel_dir, depth) or "", j + 1

    def write(nodes, font: bool = False, image_set: bool = False, level: int = 0) -> str:
        if level > MAX_CSS_DEPTH:
            raise UnsafePage(f"CSS nested deeper than {MAX_CSS_DEPTH} levels")
        out: list[str] = []
        i = 0
        face = False  # the next {} block is an @font-face body
        while i < len(nodes):
            n = nodes[i]
            t = n.type
            if t == "at-keyword" and n.lower_value == "import":
                text, i = import_rule(nodes, i)
                out.append(text)
                continue
            if t == "comment":
                piece = "/**/"  # keeps tokens apart (`0/**/auto`); the text is dropped
            elif t == "literal" and n.value == ";":
                face = False
                piece = ";"
            elif t == "at-keyword":
                face = n.lower_value == "font-face"
                piece = _leaf(n)
            elif t == "url":
                piece = url_in(n.value, font)
            elif t == "string":
                piece = url_in(n.value, False) if image_set else _quoted(n.value)
            elif t == "function" and n.lower_name == "url":
                arg = url_arg(n)
                piece = url_in(arg, font) if arg is not None else "none"
            elif t == "function" and n.lower_name in _IMAGE_SET and _has_fn(n.arguments, _SUBSTITUTED):
                ctx.refuse(f"{n.name}({tinycss2.serialize(n.arguments)[:80]})",
                           "image-set() fed by var(), env(), attr() or a custom function is not loaded")
                piece = "none"  # the string it would load is not visible here
            elif t == "function":
                inner = write(n.arguments, font, n.lower_name in _IMAGE_SET, level + 1)
                piece = _leaf(tinycss2.ast.IdentToken(0, 0, n.name)) + "(" + inner + ")"
            elif t == "{} block":
                piece = "{" + write(n.content, font or face, False, level + 1) + "}"
                face = False
            elif t in ("() block", "[] block"):
                piece = n.type[0] + write(n.content, font, image_set, level + 1) + n.type[1]
            elif t == "error":
                # a bad url makes its declaration invalid in Chrome; write one that loads nothing
                piece = _CLOSERS.get(n.kind) or ("url(gslider bad)" if n.kind == "bad-url" else " ")
            elif t == "literal" and n.value in ("<!--", "-->"):
                piece = ""
            else:
                piece = _leaf(n)
            if not piece:
                i += 1
                continue
            if out and out[-1].endswith("<") and _TAG_START.match(piece):
                out.append(" ")
            out.append(piece)
            i += 1
        return "".join(out)

    out = write(tinycss2.parse_component_value_list(css))
    return _SURROGATE.sub("\ufffd", out)  # tinycss2 keeps a lone surrogate escape; CSS reads U+FFFD


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
    for tag in soup.find_all(lambda t: t.name.lower() in _ANIMATE_TAGS):
        target = (tag.get("attributename") or tag.get("attributeName") or "").strip().lower()
        if target in ("href", "xlink:href", "src", "style"):
            tag.decompose()
        elif target in _CSS_ATTRS:
            for a in ("to", "from", "by", "values"):
                if isinstance(tag.get(a), str) and _has_url(tinycss2.parse_component_value_list(tag[a])):
                    tag[a] = _css(tag[a], ctx)

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
                if low in _CSS_ATTRS and _has_url(tinycss2.parse_component_value_list(value)):
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
