"""Local half of the daily routine (task "JobApplyAgent": at logon and 08:00). No scraping, no LLM.

1. Cloud status — reads state/last_run.json from the Modal volume; a failed (or missing) 07:00 run
   becomes a Windows notification, since the cloud can't show pop-ups on this PC.
2. Google login — warns a day before the 7-day Testing-mode expiry, and alerts once it has expired.
3. Resumes — for every Sheet row with a Drive link but no "Desktop PDF" yet:
       Drive PDF → Desktop/Job Applications/<date added>/<Company - Job Title>/Srikanth Valluri Resume.pdf
   then writes that path into the row and shows a notification. Safe to run any number of times.

Usage:
    python scripts/sync_desktop.py
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

from common import DATA_DIR, IST, ROOT, STATE_DIR, desktop_dir, load_config, read_json, setup_logging, today
from finalize_job import windows_name
from gws import GwsError, check_auth, gws
from notify import ALERT_TITLE, toast
from write_sheet import COLUMNS, sheet_id

log = logging.getLogger("sync")

MODAL = ROOT / ".venv" / "Scripts" / "modal.exe"
VOLUME = "job-apply-agent-data"
LOGIN_FILE = STATE_DIR / "google_login.json"  # written by push_modal_secrets.py after each re-login
LOGIN_DAYS, WARN_AFTER_DAYS = 7, 6
RELOGIN_HINT = "Ask Claude to 're-login Google' (one browser tab — tick both boxes: Drive + send email)."


def _letter(name: str) -> str:
    return chr(ord("A") + COLUMNS.index(name))


def _log_file():
    p = DATA_DIR / "logs" / f"{today()}-sync.log"
    return p if p.exists() else None


def check_cloud_run() -> None:
    """Alert if today's 07:00 Modal run failed or hasn't happened (after 08:00)."""
    local = STATE_DIR / "cloud_last_run.json"
    proc = subprocess.run([str(MODAL), "volume", "get", VOLUME, "state/last_run.json", str(local), "--force"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONWARNINGS": "ignore"})
    run = read_json(local) if proc.returncode == 0 else None
    now = datetime.now(IST)
    if run and run.get("date") == today():
        if run.get("status") == "failed":
            err = run.get("error", "unknown error")
            hint = RELOGIN_HINT if re.search(r"auth|login|credential|token", err, re.I) else "Ask Claude to check it."
            toast(f"{ALERT_TITLE} (cloud run {run.get('started_at', '')[11:16]})", f"{err}\n{hint}", _log_file())
            log.error("cloud run failed today: %s", err)
        else:
            log.info("cloud run OK today: %s", run.get("funnel"))
    elif run and now.hour >= 8:
        toast(f"{ALERT_TITLE}", f"Today's 07:00 cloud run didn't happen (last run: {run.get('date')}).\n"
                                "Ask Claude to check Modal.", _log_file())
        log.error("no cloud run today; last run %s", run.get("date"))
    elif not run:  # no status recorded yet (before the first cloud run) or Modal unreachable
        log.warning("no cloud run status available: %s", proc.stderr.strip()[-200:])


def check_login_age() -> None:
    info = read_json(LOGIN_FILE)
    if not info:
        return
    age = (datetime.now(IST) - datetime.fromisoformat(info["logged_in_at"])).total_seconds() / 86400
    if WARN_AFTER_DAYS <= age < LOGIN_DAYS:
        toast("Job agent: Google login expires within a day",
              f"Logged in {age:.1f} days ago; Testing-mode logins last 7 days.\n{RELOGIN_HINT}")


def sync_resumes(cfg: dict) -> int:
    res = gws("sheets", "spreadsheets", "values", "get",
              params={"spreadsheetId": sheet_id(), "range": f"A2:{chr(ord('A') + len(COLUMNS) - 1)}"}) or {}
    root = desktop_dir() / cfg["output"]["desktop_folder"]
    updates, new_folders = [], []
    for row_no, r in enumerate(res.get("values", []), start=2):
        r = r + [""] * (len(COLUMNS) - len(r))
        get = lambda c: r[COLUMNS.index(c)].strip()
        file_id = re.search(r"/d/([A-Za-z0-9_-]+)", get("Resume (Drive)"))
        if not file_id or get("Desktop PDF"):
            continue
        day = (get("Added") or "undated")[:10]
        dest_dir = root / day / windows_name(f"{get('Company')} - {get('Job Title')}")
        dest = dest_dir / f"{cfg['output']['resume_filename']}.pdf"
        tmp = DATA_DIR / "sync" / f"{file_id.group(1)}.pdf"
        gws("drive", "files", "get", params={"fileId": file_id.group(1), "alt": "media"}, output=tmp)
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), dest)
        updates.append({"range": f"{_letter('Desktop PDF')}{row_no}", "values": [[str(dest)]]})
        new_folders.append(dest_dir)
        log.info("row %d → %s", row_no, dest)

    if updates:
        gws("sheets", "spreadsheets", "values", "batchUpdate", params={"spreadsheetId": sheet_id()},
            body={"valueInputOption": "RAW", "data": updates})
        n = len(updates)
        toast(f"Job agent: {n} resume{'s' if n != 1 else ''} on your Desktop",
              "\n".join(f.name for f in new_folders[:3]), new_folders[-1].parent)
    log.info("synced %d new resume(s) to %s", len(updates), root)
    return len(updates)


def main() -> int:
    setup_logging()
    cfg = load_config()
    check_cloud_run()
    check_login_age()
    try:
        check_auth()
    except GwsError as e:
        toast(f"{ALERT_TITLE}: Google login expired", f"Resumes can't be made or synced.\n{RELOGIN_HINT}", _log_file())
        log.error("%s", e)
        return 1
    sync_resumes(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
