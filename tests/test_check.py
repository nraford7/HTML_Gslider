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
