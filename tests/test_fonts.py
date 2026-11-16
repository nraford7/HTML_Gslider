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
