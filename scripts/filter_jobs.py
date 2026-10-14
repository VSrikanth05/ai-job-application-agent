"""Step 3.2 — rule-based hard filters + cross-day dedupe (no LLM calls).

Usage:
    python scripts/filter_jobs.py [--raw output/runs/<date>/raw_jobs.json] [--ignore-seen]

Writes output/runs/<date>/filtered.json with kept jobs and per-rule reject counts.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from common import STATE_DIR, job_keys, load_config, read_json, run_dir, setup_logging, today, write_json

log = logging.getLogger("filter")

SEEN_PATH = STATE_DIR / "seen_jobs.json"
SEEN_TTL_DAYS = 60

# In a location field, broad words are fine; in a description they must clearly be about where you can work
# from ("global"/"international" appear in almost every company blurb).
INDIA_OK = re.compile(r"\b(india|indian|apac|asia|asia[- ]pacific|worldwide|anywhere|global(?:ly)?|"
                      r"international|any (?:country|location)|work from anywhere)\b", re.I)
INDIA_OK_DESC = re.compile(r"\b(india|apac|asia[- ]pacific|remote[- ]worldwide|worldwide remote|anywhere in the world|"
                           r"work from anywhere|from anywhere|globally remote|remote globally|fully distributed|"
                           r"any (?:country|time ?zone|location))\b", re.I)
LOCKED = re.compile(
    r"\b(?:us|u\.s\.|usa|united states|uk|eu|europe|canada|americas|latam|emea)[- ]only\b|"
    r"\bonly (?:open to )?(?:candidates )?(?:in|from|based in) (?:the )?(?:us|u\.s\.|usa|united states|uk|eu|europe|canada|latam)\b|"
    r"\b(?:must|need to|required to) (?:be )?(?:legally )?(?:authori[sz]ed|eligible) to work in (?:the )?"
    r"(?:us|u\.s\.|usa|united states|uk|united kingdom|eu|europe|canada)\b|"
    r"\bmust (?:be )?(?:located|based|reside|residing|live) in (?:the )?"
    r"(?:us|u\.s\.|usa|united states|uk|united kingdom|eu|europe|canada|latam|north america|[a-z]+ time ?zones?)\b|"
    r"\b(?:us|u\.s\.) citizens?(?:hip)? (?:only|required)\b|\bsecurity clearance\b|\bgreen card\b", re.I)
# A structured location naming only a non-India region (LinkedIn country, board region fields).
FOREIGN_LOC = re.compile(r"\b(united states|usa|u\.s\.|\bus\b|canada|united kingdom|\buk\b|europe|european union|"
                         r"americas|north america|latam|latin america|emea|germany|france|spain|netherlands|poland|"
                         r"australia|brazil|mexico|israel|ireland|portugal|sweden|singapore|japan)\b", re.I)
US_STATE_LOC = re.compile(r",\s*[A-Z]{2}\b")  # "Atlanta, GA" / "Provo, UT" (LinkedIn US locations)
NOT_FULL_TIME = re.compile(r"\bpart[- ]?time\b|\bcontract(?:or|ual)?\b|\bfreelance|\btemporary\b|\btemp\b|"
                           r"\bintern(?:ship)?\b|\bvolunteer|\bper[- ]task\b|\bgig\b|\bhourly\b", re.I)
HOURLY_PAY = re.compile(r"[$€£]\s?\d+(?:\.\d+)?\s*(?:/|per)\s*(?:hr|hour|task)\b", re.I)
ONSITE = re.compile(r"\bon[- ]?site\b|\bin[- ]office\b|\bwork from office\b|\bhybrid\b", re.I)
YEARS = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(?:\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b"
                   r"(?:[^.\n]{0,60}?\bexperience\b)?", re.I)
INR = re.compile(r"₹|\binr\b|\blpa\b|\blakhs?\b|\blacs?\b|\bcrore\b", re.I)


def required_years(text: str) -> int | None:
    """Largest 'N years ... experience' minimum in the text (ignores >15: company age etc.)."""
    found = []
    for m in YEARS.finditer(text):
        window = text[max(0, m.start() - 40): m.end() + 60].lower()
        if re.search(r"experience|\bexp\b", window):
            n = int(m.group(1))
            if n <= 15:
                found.append(n)
    return max(found) if found else None


def reject_reason(job: dict, cfg: dict) -> str | None:
    title, loc, desc = job["title"], job["location"], job["description"]
    hf = cfg["hard_filters"]
    head = f"{title} {loc}"

    # not remote
    remote_boards = {"remoteok", "remotive", "weworkremotely"}
    is_remote = job["source"] in remote_boards or re.search(r"\bremote\b", f"{head} {desc}", re.I)
    if not is_remote:
        return "not_remote"
    if ONSITE.search(head) and not re.search(r"\bremote\b", head, re.I) and not re.search(r"\bindia\b", loc, re.I):
        return "not_remote"

    # not full-time
    if NOT_FULL_TIME.search(job["employment_type"]) and not re.search(r"full[- ]?time", job["employment_type"], re.I):
        return "not_full_time"
    if NOT_FULL_TIME.search(title) or HOURLY_PAY.search(desc):
        return "not_full_time"

    # too senior
    words = "|".join(map(re.escape, hf["senior_title_words"]))
    if re.search(rf"\b(?:{words})\b\.?", title, re.I):
        return "too_senior"
    if re.search(r"mid-senior|director|executive", job.get("seniority", ""), re.I):
        return "too_senior"
    yrs = required_years(f"{job.get('experience', '')} {desc}")
    if yrs is not None and yrs > hf["max_years_required"]:
        return "too_senior"

    # location-locked outside India
    india_ok = INDIA_OK.search(loc) or INDIA_OK_DESC.search(desc)
    if LOCKED.search(desc) and not india_ok:
        return "location_locked"
    foreign = FOREIGN_LOC.search(loc) or US_STATE_LOC.search(loc)
    if foreign and not INDIA_OK.search(loc) and not india_ok:
        return "location_locked"

    # INR pay (user wants USD/EUR only)
    if "INR" not in cfg["allowed_currencies"] and INR.search(job.get("salary", "")):
        return "pay_inr"
    return None


# ---------------------------------------------------------------- seen store


def load_seen() -> dict:
    seen = read_json(SEEN_PATH, {})
    cutoff = (date.fromisoformat(today()) - timedelta(days=SEEN_TTL_DAYS)).isoformat()
    return {k: v for k, v in seen.items() if v.get("date", "") >= cutoff}


def mark_seen(jobs: list[dict], status: str | None = None) -> None:
    seen = load_seen()
    for j in jobs:
        for k in job_keys(j):
            if k and k != "|":
                seen[k] = {"date": today(), "status": status or j.get("status", "")}
    write_json(SEEN_PATH, seen)


def filter_jobs(jobs: list[dict], cfg: dict, extra_seen_links: set[str] | None = None,
                ignore_seen: bool = False) -> dict:
    seen = {} if ignore_seen else load_seen()
    seen_links = set(extra_seen_links or ())
    counts, kept, rejected = Counter(), [], []
    for j in jobs:
        link, pair = job_keys(j)
        if not ignore_seen and (link in seen or pair in seen or link in seen_links):
            counts["already_seen"] += 1
            continue
        reason = reject_reason(j, cfg)
        if reason:
            counts[reason] += 1
            rejected.append({**j, "status": f"rejected:{reason}"})
        else:
            kept.append(j)
    mark_seen(rejected)
    for rule, n in counts.most_common():
        log.info("  %-16s removed %d", rule, n)
    log.info("hard filters: %d → %d", len(jobs), len(kept))
    return {"input": len(jobs), "kept": kept, "reject_counts": dict(counts),
            "rejected": [{k: r[k] for k in ("title", "company", "link", "source", "status")} for r in rejected]}


def main() -> int:
    ap = argparse.ArgumentParser(description="Hard-filter scraped jobs")
    ap.add_argument("--raw", type=Path, help="raw_jobs.json (default: today's)")
    ap.add_argument("--ignore-seen", action="store_true", help="don't skip jobs seen on earlier runs")
    args = ap.parse_args()
    setup_logging()
    raw = read_json(args.raw or run_dir() / "raw_jobs.json")
    res = filter_jobs(raw["jobs"], load_config(), ignore_seen=args.ignore_seen)
    write_json(run_dir() / "filtered.json", res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
