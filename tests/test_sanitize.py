"""B1: every temp HTML handed to Chrome goes through sanitize.safe_html first.

Pure tests check the rewrite; the Chrome tests measure a deck with the reviewer's five
vectors (file:// img, ../ img, CSS url(file://), svg image href, http://127.0.0.1 img)
and check none of them reaches the plate."""
import base64
import http.server
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
    assert "url(#grad)" in out and 'href="#local"' in out
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
