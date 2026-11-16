"""Thin subprocess wrapper around the authenticated `gws` CLI.

Every Google call in gslider goes through `call`, which is retried on HTTP 429
by `with_backoff` (delays 2, 4, 8, 16, 32 s, then the error is raised).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

BACKOFF_DELAYS = (2, 4, 8, 16, 32)
PRESENTATION_MIME = "application/vnd.google-apps.presentation"
def _protected_ids() -> tuple[str, ...]:
    """Files gslider must never write: `protected_ids` in ~/.config/gslider/config.json
    plus the comma-separated GSLIDER_PROTECTED_IDS environment variable."""
    ids = [i.strip() for i in os.environ.get("GSLIDER_PROTECTED_IDS", "").split(",") if i.strip()]
    cfg = Path.home() / ".config" / "gslider" / "config.json"
    try:
        ids += json.loads(cfg.read_text(encoding="utf-8")).get("protected_ids", [])
    except (OSError, ValueError):
        pass
    return tuple(dict.fromkeys(ids))


PROTECTED_IDS = _protected_ids()


class GwsError(Exception):
    def __init__(self, status: int | None, message: str):
        super().__init__(f"gws error {status}: {message}")
        self.status = status
        self.message = message


def _parse(text: str) -> dict | None:
    """Parse JSON starting at the first `{` (skips keyring/info lines)."""
    i = text.find("{")
    if i < 0:
        return None
    try:
        return json.loads(text[i:])
    except json.JSONDecodeError:
        return None


def _error_from(data: dict | None, fallback: str) -> GwsError:
    err = (data or {}).get("error")
    if isinstance(err, dict):
        status = err.get("code")
        return GwsError(status if isinstance(status, int) else None, err.get("message") or json.dumps(err))
    if err is not None:
        return GwsError(None, str(err))
    return GwsError(None, fallback)


def _call_once(*args: str, cwd: str | None = None) -> dict:
    out = subprocess.run(["gws", *args], capture_output=True, text=True, cwd=cwd)
    data = _parse(out.stdout)
    if data is None and out.returncode:
        data = _parse(out.stderr)
    if out.returncode or (data is not None and "error" in data):
        raise _error_from(data, (out.stderr or out.stdout or f"exit {out.returncode}").strip())
    return data or {}


def with_backoff(fn: Callable[[], T]) -> T:
    """Run fn; on GwsError with status 429 sleep 2,4,8,16,32 s and retry, then give up."""
    for delay in BACKOFF_DELAYS:
        try:
            return fn()
        except GwsError as e:
            if e.status != 429:
                raise
            time.sleep(delay)
    return fn()


def call(*args: str, cwd: str | None = None) -> dict:
    """Run `gws <args>` and return the parsed JSON response (with 429 backoff)."""
    return with_backoff(lambda: _call_once(*args, cwd=cwd))


def created_id(data: dict, what: str) -> str:
    """The `id` of a create/copy response; a success without one is a GwsError."""
    fid = data.get("id") if isinstance(data, dict) else None
    if not fid:
        raise GwsError(None, f"no id in {what} response: {json.dumps(data)[:200]}")
    return fid


def _j(obj: dict) -> str:
    return json.dumps(obj)


def refuse_protected(*ids: str | None) -> None:
    """GwsError if any id is a protected file; every write helper calls this first."""
    hit = [i for i in ids if i in PROTECTED_IDS]
    if hit:
        raise GwsError(None, f"refusing to write protected file {hit[0]}")


# --- Slides -----------------------------------------------------------------

def batch(pid: str, requests: list[dict]) -> dict:
    refuse_protected(pid)
    return call("slides", "presentations", "batchUpdate",
                "--params", _j({"presentationId": pid}),
                "--json", _j({"requests": requests}))


def get(pid: str, fields: str | None = None) -> dict:
    params: dict = {"presentationId": pid}
    if fields:
        params["fields"] = fields
    return call("slides", "presentations", "get", "--params", _j(params))


def thumbnail(pid: str, page_id: str, out_path) -> str:
    data = call("slides", "presentations", "pages", "getThumbnail",
                "--params", _j({"presentationId": pid, "pageObjectId": page_id,
                                "thumbnailProperties.thumbnailSize": "LARGE"}))
    url = data.get("contentUrl")
    if not url:
        raise GwsError(None, f"no contentUrl in thumbnail response for {page_id}")
    out = str(out_path)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    # The content host sometimes answers with an HTML error page (seen live under load):
    # check for the PNG signature and retry the download with the same backoff delays.
    for delay in (0, *BACKOFF_DELAYS):
        time.sleep(delay)
        r = subprocess.run(["curl", "-sL", "-w", "%{http_code}", url, "-o", out],
                           capture_output=True, text=True)
        status = r.stdout.strip()[-3:] or "?"
        if r.returncode:
            raise GwsError(None, f"thumbnail download failed (HTTP {status}): {r.stderr.strip()}")
        with open(out, "rb") as f:
            if f.read(8) == b"\x89PNG\r\n\x1a\n":
                return out
    raise GwsError(None, f"thumbnail for {page_id} is not a PNG after retries (last HTTP {status})")


# --- Drive ------------------------------------------------------------------

def drive_copy(file_id: str, title: str, app_properties: dict) -> str:
    refuse_protected(file_id)
    return created_id(call("drive", "files", "copy",
                           "--params", _j({"fileId": file_id}),
                           "--json", _j({"name": title, "appProperties": app_properties})), "drive copy")


def drive_get(file_id: str, fields: str) -> dict:
    return call("drive", "files", "get", "--params", _j({"fileId": file_id, "fields": fields}))


def drive_upload(path, name: str, folder_id: str, app_properties: dict) -> str:
    # gws refuses --upload paths outside its working directory: run it in the file's folder
    refuse_protected(folder_id)
    p = Path(path).resolve()
    body = {"name": name, "parents": [folder_id], "appProperties": app_properties}
    return created_id(call("drive", "files", "create", "--json", _j(body), "--upload", p.name,
                           cwd=str(p.parent)), "drive upload")


def drive_share_anyone(file_id: str) -> None:
    refuse_protected(file_id)
    call("drive", "permissions", "create",
         "--params", _j({"fileId": file_id}),
         "--json", _j({"type": "anyone", "role": "reader"}))


def drive_update_props(file_id: str, app_properties: dict) -> None:
    """Merge appProperties into a file's metadata (keys not named are kept)."""
    refuse_protected(file_id)
    call("drive", "files", "update", "--params", _j({"fileId": file_id}),
         "--json", _j({"appProperties": app_properties}))


def drive_find(query: str) -> list[dict]:
    """All files matching a Drive query (id, name, appProperties, webContentLink)."""
    files: list[dict] = []
    token = None
    while True:
        params = {"q": query, "pageSize": 100,
                  "fields": "nextPageToken,files(id,name,mimeType,appProperties,webContentLink)"}
        if token:
            params["pageToken"] = token
        data = call("drive", "files", "list", "--params", _j(params))
        files.extend(data.get("files", []))
        token = data.get("nextPageToken")
        if not token:
            return files


def drive_import_pptx(path, name: str) -> str:
    p = Path(path).resolve()
    body = {"name": name, "mimeType": PRESENTATION_MIME}
    return created_id(call("drive", "files", "create", "--json", _j(body), "--upload", p.name,
                           "--upload-content-type",
                           "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                           cwd=str(p.parent)), "pptx import")


def comments_count(file_id: str) -> int:
    data = call("drive", "comments", "list",
                "--params", _j({"fileId": file_id, "fields": "comments(id)", "pageSize": 100}))
    return len(data.get("comments", []))
