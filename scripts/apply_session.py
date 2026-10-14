"""Option B helper: work through "Not Applied" jobs from the tracker with Simplify.

Opens each job link in the Chrome profile that has the Simplify extension and shows which
tailored PDF to upload. After applying, set Status → Applied in the Sheet (or use --applied).

Usage:
    python scripts/apply_session.py                 # list Not Applied jobs, best fit first
    python scripts/apply_session.py --open 5        # open the top 5 job links + today's PDF folder
    python scripts/apply_session.py --applied 3 7   # mark Sheet rows 3 and 7 Applied (today's date)
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from common import today
from gws import gws
from write_sheet import COLUMNS, sheet_id

CHROME_PROFILE = os.getenv("SIMPLIFY_CHROME_PROFILE", "Profile 2")  # profile where Simplify is installed
CHROME_PATHS = [Path(os.path.expandvars(p)) for p in (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")]


def col(name: str) -> int:
    return COLUMNS.index(name)


def letter(idx: int) -> str:
    return chr(ord("A") + idx)


def rows() -> list[dict]:
    res = gws("sheets", "spreadsheets", "values", "get",
              params={"spreadsheetId": sheet_id(), "range": f"A2:{letter(len(COLUMNS) - 1)}"}) or {}
    out = []
    for i, r in enumerate(res.get("values", []), start=2):
        r = r + [""] * (len(COLUMNS) - len(r))
        if r[col("Job Link")]:
            out.append({"row": i, **{c: r[col(c)] for c in COLUMNS}})
    return out


def pending() -> list[dict]:
    def key(j):
        ats = re.match(r"(\d+)%", j["ATS Match"] or "")
        return (-int(j["Fit Score"] or 0), -int(ats.group(1)) if ats else 0)
    return sorted((j for j in rows() if j["Status"] in ("", "Not Applied")), key=key)


def open_in_chrome(urls: list[str]) -> None:
    chrome = next((p for p in CHROME_PATHS if p.exists()), None)
    if not chrome:
        sys.exit("Chrome not found — open the links manually.")
    subprocess.Popen([str(chrome), f"--profile-directory={CHROME_PROFILE}", *urls])


def mark_applied(row_numbers: list[int]) -> None:
    s, a = letter(col("Status")), letter(col("Applied On"))
    data = [{"range": f"{s}{n}:{a}{n}", "values": [["Applied", today()]]} for n in row_numbers]
    gws("sheets", "spreadsheets", "values", "batchUpdate", params={"spreadsheetId": sheet_id()},
        body={"valueInputOption": "USER_ENTERED", "data": data})
    print(f"Marked row(s) {', '.join(map(str, row_numbers))} as Applied ({today()}).")


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply to Not Applied jobs with Simplify")
    ap.add_argument("--open", type=int, metavar="N", help="open the top N job links in Chrome")
    ap.add_argument("--applied", type=int, nargs="+", metavar="ROW", help="mark these Sheet rows Applied")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    if args.applied:
        mark_applied(args.applied)
        return 0

    jobs = pending()
    if not jobs:
        print("No 'Not Applied' jobs in the tracker yet — the 8 AM run adds them.")
        return 0
    print(f"{len(jobs)} job(s) not applied yet (best fit first):\n")
    for j in jobs:
        print(f"  row {j['row']:>3} | {j['Fit Score']}/10 | ATS {(j['ATS Match'] or '-').split(' ')[0]:>4} | "
              f"{j['Job Title']} — {j['Company']}")
        print(f"            {j['Job Link']}\n            PDF: {j['Desktop PDF'] or j['Resume (Drive)']}")

    if args.open:
        batch = jobs[: args.open]
        open_in_chrome([j["Job Link"] for j in batch])
        folders = {str(Path(j["Desktop PDF"]).parent.parent) for j in batch if j["Desktop PDF"]}
        for f in folders:
            if Path(f).exists():
                os.startfile(f)  # noqa: S606 — Windows Explorer, to drag the PDFs into the application
        print(f"\nOpened {len(batch)} job(s) in Chrome ({CHROME_PROFILE}, where Simplify is installed).")
        print("For each: let Simplify autofill → upload that job's 'Srikanth Valluri Resume.pdf' → submit.")
        print("Then: python scripts/apply_session.py --applied " + " ".join(str(j["row"]) for j in batch))
    return 0


if __name__ == "__main__":
    sys.exit(main())
