"""Upload the Modal secret "job-apply-agent" from .env + the local gws login (values never printed).

Run after a Google re-login (records the login time for the expiry warning), or with
--keep-login-date after only changing .env keys:
    tools/gws/gws.exe auth login --scopes https://www.googleapis.com/auth/drive.file,https://www.googleapis.com/auth/gmail.send   (tick both boxes)
    .venv/Scripts/python scripts/push_modal_secrets.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
GWS = ROOT / "tools" / "gws" / "gws.exe"
MODAL = ROOT / ".venv" / "Scripts" / "modal.exe"
SECRET_NAME = "job-apply-agent"
REQUIRED = ["APIFY_API_TOKEN", "OPENROUTER_API_KEY", "GOOGLE_RESUME_TEMPLATE_ID", "GOOGLE_SHEET_ID",
            "GOOGLE_DRIVE_FOLDER_ID"]
OPTIONAL = ["OPENROUTER_MODEL", "OPENROUTER_SCORING_MODEL", "REPORT_EMAIL_TO", "NTFY_TOPIC", "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID"]
# Read by sync_desktop.py to warn a day before Google's 7-day Testing-mode login expiry.
LOGIN_FILE = ROOT / "output" / "state" / "google_login.json"


def gws_credentials() -> str:
    out = subprocess.run([str(GWS), "auth", "export", "--unmasked"], capture_output=True, text=True,
                         cwd=ROOT, check=True).stdout
    creds = json.loads(out[out.find("{"):])
    if not creds.get("refresh_token"):
        sys.exit("gws has no refresh token — run the gws auth login command in this file's docstring first.")
    return json.dumps(creds)


def main() -> int:
    env = dotenv_values(ROOT / ".env")
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        sys.exit(f".env is missing: {', '.join(missing)}")
    secret = {k: env[k] for k in REQUIRED + OPTIONAL if env.get(k)}
    secret["GWS_CREDENTIALS_JSON"] = gws_credentials()

    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(secret, f)
        proc = subprocess.run([str(MODAL), "secret", "create", SECRET_NAME, "--from-json", path, "--force"],
                              capture_output=True, text=True, cwd=ROOT, env={**os.environ, "PYTHONWARNINGS": "ignore"})
    finally:
        os.remove(path)
    if proc.returncode != 0:
        sys.exit(f"modal secret create failed: {proc.stderr.strip()[-500:]}")
    print(f"Modal secret '{SECRET_NAME}' updated with: {', '.join(sorted(secret))}")
    if "--keep-login-date" not in sys.argv:  # this push follows a fresh Google login
        LOGIN_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOGIN_FILE.write_text(json.dumps({"logged_in_at": datetime.now(timezone(timedelta(hours=5, minutes=30)))
                                          .isoformat(timespec="seconds")}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
