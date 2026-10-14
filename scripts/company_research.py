"""Step 3.6 — 3-5 interview/application talking points per qualifying job (one LLM call).

Free models have no web access, so points come from the job posting plus the model's
general knowledge; verify company facts before relying on them. Never written into the resume.

Usage:
    python scripts/company_research.py output/runs/<date>/jobs/<folder>   # writes research.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common import base_resume_text, read_json, setup_logging, write_json
from llm import chat_json

SYSTEM = """You help a candidate prepare to apply for a job. Using the job posting (and only well-known,
general facts about the company — say "unknown" rather than guessing), return JSON:
- "what_they_build": one sentence on the company's product/business
- "ai_stack": tools/models/platforms the posting or company is known to use, or "unknown"
- "talking_points": 3-5 short bullet strings linking the candidate's REAL experience to this role
- "questions_to_ask": 2 thoughtful questions for the interviewer"""


def research(job: dict, base_md: str | None = None) -> dict:
    base_md = base_md or base_resume_text()
    user = (f"JOB: {job['title']} at {job['company']}\n\n{job['description'][:5000]}\n\n"
            f"CANDIDATE RESUME:\n{base_md}")
    out = chat_json(SYSTEM, user, max_tokens=1200, temperature=0.4)
    out["talking_points"] = [str(p) for p in (out.get("talking_points") or [])][:5]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Company talking points for one job folder")
    ap.add_argument("folder", type=Path)
    args = ap.parse_args()
    setup_logging()
    write_json(args.folder / "research.json", research(read_json(args.folder / "job.json")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
