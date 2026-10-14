"""Step 3.7 — log qualifying jobs to the Google Sheet through gws.

Rows are deduped by job link, so re-running the same day adds nothing new.
The Sheet carries what used to be in per-job notes (fit reason, missing skills,
talking points) because the Desktop job folders hold only the PDF.
"""

from __future__ import annotations

import json
import logging
import os

from common import job_keys, now_iso
from gws import GwsError, gws

log = logging.getLogger("sheet")

COLUMNS = ["Job Title", "Company", "Role Category", "Job Link", "Source", "Fit Score", "ATS Match", "Fit Reason",
           "India Eligible", "Pay Currency", "Top Missing Skills", "Talking Points", "Resume (Drive)",
           "Desktop PDF", "Status", "Applied On", "Notes", "Added"]
LINK_COL = COLUMNS.index("Job Link")
STATUS_COL = COLUMNS.index("Status")
# Status dropdown values → background colour (google_setup.format_sheet applies these)
STATUSES = {"Not Applied": "#FCE5CD", "Applied": "#B6D7A8", "Interview": "#CFE2F3", "Offer": "#93C47D",
            "Rejected": "#F4CCCC", "Skipped": "#EFEFEF"}
DEFAULT_STATUS = "Not Applied"


def sheet_id() -> str:
    sid = os.getenv("GOOGLE_SHEET_ID")
    if not sid:
        raise GwsError("setup", "GOOGLE_SHEET_ID is not set — run scripts/google_setup.py")
    return sid


def row_for(job: dict, research: dict | None, drive_link: str, desktop_pdf: str, ats: str = "") -> list[str]:
    fit = job.get("fit", {})
    points = (research or {}).get("talking_points") or []
    # "Applied On" and "Notes" stay empty for the user to fill in
    return [job["title"], job["company"], fit.get("role_category", ""), job["link"], job["source"],
            str(fit.get("score", "")), ats, fit.get("reason", ""), fit.get("india_eligible", ""),
            fit.get("pay_currency", ""), "; ".join((fit.get("missing_requirements") or [])[:4]),
            "\n".join(f"• {p}" for p in points), drive_link, desktop_pdf, DEFAULT_STATUS, "", "",
            now_iso()]


def _link_key(link: str) -> str:
    return job_keys({"link": link})[0]


def existing_links() -> set[str]:
    col = chr(ord("A") + LINK_COL)
    res = gws("sheets", "spreadsheets", "values", "get",
              params={"spreadsheetId": sheet_id(), "range": f"{col}2:{col}"}) or {}
    return {_link_key(r[0]) for r in res.get("values", []) if r and r[0]}


def append_rows(rows: list[list[str]]) -> int:
    """Appends rows whose job link isn't already logged; returns how many were written."""
    have = existing_links()
    new = [r for r in rows if _link_key(r[LINK_COL]) not in have]
    if new:
        # RAW so job text starting with "=" or "+" is never evaluated as a formula.
        # OVERWRITE fills the next empty rows, which already carry the Status dropdown/colours
        # applied by google_setup.format_sheet (rows 2-5000).
        gws("sheets", "spreadsheets", "values", "append",
            params={"spreadsheetId": sheet_id(), "range": "A1", "valueInputOption": "RAW",
                    "insertDataOption": "OVERWRITE"},
            body={"values": new})
        log.info("appended %d row(s) to the Google Sheet", len(new))
    return len(new)


if __name__ == "__main__":
    print(json.dumps(sorted(existing_links()), indent=1))
