"""ATS keyword matching: which of a job description's skill keywords the candidate has, and how
many of them a resume actually contains (ATS systems match keywords literally).

Vocabulary = skills inventory + base resume skills + config/tech_terms.txt. Items written as
"Long Form (ACR)" match either form, so "LLMs" in a JD matches "Large Language Models (LLMs)".
"""

from __future__ import annotations

import re

from validate_resume import load_terms


def variants(item: str) -> list[str]:
    m = re.fullmatch(r"\s*(.+?)\s*\((.+?)\)\s*", item)
    forms = [m.group(1), m.group(2)] if m else [item]
    out = []
    for f in forms:
        out.append(f)
        if re.fullmatch(r"[A-Z]{2,}s", f):  # LLMs -> LLM
            out.append(f[:-1])
    return out


def _present(form: str, text: str) -> bool:
    flags = 0 if re.fullmatch(r"[A-Z]{2,4}s?", form) else re.I  # short acronyms: case-sensitive
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(form)}(?![A-Za-z0-9])", text, flags) is not None


def has_keyword(item: str, text: str) -> bool:
    return any(_present(v, text) for v in variants(item))


def _drop_covered(items: list[str]) -> list[str]:
    """Remove items fully covered by a richer one ("RAG" vs "Retrieval-Augmented Generation (RAG)")."""
    forms = {i: {v.lower() for v in variants(i)} for i in items}
    return [i for i in items if not any(o != i and forms[i] < forms[o] for o in items)]


def jd_keywords(job: dict, allowed: list[str]) -> dict:
    """{'have': allowed items the JD asks for, 'missing': JD keywords the candidate can't claim}."""
    jd = f"{job.get('title', '')}\n{job.get('description', '')}"
    allowed_text = "\n".join(allowed)
    have = _drop_covered(list(dict.fromkeys(a for a in allowed if has_keyword(a, jd))))
    missing = []
    for term in load_terms():
        if has_keyword(term, jd) and not has_keyword(term, allowed_text) and term not in missing:
            missing.append(term)
    return {"have": have, "missing": missing}


def ats_match(resume_md: str, job: dict, allowed: list[str]) -> dict:
    """Share of the JD's keywords present in the resume (what an ATS keyword filter sees)."""
    kw = jd_keywords(job, allowed)
    wanted = kw["have"] + kw["missing"]
    found = [k for k in wanted if has_keyword(k, resume_md)]
    absent = [k for k in wanted if k not in found]
    pct = round(100 * len(found) / len(wanted)) if wanted else 100
    return {"percent": pct, "found": found, "absent": absent, "total": len(wanted)}


def summary(m: dict) -> str:
    text = f"{m['percent']}% ({len(m['found'])}/{m['total']})"
    return text + (f" · missing: {', '.join(m['absent'][:6])}" if m["absent"] else "")
