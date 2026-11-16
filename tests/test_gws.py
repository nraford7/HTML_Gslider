import json
import subprocess

import pytest

from gslider import gws


def _proc(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=["gws"], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def no_sleep(monkeypatch):
    delays = []
    monkeypatch.setattr(gws.time, "sleep", lambda s: delays.append(s))
    return delays


def test_call_parses_json_after_keyring_line(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _proc(stdout='Using keyring backend: keychain\n{"id": "abc", "n": 2}\n')

    monkeypatch.setattr(gws.subprocess, "run", fake_run)
    assert gws.call("drive", "files", "get") == {"id": "abc", "n": 2}
    assert seen["cmd"] == ["gws", "drive", "files", "get"]


def test_call_raises_on_error_key(monkeypatch):
    body = {"error": {"code": 400, "message": "Invalid requests[0]"}}
    monkeypatch.setattr(gws.subprocess, "run", lambda cmd, **kw: _proc(stdout=json.dumps(body)))
    with pytest.raises(gws.GwsError) as ei:
        gws.call("slides", "presentations", "get")
    assert ei.value.status == 400
    assert "Invalid requests[0]" in ei.value.message


def test_call_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(gws.subprocess, "run", lambda cmd, **kw: _proc(stderr="boom", returncode=1))
    with pytest.raises(gws.GwsError) as ei:
        gws.call("x")
    assert "boom" in ei.value.message


def test_backoff_retries_429_then_succeeds(monkeypatch, no_sleep):
    outs = [
        _proc(stdout=json.dumps({"error": {"code": 429, "message": "rate"}}), returncode=1),
        _proc(stdout=json.dumps({"error": {"code": 429, "message": "rate"}}), returncode=1),
        _proc(stdout='{"replies": []}'),
    ]
    monkeypatch.setattr(gws.subprocess, "run", lambda cmd, **kw: outs.pop(0))
    assert gws.batch("pid1", [{"deleteObject": {"objectId": "x"}}]) == {"replies": []}
    assert no_sleep == [2, 4]


def test_backoff_gives_up_after_five(monkeypatch, no_sleep):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _proc(stdout=json.dumps({"error": {"code": 429, "message": "rate"}}), returncode=1)

    monkeypatch.setattr(gws.subprocess, "run", fake_run)
    with pytest.raises(gws.GwsError) as ei:
        gws.get("pid1")
    assert ei.value.status == 429
    assert no_sleep == [2, 4, 8, 16, 32]
    assert len(calls) == 6


def test_backoff_does_not_retry_other_errors(monkeypatch, no_sleep):
    monkeypatch.setattr(gws.subprocess, "run",
                        lambda cmd, **kw: _proc(stdout='{"error": {"code": 400, "message": "bad"}}', returncode=1))
    with pytest.raises(gws.GwsError):
        gws.get("pid1")
    assert no_sleep == []


def test_batch_command_shape(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _proc(stdout="{}")

    monkeypatch.setattr(gws.subprocess, "run", fake_run)
    reqs = [{"createSlide": {"objectId": "s01_slide"}}]
    gws.batch("P", reqs)
    cmd = seen["cmd"]
    assert cmd[:4] == ["gws", "slides", "presentations", "batchUpdate"]
    assert json.loads(cmd[cmd.index("--params") + 1]) == {"presentationId": "P"}
    assert json.loads(cmd[cmd.index("--json") + 1]) == {"requests": reqs}


def test_drive_copy_sets_app_properties(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _proc(stdout='{"id": "NEW"}')

    monkeypatch.setattr(gws.subprocess, "run", fake_run)
    assert gws.drive_copy("SRC", "T", {"gslider": "1"}) == "NEW"
    cmd = seen["cmd"]
    assert cmd[:4] == ["gws", "drive", "files", "copy"]
    assert json.loads(cmd[cmd.index("--params") + 1])["fileId"] == "SRC"
    assert json.loads(cmd[cmd.index("--json") + 1]) == {"name": "T", "appProperties": {"gslider": "1"}}


def test_comments_count(monkeypatch):
    monkeypatch.setattr(gws.subprocess, "run",
                        lambda cmd, **kw: _proc(stdout='{"comments": [{"id": "a"}, {"id": "b"}]}'))
    assert gws.comments_count("F") == 2


def test_drive_upload_and_import(monkeypatch, tmp_path):
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        return _proc(stdout='{"id": "UP"}')

    monkeypatch.setattr(gws.subprocess, "run", fake_run)
    p = tmp_path / "a.png"
    p.write_bytes(b"x")
    assert gws.drive_upload(p, "a.png", "FOLD", {"sha256": "h"}) == "UP"
    body = json.loads(cmds[0][cmds[0].index("--json") + 1])
    assert body == {"name": "a.png", "parents": ["FOLD"], "appProperties": {"sha256": "h"}}
    assert cmds[0][cmds[0].index("--upload") + 1] == p.name  # run with cwd = p.parent
    assert gws.drive_import_pptx(p, "Base") == "UP"
    body = json.loads(cmds[1][cmds[1].index("--json") + 1])
    assert body["mimeType"] == "application/vnd.google-apps.presentation"


def test_upload_runs_in_file_folder(monkeypatch, tmp_path):
    """gws refuses --upload paths outside its cwd, so uploads run in the file's folder."""
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, cwd=kw.get("cwd"))
        return subprocess.CompletedProcess(cmd, 0, '{"id": "f1"}', "")

    monkeypatch.setattr(gws.subprocess, "run", fake_run)
    f = tmp_path / "a.pptx"
    f.write_bytes(b"x")
    assert gws.drive_import_pptx(f, "n") == "f1"
    assert seen["cwd"] == str(tmp_path.resolve()) and seen["cmd"][seen["cmd"].index("--upload") + 1] == "a.pptx"


def test_success_without_id_is_gws_error(monkeypatch, tmp_path):
    monkeypatch.setattr(gws.subprocess, "run", lambda cmd, **kw: _proc(stdout='{"name": "x"}'))
    p = tmp_path / "a.png"
    p.write_bytes(b"x")
    for fn in (lambda: gws.drive_copy("SRC", "T", {}),
               lambda: gws.drive_upload(p, "a.png", "F", {}),
               lambda: gws.drive_import_pptx(p, "B")):
        with pytest.raises(gws.GwsError, match="no id"):
            fn()


def test_thumbnail_retries_when_download_is_not_png(monkeypatch, tmp_path):
    import gslider.gws as g
    calls = {"n": 0}
    monkeypatch.setattr(g, "call", lambda *a: {"contentUrl": "https://x/y"})
    monkeypatch.setattr(g.time, "sleep", lambda s: None)

    def fake_run(cmd, **kw):
        calls["n"] += 1
        body = b"<!DOCTYPE html>" if calls["n"] == 1 else b"\x89PNG\r\n\x1a\nrest"
        open(cmd[cmd.index("-o") + 1], "wb").write(body)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": "200"})()

    monkeypatch.setattr(g.subprocess, "run", fake_run)
    g.thumbnail("pid", "page", tmp_path / "t.png")
    assert calls["n"] == 2


# --- protected decks (review S7) --------------------------------------------------

PROT = gws.PROTECTED_IDS[0]


@pytest.mark.parametrize("write", [
    lambda: gws.batch(PROT, [{"deleteObject": {"objectId": "x"}}]),
    lambda: gws.drive_copy(PROT, "T", {"gslider": "1"}),
    lambda: gws.drive_upload("/tmp/x.png", "x.png", PROT, {}),
    lambda: gws.drive_share_anyone(PROT),
    lambda: gws.drive_update_props(PROT, {"shared": "1"}),
])
def test_write_helpers_refuse_protected_ids(monkeypatch, write):
    calls = []
    monkeypatch.setattr(gws.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _proc(stdout="{}"))
    with pytest.raises(gws.GwsError, match="protected"):
        write()
    assert calls == []


def test_drive_update_props_command_shape(monkeypatch):
    seen = {}
    monkeypatch.setattr(gws.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd) or _proc(stdout='{"id": "F"}'))
    gws.drive_update_props("F", {"shared": "1"})
    cmd = seen["cmd"]
    assert cmd[:4] == ["gws", "drive", "files", "update"]
    assert json.loads(cmd[cmd.index("--params") + 1])["fileId"] == "F"
    assert json.loads(cmd[cmd.index("--json") + 1]) == {"appProperties": {"shared": "1"}}
