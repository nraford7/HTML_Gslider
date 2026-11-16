"""Load layouts.yaml into a Layout object."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

LAYOUTS_FILE = Path(__file__).with_name("layouts.yaml")


@dataclass(frozen=True)
class Layout:
    page: dict
    chrome: dict
    title: dict
    lead_gap: float
    grid: dict  # n cells -> {pitch, w}
    cells: dict
    rows: dict
    band: dict
    block_gap: float
    floor: float
    styles: dict  # styles[role][ground] -> {font, size, color, weight, line_spacing, space_after}
    drop_selectors: list
    drop_glyphs: list
    inset: float
    margin: float
    grounds: dict
    rule: dict
    cover: dict = field(default_factory=dict)
    closing: dict = field(default_factory=dict)
    background: dict = field(default_factory=dict)
    side_image: dict = field(default_factory=dict)
    table: dict = field(default_factory=dict)
    rows_compact: dict = field(default_factory=dict)
    row_pad: float = 20
    section_pos: tuple = (48, 29, 700)  # section label text x, y, width: level with the logo
    bullet_indent: float = 12
    min_col: float = 30  # narrowest table text column, pt
    min_image_h: float = 40  # a full-width image needs this much room above the floor, pt
    band_y: float = 438  # a band's rule sits here unless the content above pushes it lower


def load_layout(path: Path = LAYOUTS_FILE) -> Layout:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    styles = {}
    for role, (font, size, ivory, dark, spacing, after) in raw["styles"].items():
        styles[role] = {
            ground: {
                "font": font,
                "size": size,
                "color": colour,
                "weight": 400,
                "line_spacing": spacing,
                "space_after": after,
            }
            for ground, colour in (("ivory", ivory), ("dark", dark))
        }
    return Layout(
        page=raw["page"],
        chrome=raw["chrome"],
        title=raw["title"],
        lead_gap=raw["lead_gap"],
        grid={int(k): v for k, v in raw["grid"].items()},
        cells=raw["cells"],
        rows=raw["rows"],
        band=raw["band"],
        block_gap=raw["block_gap"],
        floor=raw["floor"],
        styles=styles,
        drop_selectors=raw["drop_selectors"],
        drop_glyphs=raw["drop_glyphs"],
        inset=raw["inset"],
        margin=raw["margin"],
        grounds=raw["grounds"],
        rule=raw["rule"],
        cover=raw.get("cover", {}),
        closing=raw.get("closing", {}),
        background=raw.get("background", {}),
        side_image=raw.get("side_image", {}),
        table=raw.get("table", {}),
        rows_compact=raw.get("rows_compact", {}),
        row_pad=raw.get("row_pad", 20),
        section_pos=tuple(raw.get("section_pos", (48, 29, 700))),
        bullet_indent=raw.get("bullet_indent", 12),
        min_col=raw.get("min_col", 30),
        min_image_h=raw.get("min_image_h", 40),
        band_y=raw.get("band_y", 438),
    )
