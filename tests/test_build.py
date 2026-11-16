import json
import os
from pathlib import Path

import pytest

from gslider import build, cli, gws

FIX = Path(__file__).parent / "fixtures"
PAGE = {"width": {"magnitude": 12192000, "unit": "EMU"}, "height": {"magnitude": 6858000, "unit": "EMU"}}


def _plan(n, status="native", notes=None, missing=()):
    sid = f"s{n:02d}_slide"
    return {"n": n, "slide_id": sid, "html_id": f"h{n}", "ground": "dark", "notes_text": notes,
            "status": status, "reason": None if status == "native" else "overflow",
            "requests": [{"createSlide": {"objectId": sid}}], "boxes": [],
            "missing_assets": list(missing)}


class FakeGoogle:
    """Records every gws call build.py makes; `writes` lists the mutating ones in order."""

    def __init__(self, page=PAGE, fail_on=None):
        self.page = page
        self.fail_on = fail_on  # slide objectId whose batch raises
        self.writes: list[tuple] = []
        self.props = {"good": {"gslider": "1"}, "plain": {}, "commented": {"gslider": "1"}}
        self.comments = {"good": 0, "plain": 0, "commented": 2}
        self.slides = {"good": ["s01_slide", "s02_slide"], "copy1": ["p1"]}
        self.notes_inserts: list[tuple] = []
        self.reads: list[tuple] = []
        self.missing: set[str] = set()  # ids that 404
        self.copy_error: Exception | None = None
        self.notes_fail = False

    def drive_get(self, file_id, fields):
        self.reads.append(("drive_get", file_id))
        if file_id in self.missing:
            raise gws.GwsError(404, f"File not found: {file_id}.")
        return {"id": file_id, "appProperties": self.props.get(file_id, {}),
                "mimeType": gws.PRESENTATION_MIME}

    def comments_count(self, file_id):
        self.reads.append(("comments", file_id))
        c = self.comments.get(file_id, 0)
        return c.pop(0) if isinstance(c, list) else c

    def drive_copy(self, file_id, title, app_properties):
        if self.copy_error:
            raise self.copy_error
        self.writes.append(("copy", file_id, title, app_properties))
        self.props["copy1"] = dict(app_properties)
        return "copy1"

    def get(self, pid, fields=None):
        self.reads.append(("get", pid))
        if pid in self.missing:
            raise gws.GwsError(404, f"Requested entity was not found: {pid}")
        slides = [{"objectId": s, "slideProperties": {"notesPage": {"notesProperties": {
            "speakerNotesObjectId": f"{s}_notes"}}}} for s in self.slides.get(pid, [])]
        return {"presentationId": pid, "pageSize": self.page, "slides": slides}

    def batch(self, pid, requests):
        self.writes.append(("batch", pid, requests))
        for r in requests:
            if "createSlide" in r:
                oid = r["createSlide"]["objectId"]
                if oid == self.fail_on:
                    raise gws.GwsError(400, f"Invalid requests[3]: bad thing on {oid}")
                self.slides.setdefault(pid, []).append(oid)
            if "deleteObject" in r:
                self.slides[pid] = [s for s in self.slides.get(pid, []) if s != r["deleteObject"]["objectId"]]
            if "insertText" in r and r["insertText"]["objectId"].endswith("_notes"):
                if self.notes_fail:
                    raise gws.GwsError(400, "notes shape is read-only")
                self.notes_inserts.append((r["insertText"]["objectId"], r["insertText"]["text"]))
        return {"replies": []}


@pytest.fixture
def google(monkeypatch, tmp_path):
    fake = FakeGoogle()
    for name in ("drive_get", "comments_count", "drive_copy", "get", "batch"):
        monkeypatch.setattr(gws, name, getattr(fake, name))

    def fake_revoke(ids, perms=None):  # no build test may reach Drive's permissions
        fake.writes.append(("revoke", list(ids), dict(perms or {})))
        return list(ids), []

    monkeypatch.setattr(build.assets, "revoke", fake_revoke)
    monkeypatch.setattr(build.assets, "SHARE_LOG", [])
    monkeypatch.setattr(build, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps({"base_id": "base1"}))
    monkeypatch.setattr(build, "OUT", tmp_path / "out")
    return fake


@pytest.fixture
def fake_compile(monkeypatch):
    """Ten fixed plans (no fonts, no assets) so build tests stay offline and fast."""
    state = {"plans": [_plan(n) for n in range(1, 11)]}
    monkeypatch.setattr(build, "parse", lambda html, include_internal=False, source=None: {
        "title": "t", "source": source,
        "slides": [{"n": n, "id": f"h{n}", "ground": "dark", "kind": "content", "blocks": [],
                    "raw_text": []} for n in range(1, 11)]})
    monkeypatch.setattr(build, "compile_deck", lambda deck, layout, assets, ivory, logo=True: state["plans"])
    monkeypatch.setattr(build, "load_layout", lambda: None)
    # html style (the default): no Chrome; the same fixed plans
    monkeypatch.setattr(build, "_measure_all", lambda html_path, wraps, workdir, workers=3: {})
    monkeypatch.setattr(build, "compile_html_deck", lambda deck, measures, assets: state["plans"])
    return state


@pytest.fixture
def html(tmp_path):
    p = tmp_path / "My Deck.html"
    p.write_text("<html></html>")
    return p


# --- Review Focus 4 ------------------------------------------------------------

@pytest.mark.parametrize("pid, why", [("plain", "not created by gslider"), ("commented", "2 comment")])
def test_into_refuses_unmarked_or_commented(google, fake_compile, html, pid, why):
    with pytest.raises(build.BuildError, match=why):
        build.build(html, "T", into=pid)
    assert google.writes == []  # refused before any write


def test_into_refuses_live_deck_and_base(google, fake_compile, html):
    with pytest.raises(build.BuildError, match="protected"):
        build.build(html, "T", into=build.PROTECTED_IDS[0])
    assert google.reads == []  # refused before any Drive call, read or write
    with pytest.raises(build.BuildError, match="base"):
        build.build(html, "T", into="base1")
    assert google.writes == []


def test_into_clears_existing_slides_and_rebuilds_same_id(google, fake_compile, html):
    res = build.build(html, "T", into="good")
    assert res.pid == "good"
    assert not any(w[0] == "copy" for w in google.writes)
    assert google.slides["good"] == [f"s{n:02d}_slide" for n in range(1, 11)]  # old ones gone, no append


# --- copy, marker, page size ---------------------------------------------------

def test_build_marks_copy_and_checks_page_size(google, fake_compile, html):
    res = build.build(html, "My title")
    copy = google.writes[0]
    assert copy == ("copy", "base1", "My title", {"gslider": "1"})
    assert res.pid == "copy1" and res.url.endswith("/copy1/edit")
    assert google.slides["copy1"] == [f"s{n:02d}_slide" for n in range(1, 11)]
    batches = [w for w in google.writes if w[0] == "batch"]
    # clear batch, ten slide batches in order, final placeholder delete
    created = [r["createSlide"]["objectId"] for w in batches for r in w[2] if "createSlide" in r]
    assert created[1:] == [f"s{n:02d}_slide" for n in range(1, 11)]
    assert (res.out_dir / "deck.json").exists() and (res.out_dir / "plans.json").exists()
    assert json.loads((res.out_dir / "build.json").read_text())["pid"] == "copy1"


def test_build_refuses_wrong_page_size(google, fake_compile, html):
    google.page = {"width": {"magnitude": 9144000, "unit": "EMU"}, "height": {"magnitude": 5143500, "unit": "EMU"}}
    with pytest.raises(build.BuildError, match="pageSize"):
        build.build(html, "T")
    assert google.writes == []  # the base is checked before the copy (review S2)


# --- Review Focus 5 ------------------------------------------------------------

def test_batch_error_names_slide(google, fake_compile, html):
    google.fail_on = "s09_slide"
    with pytest.raises(build.BuildError) as e:
        build.build(html, "T")
    msg = str(e.value)
    assert "slide 9" in msg and "https://docs.google.com/presentation/d/copy1/edit" in msg
    assert google.slides["copy1"][-8:] == [f"s{n:02d}_slide" for n in range(1, 9)]  # earlier slides stay


def test_missing_assets_refuse_before_copy(google, fake_compile, html):
    fake_compile["plans"][2] = _plan(3, missing=["abc"])
    with pytest.raises(build.BuildError, match="slide 3"):
        build.build(html, "T")
    assert google.writes == []


def test_fallback_notes_inserted_after_slide(google, fake_compile, html):
    fake_compile["plans"][1] = _plan(2, status="fallback", notes="line one\nline two")
    build.build(html, "T")
    assert google.notes_inserts == [("s02_slide_notes", "line one\nline two")]


# --- asset flow with the real parser and compiler -----------------------------

@pytest.mark.usefixtures("fonts_ready")
def _brand_logo(monkeypatch, both=False):
    """Configure a brand logo in the (temporary) gslider config."""
    d = build.CONFIG_PATH.parent
    d.mkdir(parents=True, exist_ok=True)
    (d / "logo.png").write_bytes(b"\x89PNG light")
    brand = {"logo": str(d / "logo.png")}
    if both:
        (d / "logo-dark.png").write_bytes(b"\x89PNG dark")
        brand["logo_dark"] = str(d / "logo-dark.png")
    build.save_config(brand=brand)


def test_no_brand_means_no_logo(google, monkeypatch):
    monkeypatch.setattr(build.assets, "CHROME", "/fake/chrome")
    monkeypatch.setattr(build.assets, "render_slide_png", lambda h, n, out: Path(out))
    published = []
    monkeypatch.setattr(build.assets, "publish_image", lambda d, m, n: published.append(n) or "u")
    assert build.logo_files() == {}
    res = build.build(FIX / "cols3.html", "T", style="template")
    reqs = [r for p in res.plans for r in p["requests"]]
    assert not any("logo" in r.get("createImage", {}).get("objectId", "") for r in reqs)
    assert not any("logo" in n for n in published)


def test_one_logo_serves_both_grounds(google, monkeypatch):
    _brand_logo(monkeypatch)
    files = build.logo_files()
    assert files["logo"] == files["logo_dark"]
    _brand_logo(monkeypatch, both=True)
    files = build.logo_files()
    assert files["logo"].name == "logo.png" and files["logo_dark"].name == "logo-dark.png"


def test_two_pass_assets_publish_logo_and_fallback(google, monkeypatch):
    published, rendered = [], []

    def fake_publish(data, mime, name):
        published.append(name)
        return f"https://drive.google.com/uc?export=download&id={len(published)}"

    def fake_render(html_path, n, out):
        rendered.append(n)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"\x89PNG fake")
        return Path(out)

    _brand_logo(monkeypatch)
    monkeypatch.setattr(build.assets, "CHROME", "/fake/chrome")
    monkeypatch.setattr(build.assets, "publish_image", fake_publish)
    monkeypatch.setattr(build.assets, "render_slide_png", fake_render)
    res = build.build(FIX / "overflow.html", "T", style="template")
    assert [p["status"] for p in res.plans] == ["native", "fallback"]
    assert rendered == [2]  # .slide-wrap index of the fallback slide
    assert any("logo" in n for n in published) and any("fallback" in n for n in published)
    assert all(not p["missing_assets"] for p in res.plans)


def test_wrap_index_skips_internal_slides():
    html = ('<div class="slide-wrap" data-slide-id="a"></div>'
            '<div class="slide-wrap" data-slide-id="version-note"></div>'
            '<div class="slide-wrap" data-slide-id="b"></div>')
    assert build.wrap_indexes(html, include_internal=False) == {1: 1, 2: 3}
    assert build.wrap_indexes(html, include_internal=True) == {1: 1, 2: 2, 3: 3}


# --- init-base -------------------------------------------------------------------

def test_init_base_imports_pptx_and_probes_blank(google, monkeypatch, tmp_path):
    imported = []

    def fake_import(path, name):
        from pptx import Presentation
        prs = Presentation(str(path))
        imported.append((name, prs.slide_width, prs.slide_height, len(prs.slides)))
        return "base9"

    monkeypatch.setattr(gws, "drive_import_pptx", fake_import)
    (tmp_path / "config.json").unlink()
    pid = build.init_base()
    assert pid == "base9"
    assert imported == [(build.BASE_NAME, 12192000, 6858000, 1)]
    probe = [w for w in google.writes if w[0] == "batch"][0][2]
    assert probe[0]["createSlide"]["slideLayoutReference"] == {"predefinedLayout": "BLANK"}
    assert "deleteObject" in probe[1]
    assert json.loads(build.CONFIG_PATH.read_text())["base_id"] == "base9"


def test_init_base_refuses_when_configured(google):
    with pytest.raises(build.BuildError, match="already"):
        build.init_base()


# --- CLI ---------------------------------------------------------------------------

def test_cli_parse_writes_json(tmp_path, capsys):
    out = tmp_path / "o"
    assert cli.main(["parse", str(FIX / "cols3.html"), "--out", str(out)]) == 0
    deck = json.loads((out / "deck.json").read_text())
    assert deck["slides"] and deck["slides"][0]["blocks"]
    assert str(out / "deck.json") in capsys.readouterr().out


def test_cli_parses_ivory_and_flags(monkeypatch, tmp_path):
    seen = {}

    def fake_build(html_path, title, ivory=frozenset(), include_internal=False, into=None, out_dir=None,
                   style="html"):
        seen.update(title=title, ivory=ivory, inc=include_internal, into=into)
        return build.BuildResult("p", "https://x/p", [], tmp_path)

    monkeypatch.setattr(build, "build", fake_build)
    cli.main(["build", "d.html", "--title", "X", "--ivory", "3, 4,14", "--include-internal", "--into", "abc"])
    assert seen == {"title": "X", "ivory": {3, 4, 14}, "inc": True, "into": "abc"}


def test_cli_help_mentions_ivory_positions(capsys):
    with pytest.raises(SystemExit):
        cli.main(["build", "--help"])
    assert "after internal slides are skipped" in " ".join(capsys.readouterr().out.split())


def test_fallback_render_uses_html_index_when_present(google, fake_compile, monkeypatch, tmp_path):
    """parse may give html_index (slide-wrap position counting internal slides); build must use it."""
    fake_compile["plans"][0] = _plan(1, status="fallback", missing=["fallback:1"])
    real_parse = build.parse

    def parse_with_index(html, include_internal=False, source=None):
        deck = real_parse(html, include_internal, source)
        deck["slides"][0]["html_index"] = 2
        return deck

    monkeypatch.setattr(build, "parse", parse_with_index)
    rendered = []

    def fake_render(html_path, n, out):
        rendered.append(n)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"png")
        return Path(out)

    monkeypatch.setattr(build.assets, "CHROME", "/fake/chrome")
    monkeypatch.setattr(build.assets, "render_slide_png", fake_render)
    monkeypatch.setattr(build.assets, "publish_image", lambda d, m, n: "https://u/1")
    p = tmp_path / "d.html"
    p.write_text("<html></html>")
    with pytest.raises(build.BuildError, match="slide 1"):  # fake compile keeps reporting it missing
        build.build(p, "T")
    assert rendered == [2]


# --- review findings (chunk B+C) ------------------------------------------------------

@pytest.fixture
def publish_log(monkeypatch):
    log = []
    monkeypatch.setattr(build, "publish_assets", lambda *a, **k: log.append(a) or {})
    return log


def test_stale_base_found_before_any_publishing(google, fake_compile, html, monkeypatch):
    """S2: a deleted base is reported before images are rendered or uploaded."""
    fake_compile["plans"][0] = _plan(1, missing=["logo"])
    published = []
    monkeypatch.setattr(build.assets, "publish_image", lambda *a: published.append(a) or "https://u/id=1")
    google.missing.add("base1")
    with pytest.raises(build.BuildError, match="init-base"):
        build.build(html, "T")
    assert published == [] and google.writes == []


def test_drive_copy_404_is_build_error(google, fake_compile, html):
    google.copy_error = gws.GwsError(404, "File not found: base1.")
    with pytest.raises(build.BuildError, match="base deck missing; run init-base"):
        build.build(html, "T")


def test_failed_build_records_state_and_retry_hint(google, fake_compile, html):
    """S1: a mid-push failure leaves build.json status=failed with the last good slide."""
    google.fail_on = "s09_slide"
    with pytest.raises(build.BuildError) as e:
        build.build(html, "T")
    assert "retry with --into copy1" in str(e.value)
    meta = json.loads((build.OUT / "my-deck" / "build.json").read_text())
    assert meta["status"] == "failed"
    assert "slide 9" in meta["error"]
    assert meta["last_good_slide"] == 8
    assert meta["url"].endswith("/copy1/edit")


def test_unexpected_error_becomes_build_error(google, fake_compile, html, monkeypatch):
    fake_compile["plans"][0] = _plan(1, missing=["logo"])

    def boom(*a, **k):
        raise RuntimeError("Chrome render of slide 1 failed")

    monkeypatch.setattr(build, "publish_assets", boom)
    with pytest.raises(build.BuildError, match="Chrome render") as e:
        build.build(html, "T")
    assert "--into" not in str(e.value)  # no copy was made
    meta = json.loads((build.OUT / "my-deck" / "build.json").read_text())
    assert meta["status"] == "failed" and meta["pid"] is None and meta["last_good_slide"] == 0


def test_build_json_written_before_publishing_and_grows(google, fake_compile, html, monkeypatch):
    """S6: build.json exists before the first upload and lists each shared id as it goes."""
    _brand_logo(monkeypatch)
    fake_compile["plans"][0] = _plan(1, missing=["logo", "logo_dark"])
    seen = []

    def fake_publish(data, mime, name):
        meta = json.loads((build.OUT / "my-deck" / "build.json").read_text())
        seen.append((meta["status"], list(meta["shared_assets"])))
        return f"https://drive.google.com/uc?export=download&id=F{len(seen)}"

    monkeypatch.setattr(build.assets, "publish_image", fake_publish)
    with pytest.raises(build.BuildError):  # fake compile keeps reporting assets missing
        build.build(html, "T")
    assert seen == [("building", []), ("building", ["F1"])]
    meta = json.loads((build.OUT / "my-deck" / "build.json").read_text())
    assert meta["shared_assets"] == ["F1", "F2"]


def test_out_dir_lock_refuses_parallel_build(google, fake_compile, html):
    """S3: a second build into the same out/<slug> fails fast and names --out."""
    out = build.OUT / "my-deck"
    out.mkdir(parents=True)
    (out / build.LOCK_NAME).write_text(str(os.getpid()))
    with pytest.raises(build.BuildError, match="--out"):
        build.build(html, "T")
    assert google.writes == []


def test_stale_lock_is_taken_over_and_released(google, fake_compile, html):
    out = build.OUT / "my-deck"
    out.mkdir(parents=True)
    (out / build.LOCK_NAME).write_text("999999999")  # no such process
    build.build(html, "T")
    assert not (out / build.LOCK_NAME).exists()


def test_into_rechecks_comments_before_clear(google, fake_compile, html):
    """S8: a comment added while assets were publishing stops the clear."""
    google.comments["good"] = [0, 1]
    with pytest.raises(build.BuildError, match="comment"):
        build.build(html, "T", into="good")
    assert google.writes == []
    assert google.slides["good"] == ["s01_slide", "s02_slide"]


def test_protected_ids_live_in_gws():
    assert build.PROTECTED_IDS is gws.PROTECTED_IDS


def test_first_slide_failure_message(google, fake_compile, html):
    google.fail_on = "s01_slide"
    with pytest.raises(build.BuildError) as e:
        build.build(html, "T")
    assert "1-0" not in str(e.value) and "no slides" in str(e.value).lower()


def test_notes_failure_message(google, fake_compile, html):
    """Row 14: notes come after every slide, so a notes failure leaves all slides in the deck."""
    fake_compile["plans"][1] = _plan(2, status="fallback", notes="text")
    google.notes_fail = True
    with pytest.raises(build.BuildError) as e:
        build.build(html, "T")
    msg = str(e.value)
    assert "all 10 slides are in the deck" in msg
    assert "speaker notes failed for slides 2" in msg and "--into copy1" in msg


def test_ivory_out_of_range_warns(google, fake_compile, html, capsys):
    build.build(html, "T", ivory={3, 11, 0}, style="template")
    err = capsys.readouterr().err
    assert "--ivory" in err and "0, 11" in err


def test_cli_one_line_errors(monkeypatch, capsys, tmp_path):
    """S1: Google, file and render errors exit 1 with one line, no traceback."""
    for exc in (gws.GwsError(500, "backend"), OSError("disk full"), RuntimeError("chrome"), ValueError("bad")):
        def fake_build(*a, exc=exc, **k):
            raise exc
        monkeypatch.setattr(build, "build", fake_build)
        assert cli.main(["build", str(FIX / "cols3.html"), "--title", "X"]) == 1
        err = capsys.readouterr().err
        assert err.startswith("gslider: ") and err.count("\n") == 1, err
    assert cli.main(["build", str(tmp_path / "nope.html")]) == 1
    assert "nope.html" in capsys.readouterr().err


def test_cli_check_refuses_out_for_other_pid(tmp_path, capsys):
    """S11."""
    (tmp_path / "build.json").write_text(json.dumps({"pid": "AAA", "status": "done"}))
    assert cli.main(["check", "BBB", "--out", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "AAA" in err and "BBB" in err


def test_slugify_strips_accents_and_never_empty():
    assert build.slugify("Présentation Été") == "presentation-ete"
    s = build.slugify("提案")
    assert len(s) == 8 and s == build.slugify("提案") and s != build.slugify("別案")


# --- fresheyes rows 3, 8, 14, 15 ------------------------------------------------------

def test_empty_lock_is_busy(google, fake_compile, html):
    """Row 3: an empty lock (a writer caught between create and write) is busy, never stale."""
    out = build.OUT / "my-deck"
    out.mkdir(parents=True)
    (out / build.LOCK_NAME).write_text("")
    with pytest.raises(build.BuildError) as e:
        build.build(html, "T")
    msg = str(e.value)
    assert str(out / build.LOCK_NAME) in msg and "delete" in msg and "by hand" in msg
    assert (out / build.LOCK_NAME).exists() and google.writes == []


def test_live_lock_still_busy(google, fake_compile, html):
    out = build.OUT / "my-deck"
    out.mkdir(parents=True)
    (out / build.LOCK_NAME).write_text(str(os.getppid()))  # a live process that is not us
    with pytest.raises(build.BuildError, match=f"process {os.getppid()}"):
        build.build(html, "T")
    assert (out / build.LOCK_NAME).read_text() == str(os.getppid()) and google.writes == []


def test_lock_written_atomically(google, fake_compile, html, monkeypatch):
    """Row 3: the lock name only ever appears with the PID already in it (temp file + os.link)."""
    seen = []
    real_link = os.link

    def spy_link(src, dst, *a, **k):
        seen.append((Path(src).read_text(), Path(dst).name))
        return real_link(src, dst, *a, **k)

    monkeypatch.setattr(build.os, "link", spy_link)
    build.build(html, "T")
    assert seen == [(str(os.getpid()), build.LOCK_NAME)]
    out = build.OUT / "my-deck"
    assert sorted(p.name for p in out.iterdir()) == ["build.json", "deck.json", "plans.json"]


def test_chrome_env_override(monkeypatch, tmp_path):
    chrome = tmp_path / "my-chrome"
    chrome.write_text("")
    monkeypatch.setenv("GSLIDER_CHROME", str(chrome))
    assert build.assets.find_chrome() == str(chrome)


def test_chrome_found_on_path(monkeypatch, tmp_path):
    monkeypatch.delenv("GSLIDER_CHROME", raising=False)
    monkeypatch.setattr(build.assets, "MAC_CHROME", str(tmp_path / "absent"))
    (tmp_path / "chromium").write_text("#!/bin/sh\n")
    (tmp_path / "chromium").chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert build.assets.find_chrome() == str(tmp_path / "chromium")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert build.assets.find_chrome() is None


def test_chrome_env_missing_path_refused(google, fake_compile, html, monkeypatch, tmp_path):
    """Review Focus 4: a GSLIDER_CHROME that does not exist is refused, not replaced by the default."""
    monkeypatch.setenv("GSLIDER_CHROME", str(tmp_path / "no-such-chrome"))
    assert build.assets.find_chrome() is None
    monkeypatch.setattr(build.assets, "CHROME", build.assets.find_chrome())
    fake_compile["plans"][0] = _plan(1, status="fallback", missing=["logo", "fallback:1"])
    published = []
    monkeypatch.setattr(build.assets, "publish_image", lambda *a: published.append(a) or "https://u/id=1")
    with pytest.raises(build.BuildError, match="GSLIDER_CHROME") as e:
        build.build(html, "T", style="template")
    assert "no-such-chrome" in str(e.value)
    assert published == [] and google.writes == []


def test_build_refuses_fallback_without_chrome_before_upload(google, fake_compile, html, monkeypatch):
    monkeypatch.delenv("GSLIDER_CHROME", raising=False)
    monkeypatch.setattr(build.assets, "CHROME", None)
    fake_compile["plans"][4] = _plan(5, status="fallback", missing=["logo", "fallback:5"])
    calls = []
    monkeypatch.setattr(build, "publish_assets", lambda *a, **k: calls.append(a) or {})
    with pytest.raises(build.BuildError, match="GSLIDER_CHROME"):
        build.build(html, "T", style="template")
    assert calls == [] and google.writes == []


def test_no_chrome_needed_without_fallbacks(google, fake_compile, html, monkeypatch):
    monkeypatch.setattr(build.assets, "CHROME", None)
    build.build(html, "T")  # no fallback slide: no render, no refusal


def _gets(google, pid="copy1"):
    return [r for r in google.reads if r == ("get", pid)]


def test_notes_fetched_once(google, fake_compile, html):
    """Row 14: one get and one notes batch for all fallback slides, after the placeholder delete."""
    for n in (2, 5, 9):
        fake_compile["plans"][n - 1] = _plan(n, status="fallback", notes=f"notes {n}")
    gets_before = len(_gets(google))
    build.build(html, "T")
    # build reads pageSize once and the slide list once (clear) before pushing; notes add one more
    assert len(_gets(google)) - gets_before == 3
    assert google.notes_inserts == [(f"s{n:02d}_slide_notes", f"notes {n}") for n in (2, 5, 9)]
    batches = [w[2] for w in google.writes if w[0] == "batch"]
    notes_batches = [b for b in batches if any("insertText" in r for r in b)]
    assert len(notes_batches) == 1 and notes_batches[0] is batches[-1]
    assert batches[-2] == [{"deleteObject": {"objectId": build.PLACEHOLDER}}]


def test_no_fallbacks_no_notes_fetch(google, fake_compile, html):
    """Review Focus 5: no fallback slide, no notes get."""
    build.build(html, "T")
    assert len(_gets(google)) == 2  # pageSize check and clear only
    assert google.notes_inserts == []


def test_notes_failure_semantics(google, fake_compile, html):
    for n in (3, 7):
        fake_compile["plans"][n - 1] = _plan(n, status="fallback", notes="text")
    google.notes_fail = True
    with pytest.raises(build.BuildError) as e:
        build.build(html, "T")
    msg = str(e.value)
    assert "all 10 slides are in the deck" in msg and "slides 3, 7" in msg
    assert "rerun with --into copy1 (rebuilds the whole deck)" in msg
    meta = json.loads((build.OUT / "my-deck" / "build.json").read_text())
    assert meta["status"] == "failed" and meta["last_good_slide"] == 10
    assert google.slides["copy1"] == [f"s{n:02d}_slide" for n in range(1, 11)]  # placeholder gone too


def test_title_without_full_parse(google, fake_compile, monkeypatch, tmp_path):
    """Row 15: build without --title parses the deck once (in build), not also in the CLI."""
    calls = []
    fake_parse = build.parse

    def counting(html, include_internal=False, source=None):
        calls.append(source)
        return fake_parse(html, include_internal, source)

    monkeypatch.setattr(build, "parse", counting)
    monkeypatch.setattr(cli, "parse", counting)
    p = tmp_path / "d.html"
    p.write_text("<html><head><title> Client &amp; Co\n Proposal </title></head><body></body></html>")
    seen = []
    real_build = build.build
    monkeypatch.setattr(build, "build", lambda h, title, **k: seen.append(title) or real_build(h, title, **k))
    assert cli.main(["build", str(p)]) == 0
    assert len(calls) == 1
    assert seen == ["Client & Co Proposal"]


# --- code review of the fresheyes fixes ------------------------------------------------

def test_lock_without_hard_links_is_build_error(google, fake_compile, html, monkeypatch):
    def no_link(src, dst, *a, **k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(build.os, "link", no_link)
    with pytest.raises(build.BuildError, match="cannot create .*Operation not permitted.*--out"):
        build.build(html, "T")
    assert google.writes == []
    assert not (build.OUT / "my-deck" / build.LOCK_NAME).exists()


def test_lock_holder_non_ascii_digit_is_busy(tmp_path):
    lock = tmp_path / "l"
    lock.write_text("²")
    assert build._lock_holder(lock) == 0


def test_stale_lock_replaced_meanwhile_is_not_unlinked(google, fake_compile, html, monkeypatch):
    """C1: a dead holder's lock is unlinked only if it still names that holder."""
    out = build.OUT / "my-deck"
    out.mkdir(parents=True)
    lock = out / build.LOCK_NAME
    lock.write_text("999999999")
    real = build._lock_holder
    calls = []

    def racing_holder(path):
        calls.append(1)
        if len(calls) == 2:  # another build took the stale lock over between our reads
            path.write_text(str(os.getppid()))
        return real(path)

    monkeypatch.setattr(build, "_lock_holder", racing_holder)
    with pytest.raises(build.BuildError, match=f"process {os.getppid()}"):
        build.build(html, "T")
    assert lock.read_text() == str(os.getppid()) and google.writes == []


def test_release_leaves_a_lock_that_is_not_ours(google, fake_compile, html, monkeypatch):
    """C1: on release the lock is removed only if it still holds our PID."""
    out = build.OUT / "my-deck"
    real_build = build._build

    def steal(*a, **k):
        (out / build.LOCK_NAME).unlink()
        (out / build.LOCK_NAME).write_text(str(os.getppid()))
        return real_build(*a, **k)

    monkeypatch.setattr(build, "_build", steal)
    build.build(html, "T")
    assert (out / build.LOCK_NAME).read_text() == str(os.getppid())


@pytest.mark.parametrize("name", ["google-chrome-stable", "chromium-browser"])
def test_chrome_distro_names_found_on_path(monkeypatch, tmp_path, name):
    monkeypatch.delenv("GSLIDER_CHROME", raising=False)
    monkeypatch.setattr(build.assets, "MAC_CHROME", str(tmp_path / "absent"))
    (tmp_path / name).write_text("#!/bin/sh\n")
    (tmp_path / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert build.assets.find_chrome() == str(tmp_path / name)


def test_push_retries_image_fetch_failure(monkeypatch):
    """Slides' image fetcher fails intermittently on a Drive link it just used (live, 2026-11-16).
    The batch is atomic, so the slide is retried after a pause."""
    calls = {"n": 0}
    monkeypatch.setattr(build.time, "sleep", lambda s: None)

    def flaky(pid, reqs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise build.gws.GwsError(400, "Invalid requests[2].createImage: There was a problem "
                                          "retrieving the image. The provided image should be publicly accessible")

    monkeypatch.setattr(build.gws, "batch", flaky)
    plans = [{"n": 1, "slide_id": "s01_slide", "status": "native", "requests": [{"x": 1}]}]
    build.push("pid", plans)
    assert calls["n"] >= 2  # the slide batch was sent twice


FETCH_MSG = ("Invalid requests[2].createImage: There was a problem retrieving the image. The provided "
             "image should be publicly accessible, within size limit, and in supported formats.")


def test_push_image_fetch_retry_gives_up_after_delays(monkeypatch):
    slept = []
    monkeypatch.setattr(build.time, "sleep", slept.append)

    def always(pid, reqs):
        raise build.gws.GwsError(400, FETCH_MSG)  # the exact live message

    monkeypatch.setattr(build.gws, "batch", always)
    with pytest.raises(build.BuildError, match="slide 1"):
        build.push("pid", [{"n": 1, "slide_id": "s01_slide", "status": "native", "requests": [{}]}])
    assert slept == list(build.IMAGE_FETCH_DELAYS)


def test_push_image_fetch_text_without_400_not_retried(monkeypatch):
    monkeypatch.setattr(build.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def other(pid, reqs):
        calls["n"] += 1
        raise build.gws.GwsError(500, FETCH_MSG)

    monkeypatch.setattr(build.gws, "batch", other)
    with pytest.raises(build.BuildError):
        build.push("pid", [{"n": 1, "slide_id": "s01_slide", "status": "native", "requests": [{}]}])
    assert calls["n"] == 1


def test_push_does_not_retry_other_errors(monkeypatch):
    monkeypatch.setattr(build.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def bad(pid, reqs):
        calls["n"] += 1
        raise build.gws.GwsError(400, "Invalid requests[0].createShape: bad objectId")

    monkeypatch.setattr(build.gws, "batch", bad)
    with pytest.raises(build.BuildError, match="slide 1"):
        build.push("pid", [{"n": 1, "slide_id": "s01_slide", "status": "native", "requests": [{}]}])
    assert calls["n"] == 1


# --- html style (Task 6) ---------------------------------------------------------------

def _fake_measure(n, plate):
    return {"n": n, "background": "#f4f0e7", "fonts_status": "loaded", "unloaded": [],
            "blocks": [{"x": 120, "y": 100, "w": 900, "h": 96, "lines": 1, "align": "left",
                        "line_height_px": 96, "letter_spacing_px": 0, "transform": None,
                        "runs": [{"text": "Heading", "family": "Georgia", "generic": "serif", "size_px": 80,
                                  "bold": False, "italic": False, "underline": False, "color": "#1a1917"}]}],
            "skipped": [{"text": "ROTATED LABEL", "reason": "transform rotate"}], "plate": plate}


def test_build_style_html_measures_and_publishes_plates(google, monkeypatch, tmp_path):
    src = tmp_path / "deck.html"
    src.write_text('<div class="slide-wrap" data-slide-id="version-note"><div class="slide">v</div></div>'
                   '<div class="slide-wrap" data-slide-id="a"><div class="slide">'
                   '<h1>Heading</h1><div>ROTATED LABEL</div></div></div>')
    measured, published = [], []

    def fake_measure(html_path, n, workdir):
        measured.append(n)
        plate = Path(workdir) / f"plate_s{n:02d}.png"
        plate.parent.mkdir(parents=True, exist_ok=True)
        plate.write_bytes(b"\x89PNG plate")
        return _fake_measure(n, plate)

    def fake_publish(data, mime, name, kind=None):
        published.append(name)
        assert kind == "plate"
        return f"https://drive.google.com/uc?export=download&id=f{len(published)}"

    monkeypatch.setattr(build.assets, "CHROME", "/fake/chrome")
    monkeypatch.setattr(build.M, "measure_slide", fake_measure)
    monkeypatch.setattr(build.assets, "publish_image", fake_publish)
    monkeypatch.setattr("gslider.htmlstyle.slides_family", lambda fam, gen: (fam, False))
    res = build.build(src, "T")
    assert measured == [2]  # .slide-wrap index (the internal slide is skipped)
    assert published == ["deck slide 01 plate.png"]
    (plan,) = res.plans
    assert plan["status"] == "native" and plan["style"] == "html"
    imgs = [r["createImage"] for r in plan["requests"] if "createImage" in r]
    assert imgs[0]["objectId"] == "s01_plate" and imgs[0]["url"].endswith("id=f1")
    meta = json.loads((res.out_dir / "build.json").read_text())
    assert meta["style"] == "html" and meta["plates"] == ["f1"] and meta["shared_assets"] == ["f1"]
    deck = json.loads((res.out_dir / "deck.json").read_text())
    assert deck["slides"][0]["dropped_text"] == ["ROTATED LABEL"]


def test_build_style_html_measure_failure_falls_back(google, monkeypatch, tmp_path):
    src = tmp_path / "deck.html"
    src.write_text('<div class="slide-wrap"><div class="slide"><h1>Heading</h1></div></div>')
    rendered = []

    def boom(html_path, n, workdir):
        raise build.M.MeasureError("slide 1: measure script still pending")

    def fake_render(html_path, n, out):
        rendered.append(n)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"\x89PNG")
        return Path(out)

    monkeypatch.setattr(build.assets, "CHROME", "/fake/chrome")
    monkeypatch.setattr(build.M, "measure_slide", boom)
    monkeypatch.setattr(build.assets, "render_slide_png", fake_render)
    monkeypatch.setattr(build.assets, "publish_image", lambda d, m, n: "https://u/uc?id=x")
    res = build.build(src, "T")
    assert res.plans[0]["status"] == "fallback" and "pending" in res.plans[0]["reason"]
    assert rendered == [1]


def test_build_style_html_without_chrome_refused(google, monkeypatch, tmp_path):
    src = tmp_path / "deck.html"
    src.write_text('<div class="slide-wrap"><div class="slide"><h1>Heading</h1></div></div>')
    monkeypatch.delenv("GSLIDER_CHROME", raising=False)
    monkeypatch.setattr(build.assets, "CHROME", None)
    with pytest.raises(build.BuildError, match="--style template"):
        build.build(src, "T")
    assert google.writes == []


def test_build_style_template_unchanged(google, fake_compile, html, monkeypatch):
    calls = []
    monkeypatch.setattr(build, "compile_deck",
                        lambda deck, layout, assets, ivory, logo=True: calls.append("template") or fake_compile["plans"])
    monkeypatch.setattr(build, "compile_html_deck", lambda *a: pytest.fail("html compile in template style"))
    monkeypatch.setattr(build, "_measure_all", lambda *a, **k: pytest.fail("measured in template style"))
    res = build.build(html, "T", style="template")
    assert calls == ["template"] and len(res.plans) == 10
    assert json.loads((res.out_dir / "build.json").read_text())["style"] == "template"


def test_build_unknown_style_refused(google, fake_compile, html):
    with pytest.raises(build.BuildError, match="unknown style"):
        build.build(html, "T", style="fancy")


def test_cli_style_default_html_and_template(monkeypatch, tmp_path):
    seen = []

    def fake_build(html_path, title, ivory=frozenset(), include_internal=False, into=None, out_dir=None,
                   style="html"):
        seen.append(style)
        return build.BuildResult("p", "https://x/p", [], tmp_path)

    monkeypatch.setattr(build, "build", fake_build)
    cli.main(["build", "d.html", "--title", "X"])
    cli.main(["build", "d.html", "--title", "X", "--style", "template"])
    assert seen == ["html", "template"]


# --- Task 7: revoke sharing after the build -------------------------------------------

def _publishing(monkeypatch, fake_compile, google, n_assets=2):
    """Slide 1 needs the logo assets; each publish shares a new file (perm logged like assets._share)."""
    _brand_logo(monkeypatch, both=True)
    plans = list(fake_compile["plans"])
    fake_compile["plans"] = [_plan(1, missing=["logo", "logo_dark"][:n_assets]), *plans[1:]]
    published = []

    def fake_publish(data, mime, name, kind=None):
        published.append(name)
        fid = f"F{len(published)}"
        build.assets.SHARE_LOG.append((fid, f"perm{len(published)}"))
        fake_compile["plans"] = [_plan(1), *plans[1:]]  # second compile: nothing missing
        return f"https://drive.google.com/uc?export=download&id={fid}"

    monkeypatch.setattr(build.assets, "publish_image", fake_publish)
    return published


def test_revoke_runs_after_push_and_notes(google, fake_compile, html, monkeypatch):
    fake_compile["plans"][4] = _plan(5, status="fallback", notes="text")
    _publishing(monkeypatch, fake_compile, google)
    res = build.build(html, "T")
    kinds = [w[0] for w in google.writes]
    assert kinds[-1] == "revoke"
    assert google.notes_inserts and kinds.index("revoke") > max(i for i, k in enumerate(kinds) if k == "batch")
    assert google.writes[-1][1:] == (["F1", "F2"], {"F1": "perm1", "F2": "perm2"})
    meta = json.loads((res.out_dir / "build.json").read_text())
    assert meta["status"] == "done"
    assert meta["shared_assets"] == ["F1", "F2"]
    assert meta["shared"] == [{"file": "F1", "perm": "perm1"}, {"file": "F2", "perm": "perm2"}]
    assert meta["revoked"] == ["F1", "F2"] and meta["revoke_failed"] == []
    assert res.revoked == ["F1", "F2"] and res.revoke_failed == []


def test_revoke_runs_after_failed_push(google, fake_compile, html, monkeypatch):
    """Review Focus 2: a build that fails halfway still revokes what it shared."""
    _publishing(monkeypatch, fake_compile, google)
    google.fail_on = "s05_slide"
    with pytest.raises(build.BuildError, match="slide 5"):
        build.build(html, "T")
    assert google.writes[-1][:2] == ("revoke", ["F1", "F2"])
    meta = json.loads((build.OUT / "my-deck" / "build.json").read_text())
    assert meta["status"] == "failed" and meta["last_good_slide"] == 4
    assert meta["revoked"] == ["F1", "F2"] and meta["revoke_failed"] == []


def test_revoke_runs_when_publishing_fails_midway(google, fake_compile, html, monkeypatch):
    """A file shared whose publish then failed (no on_publish) is still revoked, from the share log."""
    _brand_logo(monkeypatch, both=True)
    fake_compile["plans"][0] = _plan(1, missing=["logo", "logo_dark"])

    def fake_publish(data, mime, name, kind=None):
        build.assets.SHARE_LOG.append(("Fhalf", "permH"))
        raise gws.GwsError(500, "props update failed")

    monkeypatch.setattr(build.assets, "publish_image", fake_publish)
    with pytest.raises(build.BuildError, match="props update failed"):
        build.build(html, "T")
    assert google.writes[-1] == ("revoke", ["Fhalf"], {"Fhalf": "permH"})
    meta = json.loads((build.OUT / "my-deck" / "build.json").read_text())
    assert meta["shared_assets"] == ["Fhalf"] and meta["revoked"] == ["Fhalf"]


def test_revoke_ignores_shares_from_earlier_builds(google, fake_compile, html, monkeypatch):
    build.assets.SHARE_LOG.append(("Fold", "permOld"))  # an earlier build in this process
    res = build.build(html, "T")
    assert not any(w[0] == "revoke" for w in google.writes)  # nothing shared, nothing to revoke
    meta = json.loads((res.out_dir / "build.json").read_text())
    assert meta["revoked"] == [] and meta["revoke_failed"] == [] and meta["shared"] == []


def test_revoke_failure_does_not_fail_build(google, fake_compile, html, monkeypatch, capsys):
    _publishing(monkeypatch, fake_compile, google)
    monkeypatch.setattr(build.assets, "revoke",
                        lambda ids, perms=None: (["F1"], [{"file": "F2", "error": "gws error 500: backend"}]))
    res = build.build(html, "T")
    meta = json.loads((res.out_dir / "build.json").read_text())
    assert meta["status"] == "done"
    assert meta["revoked"] == ["F1"]
    assert meta["revoke_failed"] == [{"file": "F2", "error": "gws error 500: backend"}]
    assert "F2" in capsys.readouterr().err

    from gslider import check as C
    rep = C.write_report(res.out_dir / "report.md", "copy1", {"slides": []}, [], C.CheckResult(), meta)
    text = rep.read_text()
    assert "Sharing not removed" in text and "F2" in text and "gslider unshare-assets" in text
    assert "- F1 (link removed)" in text


def test_revoke_crash_does_not_fail_build(google, fake_compile, html, monkeypatch):
    _publishing(monkeypatch, fake_compile, google)

    def crash(ids, perms=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(build.assets, "revoke", crash)
    res = build.build(html, "T")
    meta = json.loads((res.out_dir / "build.json").read_text())
    assert meta["status"] == "done" and meta["revoked"] == []
    assert [f["file"] for f in meta["revoke_failed"]] == ["F1", "F2"]
    assert "boom" in meta["revoke_failed"][0]["error"]


def test_plates_published_with_plate_kind(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(build, "logo_files", lambda: {})
    monkeypatch.setattr(build.assets, "publish_image",
                        lambda data, mime, name, kind=None: calls.append((name, kind)) or "https://u?id=P1")
    png = tmp_path / "p.png"
    png.write_bytes(b"\x89PNG")
    build.publish_assets({"plate:1"}, {"slides": []}, tmp_path / "d.html", "", "deck", tmp_path, False,
                         plates={1: png})
    assert calls == [("deck slide 01 plate.png", "plate")]


def test_cli_build_prints_revoke_result(monkeypatch, tmp_path, capsys):
    res = build.BuildResult("p", "u", [], tmp_path, revoked=["a", "b"], revoke_failed=[{"file": "c", "error": "x"}])
    monkeypatch.setattr(build, "build", lambda *a, **k: res)
    p = tmp_path / "d.html"
    p.write_text("<html></html>")
    assert cli.main(["build", str(p)]) == 0
    out = capsys.readouterr()
    assert "link sharing removed from 2 asset files" in out.out
    assert "c" in out.err and "gslider unshare-assets" in out.err


def test_cli_unshare_assets(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(build.assets, "unshare_all", lambda purge_plates=False: seen.append(purge_plates) or {
        "files": 5, "revoked": 3, "private": 2, "purged": 1, "failed": []})
    assert cli.main(["unshare-assets", "--purge-plates"]) == 0
    assert seen == [True]
    out = capsys.readouterr().out
    assert "5 asset files" in out and "removed from 3" in out and "1 plate" in out
    monkeypatch.setattr(build.assets, "unshare_all", lambda purge_plates=False: {
        "files": 1, "revoked": 0, "private": 0, "purged": 0, "failed": [{"file": "z", "error": "e"}]})
    assert cli.main(["unshare-assets"]) == 1
    assert "z" in capsys.readouterr().err
