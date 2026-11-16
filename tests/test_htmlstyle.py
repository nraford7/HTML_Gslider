"""html style: measured text blocks -> text boxes over a text-free plate (offline)."""
import pytest

from gslider import htmlstyle as H

EMU = 12700


def _run(text, family="Libre Franklin", size=30, bold=False, italic=False, color="#1a1917",
         generic="sans-serif", underline=False):
    return {"text": text, "family": family, "generic": generic, "size_px": size, "bold": bold,
            "italic": italic, "underline": underline, "color": color}


def _block(runs, x=100, y=200, w=400, h=60, align="left", lh=45, ls=0, transform=None, lines=1):
    return {"x": x, "y": y, "w": w, "h": h, "lines": lines, "align": align, "line_height_px": lh,
            "letter_spacing_px": ls, "runs": runs, "transform": transform}


def _measure(blocks, n=1, background="#f4f0e7", unloaded=(), skipped=()):
    return {"n": n, "background": background, "fonts_status": "loaded", "unloaded": list(unloaded),
            "blocks": blocks, "skipped": list(skipped), "plate": f"/tmp/plate_s{n:02d}.png"}


def _deck(raw=("Hello",), n=1):
    return {"title": "t", "source": None, "slides": [
        {"n": n, "html_index": n, "id": f"h{n}", "kind": "content", "ground": "ivory", "blocks": [],
         "raw_text": list(raw), "dropped_text": []}]}


@pytest.fixture(autouse=True)
def offline_fonts(monkeypatch):
    """Google Fonts knows Libre Franklin and Playfair Display only; ratios fixed."""
    known = {"Libre Franklin", "Playfair Display", "Inter"}

    def fake_family(css_family, generic):
        if css_family in known:
            return css_family, False
        return {"serif": "Georgia", "monospace": "Courier New"}.get(generic, "Arial"), True

    monkeypatch.setattr(H, "slides_family", fake_family)
    monkeypatch.setattr(H, "normal_line_ratio",
                        lambda fam: {"Libre Franklin": 1.212, "Playfair Display": 1.333}.get(fam))


def _plan(blocks, assets=None, **kw):
    deck = _deck(raw=["".join(r["text"] for b in blocks for r in b["runs"])])
    return H.compile_html_deck(deck, {1: _measure(blocks, **kw)}, assets or {"plate:1": "https://u/plate"})[0]


def _req(plan, kind, oid=None):
    return [r[kind] for r in plan["requests"] if kind in r and (oid is None or r[kind].get("objectId") == oid)]


def _size(plan, oid):
    el = _req(plan, "createShape", oid)[0]["elementProperties"]
    return (el["transform"]["translateX"] / EMU, el["transform"]["translateY"] / EMU,
            el["size"]["width"]["magnitude"] / EMU, el["size"]["height"]["magnitude"] / EMU)


def test_px_to_pt_and_inset():
    plan = _plan([_block([_run("Hello")])])
    box = plan["boxes"][0]
    assert box["role"] == "text" and box["object_id"] == "s01_t0"
    assert box["x"] == pytest.approx(42.8) and box["y"] == pytest.approx(92.8)
    assert box["w"] == pytest.approx(400 * 0.5 * 1.03 + 14.4)
    assert box["h"] == pytest.approx(30.0)  # text area; the element is h + 14.4
    x, y, w, h = _size(plan, "s01_t0")
    assert (x, y, w, h) == pytest.approx((42.8, 92.8, 220.4, 44.4), abs=0.01)
    assert plan["status"] == "native" and plan["style"] == "html"


def test_center_and_right_keep_the_text_edge():
    slack = 400 * 0.5 * 0.03
    c = _plan([_block([_run("Hi")], align="center")])["boxes"][0]
    r = _plan([_block([_run("Hi")], align="right")])["boxes"][0]
    assert c["x"] == pytest.approx(42.8 - slack / 2)
    assert r["x"] == pytest.approx(42.8 - slack)


def test_run_styles():
    plan = _plan([_block([_run("Plain "), _run("bold", bold=True, color="#b5452b", italic=True)],
                         align="center")])
    styles = _req(plan, "updateTextStyle", "s01_t0")
    first, second = styles
    assert first["textRange"] == {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 6}
    assert second["textRange"] == {"type": "FIXED_RANGE", "startIndex": 6, "endIndex": 10}
    assert first["style"]["fontSize"] == {"magnitude": 15.0, "unit": "PT"}
    assert first["style"]["fontFamily"] == "Libre Franklin"
    assert first["style"]["bold"] is False and second["style"]["bold"] is True
    assert second["style"]["italic"] is True
    c = second["style"]["foregroundColor"]["opaqueColor"]["rgbColor"]
    assert c == pytest.approx({"red": 0xb5 / 255, "green": 0x45 / 255, "blue": 0x2b / 255})
    para = _req(plan, "updateParagraphStyle", "s01_t0")[0]
    assert para["style"]["alignment"] == "CENTER"
    assert _req(plan, "insertText", "s01_t0")[0]["text"] == "Plain bold"


def test_line_spacing_from_css():
    plan = _plan([_block([_run("Line")], lh=45)])
    para = _req(plan, "updateParagraphStyle", "s01_t0")[0]
    assert para["style"]["lineSpacing"] == pytest.approx(45 / (30 * 1.212) * 100, abs=0.5)
    assert para["style"]["spaceAbove"]["magnitude"] == 0 and para["style"]["spaceBelow"]["magnitude"] == 0


def test_line_spacing_unknown_family_uses_1_2():
    plan = _plan([_block([_run("Line", family="Nonesuch Sans")], lh=36)])
    para = _req(plan, "updateParagraphStyle", "s01_t0")[0]
    # Arial is the substitute; its normal line is 1.149 of the size
    assert para["style"]["lineSpacing"] == pytest.approx(36 / (30 * 1.149) * 100, abs=0.5)


def test_text_transform_applied():
    plan = _plan([_block([_run("the big "), _run("idea", bold=True)], transform="uppercase")])
    assert _req(plan, "insertText", "s01_t0")[0]["text"] == "THE BIG IDEA"
    cap = _plan([_block([_run("the big "), _run("idea", bold=True)], transform="capitalize")])
    assert _req(cap, "insertText", "s01_t0")[0]["text"] == "The Big Idea"


def test_family_substitution_reported():
    plan = _plan([_block([_run("x", family="Helvetica Neue")]), _block([_run("y", family="Georgia",
                                                                              generic="serif")])])
    assert plan["html"]["substitutions"] == [{"family": "Helvetica Neue", "used": "Arial"}]
    styles = _req(plan, "updateTextStyle", "s01_t0")
    assert styles[0]["style"]["fontFamily"] == "Arial"
    assert _req(plan, "updateTextStyle", "s01_t1")[0]["style"]["fontFamily"] == "Georgia"


def test_letter_spacing_listed_as_dropped():
    plan = _plan([_block([_run("SPACED")], ls=2.4)])
    assert plan["html"]["letter_spacing"] == [{"object_id": "s01_t0", "px": 2.4, "text": "SPACED"}]


def test_plate_is_bottom_layer():
    plan = _plan([_block([_run("a")]), _block([_run("b")], y=400)])
    kinds = [next(iter(r)) for r in plan["requests"]]
    plate_at = next(i for i, r in enumerate(plan["requests"])
                    if r.get("createImage", {}).get("objectId") == "s01_plate")
    first_box = kinds.index("createShape")
    assert plate_at < first_box
    img = plan["requests"][plate_at]["createImage"]
    assert img["url"] == "https://u/plate"
    size = img["elementProperties"]["size"]
    assert size["width"]["magnitude"] == 960 * EMU and size["height"]["magnitude"] == 540 * EMU


def test_plate_missing_is_reported():
    plan = H.compile_html_deck(_deck(), {1: _measure([_block([_run("Hello")])])}, {})[0]
    assert plan["missing_assets"] == ["plate:1"]


def test_ground_and_background():
    dark = _plan([_block([_run("a")])], background="#101010")
    light = _plan([_block([_run("a")])], background="#f4f0e7")
    assert dark["ground"] == "dark" and dark["background"] == "#101010"
    assert light["ground"] == "ivory"


def test_unloaded_font_slide_falls_back():
    deck = _deck()
    plan = H.compile_html_deck(deck, {1: _measure([_block([_run("Hello")])], unloaded=["Nope Sans"])},
                               {"fallback:1": "https://u/fb"})[0]
    assert plan["status"] == "fallback"
    assert "font Nope Sans did not load" in plan["reason"]
    assert plan["notes_text"] == "Hello"
    assert _req(plan, "createImage")[0]["url"] == "https://u/fb"


def test_measure_error_slide_falls_back():
    plan = H.compile_html_deck(_deck(), {1: {"n": 1, "error": "Chrome timed out"}}, {})[0]
    assert plan["status"] == "fallback" and "Chrome timed out" in plan["reason"]
    assert plan["missing_assets"] == ["fallback:1"]


def test_skipped_text_left_in_plate():
    deck = _deck(raw=["Hello", "ROTATED", "LABEL"])
    m = _measure([_block([_run("Hello")])], skipped=[{"text": "ROTATED LABEL", "reason": "transform"}])
    plan = H.compile_html_deck(deck, {1: m}, {"plate:1": "u"})[0]
    assert plan["html"]["left_in_plate"] == [{"text": "ROTATED LABEL", "reason": "transform"}]
    assert plan["dropped_text"] == ["ROTATED", "LABEL"]


def test_boxes_in_dom_order_and_line_height_recorded():
    plan = _plan([_block([_run("first")]), _block([_run("second")], y=300)])
    assert [b["object_id"] for b in plan["boxes"]] == ["s01_t0", "s01_t1"]
    assert plan["boxes"][0]["line_pt"] == pytest.approx(22.5)


def test_html_plan_carries_plate_png():
    plan = _plan([_block([_run("a")])])
    assert plan["plate_png"] == "/tmp/plate_s01.png"


def test_text_the_html_hides_is_dropped_and_listed():
    """Text parse sees but Chrome does not draw (display:none) is not required; a string drawn
    once and hidden once is required once."""
    deck = _deck(raw=["Brand", "Hello Brand", "Brand", "Footer line"])
    m = _measure([_block([_run("Hello Brand")])])
    plan = H.compile_html_deck(deck, {1: m}, {"plate:1": "u"})[0]
    assert sorted(plan["dropped_text"]) == ["Brand", "Brand", "Footer line"]
    assert plan["html"]["hidden"] == ["Brand", "Footer line"]
    twice = H.compile_html_deck(_deck(raw=["Brand", "Brand"]), {1: _measure([_block([_run("Brand")])])},
                                {"plate:1": "u"})[0]
    assert twice["dropped_text"] == ["Brand"]  # drawn once: required once
