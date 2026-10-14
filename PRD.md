# PRD — Job Application Agent (Srikanth Valluri)

Drop this file into the empty project folder as `PRD.md`. Run `claude`, then `/init`,
then point Claude at this file to start planning.

## 1. What This Is

A job application AI agent, built from scratch. It finds entry-level, remote, full-time AI/ML/Data
postings that hire from India and pay in USD/EUR, tailors my resume to each one, and runs itself
every day. It doubles as a portfolio project.

**Candidate:** Srikanth Valluri — Associate Software Engineer (Generative AI), ~1 year experience.
B.Tech in AI & ML (2025). Based in Hyderabad, India; wants to stay in India and work remotely.
Base resume lives at `resume/base_resume.md` (Python, SQL, LLMs, RAG, LangChain/LangGraph,
Azure OpenAI, Azure AI Search, PyTorch/TensorFlow, Scikit-learn, Docker, AWS S3/EC2).

**Constraint: the whole stack must be free.** Free tiers and free models only — no paid plans.

## 2. Goal

End to end, one run should:
1. Pull fresh postings for my target roles (see 3.1).
2. Drop anything that fails the hard filters (not remote, not full-time, too senior, can't hire from India).
3. Score each remaining job against my base resume's skills/experience.
4. Keep only jobs scoring at or above `MIN_FIT_SCORE` (default 7/10).
5. Tailor the resume per qualifying job (no fabrication).
6. Log each qualifying job to a Google Sheet (job link + resume link + score).
7. Save each tailored resume as a file in its own per-job folder (Desktop locally, Drive on Modal).
8. Run unattended, every morning (IST), hosted on Modal.

## 3. Pipeline Steps

### 3.1 Scrape
- **Target roles (search queries):**
  - AI Engineer
  - Generative AI / GenAI Engineer
  - Machine Learning Engineer
  - LLM Engineer / Agentic AI Engineer
  - Python Developer (AI/ML-focused)
  - Junior Data Analyst
  - Data Annotation / AI Data Specialist
- **Sources (all free):**
  - Apify free tier — an actor that covers LinkedIn job search (primary).
  - Remote-job board APIs/feeds as backup: RemoteOK API, Remotive API, We Work Remotely RSS.
- **Search filters:** remote, full-time, entry level / associate, posted in the last 24-48 hours.
- Role list and sources live in `config/search.yaml` so they can change without touching code.
- **Dedupe** by job link + (company, title) across sources and against jobs already in the Sheet,
  so the same posting is never processed twice.
- Output: list of `{title, company, link, description, location, employment_type, posted_date, source}`.

### 3.2 Hard Filters (rule-based, before any LLM call)
Reject a job outright if any of these is true:
- Not remote (onsite or hybrid outside India).
- Not full-time (contract, freelance, part-time, internship, "per task" gigs).
- Clearly senior: title contains Senior / Sr. / Lead / Staff / Principal / Manager / Head, or the
  description requires 3+ years of experience.
- Location-locked outside India: "US only", "must be authorized to work in the US/EU/UK",
  "must reside in …" (unless India / APAC / worldwide is also allowed).
- Log how many jobs each rule removed.

### 3.3 Score + Filter
- Compare each job's requirements against `resume/base_resume.md` skills/experience, extracting
  matched/missing requirements as part of the same scoring call (no separate parse step).
- The scoring call also returns: `seniority_fit` (does it suit ~1 yr experience?),
  `india_eligible` (yes / no / unclear), and `pay_currency` if mentioned (USD / EUR / other / unknown).
- Score 1-10 fit. Seniority mismatch and `india_eligible = no` cap the score at 5.
- **Hard cutoff: only jobs scoring `MIN_FIT_SCORE` (default 7) or higher continue past this step.**
- Log the funnel every run, e.g. "42 scraped → 18 after hard filters → 5 qualified".

### 3.4 Tailor Resume (Skill)
- `.claude/skills/tailor-resume/SKILL.md`
- Input: base resume + the job's matched/missing requirements.
- Output: reordered/re-emphasized experience and projects, mirrored language, matching keywords.
- Role-aware emphasis:
  - AI / GenAI / LLM / Agentic roles → lead with RAG systems, Azure OpenAI apps, LangChain/LangGraph,
    LUDO Bot project.
  - ML Engineer roles → lead with predictive models, Scikit-learn/PyTorch/TensorFlow, internship work.
  - Data Analyst roles → lead with SQL, Pandas/NumPy, ETL pipelines, Restaurant AI Assistant (sales data).
  - Data Annotation roles → lead with data quality, preprocessing/cleaning, NLP and LLM familiarity.
- **Hard rule: never invent experience, employers, tools, metrics, or years of experience.
  Reorder and reword only.** Missing requirements stay missing — they are logged, not added.

### 3.5 Validate (Hook)
- Before a tailored resume is saved or logged: check required sections are present
  (Summary, Skills, Experience, Projects, Education), contact details unchanged,
  no placeholder text left over.
- Fabrication check: every tool/technology in the tailored Skills section must also appear
  in the base resume.
- On a validation failure, do not write the row — flag it instead.

### 3.6 Company Research
- `scripts/company_research.py` — per qualifying job, one OpenRouter call returns 3-5 talking
  points (what the company builds, its AI stack if known, why the role fits me). Used for context
  and interview prep, not written verbatim into the resume. This runs as a plain script, not a
  Claude Code subagent, since the automated Modal path has no live interactive session.

### 3.7 Outputs
- **Google Sheet** (one row per qualifying job): job title, company, role category, job link,
  source, fit score, india_eligible, pay_currency, top missing skills, resume file link/path,
  status (`To Apply` / `Applied` / `Skipped`), timestamp.
- **Per-job folder**, named `{company}-{job-title-slug}/`, containing the tailored resume file —
  on the Desktop for the interactive path, in Google Drive for the automated Modal path (which has
  no Desktop access). The Sheet row references this folder/file either way.

### 3.8 Headless + Hosting
- The automated path (`scripts/tailor_job.py`, `scripts/score_jobs.py`, `scripts/company_research.py`)
  uses **OpenRouter free models** (`:free` variants) instead of live Claude reasoning, so it runs
  unattended with no paid API key. Model name is set in `.env` so it can be swapped easily.
- Add retry with backoff for free-model rate limits.
- Deploy as a Modal scheduled function (free monthly credits): `@app.function(schedule=modal.Cron(...))`,
  once daily at ~8:00 AM IST (02:30 UTC).
- Secrets (Apify key, OpenRouter key, Google credentials, Telegram bot token) via Modal secrets —
  never committed.
- Wrap the scheduled run in try/except; on any failure send a Telegram alert named
  `JOB-APPLY-AGENT — WHAT BROKE`, then re-raise. No silent failures.
- On success, send a short Telegram summary: funnel counts + top 3 jobs with links.

## 4. Out of Scope

- Auto-submitting applications. The agent prepares; I still click apply.
- Contract, freelance, part-time and internship roles — full-time only.
- Senior roles (3+ years required).
- Resume visual design/formatting polish beyond the text content.
- Any paid tool, plan, or model.

## 5. Privacy

- `resume/base_resume.md` contains my real phone number and email. Keep the GitHub repo private,
  or commit a redacted `resume/base_resume.example.md` and gitignore the real one.
- `output/` and `.env` are gitignored.

## 6. Folder Structure

```
job-apply-agent/
  CLAUDE.md
  PRD.md
  README.md
  GWS_SETUP.md
  .env.example
  config/search.yaml           # target roles, sources, filters, MIN_FIT_SCORE
  .claude/skills/tailor-resume/SKILL.md
  .claude/hooks/resume_hook.py
  resume/base_resume.md        # gitignored if repo is public
  scripts/
    scrape_jobs.py             # Apify (LinkedIn) + RemoteOK / Remotive / WWR
    filter_jobs.py             # rule-based hard filters + dedupe
    score_jobs.py              # scores + extracts matched/missing requirements
    tailor_job.py              # automated tailoring path (OpenRouter, for Modal)
    company_research.py
    write_sheet.py             # Google Sheets
    format_resume_doc.py       # markdown -> real Google Docs formatting
    validate_resume.py         # shared validation logic (incl. fabrication check)
    notify.py                  # Telegram alerts + daily summary
  output/                      # gitignored — raw/filtered/scored/tailored job data
  modal_app.py                 # scheduled entrypoint
  .env                         # gitignored — Apify key, OpenRouter key, Google creds, Telegram bot token
```

## 7. Verification Per Step

- Scrape: returns N jobs with title, company, link, description, location, employment_type, source;
  no duplicates.
- Hard filters: no onsite, non-full-time, senior, or US/EU-only jobs survive; per-rule reject counts logged.
- Score: every job has a numeric score; jobs below `MIN_FIT_SCORE` are excluded from all downstream steps.
- Tailor: output resume has no placeholder text, no tools absent from the base resume,
  and passes the validation hook.
- Sheet: row count matches qualifying-job count; every row has a working job link and resume
  reference; re-running the same day adds no duplicate rows.
- Desktop: folder count matches qualifying-job count; each folder has exactly one resume file.
- Headless run: `claude -p` completes with the same output as the interactive run, restricted to
  the allowed tools.
- Modal: scheduled function deploys, manual trigger runs end to end, success sends the Telegram
  summary, forced failure produces the named Telegram alert.