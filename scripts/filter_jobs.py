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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from common import (STATE_DIR, job_keys, legacy_pair_key, load_config, parse_dt, read_json, run_dir, setup_logging,
                    today, write_json)
from pay import parse_salary, pay_problem

log = logging.getLogger("filter")

SEEN_PATH = STATE_DIR / "seen_jobs.json"
SEEN_TTL_DAYS = 60

# In a description, words must clearly be about where you can work from ("global"/"international" appear
# in almost every company blurb). Location fields use INDIA_PLACE / WORLD below.
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
# Strict remote (user's rule): remote-only boards, or "remote" in the structured title/location.
REMOTE_ONLY_BOARDS = {"remoteok", "remotive", "weworkremotely", "himalayas"}
# Work-arrangement "hybrid" only — AI postings say "hybrid search/retrieval/cloud/models" and "a hybrid
# role combining research and engineering" (duties) all the time; "this role is hybrid" is an arrangement.
HYBRID_DESC = re.compile(r"\b(?:work(?:place|ing)?|work ?(?:mode|model|type|setup|arrangement)|location type|job type|"
                         r"arrangement|(?:this|the) (?:role|position|job))\s*(?:type|mode)?\s*(?:[:\-–]|is)?\s*hybrid\b|"
                         r"\bhybrid (?:work|working|schedule|arrangement|setup|office|policy|model of work)\b|"
                         r"\bhybrid\s*\(\s*\d|"
                         r"\b\d\s*(?:days?|x)\s*(?:a|per|/|each)\s*week\b[^.\n]{0,40}\b(?:office|on-?site)\b|"
                         r"\b\d\s*days?\s+(?:in|at|from)\s+(?:the |our )?(?:[A-Z][a-z]+ )?office\b|"
                         r"\b(?:on-?site|in[- ]office) (?:presence|attendance|work) (?:is )?(?:required|expected|mandatory)\b|"
                         r"\bmust (?:be able to )?(?:commute|come in(?:to)? (?:the|our) office|work from (?:the|our) office)\b", re.I)
# Extra arrangement sentences: "This is a hybrid role", "This is an in-office role", "Opportunity Type: Office".
OFFICE_DESC = re.compile(r"\b(?:this|it) is an? (?:hybrid|in[- ]office|on-?site|office[- ]based|work[- ]from[- ]office) "
                         r"(?:role|position|job|opportunity)\b|"
                         r"\b(?:opportunity|work(?:place)?|location|job) type\s*[:\-–]\s*(?:office|hybrid|on-?site|in[- ]office)\b|"
                         r"\bwork(?:ing)? from (?:the |our )?office\b|\bwfo\b", re.I)
# A role-level statement that the job IS remote — needed for jobs whose title/location don't say so (the
# public LinkedIn actor has no workplace field). "We offer remote flexibility" is deliberately not enough.
REMOTE_STATEMENT = re.compile(
    r"\b(?:fully|100\s?%|completely|permanently)[- ]remote\b|\bremote[- ](?:first|only)\b|"
    r"\b(?:this|the) (?:role|position|job|opportunity) is (?:a )?(?:fully )?remote\b|"
    r"\b(?:this|it) is an? (?:fully )?remote (?:role|position|job|opportunity)\b|"
    r"\b(?:location|work location|job location|opportunity type|work(?:place)? (?:type|mode|model|arrangement|setup)|"
    r"job type|employment mode|mode of work)\s*[:\-–]\s*(?:[\w ]{0,20}[,/(-]\s*)?(?:fully )?remote\b|"
    r"\bremote\s*[-–,(/]\s*(?:india|anywhere|worldwide|global|apac)\b|\b(?:india|anywhere|worldwide)\s*[-–,(/]\s*remote\b|"
    r"\b(?:work|working) (?:from home|remotely|fully remotely)\b|\bwork[- ]from[- ]home\b|\bwfh\b|"
    r"\bremote (?:role|position|job|opportunity)\b", re.I)
# Seniority stated in the description or as a level suffix in the title.
SENIOR_DESC = re.compile(r"\bstaff ?/ ?senior\b|\bsenior ?/ ?staff\b|\bmid(?:dle)?\+? to senior\b|"
                         r"\bsenior[- ]level (?:role|position|engineer|candidate)\b|"
                         r"\bwe(?:'re| are) (?:looking|hiring) for an? (?:staff|senior|lead|principal)\b", re.I)
SENIOR_LEVEL_TITLE = re.compile(r"(?:\b|_)(?:III|IV|VI?)\b")
# Only role-level statements override the above ("remote-first company" doesn't make a hybrid role remote).
FULLY_REMOTE = re.compile(r"\bfully[- ]remote\b|\b100\s?% remote\b|\bremote[- ]only\b|\bfully distributed\b|"
                          r"\bwork from anywhere\b", re.I)
# A place named in a location string that is India (cities/states without the word "India" included) or APAC.
INDIA_PLACE = re.compile(
    r"\b(india|indian|bharat|apac|asia|asia[- ]pacific|south asia|bangalore|bengaluru|hyderabad|secunderabad|"
    r"mumbai|navi mumbai|thane|pune|chennai|delhi|new delhi|ncr|gurgaon|gurugram|noida|ghaziabad|faridabad|"
    r"kolkata|ahmedabad|gandhinagar|kochi|cochin|trivandrum|thiruvananthapuram|jaipur|chandigarh|mohali|indore|"
    r"bhopal|coimbatore|madurai|nagpur|nashik|visakhapatnam|vizag|vijayawada|bhubaneswar|lucknow|kanpur|mysore|"
    r"mysuru|mangalore|mangaluru|surat|vadodara|patna|guwahati|dehradun|goa|telangana|karnataka|maharashtra|"
    r"tamil nadu|kerala|gujarat|andhra pradesh|west bengal|haryana|uttar pradesh|rajasthan|odisha|punjab|"
    r"madhya pradesh|bihar|assam|uttarakhand)\b", re.I)
WORLD = re.compile(r"\b(anywhere in the world|worldwide|world ?wide|work from anywhere|global(?:ly)?|international|"
                   r"any (?:country|location)|world)\b", re.I)
# Words that say nothing about WHERE (stripped before the location allowlist check).
LOC_NOISE = re.compile(r"\b(remote|remotely|work from home|wfh|telecommute|telework|unspecified|not specified|"
                       r"full[- ]?time|permanent|contract|anywhere|location|locations|and|or|the|in|of|"
                       r"only|open to|candidates|based|named|by|employer)\b|[()\[\],;:/|&+\-–—.!🌍🌎🌏]", re.I)
NOT_FULL_TIME = re.compile(r"\bpart[- ]?time\b|\bcontract(?:or|ual)?\b|\bfreelance|\btemporary\b|\btemp\b|"
                           r"\bintern(?:ship)?\b|\bvolunteer|\bper[- ]task\b|\bgig\b|\bhourly\b", re.I)
HOURLY_PAY = re.compile(r"[$€£]\s?\d+(?:\.\d+)?\s*(?:/|per)\s*(?:hr|hour|task)\b", re.I)
ONSITE = re.compile(r"\bon[- ]?site\b|\bin[- ]office\b|\bwork from office\b|\bhybrid\b", re.I)
# Remote offered as an alternative ("Remote (Worldwide) or On-site London") is still a remote job.
REMOTE_OPTION = re.compile(r"\bremote\b[^|;]{0,30}\b(?:or|/|&)\s*(?:on-?site|hybrid|in[- ]office)\b|"
                           r"\b(?:on-?site|hybrid|in[- ]office)\b[^|;]{0,30}\b(?:or|/|&)\s*remote\b", re.I)
# "0.5–3 years" starts at 0 (not 5): never start matching mid-number, and swallow the decimal part.
YEARS = re.compile(r"(?<![\d.])(\d{1,2})(?:\.\d+)?\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(?:\d{1,2}(?:\.\d+)?)?\s*\+?\s*"
                   r"(?:years?|yrs?)\b(?:[^.\n]{0,60}?\bexperience\b)?", re.I)
UP_TO = re.compile(r"(?:up ?to|upto|max(?:imum)?(?: of)?|less than|under|below|within|no more than)\s*$", re.I)
INR = re.compile(r"₹|\binr\b|\blpa\b|\blakhs?\b|\blacs?\b|\bcrores?\b|\brupees?\b", re.I)


def required_years(text: str) -> int | None:
    """Largest 'N years ... experience' minimum in the text (ignores >15: company age etc.)."""
    found = []
    for m in YEARS.finditer(text):
        if UP_TO.search(text[max(0, m.start() - 20): m.start()]):
            continue  # "up to 3 years" is a ceiling, not a requirement
        window = text[max(0, m.start() - 40): m.end() + 60].lower()
        if re.search(r"experience|\bexp\b", window):
            n = int(m.group(1))
            if n <= 15:
                found.append(n)
    return max(found) if found else None


def location_open_to_india(loc: str) -> bool:
    """Allowlist: a location passes if it names an Indian place / APAC / Asia, or if nothing is left after
    removing worldwide/global words and filler ("Remote", "Remote (Anywhere in the World)"). Any other
    specific place fails: Italy, "Remote - US", "Anywhere in US", EMEA, 東京 — a fixed list of foreign
    countries kept missing some (Italy). When remote is one of several options ("Remote (Worldwide) or
    On-site London"), only the remote option's place counts."""
    parts = re.split(r"\s+or\s+|\s*/\s*|;", loc)
    remote_parts = [p for p in parts if re.search(r"\bremote\b|anywhere|worldwide|global", p, re.I)]
    for part in remote_parts or [loc]:
        if INDIA_PLACE.search(part):
            return True
        residual = LOC_NOISE.sub(" ", WORLD.sub(" ", part))
        if not re.search(r"[^\W\d_]{2,}", residual):  # any script, so non-Latin place names count too
            return True
    return False


def reject_reason(job: dict, cfg: dict) -> str | None:
    title, loc, desc = job["title"], job["location"], job["description"]
    hf = cfg["hard_filters"]
    head = f"{title} {loc}"

    # posted within the last max_posting_age_days (user's rule: 15). Every source supplies a posting date;
    # a job whose date is missing/unreadable can't be shown to be recent, so it is rejected too.
    max_age = hf.get("max_posting_age_days")
    if max_age:
        posted = parse_dt(job.get("posted_date"))
        if posted is None:
            return "no_post_date"
        if posted < datetime.now(timezone.utc) - timedelta(days=max_age, hours=12):  # +12h: date-only values
            return "too_old"

    # not remote (strict): remote-only board, or "remote" in the title/location itself — a description that
    # merely mentions "remote" somewhere is not enough. Any hybrid/on-site wording in title/location rejects,
    # India included; hybrid/office-days in the description reject unless it also says fully remote.
    is_remote = (job["source"] in REMOTE_ONLY_BOARDS or re.search(r"\bremote\b", head, re.I)
                 or REMOTE_STATEMENT.search(desc))
    if not is_remote or (ONSITE.search(head) and not REMOTE_OPTION.search(head)):
        return "not_remote"
    if (HYBRID_DESC.search(desc) or OFFICE_DESC.search(desc)) and not FULLY_REMOTE.search(f"{head} {desc}"):
        return "not_remote"

    # not full-time: any contract/part-time/intern marker counts, even next to "full-time" ("Full-time
    # contractor"), and so does pay quoted per hour.
    if NOT_FULL_TIME.search(job["employment_type"]) or NOT_FULL_TIME.search(title) or HOURLY_PAY.search(desc):
        return "not_full_time"
    if parse_salary(job.get("salary")).period == "hour":
        return "not_full_time"

    # too senior
    words = "|".join(map(re.escape, hf["senior_title_words"]))
    if re.search(rf"\b(?:{words})\b\.?", title, re.I) or SENIOR_LEVEL_TITLE.search(title) or SENIOR_DESC.search(desc):
        return "too_senior"
    if re.search(r"mid-senior|director|executive", job.get("seniority", ""), re.I):
        return "too_senior"
    yrs = required_years(f"{job.get('experience', '')} {desc}")
    if yrs is not None and yrs > hf["max_years_required"]:
        return "too_senior"

    # location-locked outside India. A lock stated in the description ("must be located in the US") wins
    # unless the description itself opens it to India/worldwide — a board's generic "Anywhere" region
    # label doesn't override what the employer wrote.
    if LOCKED.search(desc) and not INDIA_OK_DESC.search(desc):
        return "location_locked"
    if not location_open_to_india(loc):
        return "location_locked"
    if (FOREIGN_LOC.search(loc) or US_STATE_LOC.search(loc)) and not (INDIA_PLACE.search(loc) or WORLD.search(loc)):
        return "location_locked"

    # pay (user's rules): stated pay must be an allowed currency and >= min_annual_usd; INR anywhere in
    # the posting (₹, LPA, lakh, crore) means an Indian payroll.
    problem = pay_problem(job.get("salary", ""), cfg["pay"])
    if problem:
        return problem.split(":")[0]
    if "INR" not in cfg["pay"]["currencies"] and INR.search(desc):
        return "pay_currency"
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
        if not ignore_seen and (link in seen or pair in seen or legacy_pair_key(j) in seen or link in seen_links):
            counts["already_seen"] += 1
            continue
        reason = reject_reason(j, cfg)
        if reason:
            counts[reason] += 1
            rejected.append({**j, "status": f"rejected:{reason}"})
        else:
            kept.append(j)
    # A missing posting date can be a transient fetch problem: don't remember those, so they get retried.
    mark_seen([r for r in rejected if r["status"] != "rejected:no_post_date"])
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
