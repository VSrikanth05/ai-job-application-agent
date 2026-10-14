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
from pathlib import Path

from common import base_resume_text, load_config, read_json, run_dir, setup_logging, write_json
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


def score_job(job: dict, base_md: str, cfg: dict) -> dict:
    desc = job["description"][: cfg["llm"]["description_chars"]]
    user = (f"CANDIDATE RESUME:\n{base_md}\n\nJOB:\nTitle: {job['title']}\nCompany: {job['company']}\n"
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
