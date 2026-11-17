import pytest

from gslider.fonts import line_height, measure

pytestmark = pytest.mark.usefixtures("fonts_ready")

SLIDE14_TITLE = (
    "Investment: $50,000 per roundtable; other fees agreed after Stage 1, "
    "with the rest set once the strategy hypothesis is agreed in December."
)


def test_line_height_ratios():
    assert line_height("Playfair Display", 1, 100) == pytest.approx(1.333, abs=0.001)
    assert line_height("Libre Franklin", 1, 100) == pytest.approx(1.212, abs=0.001)
    assert line_height("Libre Franklin", 14, 115) == pytest.approx(14 * 1.212 * 1.15, abs=0.01)


def test_measure_single_line():
    lines, h = measure("Short", "Libre Franklin", 14, 400)
    assert lines == 1
    assert h == pytest.approx(14 * 1.212, abs=0.01)


def test_measure_wraps():
    # Pinned once from Pillow (2026-11-16): the string is ~1726pt wide at 28pt
    # Playfair, so it wraps to 3 lines in an 864pt box (849.6pt text width).
    lines, h = measure(SLIDE14_TITLE, "Playfair Display", 28, 864)
    assert lines == 3
    assert h == pytest.approx(3 * 28 * 1.333, abs=0.01)


def test_measure_newlines():
    lines, _ = measure("One\nTwo\nThree", "Libre Franklin", 14, 400)
    assert lines == 3
    lines, _ = measure("One\n\nThree", "Libre Franklin", 14, 400)
    assert lines == 3


def test_measure_line_spacing_scales_height():
    _, h100 = measure("Short", "Libre Franklin", 14, 400, 100)
    _, h115 = measure("Short", "Libre Franklin", 14, 400, 115)
    assert h115 == pytest.approx(h100 * 1.15)


def test_unknown_family_raises():
    with pytest.raises(ValueError):
        measure("x", "Comic Sans", 14, 400)


def test_long_single_word_wraps_by_width():
    # one 400-letter word: Slides breaks it across lines, so it is not one line
    word = "A" * 400
    from gslider.fonts import text_width
    avail = 400 - 2 * 7.2
    expect = -(-text_width(word, "Libre Franklin", 14) // avail)
    lines, _ = measure(word, "Libre Franklin", 14, 400)
    assert lines == expect and lines > 10
    lines, _ = measure("short " + word, "Libre Franklin", 14, 400)
    assert lines == expect + 1


def test_wrap_slack_matches_live_slides_wrap():
    # Live build: Slides wrapped this text (client name changed) to 4 lines in a 415pt-wide box at 14pt, 115%.
    t = ("Northwind's commercial leadership and ACME's strategy and editorial leads agree the ambition, "
         "the goal for five to ten years, the risk the owners will take and the relationship priorities.")
    assert measure(t, "Libre Franklin", 14, 415, 115)[0] == 4


# --- any Google Fonts family -------------------------------------------------

import json  # noqa: E402

import gslider.fonts as fonts  # noqa: E402

INTER_CSS = """@font-face {
  font-family: 'Inter';
  font-style: normal;
  font-weight: 400;
  src: url(https://fonts.gstatic.com/s/inter/v20/abc400.ttf) format('truetype');
}
@font-face {
  font-family: 'Inter';
  font-style: normal;
  font-weight: 700;
  src: url(https://fonts.gstatic.com/s/inter/v20/abc700.ttf) format('truetype');
}
"""


def _fake_fetch(responses, calls):
    def fetch(url):
        calls.append(url)
        for name, resp in responses.items():
            if f"family={name}" in url:
                return resp
        return 400, "Missing font family"
    return fetch


def test_css_api_ttf_url_parsed():
    assert fonts.ttf_url_from_css(INTER_CSS) == "https://fonts.gstatic.com/s/inter/v20/abc400.ttf"
    assert fonts.ttf_url_from_css(INTER_CSS, weight=700).endswith("abc700.ttf")
    one = "@font-face { font-weight: 400; src: url(https://x.test/a.ttf) format('truetype'); }"
    assert fonts.ttf_url_from_css(one, weight=700) == "https://x.test/a.ttf"  # nearest weight
    assert fonts.ttf_url_from_css("<html>error</html>") is None


def test_css_url_built_with_weights():
    assert fonts.css_url("Playfair Display") == (
        "https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700"
    )


def test_unknown_family_unavailable(font_cache, monkeypatch):
    calls = []
    monkeypatch.setattr(fonts, "_fetch_css", _fake_fetch({}, calls))
    assert fonts.google_font_available("Comic Sans") is False
    n = len(calls)
    assert n >= 1
    assert json.loads((font_cache / "fonts.json").read_text())["Comic Sans"] is False
    assert fonts.google_font_available("Comic Sans") is False
    assert len(calls) == n  # second answer came from the cache
    with pytest.raises(fonts.FontUnavailable):
        fonts.font_path("Comic Sans")
    assert issubclass(fonts.FontUnavailable, ValueError)
    assert fonts.normal_line_ratio("Comic Sans") is None


def test_known_family_available_and_cached(font_cache, monkeypatch):
    calls = []
    monkeypatch.setattr(fonts, "_fetch_css", _fake_fetch({"Inter": (200, INTER_CSS)}, calls))
    assert fonts.google_font_available("Inter") is True
    assert fonts.google_font_available("Inter") is True
    assert len(calls) == 1
    assert json.loads((font_cache / "fonts.json").read_text()) == {"Inter": True}


def test_network_error_not_cached(font_cache, monkeypatch):
    def boom(url):
        raise OSError("no network")
    monkeypatch.setattr(fonts, "_fetch_css", boom)
    assert fonts.google_font_available("Inter") is False
    assert not (font_cache / "fonts.json").exists()


def test_pinned_families_skip_css_api(font_cache, monkeypatch):
    def boom(url):
        raise AssertionError("CSS API must not be called for pinned families")
    monkeypatch.setattr(fonts, "_fetch_css", boom)
    assert fonts.google_font_available("Playfair Display") is True
    assert fonts.google_font_available("Libre Franklin") is True


def test_font_path_downloads_css_ttf(font_cache, monkeypatch):
    calls, got = [], []
    monkeypatch.setattr(fonts, "_fetch_css", _fake_fetch({"Inter": (200, INTER_CSS)}, calls))

    def fake_download(url, dest):
        got.append(url)
        dest.write_bytes(b"\0" * 20_000)
    monkeypatch.setattr(fonts, "_download", fake_download)
    p = fonts.font_path("Inter")
    assert p == font_cache / "Inter.ttf" and p.exists()
    assert got == ["https://fonts.gstatic.com/s/inter/v20/abc400.ttf"]
    fonts.font_path("Inter")
    assert len(got) == 1  # cached on disk


def test_slides_family_substitution(font_cache, monkeypatch):
    calls = []
    monkeypatch.setattr(fonts, "_fetch_css", _fake_fetch({"Inter": (200, INTER_CSS)}, calls))
    assert fonts.slides_family("Helvetica Neue", "sans-serif") == ("Arial", True)
    assert fonts.slides_family("Inter", "sans-serif") == ("Inter", False)
    assert fonts.slides_family('"Inter"', "sans-serif") == ("Inter", False)
    assert fonts.slides_family("Baskerville", "serif") == ("Georgia", True)
    assert fonts.slides_family("Menlo", "monospace") == ("Courier New", True)
    assert fonts.slides_family("Weird", "") == ("Arial", True)
    assert fonts.slides_family("serif", "serif") == ("Georgia", False)  # generic, not a substitution


@pytest.mark.usefixtures("fonts_ready")
def test_existing_families_unchanged():
    assert fonts.normal_line_ratio("Playfair Display") == pytest.approx(1.333, abs=0.001)
    assert fonts.normal_line_ratio("Libre Franklin") == pytest.approx(1.212, abs=0.001)
    assert fonts.font_path("Playfair Display").name == "PlayfairDisplay.ttf"
    assert fonts.font_path("Libre Franklin").name == "LibreFranklin.ttf"


@pytest.mark.network
def test_live_css_api(font_cache, online):
    assert fonts.google_font_available("Inter") is True
    assert fonts.google_font_available("Comic Sans") is False
    ratio = fonts.normal_line_ratio("Inter")
    assert ratio is not None and 1.0 < ratio < 1.6
    assert fonts.text_width("Hello", "Inter", 14) > 0


# --- review 2: S2 cache name and download limits ---------------------------------------

@pytest.mark.parametrize("family", ["../../evil", "a/b", "x\x00y", "Inter; rm -rf"])
def test_font_cache_name_is_safe(font_cache, monkeypatch, family):
    monkeypatch.setattr(fonts, "google_font_available", lambda f: True)
    monkeypatch.setattr(fonts, "_css_for", lambda f: (200, INTER_CSS))
    got = []
    monkeypatch.setattr(fonts, "_download", lambda url, dest: got.append(dest) or dest.write_bytes(b"\0" * 20_000))
    p = fonts.font_path(family)
    assert p.parent == font_cache and all(c.isalnum() or c in "_-" for c in p.stem)
    assert got[0].parent == font_cache


@pytest.mark.parametrize("family", ["", "...", "/ /", "\x00"])
def test_font_cache_name_empty_refused(font_cache, family):
    with pytest.raises(fonts.FontUnavailable):
        fonts.font_path(family)


def test_download_has_time_and_size_limits(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(fonts.subprocess, "run", lambda cmd, **kw: seen.append(cmd))
    fonts._download("https://fonts.gstatic.com/x.ttf", tmp_path / "x.part")
    cmd = seen[0]
    assert cmd[cmd.index("--max-time") + 1] == "60" and cmd[cmd.index("--max-filesize") + 1] == "20M"
    assert cmd[cmd.index("--proto") + 1] == "=https" and cmd[cmd.index("--proto-redir") + 1] == "=https"
