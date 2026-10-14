"""End-to-end run: scrape → hard filters → score → tailor → validate/render/deliver/log → summary.

Usage:
    python scripts/run_pipeline.py                      # full run (spends Apify credit + LLM calls)
    python scripts/run_pipeline.py --raw output/runs/<date>/raw_jobs.json   # reuse a scrape, no Apify spend
    python scripts/run_pipeline.py --no-tailor          # stop after scoring; prepare job folders for the
                                                        # Claude Code tailor-resume skill
    python scripts/run_pipeline.py --ignore-seen        # re-process jobs seen on earlier runs (testing)
    python scripts/run_pipeline.py --notify             # Windows notification (+ Telegram if set) for summary / failure
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import llm
from common import load_config, read_json, run_dir, setup_logging, today, write_json, job_folder_name
from filter_jobs import filter_jobs, mark_seen
from finalize_job import TooLong, ValidationFailed, finalize
from gws import GwsError
from score_jobs import score_jobs
from scrape_jobs import scrape
from tailor_job import tailor

log = logging.getLogger("pipeline")


def run(raw_path: Path | None = None, *, tailor_resumes: bool = True, ignore_seen: bool = False,
        sources: list[str] | None = None) -> dict:
    if os.getenv("JOB_AGENT_FORCE_FAIL"):
        raise RuntimeError("forced failure (JOB_AGENT_FORCE_FAIL is set) — testing the alert path")
    cfg = load_config()
    out = run_dir()
    warnings: list[str] = []

    # 0. preflight — fail before spending Apify credit / LLM calls if Google isn't usable
    import write_sheet
    from gws import check_auth
    from render_gdoc import template_id

    from gdrive import root_folder_id

    check_auth()
    template_id()
    root_folder_id()
    logged = write_sheet.existing_links()  # also proves the Sheet is reachable

    # 1. scrape
    raw = read_json(raw_path) if raw_path else scrape(cfg, only=sources)
    warnings += [f"source {k} failed: {v}" for k, v in raw.get("errors", {}).items()]
    if raw.get("errors") and not raw["jobs"]:
        raise RuntimeError(f"every source failed: {raw['errors']}")

    # 2. hard filters + dedupe (against earlier runs and the Sheet)
    filtered = filter_jobs(raw["jobs"], cfg, logged, ignore_seen=ignore_seen)
    write_json(out / "filtered.json", filtered)

    # 3. score
    scored = score_jobs(filtered["kept"], cfg)
    write_json(out / "scored.json", scored)
    if scored["errors"] and not scored["scored"] and filtered["kept"]:
        raise RuntimeError(f"LLM scoring failed for every job: {scored['errors'][0]}")
    warnings += scored["errors"][:3]
    mark_seen([{**j, "status": f"scored:{j['fit']['score']}"} for j in scored["scored"] if not j["fit"]["qualified"]])

    # 4-7. tailor → finalize, per qualifying job
    qualified = scored["qualified"][: cfg["llm"]["max_qualified_per_run"]]
    if len(scored["qualified"]) > len(qualified):
        warnings.append(f"{len(scored['qualified']) - len(qualified)} qualified job(s) deferred (max_qualified_per_run)")
    delivered, flagged, prepared = [], [], []
    for job in qualified:
        job = {**job, "run_date": out.name}  # Desktop folder groups jobs by scrape date
        folder = out / "jobs" / job_folder_name(job)
        folder.mkdir(parents=True, exist_ok=True)
        write_json(folder / "job.json", job)
        if not tailor_resumes:
            prepared.append(str(folder))
            continue
        try:
            for shorten in (False, True):  # one shorter retry if the PDF spills past max_pages
                md, notes = tailor(job, cfg, shorten=shorten)
                (folder / "resume.md").write_text(md, encoding="utf-8")
                if notes:
                    write_json(folder / "tailor_notes.json", notes)
                try:
                    delivered.append(finalize(folder, cfg))
                    break
                except TooLong as e:
                    if shorten:
                        raise
                    log.warning("%s — re-tailoring shorter", e)
        except llm.DailyLimitReached as e:
            warnings.append(f"OpenRouter daily limit reached — {len(qualified) - len(delivered)} job(s) not tailored")
            log.error("%s", e)
            break
        except GwsError as e:
            if e.kind in ("auth", "setup"):
                raise  # every later job would fail the same way — alert instead of flagging all
            log.error("FLAGGED %s @ %s: %s", job["title"], job["company"], e)
            flagged.append({"title": job["title"], "company": job["company"], "link": job["link"], "error": str(e)})
        except (ValidationFailed, TooLong, ValueError, llm.LLMError) as e:
            log.error("FLAGGED %s @ %s: %s", job["title"], job["company"], e)
            flagged.append({"title": job["title"], "company": job["company"], "link": job["link"], "error": str(e)})

    summary = {
        "date": today(),
        "funnel": {"scraped": len(raw["jobs"]), "after_filters": len(filtered["kept"]),
                   "scored": len(scored["scored"]), "qualified": len(scored["qualified"]),
                   "delivered": len(delivered)},
        "per_source": raw.get("per_source", {}),
        "reject_counts": filtered["reject_counts"],
        "delivered": delivered, "flagged": flagged, "prepared_for_claude": prepared,
        "warnings": warnings, "apify_usd": raw.get("apify_usd", 0.0), "llm_calls": llm.calls_made,
    }
    write_json(out / "summary.json", summary)
    f = summary["funnel"]
    log.info("FUNNEL: %d scraped → %d after hard filters → %d scored → %d qualified → %d delivered (%d flagged)",
             f["scraped"], f["after_filters"], f["scored"], f["qualified"], f["delivered"], len(flagged))
    if prepared:
        log.info("prepared %d job folder(s) for the tailor-resume skill under %s", len(prepared), out / "jobs")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the job application pipeline")
    ap.add_argument("--raw", type=Path, help="reuse a raw_jobs.json instead of scraping")
    ap.add_argument("--sources", nargs="+", help="only scrape these sources")
    ap.add_argument("--no-tailor", action="store_true", help="stop after scoring (Claude Code skill tailors)")
    ap.add_argument("--ignore-seen", action="store_true", help="re-process jobs seen on earlier runs")
    ap.add_argument("--notify", action="store_true", help="notify (Windows toast, + Telegram if configured) with summary or failure")
    args = ap.parse_args()
    setup_logging()
    import notify

    try:
        summary = run(args.raw, tailor_resumes=not args.no_tailor, ignore_seen=args.ignore_seen, sources=args.sources)
    except Exception as e:
        if args.notify:
            notify.failure_alert(e)
        raise
    if args.notify:
        notify.report(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
