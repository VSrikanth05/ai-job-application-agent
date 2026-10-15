"""Daily email of the job tracker, sent from the user's Gmail to REPORT_EMAIL_TO (gws, gmail.send scope).

Sent by modal_app.daily_run right after the 07:00 run (works with the laptop off):
  - subject: date + how many new jobs (or "no new matches" / "run FAILED")
  - today's funnel, today's new jobs, every job still "Not Applied", status counts, tracker link
The Sheet is the source of truth, so the email shows the user's own status edits.

Usage:
    python scripts/email_report.py            # send today's report now (reads the Sheet)
    python scripts/email_report.py --dry-run  # write the HTML to output/tmp/email_preview.html, don't send
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import sys
import tempfile
import traceback
from collections import Counter
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from common import DATA_DIR, IST, today

log = logging.getLogger("email")

ACCENT = "#1F3A5F"
STATUS_BG = {"Not Applied": "#FCE5CD", "Applied": "#B6D7A8", "Interview": "#CFE2F3", "Offer": "#93C47D",
             "Rejected": "#F4CCCC", "Skipped": "#EFEFEF"}


def recipient() -> str | None:
    return (os.getenv("REPORT_EMAIL_TO") or "").strip() or None


def tracker_url() -> str:
    return f"https://docs.google.com/spreadsheets/d/{os.getenv('GOOGLE_SHEET_ID', '')}/edit"


def sheet_rows() -> list[dict]:
    """All job rows as {column name: value} (the agent's own columns + the user's Status/Applied On/Notes)."""
    from gws import gws
    from write_sheet import COLUMNS, sheet_id

    res = gws("sheets", "spreadsheets", "values", "get",
              params={"spreadsheetId": sheet_id(), "range": f"A2:{chr(ord('A') + len(COLUMNS) - 1)}5000"}) or {}
    rows = []
    for r in res.get("values", []):
        if not r or not str(r[0]).strip():
            continue
        r = list(r) + [""] * (len(COLUMNS) - len(r))
        rows.append(dict(zip(COLUMNS, r)))
    return rows


def _e(s) -> str:
    return html.escape(str(s or ""))


def _link(url: str, text: str) -> str:
    return f'<a href="{_e(url)}" style="color:{ACCENT}">{_e(text)}</a>' if url else _e(text)


def _job_table(rows: list[dict], show_status: bool) -> str:
    if not rows:
        return '<p style="color:#666;margin:4px 0 12px">None.</p>'
    head = ["Company", "Role", "Fit", "ATS", "Pay", "Job", "Resume"] + (["Status", "Added"] if show_status else [])
    th = "".join(f'<th style="text-align:left;padding:6px 8px;border-bottom:2px solid {ACCENT}">{h}</th>' for h in head)
    body = []
    for r in rows:
        status = r.get("Status") or "Not Applied"
        cells = [f"<b>{_e(r.get('Company'))}</b>", _e(r.get("Job Title")), f"{_e(r.get('Fit Score'))}/10",
                 _e((r.get("ATS Match") or "").split(" ")[0]), _e(r.get("Pay Currency") or "—"),
                 _link(r.get("Job Link"), "Open job"), _link(r.get("Resume (Drive)"), "PDF")]
        if show_status:
            cells += [f'<span style="background:{STATUS_BG.get(status, "#eee")};padding:2px 6px;border-radius:4px">'
                      f'{_e(status)}</span>', _e((r.get("Added") or "")[:10])]
        body.append("<tr>" + "".join(f'<td style="padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top">{c}</td>'
                                     for c in cells) + "</tr>")
    return (f'<table style="border-collapse:collapse;font-size:13px;width:100%;margin:4px 0 14px">'
            f"<tr>{th}</tr>{''.join(body)}</table>")


def build(summary: dict | None = None, error: BaseException | None = None) -> tuple[str, str, str]:
    """(subject, plain text, html). Reads the Sheet; if that fails (e.g. Google login expired) the email
    still goes out with whatever is known, so the user hears about it."""
    day = datetime.now(IST)
    when = day.strftime("%a %d %b")
    rows, sheet_err = [], None
    try:
        rows = sheet_rows()
    except Exception as e:  # noqa: BLE001 — a report must never crash the run
        sheet_err = f"{type(e).__name__}: {e}"
    new = [r for r in rows if (r.get("Added") or "").startswith(today())]
    pending = [r for r in rows if (r.get("Status") or "Not Applied") == "Not Applied"]
    pending.sort(key=lambda r: -float(r.get("Fit Score") or 0))
    counts = Counter((r.get("Status") or "Not Applied") for r in rows)

    if error is not None:
        subject = f"⚠ Job agent {when}: run FAILED — {type(error).__name__}"
    elif new:
        subject = f"Job agent {when}: {len(new)} new job{'s' if len(new) != 1 else ''} — " + \
                  ", ".join(r.get("Company", "") for r in new[:4]) + ("…" if len(new) > 4 else "")
    else:
        subject = f"Job agent {when}: no new matches · {len(pending)} still to apply"

    parts = [f'<div style="font-family:Segoe UI,Arial,sans-serif;color:#222;max-width:900px">',
             f'<h2 style="color:{ACCENT};margin:0 0 4px">Job Applications — {_e(day.strftime("%A %d %B %Y"))}</h2>',
             f'<p style="margin:0 0 14px">{_link(tracker_url(), "Open the full tracker")} · '
             f'{len(rows)} jobs tracked · ' + " · ".join(f"{_e(k)}: {v}" for k, v in counts.most_common()) + "</p>"]
    if error is not None:
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))[-1500:]
        hint = ""
        if any(w in str(error).lower() for w in ("auth", "login", "token", "credential")):
            hint = "<p><b>Likely cause: the Google login expired.</b> Ask Claude to “re-login Google”.</p>"
        parts += [f'<h3 style="color:#B00020">This morning’s run failed</h3><p>{_e(type(error).__name__)}: {_e(error)}</p>',
                  hint, f'<pre style="background:#f6f6f6;padding:8px;font-size:11px;white-space:pre-wrap">{_e(tb)}</pre>']
    if summary:
        f = summary.get("funnel", {})
        parts.append(f'<p style="background:#f4f7fb;padding:8px 10px;border-radius:6px">This morning: '
                     f'<b>{f.get("scraped", 0)}</b> jobs found → <b>{f.get("after_filters", 0)}</b> passed your rules '
                     f'(remote, India-eligible, ≤15 days old, entry level, high pay) → <b>{f.get("scored", 0)}</b> scored → '
                     f'<b>{f.get("qualified", 0)}</b> qualified → <b>{f.get("delivered", 0)}</b> resumes ready</p>')
        for w in (summary.get("warnings") or [])[:5]:
            parts.append(f'<p style="color:#8a6d00;margin:2px 0">⚠ {_e(w)}</p>')
    parts += [f'<h3 style="color:{ACCENT};margin:16px 0 4px">New today ({len(new)})</h3>', _job_table(new, False),
              f'<h3 style="color:{ACCENT};margin:16px 0 4px">Still to apply ({len(pending)})</h3>', _job_table(pending, True)]
    if sheet_err:
        parts.append(f'<p style="color:#B00020">Couldn’t read the tracker: {_e(sheet_err)}</p>')
    parts.append('<p style="color:#888;font-size:11px;margin-top:18px">Sent by your job agent after the 07:00 IST run. '
                 'Change a job’s Status in the tracker and it updates here tomorrow.</p></div>')
    html_body = "".join(parts)

    text = [subject, "", f"Tracker: {tracker_url()}", ""]
    if summary:
        f = summary.get("funnel", {})
        text.append(f"{f.get('scraped', 0)} found -> {f.get('after_filters', 0)} passed rules -> {f.get('scored', 0)} scored "
                    f"-> {f.get('qualified', 0)} qualified -> {f.get('delivered', 0)} resumes")
    text += ["", f"New today ({len(new)}):"] + [f"- {r.get('Company')} | {r.get('Job Title')} | {r.get('Fit Score')}/10 | {r.get('Job Link')}" for r in new]
    text += ["", f"Still to apply ({len(pending)}):"] + [f"- {r.get('Company')} | {r.get('Job Title')} | {r.get('Job Link')}" for r in pending]
    return subject, "\n".join(text), html_body


def send(subject: str, text: str, html_body: str, to: str) -> None:
    from gws import gws

    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    tmp = Path(tempfile.mkdtemp()) / "report.eml"
    tmp.write_bytes(msg.as_bytes())
    # Upload as a raw RFC 822 message: no command-line length limit, any HTML size.
    gws("gmail", "users", "messages", "send", params={"userId": "me"}, upload=tmp,
        extra=["--upload-content-type", "message/rfc822"])
    log.info("emailed '%s' to %s", subject, to)


def send_report(summary: dict | None = None, error: BaseException | None = None) -> bool:
    """Never raises (a failed email must not fail or mask the run); returns whether it was sent."""
    to = recipient()
    if not to:
        log.warning("REPORT_EMAIL_TO not set — daily email skipped")
        return False
    try:
        send(*build(summary, error), to)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("daily email failed: %s: %s", type(e).__name__, e)
        return False


def main() -> int:
    from common import read_json, run_dir, setup_logging

    ap = argparse.ArgumentParser(description="Email today's job-tracker report")
    ap.add_argument("--dry-run", action="store_true", help="write output/tmp/email_preview.html instead of sending")
    args = ap.parse_args()
    setup_logging()
    summary = read_json(run_dir() / "summary.json")
    if args.dry_run:
        subject, _, html_body = build(summary)
        out = DATA_DIR / "tmp" / "email_preview.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html_body, encoding="utf-8")
        print(subject, "\n->", out)
        return 0
    return 0 if send_report(summary) else 1


if __name__ == "__main__":
    sys.exit(main())
