"""Step 3.5 — validate a tailored resume against the base resume (shared by the
automated path and the Claude Code hook). Stdlib only.

Usage:
    python scripts/validate_resume.py path/to/tailored.md      # exit 1 + reasons on failure
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from resume_model import REQUIRED_SECTIONS, parse  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "resume" / "base_resume.md"
TERMS = ROOT / "config" / "tech_terms.txt"
INVENTORY = ROOT / "resume" / "skills_inventory.md"

PLACEHOLDER = re.compile(
    r"\[(?:[A-Z][A-Za-z ]{1,30}|X+|TBD|TODO)\](?!\()|\{\{.*?\}\}|<[A-Z_ ]{3,}>|\bTODO\b|\bTBD\b|\bXXX\b|lorem ipsum|"
    r"\bplaceholder\b|\binsert (?:here|company|name)\b", re.I)


def load_terms() -> list[str]:
    return [l.strip() for l in TERMS.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]


def _has(term: str, text: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text, re.I) is not None


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def load_inventory() -> list[str]:
    """Skills from resume/skills_inventory.md — allowed in the Skills section only."""
    if not INVENTORY.exists():
        return []
    return [i for _, items in parse(INVENTORY.read_text(encoding="utf-8")).skills for i in items]


def _without_skills(md: str) -> str:
    """The resume minus its ## Skills section (where inventory skills are allowed)."""
    return re.sub(r"(?ms)^## Skills\s*$.*?(?=^## |\Z)", "", md)


def validate(tailored_md: str, base_md: str, terms: list[str] | None = None,
             inventory: list[str] | None = None) -> list[str]:
    """Returns a list of problems; empty means valid."""
    terms = load_terms() if terms is None else terms
    inventory = load_inventory() if inventory is None else inventory
    t, b = parse(tailored_md), parse(base_md)
    errors = []

    missing = [s for s in REQUIRED_SECTIONS if s not in t.sections]
    if missing:
        errors.append(f"missing sections: {', '.join(missing)}")

    if (t.name, t.contact) != (b.name, b.contact):
        errors.append("name/contact line differs from the base resume")

    for m in PLACEHOLDER.finditer(tailored_md):
        errors.append(f"placeholder text left over: {m.group(0)!r}")

    base_flat = _squash(base_md)
    inv = {_squash(i) for i in inventory}
    for cat, items in t.skills:
        for item in items:
            if _squash(item) not in base_flat and _squash(item) not in inv:
                errors.append(f"skill not in base resume or skills inventory: {item!r} ({cat})")

    # Summary/bullets may only name tools the base resume itself mentions;
    # the Skills section may also use the inventory.
    body, skills_text = _without_skills(tailored_md), "\n".join(", ".join(i) for _, i in t.skills)
    inv_text = "\n".join(inventory)
    for term in terms:
        if _has(term, body) and not _has(term, base_md):
            errors.append(f"tool/technology not in base resume: {term!r}")
        elif _has(term, skills_text) and not (_has(term, base_md) or _has(term, inv_text)):
            errors.append(f"tool/technology not in base resume or skills inventory: {term!r}")

    num = r"\d+(?:\.\d+)?"
    new_numbers = (set(re.findall(num, _without_skills(tailored_md))) - set(re.findall(num, base_md))) | \
                  (set(re.findall(num, skills_text)) - set(re.findall(num, base_md + inv_text)))
    if new_numbers:
        errors.append(f"numbers/metrics not in base resume: {sorted(new_numbers)}")

    base_exp = {e.heading: e.meta for e in b.entries("Experience")}
    for e in t.entries("Experience"):
        if e.heading not in base_exp:
            errors.append(f"experience entry not in base resume: {e.heading!r}")
        elif e.meta != base_exp[e.heading]:
            errors.append(f"dates changed for {e.heading!r}")
    base_proj = {e.heading for e in b.entries("Projects")}
    for e in t.entries("Projects"):
        if e.heading not in base_proj:
            errors.append(f"project not in base resume: {e.heading!r}")
    if sorted(map(_squash, t.education)) != sorted(map(_squash, b.education)):
        errors.append("education section differs from the base resume")

    return errors


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    errors = validate(Path(sys.argv[1]).read_text(encoding="utf-8"), BASE.read_text(encoding="utf-8"))
    for e in errors:
        print(f"- {e}")
    print("VALID" if not errors else f"INVALID ({len(errors)} problems)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
