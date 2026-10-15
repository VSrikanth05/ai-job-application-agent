"""Step 3.3 — score jobs 1-10 against the base resume (one LLM call per job) and keep
jobs at or above min_fit_score.

A cheap keyword-overlap pre-rank picks which jobs get an LLM call, because free
OpenRouter models allow only ~50 requests/day.

Usage:
    python scripts/score_jobs.py [--filtered output/runs/<date>/filtered.json]
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date
from pathlib import Path

from common import base_resume_text, load_config, read_json, run_dir, setup_logging, today, write_json
from llm import DailyLimitReached, LLMError, chat_json
from resume_model import parse

log = logging.getLogger("score")

ROLE_CATEGORIES = ["AI/GenAI", "ML", "Data Analyst", "Data Annotation", "Python Developer", "Other"]
CURRENCIES = ["USD", "EUR", "GBP", "INR", "other", "unknown"]

SYSTEM = f"""You are a strict technical recruiter screening remote jobs for one candidate.
Compare the job to the candidate's resume and return JSON with exactly these keys:
- "score": integer 1-10, how well the candidate's ACTUAL skills/experience fit the job's requirements
- "matched_requirements": list of job requirements the resume clearly satisfies (short phrases)
- "missing_requirements": list of job requirements the resume does not show (short phrases)
- "seniority_fit": true if the role suits someone with ~1 year of professional experience
- "required_years": minimum years of experience the job asks for, or null if not stated
- "india_eligible": "yes" if a candidate living in India can be hired (worldwide/APAC/India/anywhere),
  "no" if restricted to other countries/regions or requires work authorization elsewhere, "unclear" otherwise
- "pay_currency": one of {CURRENCIES}. If pay isn't stated, infer only when obvious
  (an India-based company hiring in India -> "INR"); otherwise "unknown"
- "role_category": one of {ROLE_CATEGORIES}
- "reason": one sentence explaining the score
Score 8-10 only when most core requirements are matched. Do not reward skills the resume lacks."""


def keyword_rank(job: dict, terms: list[str]) -> int:
    text = f"{job['title']} {job['description']}".lower()
    return sum(1 for t in terms if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", text))


def resume_terms(base_md: str) -> list[str]:
    terms = {i.lower() for _, items in parse(base_md).skills for i in items}
    terms |= {"llm", "genai", "generative ai", "machine learning", "python", "rag", "nlp", "azure", "openai",
              "data analyst", "sql", "annotation", "langchain", "vector"}
    return sorted(terms)


def apply_rules(result: dict, cfg: dict) -> dict:
    score = int(result.get("score") or 0)
    if result.get("seniority_fit") is False or result.get("india_eligible") == "no":
        score = min(score, 5)
    cur = result.get("pay_currency") or "unknown"
    currency_ok = cur in cfg["allowed_currencies"] or (cur == "unknown" and cfg["allow_unknown_currency"])
    result["score"] = score
    result["qualified"] = score >= cfg["min_fit_score"] and currency_ok
    if score >= cfg["min_fit_score"] and not currency_ok:
        result["disqualified_reason"] = f"pay_currency={cur}"
    return result


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_RANGE = re.compile(r"([A-Za-z]{3})[a-z]*\.? (\d{4})\s*[–-]\s*(Present|Current|Now|([A-Za-z]{3})[a-z]*\.? (\d{4}))", re.I)


def experience_note(base_md: str, today: date) -> str:
    """The model has no reliable sense of today's date, so "Sep 2025 – Present" was read as ~3 months.
    Spell out today's date and each role's length, computed from the resume's own date ranges."""
    lines = []
    for e in parse(base_md).entries("Experience"):
        m = DATE_RANGE.search(e.meta or "")
        if not m or m.group(1).lower()[:3] not in MONTHS:
            continue
        start = (int(m.group(2)), MONTHS[m.group(1).lower()[:3]])
        if m.group(4):  # closed range counts both end months: Mar–Jun = 4
            if m.group(4).lower()[:3] not in MONTHS:
                continue
            end, inclusive = (int(m.group(5)), MONTHS[m.group(4).lower()[:3]]), 1
        else:           # ongoing: whole months elapsed so far
            end, inclusive = (today.year, today.month), 0
        months = (end[0] - start[0]) * 12 + end[1] - start[1] + inclusive
        lines.append(f"- {e.heading}: {months} months")
    if not lines:
        return f"TODAY'S DATE: {today.isoformat()} (use it for \"Present\" in date ranges)"
    return (f"TODAY'S DATE: {today.isoformat()} (use it for \"Present\" in date ranges). "
            "Candidate's experience computed from the resume dates:\n" + "\n".join(lines))


def score_job(job: dict, base_md: str, cfg: dict) -> dict:
    desc = job["description"][: cfg["llm"]["description_chars"]]
    note = experience_note(base_md, date.fromisoformat(today()))
    user = (f"{note}\n\nCANDIDATE RESUME:\n{base_md}\n\nJOB:\nTitle: {job['title']}\nCompany: {job['company']}\n"
            f"Location: {job['location']}\nEmployment type: {job['employment_type'] or 'not stated'}\n"
            f"Salary: {job['salary'] or 'not stated'}\n\nDescription:\n{desc}")
    raw = chat_json(SYSTEM, user, max_tokens=1500, model_env="OPENROUTER_SCORING_MODEL")
    if raw.get("role_category") not in ROLE_CATEGORIES:
        raw["role_category"] = "Other"
    return apply_rules(raw, cfg)


def score_jobs(jobs: list[dict], cfg: dict) -> dict:
    base_md = base_resume_text()
    terms = resume_terms(base_md)
    ranked = sorted(jobs, key=lambda j: keyword_rank(j, terms), reverse=True)
    limit = cfg["llm"]["max_jobs_to_score"]
    to_score, skipped = ranked[:limit], ranked[limit:]
    if skipped:
        log.info("pre-rank: scoring top %d of %d (LLM budget); %d left unscored", len(to_score), len(jobs), len(skipped))

    scored, errors = [], []
    for i, job in enumerate(to_score, 1):
        try:
            s = score_job(job, base_md, cfg)
        except DailyLimitReached as e:
            log.error("OpenRouter daily free limit hit after %d jobs: %s", i - 1, e)
            errors.append(str(e))
            skipped = to_score[i - 1:] + skipped
            break
        except LLMError as e:
            log.warning("scoring failed for %s @ %s: %s", job["title"], job["company"], e)
            errors.append(f"{job['title']} @ {job['company']}: {e}")
            continue
        scored.append({**job, "fit": s})
        log.info("[%d/%d] %2d/10 %s %s @ %s", i, len(to_score), s["score"], "✓" if s["qualified"] else " ",
                 job["title"], job["company"])

    qualified = sorted((j for j in scored if j["fit"]["qualified"]), key=lambda j: -j["fit"]["score"])
    log.info("scored %d → %d qualified (≥%d)", len(scored), len(qualified), cfg["min_fit_score"])
    return {"scored": scored, "qualified": qualified, "unscored": skipped, "errors": errors}


def main() -> int:
    ap = argparse.ArgumentParser(description="Score filtered jobs with the LLM")
    ap.add_argument("--filtered", type=Path, help="filtered.json (default: today's)")
    args = ap.parse_args()
    setup_logging()
    data = read_json(args.filtered or run_dir() / "filtered.json")
    res = score_jobs(data["kept"], load_config())
    write_json(run_dir() / "scored.json", res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
