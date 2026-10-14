# AI Job Application Agent

**Fastest way to use this:** give this whole repo to any Claude Code agent (or Cursor, Codex,
whatever you use) and say "set this up for me." The pipeline order, every environment variable,
and the exact run/deploy commands are all below — nothing else to figure out.

## What This Is

An AI agent that finds **entry-level, remote, full-time AI / ML / Data jobs** that can hire from
India and pay in USD/EUR, scores each one against my resume, tailors a **one-page resume** for every
job that's a strong fit (reorder and reword only — never invented), researches the company, and logs
it all to a Google Sheet. It runs on its own on **Modal every morning at 7:00 AM IST**; when I log in,
the new PDFs are already on my Desktop.

The whole stack is free: Apify free tier, OpenRouter `:free` models, the Google Workspace CLI, and
Modal's free monthly credit.

## Architecture

```
Scrape (Apify LinkedIn + RemoteOK / Remotive / We Work Remotely)
    -> Hard filters (rule-based: remote, full-time, <=2 yrs, can hire from India, not INR)
        -> Score + Filter (OpenRouter, 7+/10 cutoff, ATS keyword match)
            -> Tailor Resume (Skill + validation Hook; skills only from a proven skills inventory)
                -> Render PDF (Google Docs template via gws CLI, one page)
                    -> Company Research (OpenRouter)
                        -> Drive + Google Sheet (gws CLI) -> Desktop sync + alerts (local)
```

Every job that survives the hard filters gets scored against the base resume. Only jobs scoring
7 or higher (with USD/EUR or unstated pay) continue — the rest are remembered as "seen" so they're
never processed twice, and never touch the Sheet.

## Setup

**Prerequisites:**
- Python 3.12+ (developed on Windows 11; the cloud run is Linux)
- The [`gws` CLI](https://github.com/googleworkspace/cli) v0.22.5 — see [`GWS_SETUP.md`](GWS_SETUP.md)
  for the full walkthrough (binary, Google Cloud project, OAuth, first login)
- An [Apify](https://apify.com) account (free tier, $5/month credit)
- An [OpenRouter](https://openrouter.ai) account (free-tier models used by default)
- A [Modal](https://modal.com) account, only if you want the daily cloud run

**Step 1 — Install:**

```bash
git clone https://github.com/VSrikanth05/ai-job-application-agent.git
cd ai-job-application-agent
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

**Step 2 — Environment:**

```bash
copy .env.example .env            # macOS/Linux: cp .env.example .env
# fill in APIFY_API_TOKEN and OPENROUTER_API_KEY (see .env.example for where to get each)
```

**Step 3 — Your resume:**

```bash
copy resume\base_resume.example.md resume\base_resume.md
```

Put your real resume in `resume/base_resume.md` (same markdown layout; it's gitignored so your phone
number never gets committed). List every skill you can back up in `resume/skills_inventory.md` —
tailoring may only use skills from these two files.

**Step 4 — Google (Sheet, Drive, resume template):** sign in to `gws` as described in
[`GWS_SETUP.md`](GWS_SETUP.md), then:

```bash
python scripts/google_setup.py
```

This creates a **Job Applications** Drive folder with the resume template (Google Doc) and the
tracker Sheet inside, and writes `GOOGLE_RESUME_TEMPLATE_ID`, `GOOGLE_SHEET_ID` and
`GOOGLE_DRIVE_FOLDER_ID` into `.env` for you — leave those blank.

**Step 5 — Check the search settings:** roles, sources, filters, the fit cutoff and the daily Apify
budget all live in [`config/search.yaml`](config/search.yaml). Preview what a run would do and cost
(no Apify spend):

```bash
python scripts/scrape_jobs.py --dry-run
```

## Running It

The whole pipeline in one command:

```bash
python scripts/run_pipeline.py
```

Or step by step:

```bash
python scripts/run_pipeline.py --no-tailor    # 1-4. scrape -> filter -> score -> one job folder
                                              #      (output/runs/<date>/jobs/<job>/job.json) per qualifying job
python scripts/tailor_job.py <job folder>     # 5. resume.md (validated, no fabrication)
python scripts/finalize_job.py <job folder>   # 6. PDF -> Drive/Desktop -> research -> Sheet row
```

Each early stage also runs on its own for inspecting/debugging, reading and writing today's
`output/runs/<date>/`: `scrape_jobs.py` (raw_jobs.json) → `filter_jobs.py` (filtered.json, jobs removed
per rule) → `score_jobs.py` (scored.json: fit score, matched/missing, pay currency).
Re-run without new scraping: `run_pipeline.py --raw output/runs/<date>/raw_jobs.json --ignore-seen`.

Or as a single test run in the cloud through Modal (without deploying it):

```bash
modal run modal_app.py
```

Output per qualifying job: `Desktop\Job Applications\<date>\<Company - Job Title>\Srikanth Valluri Resume.pdf`,
the same PDF in Drive, and one Sheet row (job link, fit score, ATS match, missing skills, talking
points, Drive link, and a **Not Applied → Applied** status dropdown).

## Deploying (daily automatic run)

```bash
modal token new                                # once: sign in to Modal
python scripts/push_modal_secrets.py           # uploads .env keys + the gws login as Modal secret "job-apply-agent"
modal deploy modal_app.py
```

Runs daily at **7:00 AM Asia/Kolkata**. Re-run `push_modal_secrets.py` after a Google re-login, and
`modal deploy` after changing code, `config/` or `resume/`. Checks that spend nothing (or almost):

```bash
modal run modal_app.py --connectivity   # job boards, Apify dry run, one OpenRouter call
modal run modal_app.py --smoke          # Google login, template -> PDF, Drive upload, Sheet row
modal run modal_app.py --force-fail     # proves the failure alert path
```

## Desktop Sync + Failure Alerts

The cloud can't reach my PC, so a small Windows scheduled task does the local half:

```bash
powershell -ExecutionPolicy Bypass -File scripts\install_schedule.ps1
```

At every logon and at 08:00 it runs `scripts/sync_desktop.py`, which:

1. Downloads the day's new resume PDFs from Drive to the Desktop and fills the Sheet's "Desktop PDF" column.
2. Reads the cloud run's status (`state/last_run.json` on the Modal volume) and shows a Windows
   notification if today's run **failed** or **didn't happen** — a broken cron never fails silently.
3. Warns a day before the Google login expires (Testing-mode OAuth lasts 7 days) and alerts once it has.

Telegram / ntfy phone alerts are wired into `scripts/notify.py` too, but off unless you set
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` or `NTFY_TOPIC`.

## Honest Status

- **Verified end to end in the cloud** (first full run): 65 jobs scraped → 12 after hard filters →
  10 scored → 0 qualified that day (best 6/10). Cloud smoke test: template → one-page PDF → Drive →
  Sheet → Desktop sync all work. Forced failure → desktop alert works.
- **Claude Code path verified headless** (`claude -p` with only the needed tools): tailored, blocked
  once by the hook (missing section), fixed, rendered, logged.
- Real bugs found and fixed along the way:
  - `gws` refuses `--upload/--output` paths that resolve outside its working directory — the Modal
    volume is a symlink, so `scripts/gws.py` now stages every file through `.gws_io/`.
  - The validation hook read Claude Code's payload as cp1252 on Windows and rejected every resume
    containing "—" — fixed in `.claude/hooks/resume_hook.py`.
  - The model borrowed claims from job ads ("ranking, recommendation") — now blocked by the
    capability list in `config/tech_terms.txt`.
- Known limits: OpenRouter's free tier allows ~50 requests/day (scoring stops cleanly and resumes
  the next day); the Google login expires every 7 days (one-click re-login); a qualifying job hasn't
  yet gone through *cloud* tailoring on a live day (each piece is tested separately).

## After the Sheet Is Ready — Applying

This repo stops at "qualifying jobs, tailored resumes, one spreadsheet row each." Nothing here
auto-submits an application — a human always makes the final click.

**Recommended — [Simplify Jobs](https://simplify.jobs/copilot)** (free Chrome extension). Fill out
its one-time profile, then it autofills the rest of any application form. A helper opens the best
"Not Applied" jobs for you:

```bash
python scripts/apply_session.py             # list Not Applied jobs, best fit first
python scripts/apply_session.py --open 5    # open the top 5 job links in Chrome + the PDF folder
python scripts/apply_session.py --applied 3 7   # mark Sheet rows 3 and 7 as Applied (today's date)
```

For each job: let Simplify autofill → upload that job's tailored PDF → submit → mark **Applied**.

**Alternative — [browser-use](https://github.com/browser-use/browser-use).** An open-source framework
for building a browser-controlling AI agent that could auto-apply from the Sheet — less reliable than
filling forms yourself with autofill, and not implemented here.

## Claude Code Building Blocks Used

- **`CLAUDE.md`** — always-on project memory: hard rules, pipeline order, gotchas, deploy commands.
- **A Skill** (`.claude/skills/tailor-resume/`) — the interactive resume-tailoring procedure.
- **A Hook** (`.claude/hooks/resume_hook.py`) — blocks any tailored resume from being saved if it's
  missing a section, changes contact details, leaves placeholders, or names a tool/number/claim the
  base resume (or skills inventory) can't back up.
- **Headless mode** — `claude -p` runs the same Skill + Hook with a restricted tool list.
- **Company research runs as a plain script**, not a Claude Code subagent — a real subagent needs a
  live interactive session, which Modal's headless daily cron can't provide.

## Limits

- Target roles: AI / GenAI / ML / LLM / Agentic AI Engineer, Python Developer (AI), Junior Data
  Analyst, Data Annotation / AI Data Specialist — entry level, remote, full-time, hiring from India.
- The 7+/10 fit cutoff is enforced in code; nothing below it reaches tailoring or the Sheet.
- Tailoring only reorders and rewords what's already on the base resume — it never invents
  experience, employers, tools, metrics, or years. Missing skills are listed in the Sheet, not added.
- Free tiers set the pace: ≤ $0.16/day of Apify credit and ~50 free LLM requests/day.
- A human always clicks the final "apply" — nothing here submits an application on its own.

## Privacy

`.env`, `resume/base_resume.md` (real phone number), `secrets/`, `output/` and `tools/gws/` are
gitignored; `resume/base_resume.example.md` is the redacted copy. Cloud credentials live only in
the Modal secret.

## Credits

README structure adapted from [sskghub/job-application-ai-agent](https://github.com/sskghub/job-application-ai-agent).

## License

MIT — see [`LICENSE`](LICENSE).
