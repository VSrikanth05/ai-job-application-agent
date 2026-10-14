"""Map a resume onto the Google Docs template's {{PLACEHOLDER}} slots.

The template is generated from the base resume: name, contact, experience headings/dates
and education are literal text; everything tailoring may change is a slot. Slot counts
come from the base resume, so a tailored resume must keep the same structure
(same number of skill lines, same bullets per entry) — tailor_job.assemble guarantees it.
"""

from __future__ import annotations

from resume_model import Entry, Resume, parse, render


class TemplateMismatch(ValueError):
    pass


def template_markdown(base_md: str) -> str:
    b = parse(base_md)
    skills = [(f"{{{{SKILL_CAT_{i}}}}}", [f"{{{{SKILL_ITEMS_{i}}}}}"]) for i in range(1, len(b.skills) + 1)]
    exp = [Entry(e.heading, e.meta, [f"{{{{EXP{i}_B{j}}}}}" for j in range(1, len(e.bullets) + 1)])
           for i, e in enumerate(b.entries("Experience"), 1)]
    proj = [Entry(f"{{{{PROJ{i}_TITLE}}}}", e.meta, [f"{{{{PROJ{i}_B{j}}}}}" for j in range(1, len(e.bullets) + 1)])
            for i, e in enumerate(b.entries("Projects"), 1)]
    md = render(b.name, b.contact, "{{SUMMARY}}", skills, exp, proj, b.education)
    # render() writes "- **{{SKILL_CAT_1}}:** {{SKILL_ITEMS_1}}" — exactly the slot layout we want.
    return md


def slot_values(md: str, base_md: str) -> dict[str, str]:
    t, b = parse(md), parse(base_md)
    values = {"{{SUMMARY}}": t.summary}

    if len(t.skills) != len(b.skills):
        raise TemplateMismatch(f"template has {len(b.skills)} skill lines, resume has {len(t.skills)}")
    for i, (cat, items) in enumerate(t.skills, 1):
        values[f"{{{{SKILL_CAT_{i}}}}}"] = cat
        values[f"{{{{SKILL_ITEMS_{i}}}}}"] = ", ".join(items)

    def fill(section: str, prefix: str, with_title: bool):
        te, be = t.entries(section), b.entries(section)
        if len(te) != len(be):
            raise TemplateMismatch(f"{section}: template has {len(be)} entries, resume has {len(te)}")
        for i, (tent, bent) in enumerate(zip(te, be), 1):
            if len(tent.bullets) != len(bent.bullets):
                raise TemplateMismatch(f"{section} #{i}: template has {len(bent.bullets)} bullets, resume has {len(tent.bullets)}")
            if not with_title and tent.heading != bent.heading:
                raise TemplateMismatch(f"{section} #{i}: entries must stay in base order")
            if with_title:
                values[f"{{{{{prefix}{i}_TITLE}}}}"] = tent.heading
            for j, bullet in enumerate(tent.bullets, 1):
                values[f"{{{{{prefix}{i}_B{j}}}}}"] = bullet

    fill("Experience", "EXP", with_title=False)
    fill("Projects", "PROJ", with_title=True)
    return values
