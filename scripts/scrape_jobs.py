"""Step 3.1 — scrape fresh postings: LinkedIn + Naukri (Apify) + RemoteOK / Remotive / We Work Remotely.

Usage:
    python scripts/scrape_jobs.py                          # all enabled sources
    python scripts/scrape_jobs.py --sources remoteok wwr   # subset
    python scripts/scrape_jobs.py --dry-run                # Apify inputs + worst-case cost; free boards still run

Writes output/runs/<date>/raw_jobs.json (+ raw Apify payloads in output/runs/<date>/apify/).
Each job: {title, company, link, description, location, employment_type, posted_date,
           source, experience, salary, seniority}
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import feedparser
import requests

from common import ROOT, job_keys, load_config, run_dir, setup_logging, write_json

log = logging.getLogger("scrape")
UA = {"User-Agent": "Mozilla/5.0 (job-apply-agent; personal job search)"}


# ---------------------------------------------------------------- helpers


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<\s*(br|/p|/li|/div|/h\d)\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<\s*li[^>]*>", "\n- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def job(**kw) -> dict:
    base = dict(title="", company="", link="", description="", location="", employment_type="",
                posted_date="", source="", experience="", salary="", seniority="")
    base.update({k: (v.strip() if isinstance(v, str) else v) for k, v in kw.items() if v is not None})
    return base


def title_matches(title: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(title) for p in patterns)


def fresh(dt: datetime | None, max_age_days: int) -> bool:
    if dt is None:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= datetime.now(timezone.utc) - timedelta(days=max_age_days)


# ---------------------------------------------------------------- free boards


def scrape_remoteok(cfg: dict, patterns) -> list[dict]:
    data = requests.get("https://remoteok.com/api", headers=UA, timeout=60).json()
    out = []
    for it in data[1:]:  # data[0] is the legal notice
        if not title_matches(it.get("position", ""), patterns):
            continue
        posted = datetime.fromisoformat(it["date"]) if it.get("date") else None
        if not fresh(posted, cfg["max_age_days"]):
            continue
        lo, hi = int(it.get("salary_min") or 0), int(it.get("salary_max") or 0)
        out.append(job(
            title=it.get("position"), company=it.get("company"), link=it.get("url"),
            description=strip_html(it.get("description")), location=it.get("location") or "Remote",
            posted_date=it.get("date"), source="remoteok",
            salary=f"USD {lo:,}-{hi:,}" if hi else "",
        ))
    return out


def scrape_remotive(cfg: dict, patterns) -> list[dict]:
    # Remotive asks for few requests per day — one unfiltered call, filtered locally.
    data = requests.get("https://remotive.com/api/remote-jobs", headers=UA, timeout=60).json()
    out = []
    for it in data.get("jobs", []):
        if not title_matches(it.get("title", ""), patterns):
            continue
        posted = datetime.fromisoformat(it["publication_date"]) if it.get("publication_date") else None
        if not fresh(posted, cfg["max_age_days"]):
            continue
        out.append(job(
            title=it.get("title"), company=it.get("company_name"), link=it.get("url"),
            description=strip_html(it.get("description")),
            location=f"Remote ({it.get('candidate_required_location') or 'unspecified'})",
            employment_type=(it.get("job_type") or "").replace("_", "-"),
            posted_date=it.get("publication_date"), source="remotive", salary=it.get("salary") or "",
        ))
    return out


def scrape_weworkremotely(cfg: dict, patterns) -> list[dict]:
    out = []
    for url in cfg["feeds"]:
        resp = requests.get(url, headers=UA, timeout=60)
        resp.raise_for_status()
        for e in feedparser.parse(resp.content).entries:
            company, _, title = (e.get("title") or "").partition(": ")
            if not title:
                company, title = "", company
            if not title_matches(title, patterns):
                continue
            posted = parsedate_to_datetime(e["published"]) if e.get("published") else None
            if not fresh(posted, cfg["max_age_days"]):
                continue
            out.append(job(
                title=title, company=company, link=e.get("link"),
                description=strip_html(e.get("summary")),
                location=f"Remote ({e.get('region') or 'unspecified'})",
                employment_type=e.get("type") or "",
                posted_date=posted.isoformat() if posted else "", source="weworkremotely",
            ))
    return out


# ---------------------------------------------------------------- Apify sources


SECRET_INPUT_KEYS = {"cookies", "userAgent"}  # never logged


def linkedin_auth() -> dict:
    """The actor scrapes as the user's logged-in LinkedIn account (Cookie-Editor JSON export + user agent)."""
    path = ROOT / (os.getenv("LINKEDIN_COOKIES_FILE") or "secrets/linkedin_cookies.json")
    ua = os.getenv("LINKEDIN_USER_AGENT")
    if not path.exists() or not ua:
        raise RuntimeError(f"LinkedIn cookies ({path}) or LINKEDIN_USER_AGENT missing — see README 'LinkedIn cookies'")
    cookies = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cookies, list) or not cookies:
        raise RuntimeError(f"{path} must be the JSON array exported by Cookie-Editor")
    return {"cookies": cookies, "userAgent": ua}


def linkedin_public_input(cfg: dict, roles: list[str], limit: int) -> dict:
    """Public (logged-out) actor: one run, one search URL per role x location."""
    urls = []
    for loc in cfg["locations"]:
        for role in roles:
            q = {"keywords": role, "location": loc["name"], "geoId": loc["geo_id"], **cfg["url_filters"]}
            urls.append("https://www.linkedin.com/jobs/search/?" + urlencode(q))
    return {"urls": urls, "limitPerSource": limit, "datePosted": cfg["date_posted"], "scrapeCompany": False}


def linkedin_inputs(cfg: dict, roles: list[str], count: int, with_auth: bool = True) -> list[dict]:
    """Cookies mode: one actor run per location (the actor takes a single search); roles are OR-ed."""
    auth = linkedin_auth() if with_auth else {}
    keywords = " OR ".join(f"({r})" for r in roles)
    inputs = []
    for loc in cfg["locations"]:
        q = {"keywords": keywords, "location": loc["name"], "geoId": loc["geo_id"], **cfg["url_filters"]}
        inputs.append({**auth, "searchUrl": "https://www.linkedin.com/jobs/search/?" + urlencode(q), "count": count,
                       "scrapeJobDetails": False, "scrapeSkills": False, "scrapeCompany": False})
    return inputs


def normalize_linkedin(it: dict) -> dict:
    loc = it.get("location") or ""
    if isinstance(loc, dict):
        loc = loc.get("defaultLocalizedName") or loc.get("abbreviatedLocalizedName") or ""
    # "India (Remote)" — filter_jobs reads remote/on-site/hybrid from the location text.
    workplace = ", ".join(w.get("localizedName", "") for w in it.get("jobWorkplaceTypes") or [] if isinstance(w, dict))
    salary = it.get("salary") or ""
    if isinstance(salary, dict):
        salary = salary.get("formattedBaseSalary") or ""
    link = (it.get("link") or "").split("?")[0]
    job_id = it.get("id") or (re.search(r"(\d{6,})/?$", link) or [None, None])[1]
    return job(
        title=it.get("title"), company=it.get("companyName") or (it.get("company") or {}).get("name"),
        # Country subdomains (in./de./ca.) point at the same job; canonicalize for dedupe.
        link=f"https://www.linkedin.com/jobs/view/{job_id}" if job_id else link,
        description=it.get("descriptionText") or strip_html(it.get("descriptionHtml")),
        location=f"{loc} ({workplace})" if workplace else loc, employment_type=it.get("employmentType") or "",
        posted_date=it.get("postedAt") or "", source="linkedin", salary=str(salary),
        seniority=it.get("seniorityLevel") or "",
    )


def naukri_input(cfg: dict, roles: list[str]) -> dict:
    return {"jobBoard": "naukri", "keyword": ", ".join(roles), "maxJobs": cfg["max_jobs"],
            "fetchDetails": cfg["fetch_details"], "freshness": cfg["freshness"],
            "experience": cfg["experience"], "workMode": cfg["work_mode"], "sortBy": cfg["sort_by"]}


def normalize_naukri(it: dict) -> dict:
    j = it.get("jobDetails") or it
    sal = j.get("salaryDetail") or {}
    wfh = j.get("wfhLabel") or ""
    locs = ", ".join(l.get("label", "") for l in j.get("locations") or [])
    return job(
        title=j.get("title"), company=(j.get("companyDetail") or {}).get("name") or j.get("companyName"),
        link=j.get("staticUrl") or f"https://www.naukri.com/job-listings-{j.get('jobId')}",
        description=strip_html(j.get("description") or j.get("shortDescription")),
        location=f"{locs} ({wfh})" if wfh else locs, employment_type=j.get("employmentType") or "",
        posted_date=j.get("createdDate") or "", source="naukri", experience=j.get("experienceText") or "",
        salary="" if sal.get("hideSalary") else f"{sal.get('label', '')} {sal.get('currency', '')}".strip(),
    )


def run_apify(name: str, cfg: dict, roles: list[str], remaining: float, dry_run: bool, out_dir) -> tuple[list[dict], float]:
    """Returns (normalized jobs, estimated spend). Shrinks/skips to fit `remaining` budget."""
    if name == "linkedin" and cfg.get("mode", "public") == "public":
        n_urls = len(cfg["locations"]) * len(roles)
        limit = cfg["limit_per_search"]
        worst = lambda lim: cfg["price_start_usd"] + cfg["price_item_usd"] * lim * n_urls
        if worst(limit) > remaining:
            limit = int((remaining - cfg["price_start_usd"]) / (cfg["price_item_usd"] * n_urls))
            if limit < 1:
                log.warning("linkedin: SKIPPED — only $%.3f budget left", remaining)
                return [], 0.0
            log.warning("linkedin: limit_per_search → %d to fit daily budget", limit)
        inputs, cap, normalize = [linkedin_public_input(cfg, roles, limit)], worst(limit), normalize_linkedin
    elif name == "linkedin":
        n_runs = len(cfg["locations"])
        count = cfg["count_per_search"]
        worst = lambda c: n_runs * (cfg["price_start_usd"] + cfg["price_item_usd"] * c)
        if worst(count) > remaining:
            count = int((remaining / n_runs - cfg["price_start_usd"]) / cfg["price_item_usd"])
            if count < 1:
                log.warning("linkedin: SKIPPED — only $%.3f budget left", remaining)
                return [], 0.0
            log.warning("linkedin: count_per_search → %d to fit daily budget", count)
        inputs, cap, normalize = linkedin_inputs(cfg, roles, count, with_auth=not dry_run), worst(count), normalize_linkedin
    else:
        inputs, cap, normalize = [naukri_input(cfg, roles)], cfg["max_charge_usd"], normalize_naukri
        if cap > remaining:
            log.warning("%s: SKIPPED — needs $%.2f cap, $%.3f left", name, cap, remaining)
            return [], 0.0

    if dry_run:
        for inp in inputs:
            log.info("[dry-run] %s: %s", name, {k: (f"{len(v)} search URLs" if k == "urls" else v)
                                                 for k, v in inp.items() if k not in SECRET_INPUT_KEYS})
        log.info("[dry-run] %s: %d run(s), worst case $%.3f", name, len(inputs), cap)
        return [], cap

    from apify_client import ApifyClient

    client = ApifyClient(os.environ["APIFY_API_TOKEN"])
    items, run_ids = [], []
    for inp in inputs:
        run = client.actor(cfg["actor"]).call(run_input=inp, max_total_charge_usd=Decimal(f"{cap / len(inputs):.4f}"),
                                              logger=None)
        if run is None or run.status != "SUCCEEDED":
            raise RuntimeError(f"{name}: Apify run ended with status {getattr(run, 'status', None)}")
        items += client.dataset(run.default_dataset_id).list_items().items
        run_ids.append(run.id)
    write_json(out_dir / "apify" / f"{name}.json", items)
    spent = len(inputs) * cfg["price_start_usd"] + cfg["price_item_usd"] * len(items)
    log.info("%s: %d items (Apify runs %s, ~$%.3f)", name, len(items), ", ".join(run_ids), spent)
    return [normalize(it) for it in items], spent


FREE = {"remoteok": scrape_remoteok, "remotive": scrape_remotive, "weworkremotely": scrape_weworkremotely}


# ---------------------------------------------------------------- main


def dedupe(jobs: list[dict]) -> list[dict]:
    seen, out = set(), []
    for j in jobs:
        keys = job_keys(j)
        if not j["title"] or not j["link"] or seen & set(keys):
            continue
        seen.update(keys)
        out.append(j)
    return out


def scrape(config: dict, only: list[str] | None = None, dry_run: bool = False) -> dict:
    out_dir = run_dir()
    patterns = [re.compile(p, re.I) for p in config["title_keywords"]]
    budget, spent = float(config["daily_budget_usd"]), 0.0
    jobs, per_source, errors = [], {}, {}

    for name, scfg in config["sources"].items():
        if (only and name not in only) or not scfg.get("enabled", True):
            continue
        try:
            if scfg["kind"] == "apify":
                found, cost = run_apify(name, scfg, config["roles"], budget - spent, dry_run, out_dir)
                spent += cost
            else:
                found = FREE[name](scfg, patterns)
                log.info("%s: %d relevant jobs", name, len(found))
        except Exception as e:  # one broken source shouldn't kill the run; reported in the summary
            log.exception("%s: FAILED", name)
            errors[name] = f"{type(e).__name__}: {e}"
            continue
        per_source[name] = len(found)
        jobs.extend(found)

    unique = dedupe(jobs)
    log.info("scraped %d → %d after dedupe (Apify ~$%.3f%s)", len(jobs), len(unique), spent,
             " worst case" if dry_run else "")
    result = {"per_source": per_source, "errors": errors, "apify_usd": round(spent, 4), "jobs": unique}
    if not dry_run:
        write_json(out_dir / "raw_jobs.json", result)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape job postings")
    ap.add_argument("--sources", nargs="+", help="limit to these source names from config")
    ap.add_argument("--dry-run", action="store_true", help="no Apify calls (free boards still run)")
    args = ap.parse_args()
    setup_logging()
    res = scrape(load_config(), only=args.sources, dry_run=args.dry_run)
    return 1 if res["errors"] and not res["jobs"] else 0


if __name__ == "__main__":
    sys.exit(main())
