"""B1: every temp HTML handed to Chrome goes through sanitize.safe_html first.

Pure tests check the rewrite; the Chrome tests measure a deck with the reviewer's five
vectors (file:// img, ../ img, CSS url(file://), svg image href, http://127.0.0.1 img)
and check none of them reaches the plate."""
import base64
import http.server
import re
import socket
import threading
from pathlib import Path

import pytest
from PIL import Image

from gslider import assets, measure, sanitize

needs_chrome = pytest.mark.skipif(not assets.CHROME, reason="Google Chrome not installed")

RED, GREEN, BLUE, YELLOW, WHITE = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 255, 255)


def _png(path: Path, rgb, size=(40, 40)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, rgb).save(path)
    return path


@pytest.fixture
def folder(tmp_path):
    """tmp/deck/ (the HTML folder) with img/ok.png (blue); tmp/secret/s.png (red) outside it."""
    deck = tmp_path / "deck"
    _png(deck / "img" / "ok.png", BLUE)
    _png(tmp_path / "secret" / "s.png", RED)
    return deck


def _clean(html: str, base: Path, refused=None) -> str:
    return sanitize.safe_html(html, base, refused)


# --- pure ---------------------------------------------------------------------------

def test_scripts_frames_and_handlers_removed(folder):
    out = _clean('<html><body><script>alert(1)</script><svg><script>x()</script></svg>'
                 '<iframe src="a.html"></iframe><object data="a"></object><embed src="b">'
                 '<img src="img/ok.png" onerror="evil()" onload="evil()">'
                 '<meta http-equiv="refresh" content="0;url=file:///etc/passwd"><base href="file:///">'
                 '</body></html>', folder)
    for bad in ("<script", "<iframe", "<object", "<embed", "onerror", "onload", "evil", "refresh", "<base"):
        assert bad not in out, bad


def test_local_image_inlined_outside_refused(folder, tmp_path):
    refused = []
    secret = (tmp_path / "secret" / "s.png").as_uri()
    out = _clean(f'<img src="img/ok.png"><img src="../secret/s.png"><img src="{secret}">'
                 f'<img src="http://127.0.0.1:1/x.png">', folder, refused)
    assert out.count("data:image/png;base64,") >= 1
    assert "secret" not in out and "127.0.0.1" not in out
    srcs = {r["src"] for r in refused}
    assert srcs == {"../secret/s.png", secret, "http://127.0.0.1:1/x.png"}
    assert all(r["reason"] for r in refused)
    # a refused <img> keeps its box but shows nothing (a transparent pixel, no alt text)
    assert out.count(sanitize.BLANK_GIF) == 3


def test_srcset_poster_svg_image_and_css_urls(folder, tmp_path):
    secret = (tmp_path / "secret" / "s.png").as_uri()
    html = (f'<style>.a{{background:url("{secret}") center/cover}} .b{{background-image:url(img/ok.png)}}'
            f'.c{{fill:url(#grad)}}</style>'
            f'<div style="background:url({secret})"></div>'
            f'<picture><source srcset="{secret} 2x, img/ok.png 1x"><img srcset="../secret/s.png 1x"></picture>'
            f'<video poster="{secret}" src="movie.mp4"></video>'
            f'<svg><image href="{secret}"/><image xlink:href="../secret/s.png"/>'
            f'<use href="other.svg#x"/><use href="#local"/><rect fill="url(file:///x.svg#g)"/></svg>')
    out = _clean(html, folder)
    assert "secret" not in out and "file:" not in out and "other.svg" not in out and "movie.mp4" not in out
    assert 'url("#grad")' in out and 'href="#local"' in out
    assert out.count("data:image/png;base64,") == 2  # .b and the srcset candidate inside the folder


def test_css_escapes_cannot_hide_a_url(folder, tmp_path):
    secret = (tmp_path / "secret" / "s.png").as_uri()
    out = _clean(f'<style>.a{{background:\\75 rl({secret})}} .b{{background:u\\rl({secret})}}</style>', folder)
    assert "secret" not in out


def test_image_set_strings_rewritten(folder, tmp_path):
    secret = (tmp_path / "secret" / "s.png").as_uri()
    out = _clean(f'<style>.a{{background-image:image-set("{secret}" 1x, "img/ok.png" 2x)}}</style>', folder)
    assert "secret" not in out and "data:image/png;base64," in out


def test_data_uris_kept(folder):
    jpeg = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff" + b"0" * 10).decode()
    font = "data:font/woff2;base64,d09GMgABAAAA"
    out = _clean(f'<style>@font-face{{font-family:X;src:url({font}) format("woff2")}}'
                 f'.a{{background:url({jpeg})}}</style><img src="{jpeg}">', folder)
    assert out.count(jpeg) == 2 and font in out


def test_stylesheets(folder, tmp_path):
    (folder / "css").mkdir()
    (folder / "css" / "in.css").write_text(".in{background:url(../img/ok.png)} .x{color:#123456}")
    (tmp_path / "out.css").write_text(".out{color:#654321}")
    refused = []
    out = _clean('<head><link rel="stylesheet" href="css/in.css"><link rel="stylesheet" href="../out.css">'
                 '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">'
                 '<link rel="stylesheet" href="https://cdn.example.com/x.css">'
                 '<link rel="preload" href="file:///etc/passwd"><link rel="icon" href="x.ico"></head>', folder, refused)
    assert "#123456" in out and "data:image/png;base64," in out  # inside: inlined, its url() rewritten
    assert "#654321" not in out and "out.css" not in out
    assert 'href="https://fonts.googleapis.com/css2?family=Inter"' in out
    assert "cdn.example.com" not in out and "passwd" not in out and "x.ico" not in out
    assert {r["src"] for r in refused} >= {"../out.css", "https://cdn.example.com/x.css"}


def test_font_urls(folder):
    gstatic = "https://fonts.gstatic.com/s/inter/v1/a.woff2"
    out = _clean('<style>@import url("https://fonts.googleapis.com/css2?family=Inter");'
                 '@import url("../evil.css");'
                 f'@font-face{{font-family:A;src:url({gstatic}) format("woff2"), url(file:///x.woff2)}}'
                 f'.a{{background:url({gstatic})}}</style>', folder)
    assert out.count(gstatic) == 1  # kept in @font-face only
    assert "fonts.googleapis.com/css2?family=Inter" in out
    assert "evil.css" not in out and "file:" not in out


def test_safe_deck_is_cached_and_lists_refusals(folder, tmp_path):
    page = folder / "deck.html"
    page.write_text('<img src="../secret/s.png"><img src="../secret/s.png">')
    html1, ref1 = sanitize.safe_deck(page)
    html2, ref2 = sanitize.safe_deck(page)
    assert html1 is html2 and ref1 == ref2
    assert ref1 == [{"src": "../secret/s.png", "reason": ref1[0]["reason"]}]  # listed once
    assert sanitize.refusals(page) == ref1


def test_chrome_flags_block_the_network():
    flags = " ".join(sanitize.CHROME_FLAGS)
    assert "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE fonts.googleapis.com, EXCLUDE fonts.gstatic.com" in flags


def test_prepare_html_uses_the_sanitiser(folder, tmp_path):
    page = folder / "deck.html"
    page.write_text('<div class="slide-wrap"><div class="slide"><img src="../secret/s.png">'
                    '<script>forge()</script></div></div>')
    html, out_id = measure.prepare_html(page, 1)
    assert "forge" not in html and "secret" not in html and "file://" not in html
    assert out_id in html and out_id != measure.OUT_ID  # per-run id passed into measure.js


def test_render_uses_the_sanitiser(folder, monkeypatch, tmp_path):
    page = folder / "deck.html"
    page.write_text('<div class="slide-wrap"><div class="slide"><img src="../secret/s.png"></div></div>')
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["html"] = Path(cmd[-1][len("file://"):]).read_text()
        shot = next(a for a in cmd if a.startswith("--screenshot="))[len("--screenshot="):]
        Path(shot).write_bytes(b"png")
        return assets.subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(assets.subprocess, "run", fake_run)
    monkeypatch.setattr(assets, "OUT", tmp_path / "o")
    assets.render_slide_png(page, 1, tmp_path / "s.png")
    assert "secret" not in seen["html"] and "file://" not in seen["html"]
    assert all(f in seen["cmd"] for f in sanitize.CHROME_FLAGS)


# --- real Chrome ----------------------------------------------------------------------

class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture
def loopback(tmp_path):
    """An HTTP server on 127.0.0.1 serving srv.png (green)."""
    root = tmp_path / "srv"
    _png(root / "srv.png", GREEN)
    handler = lambda *a, **k: _Quiet(*a, directory=str(root), **k)  # noqa: E731
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def _area(img, box) -> set:
    return set(img.crop(box).getdata())


@needs_chrome
def test_five_vectors_absent_from_plate(folder, tmp_path, loopback):
    secret = (tmp_path / "secret" / "s.png").as_uri()
    (folder / "in.css").write_text(".inside{background:#ffff00}")
    (tmp_path / "outside.css").write_text(".outside{background:#ff0000 !important}")
    pos = "position:absolute;width:200px;height:200px;top:400px;"
    page = folder / "deck.html"
    page.write_text(f"""<!doctype html><html><head>
<link rel="stylesheet" href="in.css"><link rel="stylesheet" href="../outside.css">
<style>*{{margin:0}} .slide-wrap,.slide{{width:1920px;height:1080px;position:relative;background:#fff}}
.bg{{{pos}left:700px;background:url({secret})}}</style></head><body>
<div class="slide-wrap"><div class="slide">
<p style="position:absolute;left:100px;top:100px;font:40px Georgia">Text</p>
<img src="{secret}" style="{pos}left:100px">
<img src="../secret/s.png" style="{pos}left:400px">
<div class="bg"></div>
<svg style="{pos}left:1000px" width="200" height="200"><image href="{secret}" width="200" height="200"/></svg>
<img src="http://127.0.0.1:{loopback}/srv.png" style="{pos}left:1300px">
<img src="img/ok.png" style="{pos}left:1600px">
<div class="inside" style="{pos}left:100px;top:700px"></div>
<div class="outside" style="{pos}left:400px;top:700px"></div>
</div></div></body></html>""")
    m = measure.measure_slide(page, 1, tmp_path / "m")
    img = Image.open(m["plate"]).convert("RGB")
    for left in (100, 400, 700, 1000, 1300):
        colours = _area(img, (left + 20, 420, left + 180, 580))
        assert RED not in colours and GREEN not in colours, (left, colours)
    assert _area(img, (1620, 420, 1780, 580)) == {BLUE}  # an image inside the folder still loads
    assert _area(img, (120, 720, 280, 880)) == {YELLOW}  # stylesheet inside the folder applied
    assert _area(img, (420, 720, 580, 880)) == {WHITE}  # stylesheet outside the folder not applied
    assert [b["runs"][0]["text"] for b in m["blocks"]] == ["Text"]
    refused = {r["src"] for r in m["images_not_loaded"]}
    assert {secret, "../secret/s.png", f"http://127.0.0.1:{loopback}/srv.png"} <= refused


@needs_chrome
def test_deck_script_cannot_forge_measure_output(tmp_path):
    page = tmp_path / "deck.html"
    page.write_text("""<html><body><div class="slide-wrap" style="width:1920px;height:1080px">
<p style="font:40px Georgia">Real text</p></div>
<img src="x" onerror="var s=document.createElement('script');s.id='gslider-measure';s.type='application/json';
s.textContent='{&quot;status&quot;:&quot;done&quot;,&quot;blocks&quot;:[]}';document.body.appendChild(s)">
<script>var s=document.createElement('script');s.id='gslider-measure';s.type='application/json';
s.textContent='{"status":"done","background":"#123456","blocks":[]}';document.body.appendChild(s);</script>
</body></html>""")
    m = measure.measure_slide(page, 1, tmp_path / "m")
    assert [b["runs"][0]["text"] for b in m["blocks"]] == ["Real text"]
    assert m["background"] != "#123456"


def _online() -> bool:
    try:
        socket.create_connection(("fonts.googleapis.com", 443), timeout=5).close()
        return True
    except OSError:
        return False


@needs_chrome
@pytest.mark.network
def test_google_fonts_still_load(tmp_path):
    if not _online():
        pytest.skip("offline")
    page = tmp_path / "deck.html"
    page.write_text("""<html><head>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Lobster&display=block">
<style>.slide-wrap{width:1920px;height:1080px} p{font:60px 'Lobster', serif}</style></head>
<body><div class="slide-wrap"><p>Fonts load</p></div></body></html>""")
    m = measure.measure_slide(page, 1, tmp_path / "m")
    assert m["unloaded"] == [] and m["blocks"][0]["runs"][0]["family"] == "Lobster"


# --- parser differential and the CSP backstop (re-review B1) ---------------------------

_COMMENT_TRICKS = (
    "<!--><img src='{s}'>-->",  # an abruptly closed comment: empty to Chrome, open to html.parser
    "<!---><img src='{s}'>-->",
    "<!-- a --!><img src='{s}'>-->",
)


@pytest.mark.parametrize("trick", _COMMENT_TRICKS)
def test_comment_tricks_cannot_hide_markup(folder, tmp_path, trick):
    secret = (tmp_path / "secret" / "s.png").as_uri()
    out = _clean(f"<div>{trick.format(s=secret)}</div>", folder)
    assert "secret" not in out and "<!--" not in out


def test_comments_removed(folder):
    out = _clean("<p>a<!-- note --></p><![CDATA[x]]><?pi y?>", folder)
    assert "note" not in out and "<!" not in out and "<?" not in out


def test_csp_first_in_head_and_nonce_on_measure_script(folder):
    page = folder / "deck.html"
    page.write_text('<html><head><style>p{}</style></head><body><div class="slide-wrap"></div></body></html>')
    html, _ = measure.prepare_html(page, 1)
    soup = sanitize.parse(html)
    first = soup.head.find(True)
    assert first.name == "meta" and first["http-equiv"] == "Content-Security-Policy"
    policy = first["content"]
    assert "default-src 'none'" in policy and "'unsafe-inline'" not in policy.split("script-src")[1].split(";")[0]
    nonce = soup.find("script", id="gslider-measure-js")["nonce"]
    assert f"'nonce-{nonce}'" in policy


def test_render_page_has_csp_without_scripts(folder, monkeypatch, tmp_path):
    page = folder / "deck.html"
    page.write_text('<div class="slide-wrap"><div class="slide">x</div></div>')
    seen = {}

    def fake_run(cmd, **kw):
        seen["html"] = Path(cmd[-1][len("file://"):]).read_text()
        shot = next(a for a in cmd if a.startswith("--screenshot="))[len("--screenshot="):]
        Path(shot).write_bytes(b"png")
        return assets.subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(assets.subprocess, "run", fake_run)
    monkeypatch.setattr(assets, "OUT", tmp_path / "o")
    assets.render_slide_png(page, 1, tmp_path / "s.png")
    meta = sanitize.parse(seen["html"]).head.find(True)
    assert meta["http-equiv"] == "Content-Security-Policy" and "script-src 'none'" in meta["content"]


def _red_text_page(folder: Path, tmp_path: Path, body: str) -> Path:
    (tmp_path / "secret" / "t.html").write_text(
        "<body style='margin:0;background:#ff0000'>secret</body>")
    page = folder / "deck.html"
    page.write_text("<!doctype html><html><head><style>*{margin:0} .slide-wrap{width:1920px;height:1080px;"
                    "position:relative;background:#fff}</style></head><body><div class='slide-wrap'>"
                    f"<p style='position:absolute;left:100px;top:100px;font:40px Georgia'>Text</p>{body}"
                    "</div></body></html>")
    return page


@needs_chrome
def test_comment_trick_absent_from_plate(folder, tmp_path):
    s, t = (tmp_path / "secret" / "s.png").as_uri(), (tmp_path / "secret" / "t.html").as_uri()
    pos = "position:absolute;top:400px;width:300px;height:300px"
    page = _red_text_page(folder, tmp_path,
                          f"<!--><img src='{s}' style='{pos};left:100px'>"
                          f"<iframe src='{t}' style='{pos};left:500px;border:0'></iframe>-->")
    m = measure.measure_slide(page, 1, tmp_path / "m")
    img = Image.open(m["plate"]).convert("RGB")
    assert RED not in _area(img, (100, 400, 900, 700))


@needs_chrome
def test_csp_blocks_local_files_even_unsanitised(folder, tmp_path, monkeypatch):
    """Layer two on its own: hand Chrome the raw deck (sanitiser skipped); the CSP still blocks
    file:// images and frames."""
    s, t = (tmp_path / "secret" / "s.png").as_uri(), (tmp_path / "secret" / "t.html").as_uri()
    pos = "position:absolute;top:400px;width:300px;height:300px"
    page = _red_text_page(folder, tmp_path,
                          f"<img src='{s}' style='{pos};left:100px'>"
                          f"<iframe src='{t}' style='{pos};left:500px;border:0'></iframe>"
                          f"<div style='{pos};left:900px;background:url({s})'></div>")
    monkeypatch.setattr(sanitize, "safe_deck", lambda p: (Path(p).read_text(), []))
    m = measure.measure_slide(page, 1, tmp_path / "m")
    img = Image.open(m["plate"]).convert("RGB")
    assert RED not in _area(img, (100, 400, 1200, 700))
    assert [b["runs"][0]["text"] for b in m["blocks"]] == ["Text"]  # our nonce'd script still ran


# --- mutation through foreign-content <style> (re-review pass 3) ------------------------

_REDIRECT = "<meta http-equiv='refresh' content='0;url={t}'>"
_MUTATIONS = (
    "<math><mtext><table><mglyph><style>" + _REDIRECT,
    "<svg><style>&lt;/style&gt;" + _REDIRECT.replace("<", "&lt;").replace(">", "&gt;") + "</style></svg>",
    "<math><style>&lt;/style&gt;&lt;img src='{t}'&gt;</style></math>",
)


@pytest.mark.parametrize("trick", _MUTATIONS)
def test_output_reparses_to_nothing_live(folder, tmp_path, trick):
    """What Chrome reads back from safe_html has no redirect, no outside path, and is stable."""
    t = (tmp_path / "secret" / "t.html").as_uri()
    out = _clean(f"<div class='slide-wrap'>{trick.format(t=t)}</div>", folder)
    again = sanitize.parse(out)
    assert not again.find("meta", attrs={"http-equiv": True})
    assert not [i for i in again.find_all("img") if "secret" in i.get("src", "")]
    assert _clean(out, folder) == out  # a fixed point


def test_style_text_never_holds_a_tag_start(folder):
    out = _clean("<svg><style>&lt;/style&gt;&lt;b&gt;</style></svg><style>p::after{content:'<i>'}</style>", folder)
    bodies = re.findall(r"<style[^>]*>(.*?)</style", out, re.S | re.I)
    assert bodies and not any(re.search(r"<[A-Za-z/!?]", b) for b in bodies)


def test_style_keeps_media_ranges_and_cdo(folder):
    out = _clean("<style><!-- p{color:red} --> @media (width < 600px){a{b:c}} @media (1px<width<=2px){d{e:f}}"
                 "</style>", folder)
    assert "p{color:red}" in out and "(width < 600px)" in out and "(1px< width<=2px)" in out
    assert "<!--" not in out


def test_style_strings_and_urls_escape_less_than(folder):
    out = _clean("<style>p::after{content:'<b>'} a{background:url(data:image/svg+xml,<svg/>)}</style>", folder)
    assert 'content:"\\3c b>"' in out and 'url("data:image/svg+xml,\\3c svg/>")' in out


def test_svg_data_uri_with_parens_and_quotes_survives(folder):
    svg = ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'><path transform='rotate(45)' "
           "fill='rgb(1,2,3)' d='M0 0'/></svg>")
    out = _clean(f'<style>.a{{background:url("{svg}")}}</style>', folder)
    css = re.search(r"<style>(.*?)</style>", out, re.S).group(1)
    assert "< /" not in css and css.count("\\3c ") == 3
    dq = "data:image/svg+xml,%3Csvg xmlns=\"http://www.w3.org/2000/svg\"%3E%3C/svg%3E"
    out = _clean(f"<style>.b{{background:url('{dq}')}}</style>", folder)
    assert 'url("data:image/svg+xml,%3Csvg xmlns=\\"http://www.w3.org/2000/svg\\"%3E%3C/svg%3E")' in out


def test_comment_quote_does_not_desync_strings(folder):
    out = _clean("<style>/* don't */ .a::after{content:'<b>'} /* <i> */</style>", folder)
    assert 'content:"\\3c b>"' in out and "<i" not in out and "/**/" in out


def test_cdo_kept_inside_strings(folder):
    out = _clean("<style><!-- p{x:y} --> a::after{content:\"a<!--b\"}</style>", folder)
    assert '"a\\3c !--b"' in out and "<!--" not in out


def _style(out: str) -> str:
    return re.search(r"<style>(.*?)</style>", out, re.S).group(1)


def test_commented_import_and_url_not_loaded(folder):
    refused = []
    css = ("/* @import url('https://fonts.googleapis.com/css2?family=Inter') */ .x{background:#0cc}"
           " /* url(old.png) */ a::after{content:'url(x.png)'}")
    out = _style(_clean(f"<style>{css}</style>", folder, refused))
    assert out == '/**/ .x{background:#0cc} /**/ a::after{content:"url(x.png)"}' and refused == []


def test_digit_escapes_in_selectors_kept(folder):
    css = r".\31 0{color:red} .\32xl\:grid-cols-4{display:grid}"
    assert _style(_clean(f"<style>{css}</style>", folder)) == r".\31 0{color:red} .\32 xl\:grid-cols-4{display:grid}"


def test_escaped_quote_in_selector_does_not_hide_urls(folder):
    out = _style(_clean(r"<style>.a\'b{} .bg-\[url\(\'x\'\)\]{} .c{background:url(img/ok.png)}</style>", folder))
    assert 'url("data:image/png;base64,' in out


def test_bad_url_stays_bad_and_later_urls_rewritten(folder, tmp_path):
    secret = (tmp_path / "secret" / "s.png").as_uri()
    out = _style(_clean(f"<style>.a{{background:url(a\"b)}} .b{{background:url({secret})}}</style>", folder))
    assert "secret" not in out and "url(gslider bad)" in out and "background:none" in out


def test_escaped_image_set_strings_rewritten(folder, tmp_path):
    secret = (tmp_path / "secret" / "s.png").as_uri()
    out = _clean(f'<style>.a{{background:image\\2d set("{secret}" 1x)}}</style>'
                 f'<p style=\'background:-webkit-image\\2d set("{secret}" 1x)\'>', folder)
    assert "secret" not in out


def test_unclosed_image_set_in_comment_harmless(folder):
    out = _style(_clean('<style>/* image-set( */ a{font-family:"Inter"} b::after{content:"x"}</style>', folder))
    assert 'font-family:"Inter"' in out and 'content:"x"' in out


def test_class_with_url_text_untouched(folder):
    out = _clean("<div class=\"bg-[url('img/ok.png')]\" data-x=\"url(x)\"></div>", folder)
    soup = sanitize.parse(out)
    assert soup.div["class"] == ["bg-[url('img/ok.png')]"] and soup.div["data-x"] == "url(x)"


def test_string_import_inlines_local_file(folder):
    (folder / "s.css").write_text(".s{color:red}")
    out = _style(_clean('<style>a::after{content:"q"} @import "s.css"; b{c:d}</style>', folder))
    assert ".s{color:red}" in out and 'content:"q"' in out


def test_hash_url_requoted(folder):
    out = _style(_clean('<style>a{fill:url("#g(1)")} b{background:url("#x) , url(file:///s.png")}</style>', folder))
    assert 'url("#g(1)")' in out and 'url("#x) , url(file:///s.png")' in out


def test_unquoted_url_with_escaped_parens(folder, tmp_path):
    (folder / "a(1).png").write_bytes((folder / "img" / "ok.png").read_bytes())
    out = _style(_clean(r"<style>a{background:url(a\(1\).png)}</style>", folder))
    assert out.startswith('a{background:url("data:image/png;base64,') and out.endswith('")}')


def test_string_open_at_end_of_style(folder):
    assert _style(_clean("<style>a::after{content:\"<b>", folder)).startswith('a::after{content:"\\3c b>"')


def test_requoted_url_escapes_backslash_and_newline(folder):
    out = _clean(r'<style>a{background:url("data:image/svg+xml,a\5c b\a c")}</style>', folder)
    assert r'url("data:image/svg+xml,a\\b\a c")' in out and _clean(out, folder) == out


def test_import_target_unescaped(folder):
    out = _clean(r'<style>@import "https://fonts.googleapis.com/css2?family=A\20 B";</style>', folder)
    assert '@import url("https://fonts.googleapis.com/css2?family=A B");' in out


def test_out_of_range_escape_does_not_crash(folder):
    out = _clean(r'<style>a{background:url("data:image/svg+xml,\110000\D800")}</style>', folder)
    assert "��" in out


def test_images_false_reads_nothing(folder, monkeypatch):
    monkeypatch.setattr(sanitize.fetch, "resolve_image", lambda *a: pytest.fail("read an image"))
    out = sanitize.safe_html("<img src='img/ok.png'><p style='background:url(img/ok.png)'>", folder, images=False)
    assert "ok.png" not in out


_BENIGN = (
    "<pre>\n\n\n\n\n\ncode</pre>",
    "<pre>\n\nline</pre>",
    "<textarea>\n\nx</textarea>",
    "<listing>\n\nx</listing>",
    "<xmp>if (a < b && c > d) { x &= y; }</xmp>",
    "<plaintext>a < b & c",
    "<noframes>a&b</noframes>",
    "<p>a<p>b</p></p><table><tr><td>x</td></tr></table>&nbsp;<br/><svg><rect/></svg>",
    "<template><p>t</p></template><noscript><p>n</p></noscript>",
)


@pytest.mark.parametrize("deck", _BENIGN)
def test_benign_decks_settle(folder, deck):
    out = _clean(f"<div class='slide-wrap'>{deck}</div>", folder)
    assert _clean(out, folder) == out


def test_pre_keeps_its_blank_first_line(folder):
    out = _clean("<pre>\n\nline</pre>", folder)
    assert sanitize.parse(out).pre.get_text() == "\nline"  # what Chrome shows for the original


def test_xmp_text_kept_as_pre(folder):
    out = _clean("<xmp>a < b</xmp>", folder)
    assert sanitize.parse(out).pre.get_text() == "a < b"


def test_parser_crash_is_unsafe_page(folder, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("resetInsertionMode")

    monkeypatch.setattr(sanitize, "BeautifulSoup", boom)
    with pytest.raises(sanitize.UnsafePage):
        sanitize.parse("<p>")


def test_safe_html_raises_when_it_cannot_settle(folder, monkeypatch):
    calls = iter(range(100))
    monkeypatch.setattr(sanitize, "_sanitize_soup", lambda soup, ctx: soup.append(str(next(calls))))
    with pytest.raises(sanitize.UnsafePage):
        _clean("<p>x</p>", folder)


def test_page_refuses_csp_outside_head():
    soup = sanitize.parse("<html><head></head><body></body></html>")
    soup.head.decompose()
    soup.html.insert(0, soup.new_tag("body"))
    text = sanitize.page(soup)  # lock_down adds a head when there is none
    assert sanitize.parse(text).head.find(True)["http-equiv"] == "Content-Security-Policy"


def test_every_meta_http_equiv_removed(folder):
    out = _clean("<head><meta http-equiv='set-cookie' content='a=b'><meta http-equiv='Refresh ' content='0'>"
                 "<meta charset='utf-8'><meta name='viewport' content='x'></head>", folder)
    soup = sanitize.parse(out)
    assert not soup.find("meta", attrs={"http-equiv": True})
    assert soup.find("meta", attrs={"charset": True}) and soup.find("meta", attrs={"name": "viewport"})


def test_page_refuses_a_live_redirect_or_foreign_script():
    soup = sanitize.parse("<html><head></head><body><p>x</p></body></html>")
    assert "Content-Security-Policy" in sanitize.page(soup)
    bad = sanitize.parse("<html><head></head><body><meta http-equiv=refresh content=0></body></html>")
    with pytest.raises(sanitize.UnsafePage):
        sanitize.page(bad)
    bad = sanitize.parse("<html><head></head><body><script>x()</script></body></html>")
    with pytest.raises(sanitize.UnsafePage):
        sanitize.page(bad, "abc")


@needs_chrome
@pytest.mark.parametrize("trick", [*_MUTATIONS[:2],
                                   "<noscript><style></noscript><meta http-equiv=refresh content='0;url={t}'>",
                                   "<math><mtext><style>&lt;/style&gt;<meta http-equiv=refresh content='0;url={t}'>"
                                   "</style></mtext></math>"])
def test_mutation_redirect_absent_from_plate_and_render(folder, tmp_path, monkeypatch, trick):
    t = (tmp_path / "secret" / "t.html").as_uri()
    page = _red_text_page(folder, tmp_path, trick.format(t=t))
    m = measure.measure_slide(page, 1, tmp_path / "m")
    assert RED not in set(Image.open(m["plate"]).convert("RGB").getdata())
    monkeypatch.setattr(assets, "OUT", tmp_path / "o")
    png = assets.render_slide_png(page, 1, tmp_path / "r.png")
    assert RED not in set(Image.open(png).convert("RGB").getdata())


@needs_chrome
def test_csp_keeps_good_content(folder, tmp_path):
    """data: images and backgrounds, in-page SVG gradients and <use> still draw under the CSP."""
    blue = "data:image/png;base64," + base64.b64encode((folder / "img" / "ok.png").read_bytes()).decode()
    pos = "position:absolute;top:400px;width:200px;height:200px"
    page = folder / "deck.html"
    page.write_text(f"""<!doctype html><html><head><style>*{{margin:0}}
.slide-wrap{{width:1920px;height:1080px;position:relative;background:#fff}}</style></head><body>
<div class="slide-wrap"><p style="position:absolute;left:100px;top:100px;font:40px Georgia">Text</p>
<img src="{blue}" style="{pos};left:100px">
<div style="{pos};left:400px;background:url({blue})"></div>
<svg style="{pos};left:700px" width="200" height="200"><defs><linearGradient id="g">
<stop offset="0" stop-color="#00ff00"/><stop offset="1" stop-color="#00ff00"/></linearGradient>
<rect id="r" width="200" height="200"/></defs><use href="#r" fill="url(#g)"/></svg>
</div></body></html>""")
    m = measure.measure_slide(page, 1, tmp_path / "m")
    img = Image.open(m["plate"]).convert("RGB")
    assert _area(img, (120, 420, 280, 580)) == {BLUE}
    assert _area(img, (420, 420, 580, 580)) == {BLUE}
    assert _area(img, (720, 420, 880, 580)) == {GREEN}


@needs_chrome
def test_media_range_still_applies(folder, tmp_path):
    page = folder / "deck.html"
    page.write_text("""<!doctype html><html><head><style>*{margin:0}
.slide-wrap{width:1920px;height:1080px;position:relative;background:#fff}
.box{position:absolute;left:100px;top:400px;width:200px;height:200px;background:#0000ff}
@media (width < 9999px){.box{background:#00ff00}}</style></head><body>
<div class="slide-wrap"><p style="position:absolute;left:100px;top:100px;font:40px Georgia">Text</p>
<div class="box"></div></div></body></html>""")
    m = measure.measure_slide(page, 1, tmp_path / "m")
    assert _area(Image.open(m["plate"]).convert("RGB"), (120, 420, 280, 580)) == {GREEN}


def test_random_css_has_no_tag_start_and_settles(folder):
    import random
    rnd = random.Random(7)
    parts = ["<", "a", "/", "!", "?", "\\", "'", '"', "(", ")", "{", "}", "url(", "image-set(", "/*", "*/",
             "<!--", "-->", "\n", " ", "#", "3c ", "x.png", "@import ", ";", "\\2d ", "svg"]
    for _ in range(300):
        css = "".join(rnd.choice(parts) for _ in range(rnd.randint(1, 30)))
        out = _clean(f"<svg><style>{css}</style></svg><style>{css}</style>", folder)
        for body in re.findall(r"<style>(.*?)</style>", out, re.S):
            assert not re.search(r"<[A-Za-z/!?]", body), (css, body)
        assert _clean(out, folder) == out, css


def _tokens(css: str) -> list:
    import tinycss2
    def flat(nodes):
        out = []
        for n in nodes:
            if n.type in ("comment", "whitespace"):
                continue
            if n.type == "function" and n.lower_name == "url":
                out.append(("url", flat(n.arguments)[0][1]))  # url("x") is url(x)
            elif n.type == "function":
                out += [("fn", n.lower_name), *flat(n.arguments), ")"]
            elif n.type.endswith("block"):
                out += [n.type, *flat(n.content)]
            else:
                out.append((n.type, getattr(n, "value", None)))
        return out
    return flat(tinycss2.parse_component_value_list(css))


def test_realistic_css_keeps_its_meaning(folder):
    css = (r"""@charset "utf-8";/*! tw */.\32xl\:grid-cols-4{grid-template-columns:repeat(4,minmax(0,1fr))}"""
           r""".font-\[\'Inter\'\]{font-family:'Inter'}@media (min-width:640px) and (width<=2000px)"""
           r"""{.sm\:p-4{padding:1rem!important}}@supports (display:grid){.g{display:grid}}"""
           r""":root{--j:{"a":1;"b":[2]};--c:#fff}.m{margin:0/**/auto;font:12px/**/Arial}"""
           r"""@keyframes k{0%{opacity:0}100%{opacity:1}}a[href^="http"]::after{content:"\201C \"q\""}"""
           r""":is(.a,.b):where(p)>span{color:red}@layer base{h1{font-weight:700}}"""
           r"""@font-face{font-family:X;src:local("X"),url(https://fonts.gstatic.com/s/x.woff2) format("woff2");"""
           r"""unicode-range:U+0000-00FF}""")
    out = _style(_clean(f"<style>{css}</style>", folder))
    assert _tokens(out) == _tokens(css)
    assert "0/**/auto" in out and "12px/**/Arial" in out


def test_cr_and_form_feed_in_strings_escaped(folder):
    out = _style(_clean(r'<style>a::after{content:"x\d y\c z";color:red}</style>', folder))
    assert out == r'a::after{content:"x\d y\c z";color:red}'


def test_image_set_with_var_or_env_becomes_none(folder, tmp_path):
    secret = "../secret/s.png"
    for css in (f'.t{{background:image-set(var(--no, "{secret}") 1x)}}',
                f'.t{{--x:"{secret}";background:image-set(var(--x) 1x)}}',
                f'.t{{background:-webkit-image-set(env(no, "{secret}") 1x)}}'):
        out = _style(_clean(f"<style>{css}</style>", folder))
        assert "image-set" not in out and "background:none" in out, out


def test_svg_animations_of_css_urls_rewritten(folder):
    out = _clean("<svg><rect><set attributeName='mask' to='url(http://127.0.0.1/a.png)'/>"
                 "<animate attributeName='clip-path' values='url(a.png);url(#c)' dur='1s'/>"
                 "<set attributeName='fill' to='blue'/><animate attributeName='opacity' values='0;1' dur='1s'/>"
                 "<animateColor attributeName='fill' to='url(http://x/a.png)'/>"
                 "<set attributeName='href' to='x.svg'/></rect></svg>", folder)
    assert "127.0.0.1" not in out and "a.png" not in out and "http://x" not in out and "x.svg" not in out
    soup = sanitize.parse(out)
    anims = {(a.name, a.get("attributeName")): a for a in soup.find_all(lambda t: t.name.lower() in
                                                                         ("set", "animate", "animatecolor"))}
    assert anims[("set", "fill")]["to"] == "blue" and anims[("animate", "opacity")]["values"] == "0;1"
    assert anims[("animate", "clip-path")]["values"] == 'none;url("#c")'
    assert anims[("set", "mask")]["to"] == "none"


@pytest.mark.parametrize("attr", ["stroke", "clip-path", "mask", "filter", "marker-start", "cursor"])
def test_svg_presentation_attributes_rewritten(folder, tmp_path, attr):
    out = _clean(f"<svg><rect {attr}='url(../secret/s.png)'/></svg>", folder)
    assert "secret" not in out


def test_bad_url_keeps_declaration_invalid(folder):
    out = _style(_clean('<style>.a{background:red;background:url(a"b)}</style>', folder))
    assert "background:red" in out and "url(gslider bad)" in out


def test_font_face_flag_ends_at_semicolon(folder):
    out = _style(_clean("<style>@font-face; .g{background:url(https://fonts.gstatic.com/s/x.woff2)}</style>", folder))
    assert "gstatic" not in out


def test_import_without_target_dropped(folder):
    refused = []
    out = _style(_clean("<style>@import layer(x); a{b:c}</style>", folder, refused))
    assert "@import" not in out and refused == []


def test_deep_css_nesting_is_unsafe_page(folder):
    with pytest.raises(sanitize.UnsafePage):
        _clean("<style>a{b:" + "(" * 3000 + ")" * 3000 + "}</style>", folder)


def test_custom_function_in_image_set_becomes_none(folder):
    refused = []
    css = '@function --f() { result: "../secret/s.png"; } .t{background:image-set(--f() 1x)}'
    out = _style(_clean(f"<style>{css}</style>", folder, refused))
    assert "image-set" not in out and any("image-set" in r["src"] for r in refused)


def test_deep_nesting_inside_image_set_is_unsafe_page(folder):
    with pytest.raises(sanitize.UnsafePage):
        _clean("<style>a{b:image-set(" + "(" * 3000 + ")" * 3000 + ")}</style>", folder)


@pytest.mark.parametrize("css", ["ul{list-style:square} ul{list-style:url(my img.png)}",
                                 "p{font-family:Inter} p{font-family:url(a b)}",
                                 "a{--x:url(a b);background:var(--x,lime)}"])
def test_bad_url_never_becomes_a_valid_word(folder, css):
    out = _style(_clean(f"<style>{css}</style>", folder))
    assert out.count("url(gslider bad)") == 1


@pytest.mark.parametrize("fn", ['if(media(width > 0): "../secret/s.png"; else: "x.png")', 'inherit(--x)',
                                'something-new("../secret/s.png")'])
def test_image_set_with_unknown_function_becomes_none(folder, fn):
    out = _style(_clean(f"<style>.t{{background:image-set({fn} 1x)}}</style>", folder))
    assert out == ".t{background:none}"


def test_image_set_with_gradient_kept(folder):
    out = _style(_clean("<style>.t{background:image-set(linear-gradient(rgb(1,2,3),red) 1x)}</style>", folder))
    assert out == ".t{background:image-set(linear-gradient(rgb(1,2,3),red) 1x)}"


def test_inlined_image_in_values_keeps_entry_count(folder):
    out = _clean("<svg><rect><animate attributeName='mask' values='url(img/ok.png);url(#m);red'"
                 " keyTimes='0;0.5;1'/></rect></svg>", folder)
    anim = sanitize.parse(out).find("animate")
    assert anim["values"] == 'none;url("#m");red'


def test_inlined_image_in_to_is_kept(folder):
    out = _clean("<svg><rect><set attributeName='mask' to='url(img/ok.png)'/></rect></svg>", folder)
    assert sanitize.parse(out).find("set")["to"].startswith('url("data:image/png;base64,')


@pytest.mark.parametrize("target", ["style", "src", "xlink:href"])
def test_href_src_style_animations_removed(folder, target):
    out = _clean(f"<svg><rect><animateTransform attributeName='{target}' to='x'/></rect></svg>", folder)
    assert not sanitize.parse(out).find(lambda t: t.name.lower() == "animatetransform")


def test_bad_url_reads_back_as_bad_url(folder):
    import tinycss2
    out = _style(_clean('<style>a{b:url(a"b)}</style>', folder))
    block = [n for n in tinycss2.parse_component_value_list(out) if n.type == "{} block"][0]
    assert any(n.type == "error" and n.kind == "bad-url" for n in block.content)
