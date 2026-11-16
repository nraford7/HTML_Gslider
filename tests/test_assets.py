import hashlib
from pathlib import Path

import pytest
from PIL import Image

from gslider import assets, gws

FIXTURES = Path(__file__).parent / "fixtures"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"


class FakeDrive:
    """In-memory Drive: one folder, files with appProperties, share log."""

    def __init__(self):
        self.files: list[dict] = []
        self.uploads: list[tuple] = []
        self.shares: list[str] = []
        self.creates: list[dict] = []

    def find(self, query):
        if "mimeType = 'application/vnd.google-apps.folder'" in query:
            return [{"id": "folder1", "name": assets.FOLDER_NAME}]
        return [dict(f, appProperties=dict(f["appProperties"])) for f in self.files
                if f"value='{f['appProperties']['sha256']}'" in query]

    def upload(self, path, name, folder_id, app_properties):
        assert Path(path).read_bytes()  # temp file exists when uploaded
        fid = f"file{len(self.files) + 1}"
        self.files.append({"id": fid, "name": name, "appProperties": app_properties})
        self.uploads.append((name, folder_id, app_properties))
        return fid

    def share(self, file_id):
        self.shares.append(file_id)

    def update_props(self, file_id, props):
        f = next(f for f in self.files if f["id"] == file_id)
        f["appProperties"] = {**f["appProperties"], **props}


@pytest.fixture
def drive(monkeypatch, tmp_path):
    fake = FakeDrive()
    monkeypatch.setattr(assets, "OUT", tmp_path)  # temp uploads never touch the repo's out/
    monkeypatch.setattr(assets, "_folder_id", None)
    monkeypatch.setattr(gws, "drive_find", fake.find)
    monkeypatch.setattr(gws, "drive_upload", fake.upload)
    monkeypatch.setattr(gws, "drive_share_anyone", fake.share)
    monkeypatch.setattr(gws, "drive_update_props", fake.update_props)

    def no_call(*args):
        raise AssertionError(f"unexpected gws call {args}")

    monkeypatch.setattr(gws, "call", no_call)
    return fake


def test_publish_shares_and_returns_uc_url(drive):
    url = assets.publish_image(PNG, "image/png", "cover.png")
    assert url == "https://drive.google.com/uc?export=download&id=file1"
    assert drive.shares == ["file1"]
    name, folder, props = drive.uploads[0]
    assert (name, folder) == ("cover.png", "folder1")
    assert props["sha256"] == hashlib.sha256(PNG).hexdigest()


def test_asset_dedupe(drive):
    first = assets.publish_image(PNG, "image/png", "cover.png")
    second = assets.publish_image(PNG, "image/png", "cover-again.png")
    assert first == second
    assert len(drive.uploads) == 1
    assert drive.shares == ["file1"]
    other = assets.publish_image(PNG + b"x", "image/png", "other.png")
    assert other.endswith("id=file2")
    assert drive.shares == ["file1", "file2"]


def test_ensure_folder_creates_when_missing(monkeypatch):
    monkeypatch.setattr(assets, "_folder_id", None)
    monkeypatch.setattr(gws, "drive_find", lambda q: [])
    made = []
    monkeypatch.setattr(gws, "call", lambda *a: made.append(a) or {"id": "newfolder"})
    assert assets.ensure_folder() == "newfolder"
    assert made[0][:3] == ("drive", "files", "create")


def test_ensure_folder_without_id_is_gws_error(monkeypatch):
    monkeypatch.setattr(assets, "_folder_id", None)
    monkeypatch.setattr(gws, "drive_find", lambda q: [])
    monkeypatch.setattr(gws, "call", lambda *a: {"name": "x"})
    with pytest.raises(gws.GwsError, match="no id"):
        assets.ensure_folder()


@pytest.mark.skipif(not assets.CHROME or not Path(assets.CHROME).exists(), reason="Google Chrome not installed")
def test_render_slide_png(monkeypatch, tmp_path):
    repo_out = assets.OUT
    before = sorted(repo_out.rglob("*")) if repo_out.exists() else []
    monkeypatch.setattr(assets, "OUT", tmp_path)
    out = tmp_path / "test_render" / "dark.png"
    assets.render_slide_png(FIXTURES / "render_dark.html", 2, out)
    assert (sorted(repo_out.rglob("*")) if repo_out.exists() else []) == before
    img = Image.open(out).convert("RGB")
    assert img.size == (1920, 1080)
    for xy in [(0, 0), (1919, 0), (0, 1079), (1919, 1079)]:
        px = img.getpixel(xy)
        assert all(abs(c - t) <= 8 for c, t in zip(px, (0x08, 0x08, 0x06))), (xy, px)


# --- review findings S3, S4, S5 -------------------------------------------------------

def test_publish_temp_file_is_unique_per_call(drive, monkeypatch, tmp_path):
    """Two builds publishing the same bytes at once must not share (and unlink) one temp path."""
    paths = []
    real_upload = drive.upload

    def upload(path, name, folder_id, app_properties):
        paths.append(Path(path))
        return real_upload(path, name, folder_id, app_properties)

    monkeypatch.setattr(gws, "drive_upload", upload)
    monkeypatch.setattr(gws, "drive_find", lambda q: [{"id": "folder1"}] if "google-apps.folder" in q else [])
    assets.publish_image(PNG, "image/png", "a.png")
    assets.publish_image(PNG, "image/png", "b.png")
    assert len(paths) == 2 and paths[0] != paths[1]
    assert all(tmp_path in p.parents for p in paths), paths  # row 13: never the repo's out/
    assert not any(p.exists() for p in paths)


def test_render_temp_html_is_unique_per_call(monkeypatch, tmp_path):
    pages = []

    def fake_run(cmd, **kw):
        pages.append(cmd[-1])
        shot = next(a for a in cmd if a.startswith("--screenshot="))[len("--screenshot="):]
        Path(shot).write_bytes(b"png")
        return gws.subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(assets.subprocess, "run", fake_run)
    monkeypatch.setattr(assets, "OUT", tmp_path)
    assets.render_slide_png(FIXTURES / "render_dark.html", 2, tmp_path / "a" / "s.png")
    assets.render_slide_png(FIXTURES / "render_dark.html", 2, tmp_path / "b" / "s.png")
    assert len(set(pages)) == 2


def test_share_failure_leaves_file_unmarked_then_shares_on_reuse(drive, monkeypatch):
    real_share = drive.share
    state = {"fail": True}

    def flaky_share(fid):
        if state["fail"]:
            raise gws.GwsError(500, "backend error")
        real_share(fid)

    monkeypatch.setattr(gws, "drive_share_anyone", flaky_share)
    with pytest.raises(gws.GwsError):
        assets.publish_image(PNG, "image/png", "a.png")
    assert drive.files[0]["appProperties"].get("shared") != "1"
    state["fail"] = False
    url = assets.publish_image(PNG, "image/png", "a.png")
    assert url.endswith("id=file1") and len(drive.uploads) == 1
    assert drive.shares == ["file1"]
    assert drive.files[0]["appProperties"]["shared"] == "1"


def test_marked_shared_file_is_not_shared_again(drive):
    assets.publish_image(PNG, "image/png", "a.png")
    assert drive.files[0]["appProperties"]["shared"] == "1"
    assets.publish_image(PNG, "image/png", "a.png")
    assert drive.shares == ["file1"]


def test_folder_lookup_requires_my_ownership(monkeypatch):
    queries = []
    monkeypatch.setattr(assets, "_folder_id", None)
    monkeypatch.setattr(gws, "drive_find", lambda q: queries.append(q) or [{"id": "f9"}])
    assert assets.ensure_folder() == "f9"
    assert "'me' in owners" in queries[0]
