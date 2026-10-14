---
name: tailor-resume
description: Tailor Srikanth's base resume to qualified jobs prepared by the pipeline (job folders under output/runs/<date>/jobs/ that contain job.json but no resume.md), then validate, render the PDF through the Google Docs template (gws), save it to the Desktop and log it to the Sheet. Use when asked to tailor resumes for today's jobs or for a specific job folder.
---

# Tailor resume

Interactive counterpart of `scripts/tailor_job.py`. Same rules, but you do the rewriting.

## Find the work

1. If no job folders exist for today, prepare them first (scrapes + scores, no tailoring):
   `.venv/Scripts/python scripts/run_pipeline.py --no-tailor`
2. Targets: every `output/runs/<today>/jobs/*/job.json` whose folder has no `resume.md`
   (or the folder the user named).

## For each job folder

1. Read `job.json`: `title`, `company`, `description`, and `fit` (`role_category`,
   `matched_requirements`, `missing_requirements`).
2. Read `resume/base_resume.md`.
3. Write `<folder>/resume.md` in exactly the base resume's markdown format. The PDF comes from a
   Google Docs template with fixed slots, so the structure must match the base resume:
   - Name line, contact line, every experience `###` heading, the italic date lines and the whole
     Education section copied **verbatim**; experience entries stay in their original order.
   - Same number of Skills lines as the base, and the same number of bullets in every entry.
   - You may reorder projects (only if their bullet counts match), bullets within an entry, and
     items within skill lines; you may rename a skill line's category.
   - Rewrite the Summary (2-3 sentences) toward the role and reword bullets to mirror the job's
     language **only where it truthfully describes the same work**.
   - Emphasis by `fit.role_category` — see `role_emphasis` in `config/search.yaml`.
4. A hook validates `resume.md` when you write it. If it blocks the write, fix exactly what it
   reports and write again — never work around it.
5. Run `.venv/Scripts/python scripts/finalize_job.py <folder>` — it renders the PDF via gws, checks
   it is one page, saves `Desktop/Job Applications/<date>/<Company - Title>/Srikanth Valluri Resume.pdf`
   and appends the Sheet row. If it reports the PDF is too long, shorten the summary/bullets and rerun.

## Hard rules (the validator enforces these)

- Never invent experience, employers, tools, technologies, metrics, numbers or years of experience.
  Reorder and reword only.
- Every Skills item must appear in the base resume or `resume/skills_inventory.md` (spelled as there).
  Build the Skills section per job: the JD's keywords the candidate has first, in the JD's wording
  (`scripts/ats.py` `jd_keywords` lists them). Inventory-only skills go in Skills, never in bullets. Anything in `config/tech_terms.txt` that the base resume doesn't contain
  (tools and claims like "ranking", "MLOps") must not appear anywhere.
- `missing_requirements` stay missing. Mention them to the user; never add them to the resume.
- No placeholders (`[Company]`, `TODO`, `{{...}}`) and no numbers that aren't in the base resume.

When done, tell the user which jobs were delivered (title, company, score, link, PDF path) and list
each job's top missing requirements.
