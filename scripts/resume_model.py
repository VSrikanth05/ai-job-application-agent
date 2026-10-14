"""Parse/render the resume markdown format used by resume/base_resume.md.

Stdlib only — imported by the Claude Code hook, which runs outside the venv.

Format:
    # Name
    contact line
    ## Summary         paragraph
    ## Skills          - **Category:** a, b, c
    ## Experience      ### Title — Company / *dates* / - bullets
    ## Projects        ### Name / - bullets
    ## Education       - lines
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

REQUIRED_SECTIONS = ["Summary", "Skills", "Experience", "Projects", "Education"]


@dataclass
class Entry:
    heading: str
    meta: str = ""  # e.g. "*Sep 2025 – Present*"
    bullets: list[str] = field(default_factory=list)


@dataclass
class Resume:
    name: str = ""
    contact: str = ""
    sections: dict[str, list[str]] = field(default_factory=dict)  # raw lines per ## section, in order

    @property
    def summary(self) -> str:
        return " ".join(l.strip() for l in self.sections.get("Summary", []) if l.strip())

    @property
    def skills(self) -> list[tuple[str, list[str]]]:
        out = []
        for line in self.sections.get("Skills", []):
            m = re.match(r"\s*[-*]\s*\*\*(.+?):?\*\*:?\s*(.*)", line)
            if m:
                out.append((m.group(1).strip().rstrip(":"), [s.strip() for s in m.group(2).split(",") if s.strip()]))
        return out

    def entries(self, section: str) -> list[Entry]:
        out: list[Entry] = []
        for line in self.sections.get(section, []):
            s = line.strip()
            if s.startswith("### "):
                out.append(Entry(s[4:].strip()))
            elif out and re.fullmatch(r"[*_].+[*_]", s) and not out[-1].bullets and not out[-1].meta:
                out[-1].meta = s
            elif out and re.match(r"[-*]\s+", s):
                out[-1].bullets.append(re.sub(r"^[-*]\s+", "", s))
        return out

    @property
    def education(self) -> list[str]:
        return [re.sub(r"^[-*]\s+", "", l.strip()) for l in self.sections.get("Education", []) if l.strip()]


def parse(md: str) -> Resume:
    r, current = Resume(), None
    for line in md.splitlines():
        if line.startswith("# ") and not r.name:
            r.name = line[2:].strip()
        elif line.startswith("## "):
            current = line[3:].strip()
            r.sections[current] = []
        elif current is None:
            if line.strip() and r.name and not r.contact:
                r.contact = line.strip()
        else:
            r.sections[current].append(line)
    return r


def render(name: str, contact: str, summary: str, skills: list[tuple[str, list[str]]],
           experience: list[Entry], projects: list[Entry], education: list[str]) -> str:
    out = [f"# {name}", "", contact, "", "## Summary", "", summary, "", "## Skills", ""]
    out += [f"- **{cat}:** {', '.join(items)}" for cat, items in skills if items]
    for title, entries in (("Experience", experience), ("Projects", projects)):
        out += ["", f"## {title}"]
        for e in entries:
            out += ["", f"### {e.heading}"]
            if e.meta:
                out.append(e.meta)
            out += [""] + [f"- {b}" for b in e.bullets]
    out += ["", "## Education", ""] + [f"- {l}" for l in education]
    return "\n".join(out) + "\n"
