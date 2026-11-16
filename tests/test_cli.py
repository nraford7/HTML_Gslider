import os
import subprocess
from pathlib import Path

from gslider import cli

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "scripts" / "install.sh"


# --- row 15: title without a full parse -----------------------------------------

def test_html_title(tmp_path):
    p = tmp_path / "d.html"
    p.write_text("<html><head><title>A &amp; B</title></head><body><div class='slide'>x</div></body></html>")
    assert cli._html_title(p) == "A & B"
    p.write_text("<html><head></head><body></body></html>")
    assert cli._html_title(p) is None
    p.write_text("<html><head><title>  </title></head></html>")
    assert cli._html_title(p) is None


def test_cli_title_falls_back_to_file_name(monkeypatch, tmp_path):
    from gslider import build
    seen = []
    monkeypatch.setattr(build, "build", lambda h, title, **k: seen.append(title)
                        or build.BuildResult("p", "u", [], tmp_path))
    p = tmp_path / "My Deck.html"
    p.write_text("<html><body></body></html>")
    assert cli.main(["build", str(p)]) == 0
    assert seen == ["My Deck"]


# --- row 7: install.sh on an externally managed Python ------------------------------

FAKE_PY = """#!/bin/bash
echo "$*" >> "$PIP_LOG"
case "$*" in
  *--break-system-packages*) [ "$RETRY_FAILS" = 1 ] && { echo "error: retry broke" >&2; exit 1; }; exit 0 ;;
esac
echo "$FIRST_ERROR" >&2
exit 1
"""


def _install(tmp_path, first_error, retry_fails=False):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "python3").write_text(FAKE_PY)
    (bin_ / "python3").chmod(0o755)
    skills = tmp_path / "skills"
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(tmp_path / "home"), "CLAUDE_SKILLS_DIR": str(skills),
           "PIP_LOG": str(tmp_path / "pip.log"), "FIRST_ERROR": first_error,
           "RETRY_FAILS": "1" if retry_fails else "0"}
    r = subprocess.run(["bash", str(INSTALL)], env=env, capture_output=True, text=True, timeout=60)
    log = (tmp_path / "pip.log").read_text().splitlines()
    return r, log, skills / "gslider"


def test_install_retries_externally_managed(tmp_path):
    r, log, link = _install(tmp_path, "error: externally-managed-environment")
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(log) == 2 and "--break-system-packages" not in log[0] and "--break-system-packages" in log[1]
    assert link.is_symlink() and os.readlink(link) == str(REPO / "skills" / "gslider")


def test_install_other_pip_error_links_skill_then_fails(tmp_path):
    r, log, link = _install(tmp_path, "error: no network")
    assert r.returncode != 0
    assert len(log) == 1  # no --break-system-packages retry for other errors
    assert "no network" in r.stderr
    assert link.is_symlink()  # the skill link step still ran
    assert "dependencies" in (r.stdout + r.stderr).lower()


def test_install_retry_failure_reported(tmp_path):
    r, log, link = _install(tmp_path, "error: externally-managed-environment", retry_fails=True)
    assert r.returncode != 0 and len(log) == 2 and "retry broke" in r.stderr
    assert link.is_symlink()


def test_html_title_ignores_svg_title(tmp_path):
    p = tmp_path / "d.html"
    p.write_text("<html><head></head><body><svg><title>Icon</title></svg></body></html>")
    assert cli._html_title(p) is None
    p.write_text("<html><body><svg><title>Icon</title></svg></body><head><title>Deck</title></head></html>")
    assert cli._html_title(p) == "Deck"
    p.write_text("<html><head><title>Deck</title></head><body><svg><title>Icon</title></svg></body></html>")
    assert cli._html_title(p) == "Deck"
