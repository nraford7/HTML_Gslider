import socket
from pathlib import Path

import pytest
from PIL import Image

from gslider import assets, measure

FIXTURES = Path(__file__).parent / "fixtures"
needs_chrome = pytest.mark.skipif(not assets.CHROME, reason="Google Chrome not installed")


def _text(block):
    return "".join(r["text"] for r in block["runs"])


def _block(m, needle):
    found = [b for b in m["blocks"] if needle in _text(b)]
    assert found, f"no block with {needle!r} in {[_text(b) for b in m['blocks']]}"
    return found[0]


def _hex_rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _ink(img, rect, bg, threshold=40):
    """Pixels inside rect that differ from bg by more than threshold on any channel."""
    x, y, w, h = (int(round(v)) for v in rect)
    crop = img.crop((max(x, 0), max(y, 0), min(x + w, img.width), min(y + h, img.height)))
    px = list(crop.getdata())
    ink = sum(1 for p in px if max(abs(p[i] - bg[i]) for i in range(3)) > threshold)
    return ink, len(px)


@pytest.fixture(scope="module")
def style_measure(tmp_path_factory):
    if not assets.CHROME:
        pytest.skip("Google Chrome not installed")
    return measure.measure_slide(FIXTURES / "html_style.html", 1, tmp_path_factory.mktemp("measure"))


# --- pure Python ----------------------------------------------------------------

def test_read_measure_json_from_dump():
    dump = ('<html><body><p>x</p><script type="application/json" id="gslider-measure">'
            '{"status":"done","text":"a \\u003c b"}</script></body></html>')
    assert measure.read_measure_json(dump) == {"status": "done", "text": "a < b"}


def test_read_measure_json_missing_or_pending():
    with pytest.raises(measure.MeasureError, match="no measure output"):
        measure.read_measure_json("<html><body></body></html>")
    with pytest.raises(measure.MeasureError, match="pending"):
        measure.read_measure_json('<script type="application/json" id="gslider-measure">'
                                  '{"status":"pending"}</script>')


def test_plate_css_hides_gradient_text():
    css = measure.PLATE_CSS
    assert "-webkit-text-fill-color: transparent" in css
    assert "-webkit-text-stroke: 0" in css
    assert "background-clip: border-box" in css
    assert "background-image: none" in css
    assert "color: transparent" in css and "text-shadow: none" in css


def test_iframes_removed_from_temp_copy(tmp_path):
    src = tmp_path / "deck.html"
    src.write_text(
        '<html><head><style>.x{color:red}</style></head><body>'
        '<div class="slide-wrap"><div class="slide"><p>Hi</p>'
        '<iframe src="https://example.com"></iframe><object data="a.swf"></object>'
        '<embed src="b.swf"></div></div></body></html>', encoding="utf-8")
    html = measure.prepare_html(src, 1)
    for tag in ("<iframe", "<object", "<embed"):
        assert tag not in html
    assert "gslider-render" in html
    assert 'id="gslider-plate-css"' in html
    assert "gslider-measure" in html  # the injected script
    with pytest.raises(ValueError, match="slide 2"):
        measure.prepare_html(src, 2)


def test_measure_js_is_package_data():
    assert measure.MEASURE_JS.exists()
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert "measure.js" in pyproject


# --- real Chrome ----------------------------------------------------------------

@needs_chrome
def test_measure_text_blocks(style_measure):
    m = style_measure
    assert m["n"] == 1
    assert m["background"] == "#f4f0e7"
    assert m["fonts_status"] == "loaded"

    h1 = _block(m, "Plain heading")
    assert abs(h1["x"] - 120) <= 2 and 95 <= h1["y"] <= 115
    assert 0 <= h1["x"] and h1["x"] + h1["w"] <= 1920 and 0 <= h1["y"] and h1["y"] + h1["h"] <= 1080
    assert h1["lines"] == 1
    assert h1["align"] == "left"
    assert h1["line_height_px"] == pytest.approx(96, abs=0.5)
    run = h1["runs"][0]
    assert (run["family"], run["generic"], run["size_px"], run["bold"]) == ("Georgia", "serif", 80, False)
    assert run["color"] == "#1a1917"
    assert h1["transform"] is None

    p = _block(m, "bold words")
    assert p["lines"] >= 2
    assert p["w"] == pytest.approx(900, abs=2)
    bold = [r for r in p["runs"] if r["bold"]]
    assert [r["text"] for r in bold] == ["bold words"]
    assert bold[0]["color"] == "#b5452b"
    assert all(r["family"] == "Helvetica" and r["generic"] == "sans-serif" for r in p["runs"])
    assert _text(p).startswith("A paragraph with bold words inside it")
    assert p["line_height_px"] == pytest.approx(48)

    grad = _block(m, "Gradient heading")
    assert grad["runs"][0]["bold"] is True
    assert grad["runs"][0]["color"] == "#c0392b"  # first gradient stop, not transparent

    th = _block(m, "Stage")
    assert th["transform"] == "uppercase"
    assert _text(th) == "Stage"
    _block(m, "Northwind")
    _block(m, "A strategy report.")


@needs_chrome
def test_rotated_text_skipped(style_measure):
    m = style_measure
    assert not [b for b in m["blocks"] if "ROTATED" in _text(b)]
    skipped = [s for s in m["skipped"] if "ROTATED LABEL" in s["text"]]
    assert skipped and "transform" in skipped[0]["reason"]
    # left in the plate: the label's ink is still there
    plate = Image.open(m["plate"]).convert("RGB")
    ink, _ = _ink(plate, (1600, 300, 300, 450), _hex_rgb(m["background"]))
    assert ink > 200


@needs_chrome
def test_plate_has_no_text(style_measure):
    m = style_measure
    plate = Image.open(m["plate"]).convert("RGB")
    assert plate.size == (1920, 1080)
    bg = _hex_rgb(m["background"])
    corner = plate.getpixel((5, 5))
    assert max(abs(corner[i] - bg[i]) for i in range(3)) <= 3
    for needle in ("Plain heading", "Gradient heading", "bold words", "Northwind"):
        b = _block(m, needle)
        ink, area = _ink(plate, (b["x"], b["y"], b["w"], b["h"]), bg)
        assert ink <= 0.02 * area, f"{needle}: {ink} ink pixels of {area}"
    # non-text visuals stay: the rule under the heading
    ink, area = _ink(plate, (130, 251, 580, 4), bg)
    assert ink == area


@needs_chrome
def test_unloaded_font_reported(tmp_path):
    deck = tmp_path / "deck.html"
    deck.write_text(
        "<html><head><style>"
        "@font-face{font-family:'Missing Face Zz';src:url('missing-font-zz.woff2') format('woff2');}"
        ".a{font-family:'Missing Face Zz',Georgia,serif;font-size:60px}"
        ".b{font-family:'Nowhere Family Qq',sans-serif;font-size:60px}"
        ".c{font-family:Georgia,serif;font-size:60px}"
        "</style></head><body><div class='slide-wrap'><div class='slide'>"
        "<p class='a'>Alpha</p><p class='b'>Beta</p><p class='c'>Gamma</p>"
        "</div></div></body></html>", encoding="utf-8")
    m = measure.measure_slide(deck, 1, tmp_path / "work")
    assert {"Missing Face Zz", "Nowhere Family Qq"} <= set(m["unloaded"])
    assert "Georgia" not in m["unloaded"] or not Path("/System/Library/Fonts").exists()
    assert m["plate"].exists()


@needs_chrome
def test_measure_deck_parallel(tmp_path):
    res = measure.measure_deck(FIXTURES / "render_dark.html", [1, 2, 3], tmp_path, workers=3)
    assert sorted(res) == [1, 2, 3]
    assert [res[n]["background"] for n in (1, 2, 3)] == ["#f4f0e7", "#080806", "#f4f0e7"]
    assert _text(_block(res[2], "Dark slide")) == "Dark slide"
    assert len({res[n]["plate"] for n in res}) == 3
    assert all(res[n]["plate"].exists() for n in res)
    # temp HTML copies and Chrome profiles are cleaned up
    assert not list(tmp_path.glob("**/*.html"))
    assert not [p for p in tmp_path.rglob("chrome-*") if p.is_dir()]


@needs_chrome
def test_lines_counted_inside_inline_block(tmp_path):
    deck = tmp_path / "deck.html"
    deck.write_text(
        "<html><head><style>h2{width:600px;font:48px/60px Georgia,serif}"
        "h2.tight{font:48px/0.9 Georgia,serif}"  # line height below the glyph box: rects overlap
        "h2 span{display:inline-block}</style></head><body><div class='slide-wrap'><div class='slide'>"
        "<h2><span>A heading long enough to wrap onto more than one line here</span></h2>"
        "<h2 class='tight'>Tight heading long enough to wrap onto more than one line</h2>"
        "</div></div></body></html>", encoding="utf-8")
    m = measure.measure_slide(deck, 1, tmp_path / "work")
    b = _block(m, "A heading long")
    assert b["lines"] == round(b["h"] / 60) >= 2
    assert _block(m, "Tight heading")["lines"] == 3


def _online(host="fonts.googleapis.com"):
    try:
        socket.create_connection((host, 443), timeout=3).close()
        return True
    except OSError:
        return False


@needs_chrome
def test_google_font_loads_before_measure(tmp_path):
    if not _online():
        pytest.skip("offline")
    deck = tmp_path / "deck.html"
    deck.write_text(
        "<html><head>"
        "<link rel='stylesheet' href='https://fonts.googleapis.com/css2?family=Libre+Franklin:wght@400;700'>"
        "<link rel='stylesheet' href='https://fonts.googleapis.com/css2?family=Nonexistent+Family+Zq'>"
        "<style>.a{font-family:'Libre Franklin',sans-serif;font-size:60px}"
        ".b{font-family:'Nonexistent Family Zq',serif;font-size:60px}</style>"
        "</head><body><div class='slide-wrap'><div class='slide'>"
        "<p class='a'>Loaded web font</p><p class='b'>Missing web font</p>"
        "</div></div></body></html>", encoding="utf-8")
    m = measure.measure_slide(deck, 1, tmp_path / "work")
    assert m["unloaded"] == ["Nonexistent Family Zq"]
    assert _block(m, "Loaded web font")["runs"][0]["family"] == "Libre Franklin"
