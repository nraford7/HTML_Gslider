import pytest

from gslider.fonts import measure
from gslider.layout import Layout, load_layout


@pytest.fixture(scope="module")
def layout() -> Layout:
    return load_layout()


def _lead_top(layout, title: str) -> float:
    st = layout.styles["title"]["dark"]
    _, h = measure(title, st["font"], st["size"], layout.title["w"], st["line_spacing"])
    return layout.title["y"] + h + layout.lead_gap


@pytest.mark.usefixtures("fonts_ready")
def test_title_lead_rule(layout):
    one = "Three stages, one decision"
    two = (
        "Investment: $50,000 per roundtable; other fees agreed after Stage 1, "
        "with the rest set in December."
    )
    st = layout.styles["title"]["dark"]
    assert measure(one, st["font"], st["size"], layout.title["w"])[0] == 1
    assert measure(two, st["font"], st["size"], layout.title["w"])[0] == 2
    assert _lead_top(layout, one) == pytest.approx(115, abs=1)
    assert _lead_top(layout, two) == pytest.approx(151, abs=1)


def test_layout_values_match_design_system(layout):
    assert layout.page == {"w": 960, "h": 540, "emu_per_pt": 12700}
    assert layout.chrome["logo"] == {"x": 841, "y": 22, "w": 91, "h": 26}
    assert layout.chrome["pageno"] == {"x": 885, "y": 513}
    assert layout.title == {"x": 48, "y": 65, "w": 864}
    assert layout.lead_gap == 12
    assert layout.grid == {
        1: {"pitch": 864, "w": 864},
        2: {"pitch": 463, "w": 401},
        3: {"pitch": 292, "w": 270},
        4: {"pitch": 220, "w": 200},
    }
    assert layout.cells == {"number_to_rule": 21, "rule_to_header": 9, "header_to_body": 32}
    assert layout.rows == {"pitch_min": 66, "num_x": 48, "label_x": 97, "text_x": 370}
    assert layout.band["label_x"] == 48 and layout.band["text_x"] == 240
    assert layout.block_gap == 24
    assert layout.floor == 495
    assert layout.inset == 7.2
    assert layout.grounds == {"dark": "#080806", "ivory": "#F4F0E7"}
    assert layout.rule == {"weight": 0.75, "color": "#C9A96E", "alpha": 0.6}


EXPECTED_STYLES = {
    # role: (font, size, ivory colour, dark colour, line spacing, space after)
    "title": ("Playfair Display", 28, "#1A1917", "#F2EFE8", 100, 0),
    "lead": ("Libre Franklin", 15, "#3A3730", "#D9D5CC", 115, 0),
    "number": ("Libre Franklin", 12, "#C8A350", "#C8A350", 100, 0),
    "band_label": ("Libre Franklin", 12, "#C8A350", "#C8A350", 100, 0),
    "header": ("Playfair Display", 18, "#9A7A42", "#D4B87A", 100, 0),
    "body": ("Libre Franklin", 14, "#3A3730", "#C8C2B6", 115, 4),
    "value": ("Libre Franklin", 19, "#D4B87A", "#D4B87A", 115, 0),
    "pageno": ("Libre Franklin", 8, "#6D706B", "#6D706B", 100, 0),
}


@pytest.mark.parametrize("role", sorted(EXPECTED_STYLES))
def test_styles_per_role_and_ground(layout, role):
    font, size, ivory, dark, spacing, after = EXPECTED_STYLES[role]
    for ground, colour in (("ivory", ivory), ("dark", dark)):
        st = layout.styles[role][ground]
        assert st == {
            "font": font,
            "size": size,
            "color": colour,
            "weight": 400,
            "line_spacing": spacing,
            "space_after": after,
        }


def test_drop_list(layout):
    assert layout.drop_selectors == [
        ".brand",
        ".footer .sub",
        ".wm-top",
        ".pageno",
        ".lp-arrow",
        ".header-bar .wm-top",
    ]
    assert layout.drop_glyphs == ["←", "→"]


def test_layout_holds_former_constants(layout):
    """Row 9: compile.py's magic numbers now live in layouts.yaml with the same values."""
    assert tuple(layout.section_pos) == (48, 29, 700)
    assert layout.bullet_indent == 12
    assert layout.min_col == 30
    assert layout.min_image_h == 40
    assert layout.band_y == 438
    bare = Layout(**{f: getattr(layout, f) for f in (
        "page", "chrome", "title", "lead_gap", "grid", "cells", "rows", "band", "block_gap", "floor",
        "styles", "drop_selectors", "drop_glyphs", "inset", "margin", "grounds", "rule")})
    assert (tuple(bare.section_pos), bare.bullet_indent, bare.min_col, bare.min_image_h, bare.band_y) == \
        ((48, 29, 700), 12, 30, 40, 438)
