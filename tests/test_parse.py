import os
import hashlib
from pathlib import Path

import pytest

from gslider.parse import norm, parse

FIX = Path(__file__).parent / "fixtures"
REFERENCE = Path(os.environ.get("GSLIDER_REFERENCE_HTML", "/nonexistent"))  # a real HTML deck, optional


def load(name, **kw):
    return parse((FIX / name).read_text(encoding="utf-8"), **kw)


def kinds(slide):
    return [b["kind"] for b in slide["blocks"]]


def test_cols3_yields_title_lead_cells():
    deck = load("cols3.html")
    assert deck["title"] == "Cols3 fixture"
    (s,) = deck["slides"]
    assert s["n"] == 1 and s["id"] == "why" and s["kind"] == "content"
    assert s["ground"] == "dark" and s["internal"] is False
    assert kinds(s) == ["section", "title", "lead", "cells"]
    assert s["blocks"][0]["text"] == "Why ACME / People"
    assert s["blocks"][1]["text"] == "ACME helps the owners answer them."
    assert s["blocks"][2]["text"] == "One coherent programme."
    cells = s["blocks"][3]["cells"]
    assert len(cells) == 3
    assert [c["num"] for c in cells] == ["01", "02", "03"]
    assert [c["header"] for c in cells] == [
        "Leaders who have sat in your seat",
        "Judgement from running the work",
        "Access to the people who decide",
    ]
    assert cells[0]["body"] == ["former ministers and chairs."]
    assert cells[0]["bullets"] == [] and cells[0]["value"] is None
    assert cells[2]["bullets"] == ["First point", "Second bold point"]
    assert cells[2]["bold"] == {"bullets": [(19, 4)]}  # "First point\nSecond bold point"
    # raw_text keeps footer chrome; check applies the drop list
    assert "Private strategic proposal" in s["raw_text"]
    assert "Why ACME / People" in s["raw_text"]


def test_timeline_rows():
    (s,) = load("timeline.html")["slides"]
    assert s["ground"] == "ivory"
    assert kinds(s) == ["section", "title", "rows"]
    b = s["blocks"][2]
    assert b["header"] is None
    assert b["style"] == "timeline"
    assert b["rows"] == [
        ["December 2026", "agree terms."],
        ["February 2027", "Ramadan (no sessions)."],
        ["March–July 2027", "three private roundtables."],
    ]


def test_tbl_keeps_all_cells_and_thead():
    (s,) = load("tbl.html")["slides"]
    assert kinds(s) == ["section", "title", "rows", "note"]
    b = s["blocks"][2]
    assert b["style"] == "table"
    assert b["header"] == ["Stage", "The owners’ question", "How the programme answers it"]
    assert b["rows"] == [
        ["Stage 0", "Where are we strong today?", "A short review."],
        ["Stage 3", "How do we make it happen?", "A strategy report."],
    ]
    assert b["widths"] == [0.15, 0.33, None]
    assert s["blocks"][3]["text"] == '"What are we trying to achieve?" One workshop.'


def test_cover_and_closing_fields():
    deck = load("cover_close.html")
    cover, close = deck["slides"]
    assert cover["kind"] == "cover" and cover["n"] == 1
    fields = [b for b in cover["blocks"] if b["kind"] == "field"]
    assert [(f["role"], f["text"]) for f in fields] == [
        ("kicker", "From London to Lisbon influence"),
        ("title", "A Lisbon market entry for Northwind."),
        ("sub", "Build the evidence."),
        ("meta", "ACME Engage / November 2026"),
    ]
    (img,) = [b for b in cover["blocks"] if b["kind"] == "image"]
    data = img["data"]
    assert img["mime"] == "image/png" and data.startswith(bytes.fromhex("89504e470d0a1a0a"))
    assert img["sha256"] == hashlib.sha256(data).hexdigest()
    assert close["kind"] == "closing"
    assert [(b["role"], b["text"]) for b in close["blocks"]] == [
        ("kicker", "The next step"),
        ("title", "We ask Northwind to commission the programme."),
        ("sub", "The commission starts with a defined first step."),
        ("contact", "Jane Doe, Director\njane@example.com"),
    ]


def test_unknown_container_block():
    (s,) = load("unknown.html")["slides"]
    assert kinds(s) == ["title", "unknown"]
    u = s["blocks"][1]
    assert u["cls"] == "orbit"
    assert u["text"] == "Alpha Beta"


def test_text_normalisation():
    assert norm("a&nbsp;b") == "a b"
    assert norm("a b") == "a b"
    assert norm("  x \n\t y  ") == "x y"
    assert norm("“q” – ’") == "“q” – ’"
    (s,) = load("entities.html")["slides"]
    assert s["blocks"][0]["text"] == "a b & “quoted” – dash"
    cells = s["blocks"][1]["cells"]
    assert cells[0]["header"] == "Owners’ view"
    assert cells[0]["body"] == ["line one", "line two"]
    assert "line two" in s["raw_text"]


def test_version_note_internal_skipped():
    deck = load("cover_close.html")
    assert [s["id"] for s in deck["slides"]] == ["cover", "close"]
    deck = load("cover_close.html", include_internal=True)
    assert [s["id"] for s in deck["slides"]] == ["version-note", "cover", "close"]
    assert deck["slides"][0]["internal"] is True
    assert [s["n"] for s in deck["slides"]] == [1, 2, 3]


@pytest.mark.skipif(not REFERENCE.exists(), reason="set GSLIDER_REFERENCE_HTML to run against a real deck")
def test_reference_deck_parses():
    deck = parse(REFERENCE.read_text(encoding="utf-8"), source=str(REFERENCE))
    slides = deck["slides"]
    assert slides, "no slides parsed"
    assert len({s["id"] for s in slides}) == len(slides)
    assert slides[0]["kind"] == "cover" and slides[-1]["kind"] == "closing"
    for s in slides:
        assert s["blocks"], s["id"]
        assert "unknown" not in kinds(s), (s["id"], kinds(s))
    # every cells block holds 1-4 cells
    for s in slides:
        for b in s["blocks"]:
            if b["kind"] == "cells":
                assert 1 <= len(b["cells"]) <= 4, s["id"]


# ---------------------------------------------------------------- review fixes (chunk A)

def slide_of(inner, cls="slide dk", content=True):
    body = f'<div class="content">{inner}</div>' if content else inner
    html = f'<div class="slide-wrap" data-slide-id="x"><div class="{cls}">{body}</div></div>'
    (s,) = parse(html)["slides"]
    return s


def texts(slide):
    return [b.get("text") for b in slide["blocks"]]


def test_html_index_counts_internal_slides():
    deck = load("cover_close.html")
    assert [(s["n"], s["html_index"]) for s in deck["slides"]] == [(1, 2), (2, 3)]
    deck = load("cover_close.html", include_internal=True)
    assert [(s["n"], s["html_index"]) for s in deck["slides"]] == [(1, 1), (2, 2), (3, 3)]


def test_more_than_four_stacks_split_into_cell_blocks():
    stacks = "".join(f'<div class="stack"><div class="col"><div class="c-title">H{i}</div></div></div>'
                     for i in range(5))
    s = slide_of(f'<div class="row2">{stacks}</div>')
    cells = [b for b in s["blocks"] if b["kind"] == "cells"]
    assert [len(b["cells"]) for b in cells] == [3, 2]


def test_band_keeps_loose_text_next_to_label():
    s = slide_of('<div class="band"><span class="st-k">Label</span> loose <b>band</b> text</div>')
    (b,) = s["blocks"]
    assert b["label"] == "Label" and b["text"] == "loose band text"
    assert b["bold"] == [(6, 4)]


def test_timeline_keeps_loose_text_next_to_key():
    s = slide_of('<div class="timeline"><div class="tl"><span class="tl-dot"></span>'
                 '<span class="tl-k">Dec</span> loose tl text</div></div>')
    assert s["blocks"][0]["rows"] == [["Dec", "loose tl text"]]


@pytest.mark.parametrize("inner, expect", [
    ('<div>bare text <p class="lead">lead</p></div>', ["bare text", "lead"]),
    ('<div class="cols-2x">loose wrap <div class="col"><div class="c-title">H</div></div></div>',
     ["loose wrap", None]),
    ('<div class="content-header"><h2>T2</h2> header loose</div>', ["T2", "header loose"]),
    ('top level loose <p class="lead">lead</p>', ["top level loose", "lead"]),
])
def test_bare_text_in_mixed_containers_kept(inner, expect):
    assert texts(slide_of(inner)) == expect


def test_content_slide_without_content_div_keeps_text():
    # out/rev/no_content.html pattern
    s = slide_of('<div class="header-bar"><span class="header-sec">Sec</span></div>'
                 '<p class="lead">Loose lead outside .content</p>', content=False)
    assert texts(s) == ["Sec", "Loose lead outside .content"]
    s = slide_of('<div class="header-bar"><span class="header-sec">Sec</span><span class="wm-top">ACME</span></div>'
                 '<p class="lead">Loose</p><div class="footer"><span class="pageno">03</span></div>',
                 content=False)
    assert texts(s) == ["Sec", "Loose"]


def test_cover_extra_text_beside_field_is_kept_as_unknown():
    s = slide_of('<div class="cv-text"><div><span class="cv-kicker">Kick</span> extra words</div>'
                 '<h1 class="cv-title">Title</h1></div>', content=False)
    assert s["kind"] == "cover"
    assert [(b["kind"], b.get("text")) for b in s["blocks"]] == [
        ("field", "Kick"), ("field", "Title"), ("unknown", "extra words")]


def test_dropped_text_lists_drop_selector_strings():
    (s,) = load("cols3.html")["slides"]
    assert set(s["dropped_text"]) == {"ACME", "Private strategic proposal", "06"}


def test_bold_ranges_are_per_field_and_occurrence():
    s = slide_of('<div class="cols-2x"><div class="col"><div class="c-title">Same</div>'
                 '<p class="c-p">Same words here <b>here</b></p></div><div class="col"><div class="c-title">Same</div>'
                 '<p class="c-p">Same words here and <b>Same</b></p></div></div>')
    c1, c2 = s["blocks"][0]["cells"]
    assert c1["body"] == ["Same words here here"] and c1["bold"] == {"body": [(16, 4)]}
    assert c2["bold"] == {"body": [(20, 4)]}
    s = slide_of('<div class="col"><p class="c-p">intro here</p><ul><li>plain</li>'
                 '<li><b>intro</b> here again</li></ul></div>')
    (c,) = s["blocks"][0]["cells"]
    assert c["bold"] == {"bullets": [(6, 5)]}  # "plain\nintro here again"


def test_nested_list_items_once_and_block_children_split():
    s = slide_of('<ul><li>Top <b>one</b><ul><li>Inner a</li><li>Inner b</li></ul></li><li>Top two</li></ul>')
    (c,) = s["blocks"][0]["cells"]
    assert c["bullets"] == ["Top one", "Inner a", "Inner b", "Top two"]
    assert c["bold"] == {"bullets": [(4, 3)]}
    s = slide_of('<p class="lead"><span>foo</span></p><div class="note"><p>foo</p><p>bar</p></div>')
    assert texts(s) == ["foo", "foo\nbar"]


def test_bold_across_br_bolds_both_lines():
    s = slide_of('<p class="lead">First line <b>bold<br>across</b> lines</p>')
    (b,) = s["blocks"]
    assert b["text"] == "First line bold\nacross lines"
    assert b["bold"] == [(11, 4), (16, 6)]


def test_table_first_all_th_row_is_header_without_thead():
    s = slide_of('<table class="tbl"><tr><th style="width:30%">A</th><th>B</th></tr>'
                 '<tr><td>1</td><td>2</td></tr></table>')
    (b,) = s["blocks"]
    assert b["header"] == ["A", "B"] and b["rows"] == [["1", "2"]] and b["widths"] == [0.3, None]


def test_dropped_text_counts_each_occurrence():
    html = """<div class="slide-wrap"><div class="slide dk">
    <div class="header-bar"><span class="header-sec">Sec</span><span class="wm-top">ACME</span></div>
    <div class="content"><h2 class="h2">Title</h2></div>
    <div class="footer"><span class="brand">ACME<span class="sub">Private</span></span><span class="pageno">02</span></div>
    </div></div>"""
    s = parse(html)["slides"][0]
    assert s["raw_text"].count("ACME") == 2
    assert s["dropped_text"].count("ACME") == 2
    assert s["dropped_text"].count("Private") == 1


# ---------------------------------------------------------------- fresheyes fixes (rows 1, 2, 4, 5, 10)

def compiled(html):
    from gslider.compile import compile_deck
    from gslider.layout import load_layout

    deck = parse(html)
    return deck, compile_deck(deck, load_layout(), {}, set())


def wrap(inner, cls="slide dk", style="", content=True):
    body = f'<div class="content"><h2>T</h2>{inner}</div>' if content else inner
    st = f' style="{style}"' if style else ""
    return f'<div class="slide-wrap" data-slide-id="x"><div class="{cls}"{st}>{body}</div></div>'


def test_identical_drop_elements_counted_twice():
    from gslider.check import coverage
    from gslider.layout import load_layout

    html = ('<div class="slide-wrap"><div class="slide dk"><div class="header-bar">'
            '<span class="wm-top">ACME</span><span class="wm-top">ACME</span></div>'
            '<div class="content"><h2>Title</h2></div></div></div>')
    deck = parse(html)
    (s,) = deck["slides"]
    assert s["raw_text"].count("ACME") == 2
    assert s["dropped_text"].count("ACME") == 2
    plans = [{"n": 1, "slide_id": "s01_slide"}]
    shape = {"objectId": "t", "shape": {"text": {"textElements": [{"textRun": {"content": "Title\n"}}]}}}
    pres = {"slides": [{"objectId": "s01_slide", "pageElements": [shape]}]}
    missing, _ = coverage(deck, plans, pres, load_layout())
    assert missing == {}


@pytest.mark.parametrize("inner", [
    '<img src="photo.jpg">',
    '<div class="col"><img src="photo.jpg"></div>',
    '<table class="tbl"><tr><td>a <img src="photo.jpg"></td></tr></table>',
    '<div class="side-img" style="background-image:url(\'photo.jpg\')"></div>',
])
def test_external_img_falls_back(inner):
    deck, (p,) = compiled(wrap(inner))
    assert any(b["kind"] == "unknown" and b["cls"].startswith("external-image")
               and b["text"] == "photo.jpg" for b in deck["slides"][0]["blocks"])
    assert p["status"] == "fallback" and "photo.jpg" in p["reason"]


def test_cover_url_image_falls_back():
    html = wrap('<div class="cv-text"><h1 class="cv-title">Title</h1></div>'
                '<div class="cv-img" style="background-image:url(cover.jpg)"></div>', content=False)
    deck, (p,) = compiled(html)
    assert deck["slides"][0]["kind"] == "cover"
    assert p["status"] == "fallback"
    assert p["reason"].startswith("unknown block") and "cover.jpg" in p["reason"]


def test_slide_background_url_falls_back():
    deck, (p,) = compiled(wrap('<p class="lead">x</p>', style="background:url(bg.jpg) center/cover"))
    (s,) = deck["slides"]
    assert s["background"] is None
    assert p["status"] == "fallback" and "bg.jpg" in p["reason"]


PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
       "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def test_mixed_images_fall_back():
    deck, (p,) = compiled(wrap(f'<img src="{PNG}"><img src="photo.jpg">'))
    ks = kinds(deck["slides"][0])
    assert "image" in ks and "unknown" in ks
    assert p["status"] == "fallback" and "photo.jpg" in p["reason"]


def test_data_uri_img_still_native_image():
    deck, (p,) = compiled(wrap(f'<img src="{PNG}">'))
    assert kinds(deck["slides"][0]) == ["title", "image"]
    assert p["status"] == "native"


def test_table_bold_ranges():
    s = slide_of('<table class="tbl"><thead><tr><th>Plain</th><th><b>Head</b></th></tr></thead>'
                 '<tr><td>x</td><td><b>bold</b> cell</td></tr><tr><td>y</td><td>none</td></tr></table>')
    (b,) = s["blocks"]
    assert b["rows"] == [["x", "bold cell"], ["y", "none"]]
    assert b["bold"] == [[[], [[0, 4]]], [[], []]]
    assert b["header_bold"] == [[], [[0, 4]]]


def test_table_without_bold_has_empty_bold_lists():
    (s,) = load("tbl.html")["slides"]
    b = s["blocks"][2]
    assert b["bold"] == [[[] for _ in row] for row in b["rows"]]
    assert b["header_bold"] == [[] for _ in b["header"]]
    s = slide_of('<table><tr><td>1</td><td>2</td></tr></table>')
    assert s["blocks"][0]["header_bold"] is None


def test_timeline_bold_ranges():
    s = slide_of('<div class="timeline"><div class="tl"><span class="tl-k"><b>Dec</b></span>'
                 ' loose <b>tl</b> text</div><div class="tl"><span class="tl-k">Jan</span> plain</div></div>')
    (b,) = s["blocks"]
    assert b["rows"] == [["Dec", "loose tl text"], ["Jan", "plain"]]
    assert b["bold"] == [[[[0, 3]], [[6, 2]]], [[], []]]
    assert b["header_bold"] is None


def test_deck_json_serialisable_with_bold():
    import json

    from gslider.parse import to_jsonable

    deck = parse(wrap('<table><tr><td><b>bold</b> cell</td></tr></table>'))
    out = json.loads(json.dumps(to_jsonable(deck)))
    rows = [b for b in out["slides"][0]["blocks"] if b["kind"] == "rows"]
    assert rows[0]["bold"] == [[[[0, 4]]]]


@pytest.mark.parametrize("where", ["img", "style", "background"])
def test_malformed_data_uri_falls_back(where):
    bad = "data:image/png;base64,A"
    inner = {"img": f'<img src="{bad}">',
             "style": f'<div class="side-img" style="background-image:url({bad})"></div>',
             "background": '<p class="lead">x</p>'}[where]
    deck, (p,) = compiled(wrap(inner, style=f"background:url({bad})" if where == "background" else ""))
    assert any(b["kind"] == "unknown" and b["cls"].startswith("bad-image")
               and b["text"] == "malformed data URI" for b in deck["slides"][0]["blocks"])
    assert p["status"] == "fallback" and "malformed data URI" in p["reason"]


def test_no_page_no_field():
    (s,) = load("cols3.html")["slides"]
    assert "page_no" not in s


@pytest.mark.parametrize("inner, own_reason", [
    (f'<div class="col"><div class="c-title">H</div><img src="{PNG}"></div>', True),
    (f'<table class="tbl"><tr><td>a <img src="{PNG}"></td></tr></table>', True),
    # the same image placed as a block and again inside a cell: the cell copy still counts
    # (this slide also overflows on the full-width image first, so that reason wins)
    (f'<img src="{PNG}"><div class="col"><div class="c-title">H</div><img src="{PNG}"></div>', False),
])
def test_data_uri_img_inside_cell_falls_back(inner, own_reason):
    deck, (p,) = compiled(wrap(inner))
    unknown = [b for b in deck["slides"][0]["blocks"] if b["kind"] == "unknown"]
    assert [b["cls"] for b in unknown] == ["inline-image image/png"]
    assert p["status"] == "fallback"
    if own_reason:
        assert "inline-image image/png" in p["reason"]


# ---------------------------------------------------------------- code review of the fresheyes fixes

SVG_URL = "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg'/>"


@pytest.mark.parametrize("style", [
    f'background-image:url(&quot;{SVG_URL}&quot;)',  # double-quoted, single quotes inside
    "background-image:url('data:image/svg+xml;utf8,<svg>(x)</svg>')",  # ')' inside the quotes
])
def test_quoted_url_with_inner_quote_or_paren_never_vanishes(style):
    deck, (p,) = compiled(wrap(f'<div class="side-img" style="{style}"></div>'))
    blocks = deck["slides"][0]["blocks"]
    assert any(b["kind"] in ("image", "unknown") for b in blocks[1:])
    assert p["status"] == "fallback"  # svg is not a placeable image


def test_url_regex_reads_each_quote_form():
    from gslider.parse import _image_from_style

    for style in ("background:url(a.jpg)", "background:url('a.jpg')", 'background:url("a.jpg")',
                  "background:url( 'a.jpg' )"):
        assert _image_from_style(style)["text"] == "a.jpg", style


def test_w60_image_is_placed_once_and_native():
    deck, (p,) = compiled(wrap(f'<p class="lead w60">x</p><img class="w60" src="{PNG}">'))
    assert kinds(deck["slides"][0]) == ["title", "lead", "image"]
    assert p["status"] == "native"


def test_image_beside_content_falls_back():
    html = ('<div class="slide-wrap"><div class="slide dk"><div class="content"><h2 class="h2">T</h2>'
            '</div><img src="photo.jpg"></div></div>')
    deck, (p,) = compiled(html)
    assert any(b["kind"] == "unknown" and b["cls"] == "external-image photo.jpg"
               for b in deck["slides"][0]["blocks"])
    assert p["status"] == "fallback" and "photo.jpg" in p["reason"]


def test_image_in_chrome_beside_content_is_not_swept():
    html = ('<div class="slide-wrap"><div class="slide dk"><div class="header-bar"><img src="logo.png">'
            '</div><div class="content"><h2 class="h2">T</h2></div><div class="footer"><img src="f.png">'
            '</div></div></div>')
    deck, (p,) = compiled(html)
    assert kinds(deck["slides"][0]) == ["title"] and p["status"] == "native"


def test_upper_case_data_uri_is_an_image():
    from gslider.parse import _image_from_src

    img = _image_from_src("DATA:image/png;BASE64," + PNG.split(",", 1)[1])
    assert img["kind"] == "image" and img["mime"] == "image/png" and img["data"].startswith(b"\x89PNG")


def test_junk_base64_is_malformed():
    from gslider.parse import BAD_IMAGE, _image_from_src

    assert _image_from_src("data:image/png;base64,$$$$")["text"] == BAD_IMAGE
    # whitespace inside the payload and missing padding are still accepted
    body = PNG.split(",", 1)[1].rstrip("=")
    assert _image_from_src("data:image/png;base64," + body[:10] + "\n " + body[10:])["kind"] == "image"
