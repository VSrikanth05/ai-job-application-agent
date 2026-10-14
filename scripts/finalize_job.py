"""Finish one job folder after its resume.md exists (automated and Claude Code paths):
validate → PDF via Google Docs template → company research → Desktop + Drive copy → Sheet row.

Desktop layout (PDF only):
    Desktop/Job Applications/<scrape date>/<Company - Job Title>/Srikanth Valluri Resume.pdf

Usage:
    python scripts/finalize_job.py output/runs/<date>/jobs/<folder> [--no-research]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
from pathlib import Path

from common import base_resume_text, desktop_dir, load_config, read_json, setup_logging, today, write_json
from filter_jobs import mark_seen
from validate_resume import validate

log = logging.getLogger("finalize")


class ValidationFailed(Exception):
    pass


class TooLong(Exception):
    """Rendered PDF exceeds output.max_pages — caller may re-tailor shorter."""

    def __init__(self, pages: int, max_pages: int):
        super().__init__(f"resume rendered to {pages} pages (max {max_pages})")
        self.pages, self.max_pages = pages, max_pages


def windows_name(text: str, maxlen: int = 80) -> str:
    """A readable folder name that's legal on Windows."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", text)
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    return s[:maxlen].rstrip(". ") or "Job"


def desktop_job_dir(job: dict, cfg: dict) -> Path:
    run_date = job.get("run_date") or today()
    name = windows_name(f"{job['company']} - {job['title']}")
    return desktop_dir() / cfg["output"]["desktop_folder"] / run_date / name


def finalize(folder: Path, cfg: dict, *, do_research: bool = True) -> dict:
    from render_gdoc import render_pdf
    import write_sheet

    job = read_json(folder / "job.json")
    md = (folder / "resume.md").read_text(encoding="utf-8")
    errors = validate(md, base_resume_text())
    if errors:
        write_json(folder / "validation_errors.json", errors)
        raise ValidationFailed("; ".join(errors))

    staging_pdf = folder / "resume.pdf"
    pages = render_pdf(md, staging_pdf, copy_name=folder.name)
    max_pages = cfg["output"]["max_pages"]
    if pages > max_pages:
        raise TooLong(pages, max_pages)

    research = read_json(folder / "research.json")
    if research is None and do_research:
        from company_research import research as run_research
        from llm import LLMError

        try:
            research = run_research(job)
            write_json(folder / "research.json", research)
        except LLMError as e:  # incl. DailyLimitReached: talking points are optional, the resume isn't
            log.warning("company research skipped (resume still delivered): %s", e)

    # Cloud (Modal) can't reach the Desktop: Drive is the only delivery there, and
    # scripts/sync_desktop.py copies the PDF to the Desktop when the user logs in.
    cloud = os.getenv("JOB_AGENT_DELIVERY") == "cloud"
    dest_dir = desktop_job_dir(job, cfg)
    filename = f"{cfg['output']['resume_filename']}.pdf"
    dest_pdf = "" if cloud else dest_dir / filename
    if not cloud:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staging_pdf, dest_pdf)

    # Same layout in Drive so the Sheet has a link that opens on any device.
    from gdrive import upload_pdf
    from gws import GwsError

    try:
        drive_link = upload_pdf(staging_pdf, [dest_dir.parent.name, dest_dir.name], filename)
    except GwsError as e:
        if cloud or e.kind in ("auth", "setup"):
            raise  # in the cloud a failed upload means the resume went nowhere → flag the job
        drive_link = ""
        log.warning("Drive upload failed (Desktop copy is fine): %s", e)

    from ats import ats_match, summary
    from tailor_job import allowed_skills

    ats = ats_match(md, job, allowed_skills(base_resume_text()))
    write_json(folder / "ats.json", ats)
    write_sheet.append_rows([write_sheet.row_for(job, research, drive_link, str(dest_pdf), summary(ats))])
    mark_seen([job], "qualified")
    log.info("delivered %s @ %s → %s", job["title"], job["company"], dest_pdf or drive_link)
    return {"title": job["title"], "company": job["company"], "link": job["link"],
            "score": job.get("fit", {}).get("score"), "ats": ats["percent"], "resume": str(dest_pdf) or drive_link,
            "drive": drive_link}


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate, render, deliver and log one tailored job folder")
    ap.add_argument("folder", type=Path)
    ap.add_argument("--no-research", action="store_true")
    args = ap.parse_args()
    setup_logging()
    try:
        print(finalize(args.folder, load_config(), do_research=not args.no_research))
    except ValidationFailed as e:
        log.error("NOT delivered — validation failed: %s", e)
        return 1
    except TooLong as e:
        log.error("NOT delivered — %s; shorten the summary/bullets in resume.md and rerun", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
