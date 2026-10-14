# Job Apply Agent

Finds entry-level, remote, full-time AI/ML/Data jobs that can hire from India and pay in USD/EUR,
scores each against my resume, tailors the resume per job (reorder/reword only — never invented),
renders it to PDF through a Google Docs template, and logs it to a Google Sheet. Runs daily at
7 AM IST on Modal (cloud); new PDFs are copied to my Desktop when I log in. It never auto-applies — I apply with the PDF (e.g. via Simplify Jobs).

Free tiers only: Apify free plan, OpenRouter `:free` models, Google Workspace CLI, Telegram.

## Output

```
Desktop\Job Applications\
  2026-10-14\                                   ← one folder per scrape
    Flexiple - Machine Learning Engineer\        ← one folder per qualifying job
      Srikanth Valluri Resume.pdf                ← the only file
```
The same PDF is copied to Google Drive (`Job Applications/<date>/<job>/`). The
tracker Sheet (its ID is `GOOGLE_SHEET_ID` in `.env`)
gets one row per job: link, fit score and reason, missing skills, talking points, clickable Drive link,
Desktop path, a colour-coded **Status** dropdown (every job starts as **Not Applied** → switch to **Applied** → Interview → Offer / Rejected /
Skipped) and empty **Applied On** / **Notes** columns for you. The agent only appends rows — your edits are safe.

## Pipeline

```
scrape (LinkedIn + Naukri via Apify + RemoteOK / Remotive / We Work Remotely)
  → hard filters (remote, full-time, ≤2 yrs, not US/EU-locked, not INR)    rule-based, no LLM
  → score 1-10 vs resume (top 25 by keyword pre-rank; ≥7 and USD/EUR/unknown pay qualifies)
  → tailor (LLM returns a re-emphasis plan; contact/employers/dates/education copied verbatim)
  → validate (no new tools, claims, numbers, employers, placeholders) — failures are flagged
  → gws: copy Docs template → fill {{placeholders}} → export PDF → delete copy
  → one-page check (one shorter retry, else flagged) → Desktop + Drive copy → Sheet row
  → Windows notification (+ Telegram if configured): summary, or "JOB-APPLY-AGENT — WHAT BROKE"
```

## Setup and run (Windows)

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env        # fill APIFY_API_TOKEN, OPENROUTER_API_KEY, LINKEDIN_USER_AGENT
```

### LinkedIn cookies

The LinkedIn actor (`curious_coder/linkedin-jobs-search-scraper`) searches as your logged-in account.
1. Install the [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)
   Chrome extension, open linkedin.com while logged in, and **Export → JSON**.
2. Save it as `secrets\linkedin_cookies.json` (gitignored).
3. Google "my user agent" in the same browser and put it in `.env` as `LINKEDIN_USER_AGENT`.

Cookies expire (and logging out of LinkedIn invalidates them): when the run summary shows a
`linkedin` source error, export them again. The other sources still run meanwhile.

Then follow [GWS_SETUP.md](GWS_SETUP.md) (Google sign-in, template + Sheet, Telegram, schedule), and:
```
.venv\Scripts\python scripts\run_pipeline.py
```
Flags: `--raw output/runs/<date>/raw_jobs.json` (reuse a scrape, no Apify spend), `--ignore-seen`
(re-process earlier jobs), `--no-tailor` (stop after scoring), `--notify` (Windows notification + Telegram).

## Applying (Option B: Simplify + tailored PDF)

Simplify Copilot is installed in Chrome **Profile 2** (override with `SIMPLIFY_CHROME_PROFILE`).
```
.venv\Scripts\python scripts\apply_session.py              # Not Applied jobs, best fit first
.venv\Scripts\python scripts\apply_session.py --open 5     # open top 5 links in that profile + PDF folder
.venv\Scripts\python scripts\apply_session.py --applied 3 7   # mark rows Applied with today's date
```
Per job: Simplify autofills → upload that job's `Srikanth Valluri Resume.pdf` → submit → mark Applied
(script, or the Status dropdown in the Sheet).

## Tailoring with Claude Code instead

```
.venv\Scripts\python scripts\run_pipeline.py --no-tailor     # prepares job folders
claude   →   "tailor resumes for today's jobs"              # uses .claude/skills/tailor-resume
```
A hook (`.claude/hooks/resume_hook.py`) runs the same validator on every `resume.md` Claude writes;
`scripts/finalize_job.py` then renders, saves and logs it.

## Costs and limits

- **Apify** (free-plan prices): LinkedIn $0.005/run (2 runs) + $0.0015/job, ≤ $0.055/day; Naukri ≤ $0.10/day.
  `daily_budget_usd` (0.16) in `config/search.yaml` is enforced → ≤ $5/month, the free credit.
- **OpenRouter free models**: ≈ 50 requests/day. Per run: ≤25 scoring calls + 2-3 per qualified job (≤6 jobs).
- **Free boards** are low-volume (Remotive's public API returns a fixed ~16 jobs).
- **Modal**: ~25 min/day on a small CPU container — cents per month, inside the free monthly credit.
- The PC doesn't need to be on; the Desktop copy happens at the next login (or 08:00).

## Privacy

`resume/base_resume.md` (real phone/email), `.env`, `secrets/`, `output/` and `tools/gws/` are
gitignored; `resume/base_resume.example.md` is the redacted copy to commit.
