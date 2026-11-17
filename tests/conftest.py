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


def pytest_configure(config):
    config.addinivalue_line("markers", "network: needs internet access (skipped offline)")


@pytest.fixture
def font_cache(tmp_path, monkeypatch):
    """Point the font cache (TTFs and fonts.json) at a temp dir."""
    import gslider.fonts as fonts

    monkeypatch.setattr(fonts, "CACHE", tmp_path / "cache")
    return tmp_path / "cache"


@pytest.fixture
def online():
    """Skip a network test when the Google Fonts CSS API cannot be reached."""
    import urllib.request

    try:
        urllib.request.urlopen("https://fonts.googleapis.com/", timeout=5)
    except Exception as exc:
        if not hasattr(exc, "code"):  # an HTTP error still means we are online
            pytest.skip(f"offline: {exc}")
