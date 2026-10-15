"""Modal deployment — runs the whole pipeline daily at 07:00 IST, before the user wakes up.

    .venv/Scripts/modal deploy modal_app.py            # (re)deploy the schedule
    .venv/Scripts/modal run modal_app.py --smoke       # cloud check: Google login, template, Drive, Sheet (no Apify/LLM)
    .venv/Scripts/modal run modal_app.py --force-fail  # test the "WHAT BROKE" ntfy alert
    .venv/Scripts/modal run modal_app.py --email       # send today's tracker email from the cloud
    .venv/Scripts/modal run modal_app.py               # one full run now (spends Apify credit + LLM calls)

Secrets: Modal secret "job-apply-agent", written by scripts/push_modal_secrets.py from .env and the
local gws login. Run state (seen jobs, per-run JSON, PDF staging) lives on the "job-apply-agent-data"
Volume mounted at /root/app/output. Resumes go to Drive; scripts/sync_desktop.py (local, at logon /
08:00) pulls them to the Desktop and turns state/last_run.json into a Windows notification — including
"cloud run failed" and "Google login expired".
"""

import os
import sys
from pathlib import Path

import modal

HERE = Path(__file__).resolve().parent
APP_DIR = "/root/app"
GWS_URL = ("https://github.com/googleworkspace/cli/releases/download/v0.22.5/"
           "google-workspace-cli-x86_64-unknown-linux-musl.tar.gz")
GWS_SHA256 = "4db473dde4b1ab872e4ff35d769b0d4af1f1a6441a605e79d5cf8ada9c87e920"

app = modal.App("job-apply-agent")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("curl", "ca-certificates")
    .run_commands(
        f"curl -sSL -o /tmp/gws.tgz {GWS_URL}",
        f"echo '{GWS_SHA256}  /tmp/gws.tgz' | sha256sum -c -",
        "tar xzf /tmp/gws.tgz -C /usr/local/bin ./gws && chmod +x /usr/local/bin/gws && rm /tmp/gws.tgz",
    )
    .pip_install_from_requirements(str(HERE / "requirements.txt"))
    .env({"JOB_AGENT_DELIVERY": "cloud", "GWS_BIN": "/usr/local/bin/gws", "PYTHONUNBUFFERED": "1"})
    .add_local_dir(HERE / "scripts", f"{APP_DIR}/scripts", ignore=["__pycache__"])
    .add_local_dir(HERE / "config", f"{APP_DIR}/config")
    .add_local_dir(HERE / "resume", f"{APP_DIR}/resume")  # base resume + skills inventory
)
volume = modal.Volume.from_name("job-apply-agent-data", create_if_missing=True)
common = dict(image=image, secrets=[modal.Secret.from_name("job-apply-agent")],
              volumes={f"{APP_DIR}/output": volume}, timeout=2 * 60 * 60)  # 25 scoring + 6 tailoring calls ≈ 40 min; headroom for free-model backoff


def _bootstrap():
    sys.path.insert(0, f"{APP_DIR}/scripts")
    from common import setup_logging

    setup_logging()


@app.function(schedule=modal.Cron("0 7 * * *", timezone="Asia/Kolkata"), **common)
def daily_run(force_fail: bool = False):
    if force_fail:
        os.environ["JOB_AGENT_FORCE_FAIL"] = "1"
    _bootstrap()
    import email_report
    import notify
    import run_pipeline
    from common import STATE_DIR, now_iso, today, write_json

    # The PC can't receive pop-ups from the cloud, so the outcome is written to the volume and
    # scripts/sync_desktop.py reads it at logon / 08:00 and shows the Windows notification. The daily
    # email (tracker report, or the failure) goes out from here, so it arrives even with the laptop off.
    status = {"date": today(), "started_at": now_iso(), "status": "failed", "error": "run did not finish"}
    try:
        summary = run_pipeline.run()
        notify.report(summary)
        status.update(status="ok", error="", funnel=summary["funnel"], warnings=summary["warnings"][:5],
                      flagged=len(summary["flagged"]))
        status["emailed"] = email_report.send_report(summary)
        return summary["funnel"]
    except Exception as e:
        status["error"] = f"{type(e).__name__}: {e}"[:600]
        notify.failure_alert(e)
        status["emailed"] = email_report.send_report(error=e)  # never raises
        raise
    finally:
        status["finished_at"] = now_iso()
        write_json(STATE_DIR / "last_run.json", status)
        volume.commit()


@app.function(**common)
def smoke_test():
    """Everything Google-side from the cloud, without Apify or LLM spend: login, template render,
    Drive upload, Sheet row (a TEST row the local sync then pulls to the Desktop)."""
    _bootstrap()
    import write_sheet
    from common import DATA_DIR, base_resume_text, today
    from gdrive import root_folder_id, upload_pdf
    from gws import check_auth
    from render_gdoc import render_pdf, template_id

    check_auth()
    template_id()
    root_folder_id()
    pdf = DATA_DIR / "tmp" / "smoke.pdf"
    pages = render_pdf(base_resume_text(), pdf, copy_name="smoke")
    link = upload_pdf(pdf, ["TEST", "Modal - SMOKE TEST delete me"], "Srikanth Valluri Resume.pdf")
    job = {"title": "SMOKE TEST delete me", "company": "Modal", "link": f"https://example.com/modal-smoke-{today()}",
           "source": "modal-smoke", "fit": {"score": 0}}
    rows = write_sheet.append_rows([write_sheet.row_for(job, None, link, "", "")])
    volume.commit()
    return {"pdf_pages": pages, "drive": link, "sheet_rows_added": rows}


@app.function(**common)
def connectivity_test():
    """Reachability of the non-Google services from the cloud: job boards + Apify inputs (dry run,
    no Apify spend) and OpenRouter (one tiny call; a daily-limit reply still proves key + network)."""
    _bootstrap()
    import llm
    from common import load_config
    from scrape_jobs import scrape

    res = scrape(load_config(), dry_run=True)
    try:
        reply = llm.chat_json("You are a health check.", 'Return {"ok": true}', max_tokens=50)
        llm_status = f"ok: {reply}"
    except llm.DailyLimitReached as e:
        llm_status = f"reachable, key valid, but today's free limit is used up ({e})"
    return {"boards": res["per_source"], "source_errors": res["errors"],
            "apify_worst_case_usd": res["apify_usd"], "openrouter": llm_status}


@app.function(**common)
def email_test():
    """Sends today's tracker email from the cloud (Gmail scope + REPORT_EMAIL_TO), no Apify/LLM spend."""
    _bootstrap()
    import email_report

    return {"emailed": email_report.send_report(), "to": email_report.recipient()}


@app.local_entrypoint()
def main(smoke: bool = False, force_fail: bool = False, connectivity: bool = False, email: bool = False):
    if email:
        print(email_test.remote())
    elif connectivity:
        print(connectivity_test.remote())
    elif smoke:
        print(smoke_test.remote())
    else:
        print(daily_run.remote(force_fail))
