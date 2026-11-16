import os

# A stand-in protected id so the guard tests never depend on the user's config.
os.environ.setdefault("GSLIDER_PROTECTED_IDS", "protected_test_deck_0000000000")

import pytest


@pytest.fixture(scope="session")
def fonts_ready():
    """Make sure both TTFs are on disk; skip font tests if Playfair cannot be fetched."""
    from gslider.fonts import font_path

    try:
        font_path("Playfair Display")
        font_path("Libre Franklin")
    except Exception as exc:  # offline or missing local font
        pytest.skip(f"fonts unavailable: {exc}")
