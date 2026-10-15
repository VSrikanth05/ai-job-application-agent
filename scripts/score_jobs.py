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
from pay import parse_salary, pay_problem
from resume_model import parse

log = logging.getLogger("score")

ROLE_CATEGORIES = ["AI/GenAI", "ML", "Data Analyst", "Data Annotation", "Python Developer", "Other"]
CURRENCIES = ["USD", "EUR", "GBP", "CHF", "CAD", "AUD", "SGD", "NZD", "INR", "other", "unknown"]

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
- "salary_text": the pay exactly as the posting states it (e.g. "$80k-$100k per year"), or null if not stated
- "pays_from_india": "yes" if the hire would be on an Indian payroll — an Indian company, or a foreign
  company's India office/entity hiring for an India-based role; "no" if the employer pays from outside India
  (e.g. a foreign company hiring remotely worldwide/across countries, or as a contractor paid in USD/EUR);
  "unclear" if you cannot tell
- "work_arrangement": "remote" if the job can be done fully remotely, "hybrid" if it requires some office
  days, "onsite" if it is office-based, "unclear" if the posting doesn't say
- "role_category": one of {ROLE_CATEGORIES}
- "reason": one sentence explaining the score
Score 8-10 only when most core requirements are matched. Do not reward skills the resume lacks."""


WORLDWIDE = re.compile(r"worldwide|anywhere|global|international", re.I)


def keyword_rank(job: dict, terms: list[str], pay_cfg: dict | None = None) -> int:
    """Picks which jobs get one of the ~25 daily LLM calls: skill overlap, plus a boost for jobs most likely
    to clear the pay rules (stated pay in an allowed currency, or open worldwide = paid from abroad)."""
    text = f"{job['title']} {job['description']}".lower()
    rank = sum(1 for t in terms if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", text))
    if pay_cfg:
        cur = parse_salary(job.get("salary"), pay_cfg.get("usd_per_unit")).currency
        if cur in pay_cfg["currencies"]:
            rank += 4
        if WORLDWIDE.search(job.get("location", "")):
            rank += 4
    return rank


def resume_terms(base_md: str) -> list[str]:
    terms = {i.lower() for _, items in parse(base_md).skills for i in items}
    terms |= {"llm", "genai", "generative ai", "machine learning", "python", "rag", "nlp", "azure", "openai",
              "data analyst", "sql", "annotation", "langchain", "vector"}
    return sorted(terms)


def pay_verdict(result: dict, salary: str, pay_cfg: dict) -> str | None:
    """None = pay acceptable under the user's rules, else the reason. Stated pay is parsed in code
    (job field first, then the model's salary_text) so the currency and floor never rest on the model."""
    salary_text = result.get("salary_text")
    stated = salary or (salary_text if isinstance(salary_text, str) else (str(salary_text) if salary_text else ""))
    parsed = parse_salary(stated, pay_cfg.get("usd_per_unit"))
    if parsed.currency:
        result["pay_currency"] = parsed.currency  # code beats the model's guess
    problem = pay_problem(stated, pay_cfg)
    if problem:
        return problem
    if parsed.currency:  # stated in an allowed currency, at or above the floor (else pay_problem fired)
        return None
    # No currency stated anywhere. A model-*guessed* USD/EUR is not evidence of foreign pay; only the
    # employer test counts. A guessed INR is evidence of an Indian payroll.
    if result.get("pay_currency") == "INR":
        return "pay_currency:INR (inferred)"
    policy = pay_cfg.get("unknown_pay", "employer_abroad_only")
    if policy == "keep" or (policy == "employer_abroad_only" and result.get("pays_from_india") == "no"):
        return None
    return f"pay_unstated:pays_from_india={result.get('pays_from_india')}"


def apply_rules(result: dict, cfg: dict, salary: str = "") -> dict:
    try:
        score = int(float(result.get("score") or 0))
    except (TypeError, ValueError):
        score = 0
    ry = result.get("required_years")
    too_senior = isinstance(ry, (int, float)) and not isinstance(ry, bool) and ry > cfg["hard_filters"]["max_years_required"]
    if result.get("seniority_fit") is False or result.get("india_eligible") == "no" or too_senior:
        score = min(score, 5)
    result["score"] = score
    # Backstop for the strict-remote rule (the filter can only read wording): the model's reading of the
    # work arrangement disqualifies hybrid/on-site jobs outright.
    if str(result.get("work_arrangement") or "").lower() in ("hybrid", "onsite", "on-site"):
        result["qualified"] = False
        result["disqualified_reason"] = f"work_arrangement={result['work_arrangement']}"
        return result
    problem = pay_verdict(result, salary, cfg["pay"])
    result["qualified"] = score >= cfg["min_fit_score"] and problem is None
    if problem:
        result["pay_problem"] = problem
    if score >= cfg["min_fit_score"] and problem:
        result["disqualified_reason"] = problem
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
    return apply_rules(raw, cfg, job.get("salary") or "")


def score_jobs(jobs: list[dict], cfg: dict) -> dict:
    base_md = base_resume_text()
    terms = resume_terms(base_md)
    ranked = sorted(jobs, key=lambda j: keyword_rank(j, terms, cfg["pay"]), reverse=True)
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
