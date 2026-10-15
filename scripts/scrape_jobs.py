"""Step 3.1 — scrape fresh postings: LinkedIn (Apify) + free sources: RemoteOK / Remotive / We Work Remotely /
Himalayas / ROLIVA (checked against each company's ATS) / Python.org jobs / HN "Who is hiring?".

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
import time
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


def _parse_dt(value) -> datetime | None:
    """ISO strings ('2026-10-14', '...Z', with offset) or epoch seconds/milliseconds → aware datetime."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            v = float(value)
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


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


def scrape_himalayas(cfg: dict, patterns) -> list[dict]:
    """Remote-only board. country=IN = India-restricted + worldwide jobs; one 20-result call per query."""
    skip = {s.lower() for s in cfg.get("skip_seniority", [])}
    out, seen, calls = [], set(), 0
    for q in cfg["queries"]:
        # Results are newest-first, 20 per page: keep paging until a page reaches past max_age_days
        # (busy queries like "AI engineer" post more than 20 a day), capped by max_pages.
        for page in range(1, cfg.get("max_pages", 3) + 1):
            if calls:
                time.sleep(1.5)  # unpublished rate limit; be gentle
            calls += 1
            resp = requests.get("https://himalayas.app/jobs/api/search", headers=UA, timeout=60, params={
                "q": q, "country": "IN", "employment_type": "Full Time", "sort": "recent", "page": page})
            resp.raise_for_status()
            items = resp.json().get("jobs", [])
            out += _himalayas_items(items, cfg, patterns, skip, seen)
            oldest = min((_parse_dt(it.get("pubDate")) for it in items if it.get("pubDate")), default=None)
            if len(items) < 20 or (oldest and not fresh(oldest, cfg["max_age_days"])):
                break
    return out


def _himalayas_items(items: list, cfg: dict, patterns, skip: set, seen: set) -> list[dict]:
    out = []
    for it in items:
        link = it.get("applicationLink") or it.get("guid")
        if not link or link in seen or not title_matches(it.get("title", ""), patterns):
            continue
        seniority = [s for s in it.get("seniority") or []]
        if seniority and all(s.lower() in skip for s in seniority):
            continue
        posted = _parse_dt(it.get("pubDate"))
        if not fresh(posted, cfg["max_age_days"]):
            continue
        seen.add(link)
        regions = it.get("locationRestrictions") or []
        lo, hi = it.get("minSalary"), it.get("maxSalary")
        salary = ""
        if lo or hi:
            salary = f"{it.get('currency') or ''} {lo or hi}-{hi or lo}/{it.get('salaryPeriod') or 'annual'}".strip()
        out.append(job(
            title=it.get("title"), company=it.get("companyName"), link=link,
            description=strip_html(it.get("description") or it.get("excerpt")),
            location=f"Remote ({', '.join(regions) if regions else 'Worldwide'})",
            employment_type=it.get("employmentType") or "", posted_date=posted.isoformat() if posted else "",
            source="himalayas", salary=salary, seniority=", ".join(seniority),
        ))
    return out


# ---- ROLIVA + company ATS lookups (Ashby / Greenhouse / Lever public job-board APIs, no auth)


def _ashby_detail(url: str, cache: dict) -> dict | None:
    m = re.search(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})", url)
    if not m:
        return None
    org, jid = m.groups()
    if org not in cache:
        r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}", headers=UA, timeout=60,
                         params={"includeCompensation": "true"})
        # A failed board fetch (429/5xx) must not look like "every job is closed": cache it as unknown.
        cache[org] = {j["id"]: j for j in r.json().get("jobs", [])} if r.ok else None
    if cache[org] is None:
        return None
    j = cache[org].get(jid)
    if not j:
        return {"closed": True}
    comp = j.get("compensation") or {}
    return {"remote": bool(j.get("isRemote")) or (j.get("workplaceType") or "").lower() == "remote",
            "description": j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")),
            "location": j.get("location") or "", "employment_type": j.get("employmentType") or "",
            "salary": comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary") or "",
            "link": j.get("jobUrl") or url, "posted": j.get("publishedAt") or ""}


def _greenhouse_detail(url: str, cache: dict) -> dict | None:
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([^/?#&]+)/jobs/(\d+)", url)
    if m:
        slug, jid = m.groups()
    else:
        # Greenhouse embedded on the company's own site: https://www.newrocket.com/careers/job?gh_jid=123.
        # The board slug isn't in the URL; it is usually the domain name ("newrocket", "databricks").
        jm = re.search(r"[?&]gh_jid=(\d+)", url)
        host = re.sub(r"^(?:www|careers|jobs|boards)\.", "", re.sub(r"^https?://", "", url).split("/")[0])
        if not jm or not host:
            return None
        slug, jid = host.split(".")[0], jm[1]
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}", headers=UA, timeout=60,
                     params={"pay_transparency": "true"})
    if r.status_code == 404:
        return {"closed": True} if m else None  # a guessed slug that 404s proves nothing about the job
    r.raise_for_status()
    j = r.json()
    loc = (j.get("location") or {}).get("name") or ""
    pay = next(iter(j.get("pay_input_ranges") or []), None)
    salary = ""
    if pay and pay.get("min_cents"):
        salary = f"{pay.get('currency_type') or ''} {pay['min_cents'] // 100}-{(pay.get('max_cents') or pay['min_cents']) // 100}/year"
    return {"remote": bool(re.search(r"\bremote\b", f"{loc} {j.get('title', '')}", re.I)),
            "description": strip_html(html.unescape(j.get("content") or "")), "location": loc,
            "employment_type": "", "salary": salary.strip(), "link": j.get("absolute_url") or url,
            "posted": j.get("first_published") or j.get("updated_at") or ""}


def _lever_detail(url: str, cache: dict) -> dict | None:
    m = re.search(r"jobs\.lever\.co/([^/?#]+)/([0-9a-f-]{36})", url)
    if not m:
        return None
    r = requests.get(f"https://api.lever.co/v0/postings/{m[1]}/{m[2]}", headers=UA, timeout=60)
    if r.status_code == 404:
        return {"closed": True}
    r.raise_for_status()
    j = r.json()
    cat = j.get("categories") or {}
    lists = "\n".join(f"{l.get('text', '')}\n{strip_html(l.get('content'))}" for l in j.get("lists") or [])
    sal = j.get("salaryRange") or {}
    salary = ""
    if sal.get("min"):
        period = {"per-year-salary": "year", "per-month-salary": "month", "per-hour-wage": "hour"}.get(sal.get("interval"), "year")
        salary = f"{sal.get('currency') or ''} {sal['min']}-{sal.get('max') or sal['min']}/{period}"
    created = datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc).isoformat() if j.get("createdAt") else ""
    return {"remote": (j.get("workplaceType") or "").lower() == "remote",
            "description": "\n\n".join(filter(None, [j.get("descriptionPlain"), lists, j.get("additionalPlain")])),
            "location": cat.get("location") or "", "employment_type": cat.get("commitment") or "",
            "salary": salary.strip(), "link": j.get("hostedUrl") or url, "posted": created}


ATS_DETAIL = {"ashby": _ashby_detail, "greenhouse": _greenhouse_detail, "lever": _lever_detail}


def scrape_roliva(cfg: dict, patterns) -> list[dict]:
    data = requests.get(cfg["url"], headers=UA, timeout=60).json()
    if data.get("paused"):
        log.warning("roliva: list is paused by its maintainer")
    cache, out, closed, not_remote, unverified = {}, [], 0, 0, 0
    for it in data.get("jobs", []):
        if not title_matches(it.get("title", ""), patterns):
            continue
        # Freshness by the employer's posting date, not first_seen_at (when the list's crawler found it —
        # it found months-old postings on its first crawl). first_seen_at only if no posting date exists.
        if not fresh(_parse_dt(it.get("posted_date") or it.get("first_seen_at")), cfg["max_age_days"]):
            continue
        fetch = ATS_DETAIL.get((it.get("source_ats") or "").lower())
        try:
            d = fetch(it["apply_url"], cache) if fetch else None
        except (requests.RequestException, ValueError) as e:
            log.debug("roliva: ATS lookup failed for %s: %s", it.get("apply_url"), e)
            d = None
        if d and d.get("closed"):
            closed += 1
            continue
        if not d:  # unknown ATS / custom careers page / lookup failed: can't confirm open + remote, no description
            unverified += 1
            continue
        if not d["remote"]:  # strict: the company's own ATS must say remote
            not_remote += 1
            continue
        if d.get("posted") and not fresh(_parse_dt(d["posted"]), cfg["max_age_days"]):
            continue  # the ATS's own publish date is authoritative
        loc = d["location"] or it.get("location") or "India"
        out.append(job(
            title=it.get("title"), company=it.get("company"),
            link=re.sub(r"/(application|apply)/?$", "", d["link"] or it["apply_url"]),
            description=d["description"] or f"{it.get('title')} at {it.get('company')}. {it.get('eligibility_text') or ''}",
            location=f"{loc} (Remote; {it.get('eligibility_text') or it.get('eligibility')})",
            employment_type=d["employment_type"], posted_date=d["posted"] or it.get("posted_date") or "",
            source="roliva", salary=d["salary"],
        ))
    log.info("roliva: %d remote + open after ATS check (%d closed, %d not remote, %d unverifiable)",
             len(out), closed, not_remote, unverified)
    return out


LEGAL_SUFFIX = re.compile(r"^(?:inc|llc|ltd|limited|gmbh|corp|co|plc|s\.?a|b\.?v|pvt\.? ltd|ag|oy|ab)\.?$", re.I)


def split_title_company(text: str) -> tuple[str, str]:
    """Python.org titles are "Role, Company" — and companies can contain a comma ("ActivePrime, Inc.")."""
    title, _, company = (text or "").rpartition(", ")
    if not title:
        return company, ""
    if LEGAL_SUFFIX.match(company.strip()) and ", " in title:
        title, _, name = title.rpartition(", ")
        company = f"{name}, {company}"
    return title, company


def scrape_pythonorg(cfg: dict, patterns) -> list[dict]:
    resp = requests.get(cfg["feed"], headers=UA, timeout=60)
    resp.raise_for_status()
    out = []
    for e in feedparser.parse(resp.content).entries:
        title, company = split_title_company(e.get("title") or "")
        if not title_matches(title, patterns):
            continue
        text = strip_html(e.get("summary"))
        location, _, desc = text.partition("\n")
        out.append(job(title=title, company=company, link=e.get("link"), description=desc.strip() or text,
                       location=location.strip(), source="pythonorg"))
    return out


HN_PLACE = re.compile(r"remote|on-?site|hybrid|worldwide|anywhere|global|time ?zones?|\butc\b|\bgmt\b|\b(?:est|pst|cet)\b|"
                      r"india|apac|asia|emea|europe|\bus\b|\busa\b|\bu\.s\.|\buk\b|canada|latam|americas", re.I)
HN_OPEN = re.compile(r"worldwide|anywhere|global(?:ly)?|\bindia\b|\bapac\b|\basia\b", re.I)
HN_ROLE = re.compile(r"engineer|developer|scientist|analyst|researcher|architect|designer|annotator|specialist|"
                     r"programmer|\bml\b|\bai\b|\bllm|\bdata\b|roles?\b|positions?\b|\bintern", re.I)
HN_MONEY = re.compile(r"[$€£]\s?\d|\d\s?k\b|\b(?:usd|eur|gbp)\b", re.I)


def parse_hn_header(first_line: str) -> dict | None:
    """'Company | Role | REMOTE (Worldwide) | Full-time | $120k' → fields. Posts that skip the company
    ('Senior Python Engineer | REMOTE (Worldwide)') get company '' and the role as title, so the
    title-based seniority filter still sees 'Senior'."""
    segs = [s.strip() for s in first_line.split("|") if s.strip()]
    if len(segs) < 2:
        return None
    place = [s for s in segs if HN_PLACE.search(s) and not HN_ROLE.search(s)]
    money = [s for s in segs if HN_MONEY.search(s)]
    rest = [s for s in segs if s not in place and s not in money and not s.lower().startswith("http")
            and not re.search(r"full[- ]?time|part[- ]?time|contract|intern(?:ship)?\b", s, re.I)]
    company_first = bool(rest) and rest[0] == segs[0] and not HN_ROLE.search(segs[0])
    company = segs[0] if company_first else ""
    roles = [s for s in rest if s != company]
    title = next((s for s in roles if HN_ROLE.search(s)), roles[0] if roles else "")
    if not title:
        return None
    return {"title": title[:120], "company": company[:80], "location": " | ".join(place),
            "employment_type": next((s for s in segs if re.search(r"full[- ]?time|part[- ]?time|contract|intern", s, re.I)), ""),
            "salary": money[0] if money else ""}


def scrape_hn_whoishiring(cfg: dict, patterns) -> list[dict]:
    api = "https://hn.algolia.com/api/v1"
    stories = requests.get(f"{api}/search_by_date", headers=UA, timeout=60,
                           params={"tags": "story,author_whoishiring", "hitsPerPage": 6}).json()["hits"]
    story = next((s for s in stories if s.get("title", "").startswith("Ask HN: Who is hiring?")), None)
    if not story:
        return []
    sid = str(story["objectID"])
    hits = requests.get(f"{api}/search", headers=UA, timeout=60,
                        params={"tags": f"comment,story_{sid}", "hitsPerPage": 1000}).json()["hits"]
    out = []
    for h in hits:
        if str(h.get("parent_id")) != sid or not h.get("comment_text"):
            continue  # replies, deleted posts
        posted = datetime.fromisoformat(h["created_at"].replace("Z", "+00:00")) if h.get("created_at") else None
        if not fresh(posted, cfg["max_age_days"]):
            continue
        # The header is the first LINE of the first paragraph (some posts have no <p> at all).
        first = strip_html(html.unescape(h["comment_text"].split("<p>")[0])).split("\n")[0][:300]
        if not re.search(r"\bremote\b", first, re.I) or not HN_OPEN.search(first):
            continue  # strict: the header itself must say remote AND worldwide/anywhere/global/India/APAC
        if not title_matches(first, patterns):
            continue
        hdr = parse_hn_header(first)
        if not hdr:
            continue
        out.append(job(**hdr, link=f"https://news.ycombinator.com/item?id={h['objectID']}",
                       description=strip_html(html.unescape(h["comment_text"])),
                       posted_date=posted.isoformat() if posted else "", source="hn_whoishiring"))
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
    # "India (Remote)" — filter_jobs reads remote/on-site/hybrid from the location text. The public actor
    # returns no workplace field, but every search URL carries LinkedIn's Remote filter (f_WT=2), so those
    # results are remote by LinkedIn's own tagging (the description is still checked for hybrid wording).
    workplace = ", ".join(w.get("localizedName", "") for w in it.get("jobWorkplaceTypes") or [] if isinstance(w, dict))
    if not workplace and re.search(r"[?&]f_WT=2(?:&|$|%2C|,)", it.get("inputUrl") or ""):
        workplace = "Remote"
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


FREE = {"remoteok": scrape_remoteok, "remotive": scrape_remotive, "weworkremotely": scrape_weworkremotely,
        "himalayas": scrape_himalayas, "roliva": scrape_roliva, "pythonorg": scrape_pythonorg,
        "hn_whoishiring": scrape_hn_whoishiring}


# ---------------------------------------------------------------- main


def dedupe(jobs: list[dict]) -> list[dict]:
    """One copy per job across sources. When the same job appears twice, keep the copy that states pay
    (the pay rules can only check what's stated), else the first one seen."""
    index, out = {}, []
    for j in jobs:
        if not j["title"] or not j["link"]:
            continue
        keys = [k for k in job_keys(j) if k and k != "|"]
        hit = next((index[k] for k in keys if k in index), None)
        if hit is None:
            out.append(j)
            for k in keys:
                index[k] = len(out) - 1
        elif j.get("salary") and not out[hit].get("salary"):
            out[hit] = j
            for k in keys:
                index[k] = hit
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
