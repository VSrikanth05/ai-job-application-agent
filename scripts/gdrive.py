"""Drive folders + uploads through gws (drive.file scope: only app-created files are visible).

Mirrors the Desktop layout under the "Job Applications" Drive folder (GOOGLE_DRIVE_FOLDER_ID):
    Job Applications/<scrape date>/<Company - Job Title>/Srikanth Valluri Resume.pdf
"""

from __future__ import annotations

import os
from pathlib import Path

from gws import GwsError, gws

FOLDER_MIME = "application/vnd.google-apps.folder"


def root_folder_id() -> str:
    fid = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    if not fid:
        raise GwsError("setup", "GOOGLE_DRIVE_FOLDER_ID is not set — run scripts/google_setup.py")
    return fid


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def find(name: str, parent: str, mime: str | None = None) -> str | None:
    q = f"name = '{_q(name)}' and '{parent}' in parents and trashed = false"
    if mime:
        q += f" and mimeType = '{mime}'"
    files = (gws("drive", "files", "list", params={"q": q, "fields": "files(id)", "pageSize": 1}) or {}).get("files", [])
    return files[0]["id"] if files else None


def ensure_folder(name: str, parent: str | None = None) -> str:
    body = {"name": name, "mimeType": FOLDER_MIME}
    if parent:
        existing = find(name, parent, FOLDER_MIME)
        if existing:
            return existing
        body["parents"] = [parent]
    return gws("drive", "files", "create", body=body, params={"fields": "id"})["id"]


def upload_pdf(local_pdf: Path, folders: list[str], filename: str) -> str:
    """Uploads (or replaces) <root>/<folders...>/<filename>; returns its web link.
    local_pdf must be inside the project directory (gws restriction)."""
    parent = root_folder_id()
    for name in folders:
        parent = ensure_folder(name, parent)
    existing = find(filename, parent)
    if existing:
        res = gws("drive", "files", "update", params={"fileId": existing, "fields": "id,webViewLink"},
                  body={}, upload=local_pdf)
    else:
        res = gws("drive", "files", "create", params={"fields": "id,webViewLink"},
                  body={"name": filename, "parents": [parent]}, upload=local_pdf)
    return res.get("webViewLink") or f"https://drive.google.com/file/d/{res['id']}/view"


def move_into(file_id: str, folder_id: str) -> None:
    meta = gws("drive", "files", "get", params={"fileId": file_id, "fields": "parents"}) or {}
    gws("drive", "files", "update", body={}, params={
        "fileId": file_id, "addParents": folder_id,
        "removeParents": ",".join(p for p in meta.get("parents", []) if p != folder_id), "fields": "id"})
