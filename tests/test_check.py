"""Offline tests for gslider.check against a recorded-shape `get` of the cols3 fixture.

Fixtures (made from a compile of tests/fixtures/cols3.html):
  check_cols3_deck.json   deck.json as build writes it
  check_cols3_plans.json  plans.json as build writes it
  check_cols3_get.json    `slides presentations get` response for that build
"""
import copy
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from gslider import check as C
from gslider import gws
from gslider.layout import load_layout

FIX = Path(__file__).parent / "fixtures"
PID = "1FAKEgsliderCheckFixture000000000000000000000"
DARK = (8, 8, 6)


def load(name):
    return json.loads((FIX / f"check_cols3_{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def deck():
    return load("deck")


@pytest.fixture
def plans():
    return load("plans")


@pytest.fixture
def pres():
    return load("get")


@pytest.fixture(scope="module")
def layout():
    return load_layout()


def element(pres, oid):
    for sl in pres["slides"]:
        for el in sl["pageElements"]:
            if el["objectId"] == oid:
                return el
    raise KeyError(oid)


def runs(pres, oid):
    return [e["textRun"] for e in element(pres, oid)["shape"]["text"]["textElements"] if "textRun" in e]


# ------------------------------------------------------------------ coverage

def test_coverage_flags_missing_string(deck, plans, pres, layout):
    missing, _ = C.coverage(deck, plans, pres, layout)
    assert missing == {}
    run = next(r for r in runs(pres, "s01_body15") if "500+" in r["content"])
    run["content"] = run["content"].replace("500+ boardroom relationships.", "500 boardroom ties.")
    missing, _ = C.coverage(deck, plans, pres, layout)
    assert missing == {1: ["500+ boardroom relationships."]}


def test_coverage_reads_speaker_notes(deck, plans, pres, layout):
    """A fallback slide carries its text in the notes; that counts as covered."""
    sl = pres["slides"][0]
    text = "\n".join(deck["slides"][0]["raw_text"])
    sl["pageElements"] = [e for e in sl["pageElements"] if "shape" not in e]
    notes = sl["slideProperties"]["notesPage"]["pageElements"][0]["shape"]
    notes["text"] = {"textElements": [{"endIndex": len(text) + 1, "paragraphMarker": {"style": {}}},
                                      {"endIndex": len(text) + 1, "textRun": {"content": text + "\n", "style": {}}}]}
    missing, _ = C.coverage(deck, plans, pres, layout)
    assert missing == {}


@pytest.mark.parametrize("via", ["source_html", "dropped_text"])
def test_coverage_ignores_drop_list(via, deck, plans, pres, layout):
    chrome = ["ACME", "ACME", "Private strategic proposal", "06"]  # .wm-top, .brand, .footer .sub, .pageno
    if via == "dropped_text":
        deck["source"] = None
        deck["slides"][0]["dropped_text"] = chrome
    missing, dropped = C.coverage(deck, plans, pres, layout)
    assert missing == {}
    assert sorted(dropped[1]) == sorted(chrome)
    # control: without the drop list the chrome strings are reported missing
    bare = copy.deepcopy(deck)
    bare["source"] = None
    bare["slides"][0].pop("dropped_text", None)
    notes = []
    missing, _ = C.coverage(bare, plans, pres, layout, notes)
    # the two chrome "ACME" strings are missing too: the title's "ACME" covers only one (S9)
    assert sorted(missing[1]) == ["06", "ACME", "ACME", "Private strategic proposal"]
    assert notes and "drop selectors not applied" in notes[0]


def test_coverage_drops_lone_arrow_glyph_only(deck, plans, pres, layout):
    deck["slides"][0]["raw_text"] += ["→", "Loop back → to start"]
    missing, _ = C.coverage(deck, plans, pres, layout)
    assert missing == {1: ["Loop back → to start"]}


def test_coverage_short_string_needs_whole_word(deck, plans, pres, layout):
    """S9: a string of 3 chars or fewer must match as a word, not inside one ("nin" in "running")."""
    deck["slides"][0]["raw_text"] += ["nin"]
    missing, _ = C.coverage(deck, plans, pres, layout)
    assert missing == {1: ["nin"]}


def test_coverage_consumes_matches(deck, plans, pres, layout):
    """S9: a string the source has twice must appear twice in the built slide."""
    deck["slides"][0]["raw_text"] += ["One coherent programme."]
    missing, _ = C.coverage(deck, plans, pres, layout)
    assert missing == {1: ["One coherent programme."]}


# ------------------------------------------------------------------ style audit

def test_style_audit_flags_second_combo(deck, plans, pres, layout):
    assert C.style_audit(pres, plans, deck, layout) == []
    for r in runs(pres, "s01_body14"):
        r["style"]["foregroundColor"]["opaqueColor"]["rgbColor"] = {"red": 1, "green": 1, "blue": 1}
    out = C.style_audit(pres, plans, deck, layout)
    combo = [v for v in out if "2 combos" in v]
    assert len(combo) == 1 and combo[0].startswith("body/dark")
    assert "#FFFFFF" in combo[0] and "#C8C2B6" in combo[0] and "s01_body14" in combo[0]


def test_style_audit_uses_built_ground(deck, plans, pres, layout):
    """Same dark colours on a slide built ivory are a violation against layouts.yaml."""
    for p in plans:
        p["ground"] = "ivory"
    out = C.style_audit(pres, plans, deck, layout)
    assert any(v.startswith("body/ivory") and "differs from layouts.yaml" in v for v in out)


def test_role_of():
    assert C.role_of("s01_band_label12") == "band_label"
    assert C.role_of("s14_body3") == "body"
    assert C.role_of("i0") is None


def test_role_of_three_digit_slide():
    """Row 11: compile names slide 100's objects s100_*; both check regexes must read them."""
    assert C.role_of("s100_body3") == "body"
    assert C.role_of("s100_band_label12") == "band_label"


def test_style_audit_maps_three_digit_slide(deck, plans, pres, layout):
    """A live slide not in plans is matched to its deck slide by the number in its object id."""
    def renum(obj):
        if isinstance(obj, dict):
            return {k: renum(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [renum(v) for v in obj]
        return obj.replace("s01_", "s100_") if isinstance(obj, str) else obj
    pres = renum(pres)
    deck = copy.deepcopy(deck)
    for s in deck["slides"]:
        if s["n"] == 1:
            s["n"], s["ground"] = 100, "ivory"
    out = C.style_audit(pres, [], deck, layout)
    assert any(v.startswith("body/ivory") and "differs from layouts.yaml" in v for v in out), out


# ------------------------------------------------------------------ overflow

pytestmark_fonts = pytest.mark.usefixtures("fonts_ready")


@pytestmark_fonts
def test_overflow_estimate_flags_tall_text(plans, layout):
    flags, _ = C.overflow(plans, layout)
    assert flags == []
    box = next(b for b in plans[0]["boxes"] if b["object_id"] == "s01_body13")
    live = {"s01_slide": {"shapes": {"s01_body13": "former ministers and chairs. " * 12}}}
    flags, _ = C.overflow(plans, layout, live)
    assert [(n, oid) for n, oid, *_ in flags] == [(1, "s01_body13")]
    n, oid, est, h = flags[0]
    assert h == round(box["h"], 1) and est > h


@pytestmark_fonts
def test_overflow_estimate_matches_compile_with_bullets(plans, layout):
    """Re-measuring the built text reproduces box h (bullets measured with their indent)."""
    p = plans[0]
    for box in p["boxes"]:
        est = C.estimate_h(box, box["text"], layout, C._bullet_start(p, box["object_id"]))
        assert est == pytest.approx(box["h"], abs=0.01), box["object_id"]


def _thumb(tmp_path, ink_band=None):
    img = Image.new("RGB", (1600, 900), DARK)
    if ink_band:
        s = 1600 / 960
        x0, y0, x1, y1 = ink_band
        ImageDraw.Draw(img).rectangle((x0 * s + 4, y0 * s + 1, x0 * s + 60, y0 * s + 6), fill=(242, 239, 232))
    path = tmp_path / "s01.png"
    img.save(path)
    return path


@pytestmark_fonts
def test_overflow_pixel_scan_confirms_ink_below_box(plans, layout, tmp_path):
    box = next(b for b in plans[0]["boxes"] if b["object_id"] == "s01_lead3")
    band = C.scan_band(box, plans[0], layout)
    assert band is not None and band[1] > box["y"] + layout.inset + box["h"]
    live = {"s01_slide": {"shapes": {"s01_lead3": _near_full_line(box, layout)}}}
    flags, _ = C.overflow(plans, layout, live, {1: _thumb(tmp_path)})
    assert flags == []
    flags, _ = C.overflow(plans, layout, live, {1: _thumb(tmp_path, band)})
    assert [oid for _, oid, *_ in flags] == ["s01_lead3"]


def _near_full_line(box, layout):
    """One line of lead text that fills 90-97% of the wrap width (the case Slides may wrap)."""
    from gslider.fonts import INSET_PT, WRAP_SLACK, text_width
    st = layout.styles["lead"]["dark"]
    avail = (box["w"] - 2 * INSET_PT) / WRAP_SLACK
    words, text = "One coherent programme for the owners and the board".split(), ""
    i = 0
    while True:
        cand = f"{text} {words[i % len(words)]}".strip()
        if text_width(cand, st["font"], st["size"]) > 0.97 * avail:
            break
        text, i = cand, i + 1
    assert text_width(text, st["font"], st["size"]) >= C.FULL * avail
    return text


@pytestmark_fonts
def test_short_single_line_box_skips_pixel_pass(plans, layout, tmp_path):
    """S12: a one-line box well inside its width is not sent to the thumbnail (no noise)."""
    box = next(b for b in plans[0]["boxes"] if b["object_id"] == "s01_lead3")
    band = C.scan_band(box, plans[0], layout)
    flags, unconfirmed = C.overflow(plans, layout, None, {1: _thumb(tmp_path, band)})
    assert flags == [] and unconfirmed == []


# ------------------------------------------------------------------ end to end (gws mocked)

@pytestmark_fonts
def test_check_writes_report_and_contact_sheet(deck, plans, pres, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gws, "get", lambda pid, fields=None: (calls.append(("get", pid)), pres)[1])

    def thumb(pid, page_id, out_path):
        calls.append(("thumb", page_id))
        Image.new("RGB", (1600, 900), DARK).save(out_path)
        return str(out_path)

    monkeypatch.setattr(gws, "thumbnail", thumb)
    (tmp_path / "build.json").write_text(json.dumps({
        "pid": PID, "url": f"https://docs.google.com/presentation/d/{PID}/edit", "title": "cols3 test",
        "status": "done", "shared_assets": ["1assetAAA", "1assetBBB"]}), encoding="utf-8")
    res = C.check(PID, deck, plans, tmp_path)
    assert res.ok, (res.coverage_missing, res.overflow, res.style_violations)
    assert ("thumb", "s01_slide") in calls
    assert Image.open(res.contact).size == (4 * 400 + 5 * C.GUTTER, 225 + 2 * C.GUTTER)
    rep = res.report.read_text(encoding="utf-8")
    assert f"https://docs.google.com/presentation/d/{PID}/edit" in rep
    assert "| 1 | content (why) | native |" in rep
    assert "1assetAAA" in rep and "Result: PASS" in rep


def _mock_get(monkeypatch, pres):
    monkeypatch.setattr(gws, "get", lambda pid, fields=None: pres)


def test_check_flags_live_slides_not_in_plans(deck, plans, pres, tmp_path, monkeypatch):
    """S10: an extra slide in the live deck fails the check."""
    extra = copy.deepcopy(pres["slides"][0])
    extra["objectId"] = "zz_extra"
    pres["slides"].append(extra)
    _mock_get(monkeypatch, pres)
    (tmp_path / "build.json").write_text(json.dumps({"pid": PID, "status": "done"}))
    res = C.check(PID, deck, plans, tmp_path, thumbnails=False, layout=load_layout())
    assert not res.ok and res.extra_slides == ["zz_extra"]
    assert "zz_extra" in res.report.read_text(encoding="utf-8")


def test_check_fails_when_build_not_done(deck, plans, pres, tmp_path, monkeypatch):
    """S10: build.json status other than done fails the check."""
    _mock_get(monkeypatch, pres)
    (tmp_path / "build.json").write_text(json.dumps({"pid": PID, "status": "failed", "error": "slide 9"}))
    res = C.check(PID, deck, plans, tmp_path, thumbnails=False, layout=load_layout())
    assert not res.ok
    assert "failed" in res.report.read_text(encoding="utf-8")


def test_report_has_fallback_caveat(deck, plans, pres, tmp_path, monkeypatch):
    """S13: fallback slides are images of the HTML; the report says what that means."""
    _mock_get(monkeypatch, pres)
    plans[0]["status"] = "fallback"
    plans[0]["boxes"] = []
    res = C.check(PID, deck, plans, tmp_path, thumbnails=False, layout=load_layout())
    rep = res.report.read_text(encoding="utf-8")
    assert "## Fallback slides" in rep and "speaker notes" in rep and "not editable" in rep


def test_shared_assets_reads_only_shared_assets():
    assert C._shared_assets({"shared_files": ["x"], "assets": ["y"]}) == []
    assert C._shared_assets({"shared_assets": ["a", "b"]}) == ["a", "b"]


def test_thumbnails_use_two_workers(monkeypatch, tmp_path):
    seen = {}
    real = C.ThreadPoolExecutor

    def spy(max_workers):
        seen["n"] = max_workers
        return real(max_workers=max_workers)

    monkeypatch.setattr(C, "ThreadPoolExecutor", spy)
    monkeypatch.setattr(gws, "thumbnail", lambda pid, page, out: Image.new("RGB", (16, 9)).save(out) or str(out))
    C.fetch_thumbs("P", [(1, "a")], tmp_path)
    assert seen["n"] == 2


# ------------------------------------------------------------------ native tables (Task 3)

def _run(text, colour=None, size=11):
    style = {"fontFamily": "Comic Sans MS", "fontSize": {"magnitude": size, "unit": "PT"}}
    if colour:
        style["foregroundColor"] = {"opaqueColor": {"rgbColor": colour}}
    return {"text": {"textElements": [{"paragraphMarker": {"style": {"lineSpacing": 300}}},
                                      {"textRun": {"content": text + "\n", "style": style}}]}}


def _table_el(oid, cells, row_h_pt=30.0, y_pt=200.0):
    return {"objectId": oid,
            "size": {"width": {"magnitude": 800 * 12700, "unit": "EMU"},
                     "height": {"magnitude": 1, "unit": "EMU"}},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": 40 * 12700, "translateY": y_pt * 12700,
                          "unit": "EMU"},
            "table": {"rows": len(cells), "columns": len(cells[0]),
                      "tableRows": [{"rowHeight": {"magnitude": row_h_pt * 12700, "unit": "EMU"},
                                     "tableCells": [{"location": {"rowIndex": i, "columnIndex": j}, **_run(t)}
                                                    if t else {"location": {"rowIndex": i, "columnIndex": j}}
                                                    for j, t in enumerate(row)]}
                                    for i, row in enumerate(cells)]}}


def _table_plan(plans, oid, h_pt, y_pt=200.0, rows=2):
    p = plans[0]
    p["boxes"].append({"object_id": oid, "role": "table", "ground": "dark", "x": 40.8, "y": y_pt, "w": 878.4,
                       "h": h_pt, "text": "", "lines": rows, "rows": rows, "cols": 2})
    p["requests"].append({"createTable": {"objectId": oid, "rows": rows, "columns": 2, "elementProperties": {
        "pageObjectId": p["slide_id"],
        "size": {"width": {"magnitude": round(878.4 * 12700), "unit": "EMU"},
                 "height": {"magnitude": round(h_pt * 12700), "unit": "EMU"}},
        "transform": {"scaleX": 1, "scaleY": 1, "translateX": round(40.8 * 12700),
                      "translateY": round(y_pt * 12700), "unit": "EMU"}}}})


def test_check_coverage_reads_table_cells(deck, plans, pres, layout):
    deck["slides"][0]["raw_text"] += ["Stage 0", "Where are we strong today?"]
    missing, _ = C.coverage(deck, plans, pres, layout)
    assert missing == {1: ["Stage 0", "Where are we strong today?"]}
    pres["slides"][0]["pageElements"].append(
        _table_el("s01_table40", [["Stage", "Question"], ["Stage 0", "Where are we strong today?"]]))
    missing, _ = C.coverage(deck, plans, pres, layout)
    assert missing == {}
    texts = C.slide_texts(pres)["s01_slide"]["shapes"]
    assert texts["s01_table40:1:1"] == "Where are we strong today?\n"


def test_style_audit_skips_tables(deck, plans, pres, layout):
    pres["slides"][0]["pageElements"].append(
        _table_el("s01_table40", [["Stage", ""], ["Stage 0", "odd font"]]))
    assert C.style_audit(pres, plans, deck, layout) == []


def test_table_overflow_read_back(plans, pres, layout):
    """Live row heights: a table that grew past the floor (or into the next box) is flagged."""
    _table_plan(plans, "s01_table40", 60.0, y_pt=420.0)
    pres["slides"][0]["pageElements"].append(_table_el("s01_table40", [["a", "b"], ["c", "d"]], 30.0, 420.0))
    assert C.table_overflow(plans, pres, layout) == []
    el = pres["slides"][0]["pageElements"][-1]
    el["table"]["tableRows"][1]["rowHeight"]["magnitude"] = 80 * 12700  # 420 + 110 > floor 495
    assert C.table_overflow(plans, pres, layout) == [(1, "s01_table40", 110.0, 60.0)]


def test_table_overflow_into_next_box(plans, pres, layout):
    p = plans[0]
    top = min(b["y"] for b in p["boxes"] if b["y"] > 150)  # a text box well above the floor
    _table_plan(plans, "s01_table40", 20.0, y_pt=top - 40)
    pres["slides"][0]["pageElements"].append(_table_el("s01_table40", [["a", "b"], ["c", "d"]], 10.0, top - 40))
    assert C.table_overflow(plans, pres, layout) == []
    pres["slides"][0]["pageElements"][-1]["table"]["tableRows"][1]["rowHeight"]["magnitude"] = 60 * 12700
    flags = C.table_overflow(plans, pres, layout)
    assert [(n, oid) for n, oid, *_ in flags] == [(1, "s01_table40")]
    assert top - 40 + 70 < layout.floor  # flagged for the overlap, not the floor


@pytestmark_fonts
def test_overflow_skips_table_boxes(plans, layout, tmp_path):
    _table_plan(plans, "s01_table40", 60.0)
    flags, unconfirmed = C.overflow(plans, layout, None, {1: _thumb(tmp_path)})
    assert flags == [] and not [u for u in unconfirmed if u[1] == "s01_table40"]
    # a table counts as an obstacle by its full rectangle
    obs = C.obstacles(plans[0], layout, "nothing")
    assert (40.8, 200.0, round(40.8 + 878.4, 1), 260.0) in [tuple(round(v, 1) for v in o) for o in obs]


def test_check_flags_grown_table(deck, plans, pres, tmp_path, monkeypatch):
    _table_plan(plans, "s01_table40", 60.0, y_pt=420.0)
    pres["slides"][0]["pageElements"].append(_table_el("s01_table40", [["a", "b"], ["c", "d"]], 60.0, 420.0))
    _mock_get(monkeypatch, pres)
    res = C.check(PID, deck, plans, tmp_path, thumbnails=False, layout=load_layout())
    assert not res.ok and [oid for _, oid, *_ in res.overflow] == ["s01_table40"]


# ------------------------------------------------------------------ html style (Task 6)

IVORY = (244, 240, 231)


def _html_fixture(tmp_path, monkeypatch, transform=None):
    """One html-style slide: heading and body over an ivory plate with a red rule."""
    from gslider import htmlstyle as H

    monkeypatch.setattr(H, "slides_family", lambda fam, gen: ("Arial", True) if fam == "Helvetica" else (fam, False))
    monkeypatch.setattr(H, "normal_line_ratio", lambda fam: None)
    plate = tmp_path / "plate_s01.png"
    img = Image.new("RGB", (1920, 1080), IVORY)
    ImageDraw.Draw(img).rectangle((120, 370, 720, 376), fill=(181, 69, 43))  # a rule under the body
    img.save(plate)

    def run(text, fam="Georgia", size=80):
        return {"text": text, "family": fam, "generic": "serif", "size_px": size, "bold": False,
                "italic": False, "underline": False, "color": "#1a1917"}

    m = {"n": 1, "background": "#f4f0e7", "fonts_status": "loaded", "unloaded": [], "plate": str(plate),
         "skipped": [{"text": "SIDE NOTE", "reason": "transform rotate(-90deg)"}],
         "blocks": [
             {"x": 120, "y": 100, "w": 900, "h": 96, "lines": 1, "align": "left", "line_height_px": 96,
              "letter_spacing_px": 0, "transform": transform, "runs": [run("Heading")]},
             {"x": 120, "y": 300, "w": 900, "h": 48, "lines": 1, "align": "left", "line_height_px": 48,
              "letter_spacing_px": 2, "transform": None, "runs": [run("Body text here", "Helvetica", 32)]}]}
    deck = {"title": "t", "source": None, "slides": [
        {"n": 1, "html_index": 1, "id": "one", "kind": "content", "ground": "ivory", "blocks": [],
         "raw_text": ["Heading", "Body text here", "SIDE NOTE"], "dropped_text": []}]}
    plans = H.compile_html_deck(deck, {1: m}, {"plate:1": "https://u/plate"})
    deck["slides"][0]["dropped_text"] = plans[0]["dropped_text"]
    shapes = []
    for b in plans[0]["boxes"]:
        shapes.append({"objectId": b["object_id"],
                       "transform": {"scaleX": 1, "scaleY": 1, "translateX": round(b["x"] * 12700),
                                     "translateY": round(b["y"] * 12700), "unit": "EMU"},
                       "shape": _run(b["text"], {"red": 0.1, "green": 0.1, "blue": 0.1}, size=40)})
    pres = {"slides": [{"objectId": "s01_slide", "pageElements": [
        {"objectId": "s01_plate", "image": {}, "transform": {"translateX": 0, "translateY": 0}}, *shapes]}]}
    return deck, plans, pres, plate


def _html_thumb(tmp_path, plate, ink_below=None):
    img = Image.open(plate).convert("RGB").resize((1600, 900))
    if ink_below:
        s = 1600 / 960
        x0, y0, x1, y1 = ink_below
        ImageDraw.Draw(img).rectangle((x0 * s + 4, y0 * s + 1, x0 * s + 60, y0 * s + 6), fill=(20, 20, 20))
    path = tmp_path / "thumb_s01.png"
    img.save(path)
    return path


def test_check_html_skips_style_audit(tmp_path, monkeypatch, layout):
    deck, plans, pres, plate = _html_fixture(tmp_path, monkeypatch)
    _mock_get(monkeypatch, pres)
    (tmp_path / "build.json").write_text(json.dumps({"pid": PID, "status": "done", "style": "html",
                                                     "plates": ["1plate"]}))
    res = C.check(PID, deck, plans, tmp_path, thumbnails=False, layout=layout)
    assert res.style == "html"
    assert res.style_violations == []  # Comic Sans 20pt on every box would violate template roles
    assert res.coverage_missing == {}
    assert res.ok


def test_html_coverage_ignores_case_from_text_transform(tmp_path, monkeypatch, layout):
    deck, plans, pres, _ = _html_fixture(tmp_path, monkeypatch, transform="uppercase")
    assert plans[0]["boxes"][0]["text"] == "HEADING"
    for sl in pres["slides"]:
        for el in sl["pageElements"]:
            if el["objectId"] == "s01_t0":
                el["shape"] = _run("HEADING")
    missing, _ = C.coverage(deck, plans, pres, layout, fold=True)
    assert missing == {}
    missing, _ = C.coverage(deck, plans, pres, layout)
    assert missing == {1: ["Heading"]}


def test_obstacles_ignore_plate(tmp_path, monkeypatch, layout):
    _, plans, _, _ = _html_fixture(tmp_path, monkeypatch)
    obs = C.obstacles(plans[0], layout, "s01_t0")
    assert len(obs) == 1  # the body box only: the full-page plate is not an obstacle


def test_html_overflow_compares_against_plate(tmp_path, monkeypatch, layout):
    _, plans, _, plate = _html_fixture(tmp_path, monkeypatch)
    body = plans[0]["boxes"][1]
    band = C.scan_band(body, plans[0], layout)
    assert band is not None and band[3] - band[1] == pytest.approx(body["line_pt"])  # one line deep
    assert band[1] < 185 < band[3]  # the plate's rule (y 185pt) lies inside the band
    # the plate's own rule under the body is not ink: the thumbnail matches the plate there
    flags, unconfirmed = C.overflow(plans, layout, None, {1: _html_thumb(tmp_path, plate)})
    assert flags == [] and unconfirmed == []
    flags, _ = C.overflow(plans, layout, None, {1: _html_thumb(tmp_path, plate, band)})
    assert [oid for _, oid, *_ in flags] == ["s01_t1"]


def test_html_overflow_without_plate_unconfirmed(tmp_path, monkeypatch, layout):
    _, plans, _, plate = _html_fixture(tmp_path, monkeypatch)
    thumb = _html_thumb(tmp_path, plate)
    plate.unlink()
    flags, unconfirmed = C.overflow(plans, layout, None, {1: thumb})
    assert flags == [] and {why for *_, why in unconfirmed} == {"no plate image to compare"}


def test_position_readback(tmp_path, monkeypatch):
    _, plans, pres, _ = _html_fixture(tmp_path, monkeypatch)
    el = next(e for e in pres["slides"][0]["pageElements"] if e["objectId"] == "s01_t1")
    el["transform"]["translateX"] += 5 * 12700  # 5pt to the right
    pos = C.position_readback(plans, pres)
    assert pos[1]["boxes"] == 2 and pos[1]["max_dx"] == pytest.approx(5.0) and pos[1]["max_dy"] == pytest.approx(0)
    assert pos[1]["off"] == ["s01_t1"] and pos[1]["missing"] == []


def test_image_diff_and_compare_sheet(tmp_path, monkeypatch):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (1600, 900), (100, 100, 100)).save(a)
    Image.new("RGB", (1920, 1080), (110, 110, 110)).save(b)
    assert C.image_diff(a, b) == pytest.approx(10, abs=0.5)
    out = C.compare_sheet({1: a, 2: a}, {1: b}, tmp_path / "compare.png", 2)
    w, h = Image.open(out).size
    assert w == 2 * 400 + 3 * C.GUTTER and h >= 2 * 225


def test_check_html_report_sections(tmp_path, monkeypatch, layout):
    deck, plans, pres, plate = _html_fixture(tmp_path, monkeypatch)
    _mock_get(monkeypatch, pres)
    monkeypatch.setattr(C, "fetch_thumbs", lambda pid, pages, out: {1: _html_thumb(tmp_path, plate)})
    src = tmp_path / "deck.html"
    src.write_text("<div class='slide-wrap'></div>")
    deck["source"] = str(src)
    rendered = []

    def fake_render(html_path, n, out):
        rendered.append(n)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Image.open(plate).save(out)
        return Path(out)

    monkeypatch.setattr(C.assets, "CHROME", "/fake/chrome")
    monkeypatch.setattr(C.assets, "render_slide_png", fake_render)
    (tmp_path / "build.json").write_text(json.dumps({"pid": PID, "status": "done", "style": "html",
                                                     "plates": ["1plate"], "shared_assets": ["1plate"],
                                                     "images_not_loaded": [{"src": "../x.png",
                                                                            "reason": "outside the folder"}]}))
    res = C.check(PID, deck, plans, tmp_path, layout=layout)
    assert rendered == [1]
    assert res.compare and res.compare.exists() and 1 in res.image_diff
    rep = res.report.read_text(encoding="utf-8")
    for heading in ("## Font substitutions", "## Letter-spacing dropped", "## Left in plate", "## Hidden in the HTML",
                    "## Position read-back", "## Image difference"):
        assert heading in rep, heading
    assert "`Helvetica` -> Arial" in rep
    assert "SIDE NOTE" in rep and "rotate" in rep
    assert "compare.png" in rep
    assert "Style audit skipped" in rep
    assert "## Images not loaded" in rep and "- `../x.png`: outside the folder" in rep


def test_render_html_slides_skips_a_deck_the_parsers_split(tmp_path, monkeypatch):
    page = tmp_path / "d.html"
    page.write_text('<!--><div class="slide-wrap">A</div>--><div class="slide-wrap">B</div>')
    monkeypatch.setattr(C.assets, "CHROME", "/fake/chrome")
    monkeypatch.setattr(C.assets, "render_slide_png", lambda *a: pytest.fail("rendered"))
    notes = []
    got = C.render_html_slides({"source": str(page), "slides": []}, [{"n": 1}], tmp_path / "o", notes)
    assert got == {} and "read differently" in notes[0]
