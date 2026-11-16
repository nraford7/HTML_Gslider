import hashlib
from pathlib import Path

import pytest
from PIL import Image

from gslider import assets, gws

FIXTURES = Path(__file__).parent / "fixtures"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"


class FakeDrive:
    """In-memory Drive: one folder, files with appProperties and permissions, call log."""

    def __init__(self):
        self.files: list[dict] = []
        self.uploads: list[tuple] = []
        self.shares: list[str] = []
        self.creates: list[dict] = []
        self.perms: dict[str, list[dict]] = {}
        self.log: list[tuple] = []  # every write, in order
        self.unshare_error: Exception | None = None

    def find(self, query):
        if "mimeType = 'application/vnd.google-apps.folder'" in query:
            return [{"id": "folder1", "name": assets.FOLDER_NAME}]
        if "key='gslider'" in query:  # unshare_all: every gslider file in the folder
            assert "'folder1' in parents" in query and "'me' in owners" in query
            return [dict(f, appProperties=dict(f["appProperties"])) for f in self.files
                    if f["appProperties"].get("gslider") == "1"]
        return [dict(f, appProperties=dict(f["appProperties"])) for f in self.files
                if f"value='{f['appProperties'].get('sha256')}'" in query]

    def upload(self, path, name, folder_id, app_properties):
        assert Path(path).read_bytes()  # temp file exists when uploaded
        fid = f"file{len(self.files) + 1}"
        self.files.append({"id": fid, "name": name, "appProperties": app_properties})
        self.uploads.append((name, folder_id, app_properties))
        return fid

    def share(self, file_id):
        self.shares.append(file_id)
        self.log.append(("share", file_id))
        self.perms.setdefault(file_id, []).append({"id": "anyoneWithLink", "type": "anyone", "role": "reader"})
        return "anyoneWithLink"

    def update_props(self, file_id, props):
        self.log.append(("props", file_id, dict(props)))
        f = next(f for f in self.files if f["id"] == file_id)
        merged = {**f["appProperties"], **props}
        f["appProperties"] = {k: v for k, v in merged.items() if v is not None}  # null removes a key

    def get(self, file_id, fields):
        self.log.append(("get", file_id))
        f = next(f for f in self.files if f["id"] == file_id)
        return {"id": file_id, "appProperties": dict(f["appProperties"])}

    def permissions(self, file_id):
        self.log.append(("list", file_id))
        return [dict(p) for p in self.perms.get(file_id, [])]

    def unshare(self, file_id, perm_id):
        self.log.append(("unshare", file_id, perm_id))
        if self.unshare_error:
            raise self.unshare_error
        before = self.perms.get(file_id, [])
        if not any(p["id"] == perm_id for p in before):
            raise gws.GwsError(404, f"Permission not found: {perm_id}.")
        self.perms[file_id] = [p for p in before if p["id"] != perm_id]

    def delete(self, file_id):
        self.log.append(("delete", file_id))
        self.files = [f for f in self.files if f["id"] != file_id]


@pytest.fixture
def drive(monkeypatch, tmp_path):
    fake = FakeDrive()
    monkeypatch.setattr(assets, "OUT", tmp_path)  # temp uploads never touch the repo's out/
    monkeypatch.setattr(assets, "_folder_id", None)
    monkeypatch.setattr(gws, "drive_find", fake.find)
    monkeypatch.setattr(gws, "drive_upload", fake.upload)
    monkeypatch.setattr(gws, "drive_share_anyone", fake.share)
    monkeypatch.setattr(gws, "drive_update_props", fake.update_props)
    monkeypatch.setattr(gws, "drive_get", fake.get)
    monkeypatch.setattr(gws, "drive_permissions", fake.permissions)
    monkeypatch.setattr(gws, "drive_unshare", fake.unshare)
    monkeypatch.setattr(gws, "drive_delete", fake.delete)

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
        return real_share(fid)

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


# --- Task 7: revoke sharing after the build -------------------------------------------

def _writes(drive, kinds=("share", "props", "unshare", "delete")):
    return [e for e in drive.log if e[0] in kinds]


def test_share_stores_perm_id(drive, monkeypatch):
    monkeypatch.setattr(assets, "SHARE_LOG", [])
    assets.publish_image(PNG, "image/png", "a.png")
    props = drive.files[0]["appProperties"]
    assert props["shared"] == "1" and props["perm"] == "anyoneWithLink"
    assert assets.SHARE_LOG == [("file1", "anyoneWithLink")]


def test_share_log_written_before_the_mark(drive, monkeypatch):
    """A permission made but not marked (props update failed) is still known to the build."""
    monkeypatch.setattr(assets, "SHARE_LOG", [])

    def bad_props(fid, props):
        raise gws.GwsError(500, "backend error")

    monkeypatch.setattr(gws, "drive_update_props", bad_props)
    with pytest.raises(gws.GwsError):
        assets.publish_image(PNG, "image/png", "a.png")
    assert assets.SHARE_LOG == [("file1", "anyoneWithLink")]


def test_publish_kind_goes_to_app_properties(drive):
    assets.publish_image(PNG, "image/png", "deck slide 01 plate.png", kind="plate")
    assert drive.files[0]["appProperties"]["kind"] == "plate"
    assets.publish_image(PNG + b"y", "image/png", "logo.png")
    assert "kind" not in drive.files[1]["appProperties"]


def test_revoke_uses_stored_perm_or_lists_anyone(drive):
    assets.publish_image(PNG, "image/png", "a.png")  # file1: perm stored at share time
    drive.files.append({"id": "file2", "name": "old.png",  # shared by an older gslider: no perm stored
                        "appProperties": {"sha256": "x", "gslider": "1", "shared": "1"}})
    drive.perms["file2"] = [{"id": "anyoneOld", "type": "anyone", "role": "reader"},
                            {"id": "u1", "type": "user", "role": "writer"}]
    drive.log.clear()
    revoked, failed = assets.revoke(["file1", "file2"])
    assert (revoked, failed) == (["file1", "file2"], [])
    assert ("unshare", "file1", "anyoneWithLink") in drive.log
    first_list = drive.log.index(("list", "file1"))
    assert drive.log.index(("unshare", "file1", "anyoneWithLink")) < first_list  # by stored id, then verified
    assert ("unshare", "file2", "anyoneOld") in drive.log
    assert ("list", "file2") in drive.log
    assert drive.perms["file1"] == [] and drive.perms["file2"] == [{"id": "u1", "type": "user", "role": "writer"}]
    for f in drive.files:
        assert f["appProperties"]["shared"] == "0" and "perm" not in f["appProperties"]


def test_revoke_perms_argument_wins_over_props(drive):
    assets.publish_image(PNG, "image/png", "a.png")
    drive.perms["file1"].append({"id": "anyone2", "type": "anyone", "role": "reader"})
    drive.log.clear()
    revoked, failed = assets.revoke(["file1"], {"file1": "anyone2"})
    assert revoked == ["file1"] and failed == []
    unshares = [e for e in drive.log if e[0] == "unshare"]
    assert unshares[0] == ("unshare", "file1", "anyone2")
    assert ("unshare", "file1", "anyoneWithLink") in unshares  # the list found the other one
    assert drive.perms["file1"] == []


def test_revoke_marks_unshared_before_deleting(drive):
    """Mark first: a delete that fails later leaves shared=0, so the next build shares again."""
    assets.publish_image(PNG, "image/png", "a.png")
    drive.log.clear()
    assets.revoke(["file1"])
    writes = _writes(drive)
    assert writes[0] == ("props", "file1", {"shared": "0", "perm": None})
    assert writes[1][0] == "unshare"


def test_revoke_stale_stored_perm_is_not_an_error(drive):
    assets.publish_image(PNG, "image/png", "a.png")
    drive.perms["file1"] = []  # removed by hand in Drive
    revoked, failed = assets.revoke(["file1"])
    assert revoked == ["file1"] and failed == []


def test_revoke_never_touches_protected_or_foreign_files(drive):
    prot = gws.PROTECTED_IDS[0]
    drive.files.append({"id": "mine_not_gslider", "name": "x.png", "appProperties": {"shared": "1"}})
    drive.perms["mine_not_gslider"] = [{"id": "anyoneWithLink", "type": "anyone"}]
    revoked, failed = assets.revoke([prot, "mine_not_gslider"])
    assert revoked == []
    assert [f["file"] for f in failed] == [prot, "mine_not_gslider"]
    assert "protected" in failed[0]["error"] and "gslider" in failed[1]["error"]
    assert _writes(drive) == []
    assert all(e[1] != prot for e in drive.log)  # not even read


def test_revoke_failure_is_returned_not_raised(drive):
    assets.publish_image(PNG, "image/png", "a.png")
    assets.publish_image(PNG + b"z", "image/png", "b.png")
    drive.unshare_error = gws.GwsError(500, "backend error")
    revoked, failed = assets.revoke(["file1", "file2"])
    assert revoked == []
    assert [f["file"] for f in failed] == ["file1", "file2"] and "backend error" in failed[0]["error"]


def test_revoke_verifies_anyone_is_gone(drive, monkeypatch):
    assets.publish_image(PNG, "image/png", "a.png")
    monkeypatch.setattr(gws, "drive_unshare", lambda fid, pid: None)  # says ok, removes nothing
    revoked, failed = assets.revoke(["file1"])
    assert revoked == [] and "still" in failed[0]["error"]


def test_dedupe_reshares_after_revoke(drive):
    assets.publish_image(PNG, "image/png", "a.png")
    assets.revoke(["file1"])
    assert drive.files[0]["appProperties"]["shared"] == "0"
    url = assets.publish_image(PNG, "image/png", "a.png")
    assert url.endswith("id=file1") and len(drive.uploads) == 1
    assert drive.shares == ["file1", "file1"]
    assert drive.files[0]["appProperties"]["shared"] == "1"
    assert drive.perms["file1"] == [{"id": "anyoneWithLink", "type": "anyone", "role": "reader"}]


def test_unshare_assets_all_and_purge_plates(drive):
    assets.publish_image(PNG, "image/png", "logo.png")                                   # file1 shared
    assets.publish_image(PNG + b"p", "image/png", "deck slide 01 plate.png", kind="plate")  # file2 shared plate
    assets.publish_image(PNG + b"q", "image/png", "deck slide 02 plate.png")             # file3 older plate: name only
    assets.revoke(["file3"])                                                             # file3 private already
    drive.files.append({"id": "foreign", "name": "x plate.png", "appProperties": {"shared": "1"}})
    drive.perms["foreign"] = [{"id": "anyoneWithLink", "type": "anyone"}]
    drive.log.clear()

    res = assets.unshare_all()
    assert res == {"files": 3, "revoked": 2, "private": 1, "purged": 0, "failed": []}
    assert drive.perms["file1"] == [] and drive.perms["file2"] == []
    assert drive.perms["foreign"] == [{"id": "anyoneWithLink", "type": "anyone"}]
    assert all(e[1] != "foreign" for e in drive.log)
    assert not any(e[0] == "props" and e[1] == "file3" for e in drive.log)  # already marked private

    res = assets.unshare_all(purge_plates=True)
    assert res == {"files": 3, "revoked": 0, "private": 3, "purged": 2, "failed": []}
    assert sorted(e[1] for e in drive.log if e[0] == "delete") == ["file2", "file3"]
    assert [f["id"] for f in drive.files] == ["file1", "foreign"]


def test_unshare_assets_skips_protected_and_reports_failures(drive, monkeypatch):
    prot = gws.PROTECTED_IDS[0]
    assets.publish_image(PNG, "image/png", "a.png")
    drive.files.append({"id": prot, "name": "p plate.png", "appProperties": {"gslider": "1", "shared": "1"}})
    drive.unshare_error = gws.GwsError(500, "backend error")
    drive.log.clear()
    res = assets.unshare_all(purge_plates=True)
    assert res["files"] == 1 and res["revoked"] == 0 and res["purged"] == 0
    assert res["failed"][0]["file"] == "file1" and "backend error" in res["failed"][0]["error"]
    assert all(e[1] != prot for e in drive.log)


def test_unshare_assets_without_folder_creates_nothing(monkeypatch):
    monkeypatch.setattr(assets, "_folder_id", None)
    monkeypatch.setattr(gws, "drive_find", lambda q: [])

    def no_call(*a):
        raise AssertionError(f"unexpected gws call {a}")

    monkeypatch.setattr(gws, "call", no_call)
    assert assets.unshare_all() == {"files": 0, "revoked": 0, "private": 0, "purged": 0, "failed": []}
