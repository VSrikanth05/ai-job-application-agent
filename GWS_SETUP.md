# Setup: Google (gws), Telegram, daily schedule

Resumes are rendered through a Google Docs template with the Google Workspace CLI
([`gws`](https://github.com/googleworkspace/cli), pinned v0.22.5 in `tools/gws/`) and saved as
`Desktop\Job Applications\<date>\<Company - Job Title>\Srikanth Valluri Resume.pdf`.
Jobs are logged to a Google Sheet. All free.

## 0. gws binary (skip if `tools/gws/gws.exe` exists)

Download `google-workspace-cli-x86_64-pc-windows-msvc.zip` from the
[v0.22.5 release](https://github.com/googleworkspace/cli/releases/tag/v0.22.5), check its `.sha256`,
and unzip `gws.exe` into `tools/gws/`.

## Current setup (done 2026-10-14)

- Google Cloud project **Job Apply Agent** (an existing project reused — the account is at its
  25-project quota). Only Drive, Docs, Sheets and Gmail APIs are enabled.
- OAuth app is in **Testing** (Publish was blocked without a home page/privacy policy). Test users:
  your Gmail account(s). Google expires Testing logins after
  **7 days** — when the daily run fails with an auth error, re-login:
  ```
  tools\gws\gws.exe auth login --scopes https://www.googleapis.com/auth/drive.file,https://www.googleapis.com/auth/gmail.send
  ```
  Open the printed URL, and on the permissions screen **tick both boxes** — Drive and "Send email on
  your behalf" (they are unticked by default; without them the login has no Drive access / can't send
  the daily email). If calls still fail with "insufficient
  authentication scopes", delete `%USERPROFILE%\.config\gws\token_cache.json` (stale cached token).
- Scopes: `drive.file` (the app sees just the files it created: template, Sheet, temp copies) and
  `gmail.send` (send-only — it can't read your mail), used for the daily tracker email.

## 1. Sign in to Google (≈5 min, needs your browser)

```
gcloud auth login
tools\gws\gws.exe auth setup --login
```
`auth setup` creates a Google Cloud project, enables the APIs and an OAuth client, then opens the
browser to sign in. When asked which services to allow, pick only **drive, docs, sheets** (unverified
apps fail with too many scopes). On "Google hasn't verified this app", click **Continue**.

Then, **once**, open the project's OAuth consent screen
(`https://console.cloud.google.com/apis/credentials/consent`) and click **Publish app**
(*In production*). In *Testing* mode Google expires the login after 7 days and the daily run would
start failing. If login ever says "Access blocked", add your Gmail under **Test users** there.

Check: `tools\gws\gws.exe auth status` should show a credential source other than `none`.

## 2. Create the resume template + tracker Sheet

```
.venv\Scripts\python scripts\google_setup.py
```
Creates a **Job Applications** folder in your Drive containing:
- **Srikanth Valluri Resume - TEMPLATE (do not delete)** — a Google Doc built from
  `resume/base_resume.md` with `{{PLACEHOLDERS}}` where tailored text goes. Restyle it however you like
  in Google Docs (fonts, colours, spacing) — keep every `{{...}}` intact and the resume on one page.
- **Job Applications Tracker** — the Sheet (one row per qualifying job).

IDs are saved to `.env`; resume PDFs are also copied into this folder per date/job. Re-running
the script is safe (it re-applies the Sheet's dropdown/colour formatting). After editing `resume/base_resume.md`, rebuild the template with
`--template`.

## 3. Telegram (optional — Windows notifications already work, ≈3 min)

1. In Telegram, message **@BotFather** → `/newbot` → copy the token → `TELEGRAM_BOT_TOKEN` in `.env`.
2. Send any message to your bot, open `https://api.telegram.org/bot<TOKEN>/getUpdates`, copy
   `"chat":{"id": ...}` → `TELEGRAM_CHAT_ID`.
3. Test: `.venv\Scripts\python scripts\notify.py "hello"`

## 4. Cloud run on Modal (07:00 IST)

1. `.venv\Scripts\pip install modal` → `.venv\Scripts\modal token new` (browser).
3. `.venv\Scripts\python scripts\push_modal_secrets.py` — uploads `.env` keys + the gws login as Modal secret `job-apply-agent`. **Re-run after every Google re-login.**
4. `.venv\Scripts\modal deploy modal_app.py` (redeploy after code/config/resume changes).
5. Checks: `modal run modal_app.py --connectivity` (boards, Apify dry run, OpenRouter), `modal run modal_app.py --email` (sends today's tracker email), `modal run modal_app.py --smoke` (then delete the TEST row/folder) and `modal run modal_app.py --force-fail`
   (then run `scripts\sync_desktop.py` → failure pop-up; afterwards `modal volume rm job-apply-agent-data state/last_run.json`).

**Daily email:** after every 07:00 run the cloud emails the tracker (new jobs, everything still "Not
Applied", the funnel — or the error if the run failed) to `REPORT_EMAIL_TO` from your own Gmail. Enable
the Gmail API once: `gcloud services enable gmail.googleapis.com --project <your project>`. Preview
without sending: `.venv\Scripts\python scripts\email_report.py --dry-run`.

Alerts are desktop pop-ups shown by the local sync (next section): cloud run failed/missing, Google
login expiring tomorrow, Google login expired. **Weekly re-login** (Testing mode, accepted):
`tools\gws\gws.exe auth login --scopes https://www.googleapis.com/auth/drive.file,https://www.googleapis.com/auth/gmail.send` (tick both boxes)
→ `.venv\Scripts\python scripts\push_modal_secrets.py`.

## 5. Local Desktop sync

```
powershell -ExecutionPolicy Bypass -File scripts\install_schedule.ps1
```
Registers the **JobApplyAgent** task: at every logon (+2 min) and daily at 08:00 it runs
`scripts\sync_desktop.py`, copying new resumes from Drive to `Desktop\Job Applications\<date>\<job>\`. Logs: `output\logs\<date>-sync.log`. Run it now: `Start-ScheduledTask JobApplyAgent`.
Remove: `Unregister-ScheduledTask -TaskName JobApplyAgent`.
