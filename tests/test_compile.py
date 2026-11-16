import os
import io
import re
from pathlib import Path

import pytest

from PIL import Image

from gslider import compile as C
from gslider.compile import Overflow, compile_deck, compile_slide
from gslider.layout import load_layout
from gslider.parse import parse

FIX = Path(__file__).parent / "fixtures"
REFERENCE = Path(os.environ.get("GSLIDER_REFERENCE_HTML", "/nonexistent"))  # a real HTML deck, optional
ASSETS = {"logo": "https://example.test/logo.png", "logo_dark": "https://example.test/logo-dark.png"}

pytestmark = pytest.mark.usefixtures("fonts_ready")


@pytest.fixture(scope="module")
def layout():
    return load_layout()


def build(name, layout, assets=None, ivory=frozenset(), **kw):
    deck = parse((FIX / name).read_text(encoding="utf-8"), **kw)
    return compile_deck(deck, layout, {**ASSETS, **(assets or {})}, set(ivory))


def reqs_of(plan, kind):
    return [r[kind] for r in plan["requests"] if kind in r]


def box(plan, role, i=0):
    return [b for b in plan["boxes"] if b["role"] == role][i]


def colour(hexstr):
    h = hexstr.lstrip("#")
    return {k: int(h[j:j + 2], 16) / 255 for k, j in (("red", 0), ("green", 2), ("blue", 4))}


def text_colour(plan, object_id):
    for r in reqs_of(plan, "updateTextStyle"):
        if r["objectId"] == object_id and r["textRange"]["type"] == "ALL":
            return r["style"]["foregroundColor"]["opaqueColor"]["rgbColor"]
    raise AssertionError(object_id)


def test_cols3_geometry(layout):
    (plan,) = build("cols3.html", layout)
    assert plan["status"] == "native", plan["reason"]
    heads = [b for b in plan["boxes"] if b["role"] == "header"]
    assert [round(b["x"], 1) for b in heads] == [40.8, 332.8, 624.8]
    assert all(b["w"] == pytest.approx(270 + 14.4) for b in heads)
    # the createShape matches the box, in EMU
    shapes = {s["objectId"]: s for s in reqs_of(plan, "createShape")}
    el = shapes[heads[1]["object_id"]]["elementProperties"]
    assert el["transform"]["translateX"] == round(332.8 * 12700)
    assert el["size"]["width"]["magnitude"] == round(284.4 * 12700)
    assert el["transform"]["unit"] == "EMU" and el["size"]["width"]["unit"] == "EMU"
    # one rule per cell, bullets on the third body
    assert len([o for o in reqs_of(plan, "createLine")]) == 3
    bullets = reqs_of(plan, "createParagraphBullets")
    assert len(bullets) == 1 and bullets[0]["bulletPreset"] == "BULLET_DISC_CIRCLE_SQUARE"


def test_title_three_lines_pushes_lead(layout):
    deck = parse((FIX / "cols3.html").read_text(encoding="utf-8"))
    blocks = deck["slides"][0]["blocks"]
    lead_y = {}
    for label, title in (("one", "Short title."),
                         ("three", "A very long title that keeps going well past the width of the page "
                                   "so that it wraps once, and then it carries on with more words still "
                                   "until it needs a third line to hold all of it.")):
        blocks[1]["text"] = title
        deck["slides"][0]["raw_text"] = [t for t in deck["slides"][0]["raw_text"]
                                         if t != "ACME helps the owners answer them."]  # text swapped above
        (plan,) = compile_deck(deck, layout, ASSETS, set())
        assert box(plan, "title")["lines"] == (1 if label == "one" else 3)
        lead_y[label] = box(plan, "lead")["y"] + layout.inset
    lh = 28 * 1.333
    assert lead_y["one"] == pytest.approx(65 + lh + 12, abs=1)
    assert lead_y["three"] == pytest.approx(65 + 3 * lh + 12, abs=1)


def test_object_ids_valid(layout):
    pat = re.compile(r"^s\d{2,}_[a-z]{3,}[a-z0-9_]*$")
    for plans_ in (build("cols3.html", layout), build("cover_close.html", layout), build("overflow.html", layout)):
        ids = []
        for p in plans_:
            for r in p["requests"]:
                (body,) = r.values()
                if r.keys() & {"createSlide", "createShape", "createLine", "createImage"}:
                    ids.append(body["objectId"])
        assert len(ids) == len(set(ids)), "duplicate objectIds"
        for oid in ids:
            assert pat.match(oid) and 5 <= len(oid) <= 50, oid


def test_styles_applied_per_ground(layout):
    (dark,) = build("cols3.html", layout)
    (light,) = build("cols3.html", layout, ivory={1})
    b_dark, b_light = box(dark, "body"), box(light, "body")
    assert text_colour(dark, b_dark["object_id"]) == colour("#C8C2B6")
    assert text_colour(light, b_light["object_id"]) == colour("#3A3730")
    st = next(r for r in reqs_of(dark, "updateTextStyle") if r["objectId"] == b_dark["object_id"])
    assert st["style"]["fontFamily"] == "Libre Franklin"
    assert st["style"]["fontSize"] == {"magnitude": 14, "unit": "PT"}
    ps = next(r for r in reqs_of(dark, "updateParagraphStyle") if r["objectId"] == b_dark["object_id"])
    assert ps["style"]["lineSpacing"] == 115
    assert ps["style"]["spaceBelow"] == {"magnitude": 4, "unit": "PT"}


def test_overflow_slide_falls_back(layout):
    plans = build("overflow.html", layout)
    assert [p["status"] for p in plans] == ["native", "fallback"]
    assert plans[1]["reason"].startswith("overflow at block")
    assert plans[0]["reason"] is None and plans[0]["requests"]


def test_unknown_block_falls_back(layout):
    (plan,) = build("unknown.html", layout)
    assert plan["status"] == "fallback"
    assert "unknown" in plan["reason"] and "orbit" in plan["reason"]


def test_fallback_has_image_and_notes(layout):
    (plan,) = build("unknown.html", layout, assets={"fallback:1": "https://example.test/f1.png"})
    imgs = reqs_of(plan, "createImage")
    assert [i["url"] for i in imgs] == ["https://example.test/f1.png"]
    size = imgs[0]["elementProperties"]["size"]
    assert size["width"]["magnitude"] == 12192000 and size["height"]["magnitude"] == 6858000
    assert plan["notes_text"] == "A slide with a mystery container.\nAlpha\nBeta"
    assert reqs_of(plan, "createShape") == []
    # without the render URL the plan says which asset is missing instead of emitting a bad request
    (pending,) = build("unknown.html", layout)
    assert reqs_of(pending, "createImage") == [] and pending["missing_assets"] == ["fallback:1"]


def test_ivory_override(layout):
    plans = build("cover_close.html", layout)
    assert [p["n"] for p in plans] == [1, 2]
    (dark,) = build("cols3.html", layout)
    (light,) = build("cols3.html", layout, ivory={1})
    bg = {p: reqs_of(p_, "updatePageProperties")[0]["pageProperties"]["pageBackgroundFill"]
          ["solidFill"]["color"]["rgbColor"] for p, p_ in (("d", dark), ("l", light))}
    assert bg["d"] == colour("#080806") and bg["l"] == colour("#F4F0E7")
    assert all(b["ground"] == "ivory" for b in light["boxes"])
    assert text_colour(light, box(light, "title")["object_id"]) == colour("#1A1917")
    # white wordmark on dark ground, dark wordmark on ivory
    assert [i["url"] for i in reqs_of(dark, "createImage")] == [ASSETS["logo"]]
    assert [i["url"] for i in reqs_of(light, "createImage")] == [ASSETS["logo_dark"]]


def test_cover_and_closing_native(layout):
    plans = build("cover_close.html", layout)
    for p in plans:
        assert p["status"] == "native", p["reason"]
        roles = {b["role"] for b in p["boxes"]}
        assert "cover_title" in roles
    assert not [b for b in plans[1]["boxes"] if b["role"] == "pageno"]


@pytest.mark.skipif(not REFERENCE.exists(), reason="set GSLIDER_REFERENCE_HTML to run against a real deck")
def test_reference_deck_mostly_native(layout):
    deck = parse(REFERENCE.read_text(encoding="utf-8"))
    plans = compile_deck(deck, layout, ASSETS, set())
    assert len(plans) == len(deck["slides"])
    # the spec's bar: at least 80% of client slides build as editable text
    assert sum(p["status"] == "native" for p in plans) >= 0.8 * len(plans), [(p["n"], p["reason"]) for p in plans]


# ---------------------------------------------------------------- review fixes (chunk A)

def png(w=4, h=2, mode="RGB", fmt="PNG"):
    buf = io.BytesIO()
    Image.new(mode, (w, h)).save(buf, fmt)
    return buf.getvalue()


def img(data, mime="image/png", **kw):
    import hashlib
    return {"kind": "image", "sha256": hashlib.sha256(data).hexdigest(), "mime": mime, "data": data, **kw}


def one(inner, layout, cls="slide dk", content=True, assets=None):
    body = f'<div class="content">{inner}</div>' if content else inner
    html = f'<div class="slide-wrap" data-slide-id="x"><div class="{cls}">{body}</div></div>'
    deck = parse(html)
    (plan,) = compile_deck(deck, layout, {**ASSETS, **(assets or {})}, set())
    return deck["slides"][0], plan


def content_slide(blocks, raw=None):
    raw = raw if raw is not None else [b.get("text") for b in blocks if b.get("text")]
    return {"n": 1, "id": "x", "kind": "content", "ground": "dark", "blocks": blocks,
            "raw_text": raw, "dropped_text": []}


def joined(plan):
    return " ".join(b["text"] for b in plan["boxes"]).replace("\n", " ")


def test_five_stacks_compile_without_crash(layout):
    stacks = "".join(f'<div class="stack"><div class="col"><div class="c-title">H{i}</div></div></div>'
                     for i in range(5))
    _, plan = one(f'<div class="content-header"><h2>T</h2></div><div class="row2">{stacks}</div>', layout)
    assert plan["status"] == "native", plan["reason"]


def test_unexpected_exception_becomes_fallback(layout):
    cells = [{"num": None, "header": f"H{i}", "body": [], "bullets": [], "value": None} for i in range(5)]
    plan = compile_slide(content_slide([{"kind": "cells", "cells": cells}], raw=[]), layout, ASSETS, "dark")
    assert plan["status"] == "fallback" and plan["reason"] == repr(KeyError(5))


def test_guard_falls_back_when_text_not_placed(layout):
    blocks = [{"kind": "title", "text": "Kept title"}]
    plan = compile_slide(content_slide(blocks, raw=["Kept title", "Lost words that never made it onto the slide"]),
                         layout, ASSETS, "dark")
    assert plan["status"] == "fallback"
    assert plan["reason"] == "text not placed: Lost words that never made it onto the s"
    assert plan["notes_text"] == "Kept title\nLost words that never made it onto the slide"


def test_guard_ignores_dropped_text_and_lone_glyphs(layout):
    sl = content_slide([{"kind": "title", "text": "Kept title"}], raw=["Kept title", "ACME", "→", "06"])
    sl["dropped_text"] = ["ACME", "06"]
    assert compile_slide(sl, layout, ASSETS, "dark")["status"] == "native"


@pytest.mark.parametrize("inner, needle, content", [
    ('<div class="band"><span class="st-k">Label</span> loose band text</div>', "loose band text", True),
    ('<div class="timeline"><div class="tl"><span class="tl-k">Dec</span> loose tl text</div></div>',
     "loose tl text", True),
    ('<div>bare text <p class="lead">lead</p></div>', "bare text", True),
    ('<div class="cols-2x">loose wrap <div class="col"><div class="c-title">H</div></div></div>', "loose wrap", True),
    ('<div class="content-header"><h2>T2</h2> header loose</div>', "header loose", True),
    ('<div class="header-bar"><span class="header-sec">Sec</span></div><p class="lead">Loose lead outside</p>',
     "Loose lead outside", False),
])
def test_loose_text_patterns_are_placed(layout, inner, needle, content):
    _, plan = one(inner, layout, content=content)
    assert plan["status"] == "native", plan["reason"]
    assert needle in joined(plan)


def test_cover_text_outside_fields_falls_back(layout):
    for inner in ('<div class="cv-text"><div><span class="cv-kicker">Kick</span> extra words</div>'
                  '<h1 class="cv-title">Title</h1></div>',
                  '<div class="cv-text"><h1 class="cv-title">Title</h1></div><p>stray outside the box</p>'):
        _, plan = one(inner, layout, content=False)
        assert plan["status"] == "fallback" and plan["reason"], inner


def test_full_width_image_near_floor_overflows(layout):
    s = C._Slide(1, "dark", layout, {})
    for y in (layout.floor - 30, layout.floor + 10):  # was a negative-height image
        with pytest.raises(Overflow):
            C._full_image(s, img(png()), y)
    assert C._full_image(s, img(png()), layout.floor - 100) == pytest.approx(layout.floor)


def test_table_widths_normalised_and_narrow_column_rejected(layout):
    cols = C._col_layout(layout, [0.9, 0.6], 2)  # 150%: scaled to 60/40
    assert [round(w) for _, w in cols] == [round(0.6 * layout.title["w"] - 18), round(0.4 * layout.title["w"])]
    assert cols[-1][0] + cols[-1][1] == pytest.approx(layout.margin + layout.title["w"])
    with pytest.raises(ValueError):
        C._col_layout(layout, [0.7, 0.6, None], 3)  # the free column is squeezed to nothing
    with pytest.raises(ValueError):
        C._col_layout(layout, [0.99, 0.01], 2)


@pytest.mark.parametrize("image, why", [
    (img(png(), mime="image/webp"), "unsupported"),
    (img(b"<svg xmlns='http://www.w3.org/2000/svg'/>", mime="image/svg+xml"), "unsupported"),
    (img(b"not an image at all"), "unreadable"),
    (img(png(6000, 5000, mode="1")), "pixels"),
    (img(b"\x89PNG\r\n\x1a\n" + bytes(50 * 1024 * 1024)), "MB"),
])
def test_bad_images_raise_value_error(image, why):
    with pytest.raises(ValueError, match=why):
        C._aspect(image)


def test_bad_image_slides_fall_back(layout):
    webp = img(png(fmt="WEBP"), mime="image/webp")
    plan = compile_slide(content_slide([{"kind": "title", "text": "T"}, webp], raw=["T"]), layout, ASSETS, "dark")
    assert plan["status"] == "fallback" and "unsupported" in plan["reason"]
    sl = content_slide([{"kind": "title", "text": "T"}], raw=["T"])
    sl["background"] = webp
    plan = compile_slide(sl, layout, ASSETS, "dark")
    assert plan["status"] == "fallback" and "unsupported" in plan["reason"]


def test_side_image_group_resets_on_full_width_block(layout):
    pic = img(png(4, 3))
    blocks = [{"kind": "title", "text": "T"}, {"kind": "lead", "text": "side text", "width": 0.6},
              {"kind": "lead", "text": "full"}, pic]
    plan = compile_slide(content_slide(blocks), layout, {**ASSETS, pic["sha256"]: "https://u/p"}, "dark")
    assert plan["status"] == "native", plan["reason"]
    full = [b for b in plan["boxes"] if b["text"] == "full"][0]
    (im,) = [r["createImage"] for r in plan["requests"] if "createImage" in r and r["createImage"]["url"] == "https://u/p"]
    top = im["elementProperties"]["transform"]["translateY"] / 12700
    assert top > full["y"] + full["h"]  # below the full-width lead, not beside the earlier group


def test_side_image_before_group_falls_back(layout):
    pic = img(png(4, 3))
    blocks = [{"kind": "title", "text": "T"}, pic, {"kind": "lead", "text": "side text", "width": 0.6}]
    plan = compile_slide(content_slide(blocks), layout, {**ASSETS, pic["sha256"]: "https://u/p"}, "dark")
    assert plan["status"] == "fallback" and plan["reason"] == "side image before group"


def bold_ranges(plan, oid):
    return [(r["textRange"]["startIndex"], r["textRange"]["endIndex"]) for r in reqs_of(plan, "updateTextStyle")
            if r["objectId"] == oid and r["fields"] == "bold"]


def test_bold_hits_right_occurrence_and_box(layout):
    # out/rev/dup_text.html pattern
    _, plan = one('<div class="cols-2x"><div class="col"><div class="c-title">Same</div>'
                  '<p class="c-p">Same words here <b>here</b></p></div><div class="col"><div class="c-title">Same</div>'
                  '<p class="c-p">Same words here and <b>Same</b></p></div></div>', layout)
    heads = [b for b in plan["boxes"] if b["role"] == "header"]
    bodies = [b for b in plan["boxes"] if b["role"] == "body"]
    assert all(bold_ranges(plan, h["object_id"]) == [] for h in heads)
    assert bold_ranges(plan, bodies[0]["object_id"]) == [(16, 20)]
    assert bold_ranges(plan, bodies[1]["object_id"]) == [(20, 24)]
    # out/rev/bold_in_bullet.html pattern
    _, plan = one('<div class="col"><p class="c-p">intro here</p><ul><li>plain</li>'
                  '<li><b>intro</b> here again</li></ul></div>', layout)
    body = box(plan, "body")
    assert body["text"] == "intro here\nplain\nintro here again"
    assert bold_ranges(plan, body["object_id"]) == [(17, 22)]


def test_bold_offsets_count_utf16(layout):
    _, plan = one('<p class="lead">🚀 <b>bold</b></p>', layout)
    assert bold_ranges(plan, box(plan, "lead")["object_id"]) == [(3, 7)]


def test_band_label_lines_counted(layout):
    import copy
    import dataclasses
    styles = copy.deepcopy(layout.styles)
    styles["band_label"]["dark"]["space_after"] = 6  # 0 in layouts.yaml, which hides the miscount
    layout = dataclasses.replace(layout, styles=styles)
    s = C._Slide(1, "dark", layout, {})
    label = "\n".join("ABCDEFGHIJKL")  # 12 one-word lines: the band is pinned up from the floor
    bottom = C._band(s, {"kind": "band", "label": label, "text": ""}, 100)
    assert bottom <= layout.floor + 0.01


# ------------------------------------------------------------------ fresheyes rows 4, 6, 9

def test_side_image_past_floor_overflows(layout):
    """Row 6: a tall side image whose bottom passes the floor overflows (was placed off the page)."""
    pic = img(png(1, 20))  # tall image beside a short group near the foot: the 120pt minimum span runs past
    blocks = [{"kind": "title", "text": "T"}, {"kind": "lead", "text": "filler words " * 130},
              {"kind": "lead", "text": "side", "width": 0.6}, pic]
    plan = compile_slide(content_slide(blocks), layout, {**ASSETS, pic["sha256"]: "https://u/p"}, "dark")
    assert plan["status"] == "fallback", "side image placed below the floor"
    assert "overflow" in plan["reason"] and "image" in plan["reason"]


def test_full_width_block_starts_below_side_image(layout):
    """Row 6: the side image counts toward the bottom a following full-width block starts from."""
    pic = img(png(1, 2))
    blocks = [{"kind": "title", "text": "T"}, {"kind": "lead", "text": "side", "width": 0.6}, pic,
              {"kind": "lead", "text": "after"}]
    plan = compile_slide(content_slide(blocks), layout, {**ASSETS, pic["sha256"]: "https://u/p"}, "dark")
    assert plan["status"] == "native", plan["reason"]
    (im,) = [r["createImage"] for r in plan["requests"] if "createImage" in r and r["createImage"]["url"] == "https://u/p"]
    img_bottom = (im["elementProperties"]["transform"]["translateY"]
                  + im["elementProperties"]["size"]["height"]["magnitude"]) / 12700
    after = [b for b in plan["boxes"] if b["text"] == "after"][0]
    assert after["y"] + layout.inset >= img_bottom


def test_side_image_inside_floor_still_native(layout):
    pic = img(png(4, 3))
    blocks = [{"kind": "title", "text": "T"}, {"kind": "lead", "text": "side text", "width": 0.6}, pic]
    plan = compile_slide(content_slide(blocks), layout, {**ASSETS, pic["sha256"]: "https://u/p"}, "dark")
    assert plan["status"] == "native", plan["reason"]


BASELINE = Path(__file__).resolve().parent.parent / "out" / "ff-baseline-requests.json"


@pytest.mark.skipif(not BASELINE.exists(), reason="no request baseline from fix/fresheyes-findings")
def test_fixture_requests_unchanged(layout):
    """Row 9: moving the constants into layouts.yaml changes no fixture's requests."""
    import json
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    for name, want in base.items():
        deck = parse((FIX / name).read_text(encoding="utf-8"))
        got = [p["requests"] for p in compile_deck(deck, layout, {"logo": "u", "logo_dark": "u"}, set())]
        assert json.loads(json.dumps(got)) == want, name


def _rows_block(bold=None, header_bold=None, style=None):
    b = {"kind": "rows", "header": ["Col A", "Col B"], "widths": [],
         "rows": [["01", "bold cell"], ["02", "plain cell"]]}
    if style == "timeline":
        b = {"kind": "rows", "style": "timeline", "header": None, "rows": [["Dec", "bold cell"], ["Jan", "plain"]]}
    if bold is not None:
        b["bold"] = bold
    if header_bold is not None:
        b["header_bold"] = header_bold
    return b


def test_rows_bold_ranges_applied(layout):
    """Row 4 (compile half): a rows block's bold ranges become bold requests on that cell's box."""
    b = _rows_block(bold=[[[], [[0, 4]]], [[], []]], header_bold=[[], [[4, 1]]])
    plan = compile_slide(content_slide([b], raw=[]), layout, ASSETS, "dark")
    assert plan["status"] == "native", plan["reason"]
    cell = [x for x in plan["boxes"] if x["text"] == "bold cell"][0]
    assert bold_ranges(plan, cell["object_id"]) == [(0, 4)]
    head = [x for x in plan["boxes"] if x["text"] == "Col B"][0]
    assert bold_ranges(plan, head["object_id"]) == [(4, 5)]
    others = [x for x in plan["boxes"] if x["object_id"] not in (cell["object_id"], head["object_id"])]
    assert all(bold_ranges(plan, x["object_id"]) == [] for x in others)


def test_rows_timeline_bold_ranges_applied(layout):
    b = _rows_block(bold=[[[[0, 3]], [[0, 4]]], [[], []]], style="timeline")
    plan = compile_slide(content_slide([b], raw=[]), layout, ASSETS, "dark")
    assert plan["status"] == "native", plan["reason"]
    key = [x for x in plan["boxes"] if x["text"] == "Dec"][0]
    cell = [x for x in plan["boxes"] if x["text"] == "bold cell"][0]
    assert bold_ranges(plan, key["object_id"]) == [(0, 3)]
    assert bold_ranges(plan, cell["object_id"]) == [(0, 4)]
    num = [x for x in plan["boxes"] if x["role"] == "number"][0]
    assert bold_ranges(plan, num["object_id"]) == []  # the row number is ours, not the cell's


@pytest.mark.parametrize("style", [None, "timeline"])
def test_rows_without_bold_emit_no_bold_requests(layout, style):
    plain = compile_slide(content_slide([_rows_block(style=style)], raw=[]), layout, ASSETS, "dark")
    empty = [[[] for _ in r] for r in _rows_block(style=style)["rows"]]
    hb = None if style else [[], []]
    with_empty = compile_slide(content_slide([_rows_block(bold=empty, header_bold=hb, style=style)], raw=[]),
                               layout, ASSETS, "dark")
    for plan in (plain, with_empty):
        assert plan["status"] == "native", plan["reason"]
        assert not [r for r in reqs_of(plan, "updateTextStyle") if r["fields"] == "bold"]
    assert plain["requests"] == with_empty["requests"]


def test_table_bold_end_to_end(layout):
    """Parse -> compile: <b> in a table cell becomes a bold run on that row's box."""
    html = """<div class="slide-wrap"><div class="slide dk"><div class="content">
    <h2 class="h2">Title</h2>
    <table class="tbl"><thead><tr><th>A</th><th>B</th></tr></thead>
    <tbody><tr><td>plain</td><td><b>bold</b> cell</td></tr></tbody></table>
    </div></div></div>"""
    plan = compile_deck(parse(html), layout, ASSETS, set())[0]
    assert plan["status"] == "native", plan["reason"]
    bold = [r["updateTextStyle"] for r in plan["requests"]
            if "updateTextStyle" in r and r["updateTextStyle"]["style"].get("bold")]
    assert any(b["textRange"].get("startIndex", 0) == 0 and b["textRange"]["endIndex"] == 4 for b in bold)
