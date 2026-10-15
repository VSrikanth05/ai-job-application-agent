"""Thin wrapper around the Google Workspace CLI (`gws`, pinned in tools/gws/).

    gws("drive", "files", "copy", params={...}, body={...})  -> parsed JSON (or None)

Raises GwsError with gws's structured exit-code meaning. Transient API errors
(429/5xx) are retried with backoff.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from common import ROOT

log = logging.getLogger("gws")

GWS_BIN = Path(os.getenv("GWS_BIN") or ROOT / "tools" / "gws" / ("gws.exe" if sys.platform == "win32" else "gws"))
EXIT_CODES = {1: "api", 2: "auth", 3: "validation", 4: "discovery", 5: "internal"}
MAX_CMDLINE = 30000  # Windows limit is 32767 chars for the whole command line
IO_DIR = ROOT / ".gws_io"  # staging for gws uploads/downloads (see gws())


class GwsError(RuntimeError):
    def __init__(self, kind: str, message: str, payload: dict | None = None):
        super().__init__(f"gws {kind} error: {message}")
        self.kind, self.payload = kind, payload or {}

    @property
    def http_code(self) -> int | None:
        return (self.payload.get("error") or {}).get("code")


def _parse(text: str):
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # some helpers print NDJSON or a trailing line; take the last JSON object
        for line in reversed(text.splitlines()):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return {"raw": text}


def gws(*args: str, params: dict | None = None, body: dict | list | None = None, upload: Path | None = None,
        output: Path | None = None, extra: list[str] | None = None, retries: int = 3):
    """gws only reads --upload / writes --output files that *resolve* inside its working directory
    (symlinks are followed — e.g. the Modal volume isn't). So files go through IO_DIR, a real folder
    in the project root, and are copied in / moved out here; callers can use any path."""
    if not GWS_BIN.exists():
        raise GwsError("setup", f"gws binary not found at {GWS_BIN} (see GWS_SETUP.md)")
    IO_DIR.mkdir(exist_ok=True)
    staged_in = staged_out = None
    cmd = [str(GWS_BIN), *args]
    if params is not None:
        cmd += ["--params", json.dumps(params, ensure_ascii=False)]
    if body is not None:
        cmd += ["--json", json.dumps(body, ensure_ascii=False)]
    if upload is not None:
        staged_in = IO_DIR / f"{uuid.uuid4().hex}{Path(upload).suffix}"
        shutil.copyfile(upload, staged_in)
        cmd += ["--upload", str(staged_in.relative_to(ROOT))]
    if output is not None:
        staged_out = IO_DIR / f"{uuid.uuid4().hex}{Path(output).suffix}"
        cmd += ["--output", str(staged_out.relative_to(ROOT))]
    cmd += extra or []
    if sum(len(c) + 3 for c in cmd) > MAX_CMDLINE:
        raise GwsError("validation", "request too large for the Windows command line; split it")

    try:
        return _run(cmd, retries, output, staged_out)
    finally:
        for f in (staged_in, staged_out):
            if f is not None and f.exists():
                f.unlink()


def _run(cmd: list[str], retries: int, output: Path | None, staged_out: Path | None):
    for attempt in range(retries + 1):
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=300, cwd=ROOT)
        if proc.returncode == 0:
            if staged_out is not None:
                Path(output).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staged_out), output)
            return _parse(proc.stdout)
        payload = _parse(proc.stdout) or _parse(proc.stderr) or {}
        payload = payload if isinstance(payload, dict) else {}
        msg = (payload.get("error") or {}).get("message") or proc.stderr.strip()[-500:] or proc.stdout.strip()[-500:]
        err = GwsError(EXIT_CODES.get(proc.returncode, f"exit {proc.returncode}"), msg, payload)
        if err.kind == "api" and err.http_code in (429, 500, 502, 503, 504) and attempt < retries:
            wait = 5 * 2 ** attempt
            log.warning("%s — retrying in %ds", err, wait)
            time.sleep(wait)
            continue
        raise err


def _credentials_from_env() -> None:
    """Cloud (Modal): the login arrives as GWS_CREDENTIALS_JSON (output of `gws auth export --unmasked`);
    gws reads it from the file named in GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE."""
    raw = os.getenv("GWS_CREDENTIALS_JSON")
    if not raw or os.getenv("GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"):
        return
    path = Path(tempfile.gettempdir()) / "gws_credentials.json"
    path.write_text(raw[raw.find("{"):], encoding="utf-8")
    path.chmod(0o600)
    os.environ["GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"] = str(path)


_credentials_from_env()


def check_auth() -> None:
    """Fail fast (before any scraping/LLM spend) if gws can't reach Google — a real, cheap API call."""
    try:
        gws("drive", "files", "list", params={"pageSize": 1, "fields": "files(id)"}, retries=1)
    except GwsError as e:
        raise GwsError("auth", f"Google login not working ({e}) — re-login: "
                               "tools/gws/gws.exe auth login --scopes https://www.googleapis.com/auth/drive.file,"
                               "https://www.googleapis.com/auth/gmail.send (tick both boxes), "
                               "then python scripts/push_modal_secrets.py") from e
