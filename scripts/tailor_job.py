"""Step 3.4 (automated path) — tailor the base resume to one job via OpenRouter.

The model only returns a re-emphasis plan (summary, skill order, reworded bullets,
project order). Name, contact, employers, dates and education are copied from the
base resume, and skills are restricted to terms that already appear in it, so the
model has no way to add experience. The result is then checked by validate_resume.

Usage:
    python scripts/tailor_job.py output/runs/<date>/jobs/<folder>    # writes resume.md there
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

from common import base_resume_text, load_config, read_json, setup_logging
from llm import chat_json
from resume_model import Entry, parse, render
from ats import jd_keywords
from validate_resume import load_inventory, load_terms, validate

log = logging.getLogger("tailor")

SYSTEM = """You tailor a candidate's resume to one job. HARD RULES:
- Never invent experience, employers, tools, technologies, metrics, numbers or years of experience.
- Reorder and reword only. Every claim must already be supported by the base resume.
- Skills may ONLY be chosen from the provided ALLOWED_SKILLS list, spelled exactly as given.
- Summary and bullets may only mention tools that the BASE RESUME itself mentions (ALLOWED_SKILLS items
  that aren't in the base resume belong in the Skills section only), even if the job asks for them.

ATS RULES (applicant tracking systems match keywords literally):
- Build the Skills section for THIS job: first every ALLOWED_SKILLS item the job description asks for
  (JD KEYWORDS YOU HAVE lists them), then other closely related ones. Leave out unrelated skills.
  Keep each skill line to at most 9 items so the resume stays on one page.
- Prefer the allowed spelling that matches the job's wording (e.g. "Large Language Models (LLMs)" when
  the job says "large language models"); use long-form-plus-acronym items where they exist.
- Name the skill lines with plain, standard category names (e.g. "Languages", "AI & ML",
  "Frameworks & Libraries", "Cloud & Tools").
- In the summary and bullets, reuse the job's exact terms for work the candidate really did.
- Do not state a number of years of experience. Do not add numbers or percentages.
- Do not claim domains, use cases or responsibilities the base resume doesn't show (e.g. ranking,
  recommendation, MLOps, computer vision, production monitoring) even in the summary.
- Use plain ASCII hyphens and spaces.
- Mirror the job's wording where it truthfully describes the same work.

Return JSON with exactly these keys:
- "summary": 2-3 sentence professional summary aimed at this role
- "skills": list of {"category": str, "items": [str]} — EXACTLY as many categories as the base resume
  (the PDF template has a fixed number of skill lines), most relevant categories and items first
- "experience": list of {"index": int, "bullets": [str]} — for EVERY experience entry, the same number of
  bullets as the base entry, reordered (most relevant first) and reworded
- "project_order": list of project indexes, most relevant first (include every project)
- "projects": list of {"index": int, "bullets": [str]} — same rules as experience"""


def allowed_skills(base_md: str) -> list[str]:
    """Skills inventory + base Skills items + known tech terms found anywhere in the base resume."""
    items = list(dict.fromkeys(load_inventory() + [i for _, its in parse(base_md).skills for i in its]))
    have = {i.lower() for i in items}
    for t in load_terms():
        if t.lower() not in have and re.search(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])", base_md, re.I):
            items.append(t)
            have.add(t.lower())
    for phrase in ("Vector Databases", "Semantic Search", "ETL Pipelines", "Predictive Analytics", "Data Cleaning",
                   "Data Preprocessing", "Prompt Engineering", "Conversational AI", "Document Ingestion"):
        if phrase.lower() in base_md.lower() and phrase.lower() not in have:
            items.append(phrase)
    return items


def build_prompt(job: dict, base_md: str, emphasis: str, feedback: list[str] | None) -> str:
    b = parse(base_md)
    base = {
        "summary": b.summary,
        "skills": [{"category": c, "items": i} for c, i in b.skills],
        "experience": [{"index": n, "heading": e.heading, "bullets": e.bullets} for n, e in enumerate(b.entries("Experience"))],
        "projects": [{"index": n, "heading": e.heading, "bullets": e.bullets} for n, e in enumerate(b.entries("Projects"))],
    }
    fit = job.get("fit", {})
    allowed = allowed_skills(base_md)
    kw = jd_keywords(job, allowed)
    parts = [
        f"JOB: {job['title']} at {job['company']}\n\n{job['description'][:5000]}",
        f"MATCHED REQUIREMENTS: {json.dumps(fit.get('matched_requirements', []))}",
        f"MISSING REQUIREMENTS (do NOT add these): {json.dumps(fit.get('missing_requirements', []))}",
        f"EMPHASIS FOR THIS ROLE TYPE: {emphasis}",
        f"JD KEYWORDS YOU HAVE (put these in Skills, job's wording): {json.dumps(kw['have'])}",
        f"JD KEYWORDS YOU DON'T HAVE (never add): {json.dumps(kw['missing'])}",
        f"ALLOWED_SKILLS: {json.dumps(allowed)}",
        f"BASE RESUME:\n{json.dumps(base, ensure_ascii=False, indent=1)}",
    ]
    if feedback:
        parts.append("YOUR PREVIOUS ATTEMPT FAILED VALIDATION — fix these:\n- " + "\n- ".join(feedback))
    return "\n\n".join(parts)


TYPO_FIXES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", " ": " ", " ": " ",
                            "‘": "'", "’": "'", "“": '"', "”": '"'})


def clean(text) -> str:
    """Models emit non-breaking hyphens/spaces; they break ATS keyword matching and PDF fonts."""
    return str(text).translate(TYPO_FIXES).strip()


def assemble(plan: dict, base_md: str) -> tuple[str, list[str]]:
    """Builds resume markdown from the model's plan; returns (markdown, notes about dropped content)."""
    b, notes = parse(base_md), []
    allowed = {s.lower(): s for s in allowed_skills(base_md)}

    skills = []
    for cat in plan.get("skills") or []:
        items = []
        for it in cat.get("items") or []:
            if str(it).lower() in allowed:
                items.append(allowed[str(it).lower()])
            else:
                notes.append(f"dropped skill not in base resume: {it!r}")
        if items:
            skills.append((str(cat.get("category") or "Skills").strip(), items))
    if len(skills) != len(b.skills):  # the Docs template has one slot per base skill line
        notes.append(f"model returned {len(skills)} usable skill lines (template needs {len(b.skills)}); kept base skills")
        skills = b.skills

    def entries(section: str, rewritten: list, order: list[int] | None) -> list[Entry]:
        base_entries = b.entries(section)
        by_idx = {r.get("index"): r.get("bullets") for r in rewritten or [] if isinstance(r, dict)}
        idx = [i for i in (order or []) if isinstance(i, int) and 0 <= i < len(base_entries)]
        idx = list(dict.fromkeys(idx)) + [i for i in range(len(base_entries)) if i not in idx]
        # Template slots have fixed bullet counts per position; reorder only if counts still line up.
        if [len(base_entries[i].bullets) for i in idx] != [len(e.bullets) for e in base_entries]:
            notes.append(f"{section}: reorder would break template bullet slots; kept base order")
            idx = list(range(len(base_entries)))
        out = []
        for i in idx:
            e = base_entries[i]
            bullets = by_idx.get(i)
            if not bullets or len(bullets) != len(e.bullets):
                notes.append(f"{section} #{i}: bullet count mismatch; kept base bullets")
                bullets = e.bullets
            out.append(Entry(e.heading, e.meta, [clean(x) for x in bullets]))
        return out

    md = render(
        b.name, b.contact, clean(plan.get("summary") or b.summary), skills,
        entries("Experience", plan.get("experience"), None),  # keep reverse-chronological job order
        entries("Projects", plan.get("projects"), plan.get("project_order")),
        b.education,
    )
    return md, notes


SHORTEN = ("The previous version rendered longer than one page. Keep the summary to 2 short sentences and "
           "make every bullet at most ~18 words, keeping the strongest keywords.")


def tailor(job: dict, cfg: dict, base_md: str | None = None, *, shorten: bool = False) -> tuple[str, list[str]]:
    """Returns (validated markdown, notes). Raises ValueError if validation still fails after one retry."""
    base_md = base_md or base_resume_text()
    emphasis = cfg["role_emphasis"].get(job.get("fit", {}).get("role_category"), "Lead with the most relevant work.")
    if shorten:
        emphasis += "\n" + SHORTEN
    feedback = None
    for attempt in (1, 2):
        plan = chat_json(SYSTEM, build_prompt(job, base_md, emphasis, feedback), max_tokens=3500, temperature=0.3)
        md, notes = assemble(plan, base_md)
        errors = validate(md, base_md)
        if not errors:
            return md, notes
        log.warning("attempt %d failed validation: %s", attempt, errors)
        feedback = errors
    raise ValueError("tailored resume failed validation: " + "; ".join(errors))


def main() -> int:
    ap = argparse.ArgumentParser(description="Tailor the resume for one job folder")
    ap.add_argument("folder", type=Path, help="job folder containing job.json")
    args = ap.parse_args()
    setup_logging()
    job = read_json(args.folder / "job.json")
    md, notes = tailor(job, load_config())
    (args.folder / "resume.md").write_text(md, encoding="utf-8")
    for n in notes:
        log.info("note: %s", n)
    log.info("wrote %s", args.folder / "resume.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
