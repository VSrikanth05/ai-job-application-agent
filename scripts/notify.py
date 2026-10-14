"""Run notifications: a Windows toast on this PC (always, on Windows) plus Telegram if
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are set. Everything is also logged, so nothing fails silently.

Clicking the toast opens today's Desktop folder (success) or the run log (failure).

Usage:
    python scripts/notify.py "test message"
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import traceback
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import requests

log = logging.getLogger("notify")

ALERT_TITLE = "JOB-APPLY-AGENT — WHAT BROKE"
# PowerShell's registered AppUserModelID: lets an unpackaged script show toasts on Windows 10/11.
TOAST_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml($env:TOAST_XML)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:TOAST_APP_ID).Show(
    [Windows.UI.Notifications.ToastNotification]::new($x))
"""


def toast(title: str, body: str, open_path: Path | None = None) -> bool:
    if sys.platform != "win32":
        return False
    launch = ""
    if open_path and open_path.exists():
        launch = f' activationType="protocol" launch={quoteattr(open_path.resolve().as_uri())}'
    lines = [l for l in body.splitlines() if l.strip()][:3]
    xml = (f"<toast{launch}><visual><binding template=\"ToastGeneric\"><text>{escape(title)}</text>"
           + "".join(f"<text>{escape(l[:200])}</text>" for l in lines) + "</binding></visual></toast>")
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", TOAST_PS],
                              env={**os.environ, "TOAST_XML": xml, "TOAST_APP_ID": TOAST_APP_ID},
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("Windows notification failed: %s", e)
        return False
    if proc.returncode != 0:
        log.warning("Windows notification failed: %s", proc.stderr.strip()[-300:])
    return proc.returncode == 0


def ntfy(title: str, body: str, *, priority: int = 3, click: str | None = None, tags: list[str] | None = None) -> bool:
    """Phone push via ntfy.sh (free, no account) to the private topic in NTFY_TOPIC."""
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        return False
    payload = {"topic": topic, "title": title, "message": body[:3500], "priority": priority, "tags": tags or []}
    if click:
        payload["click"] = click
    try:
        resp = requests.post(os.getenv("NTFY_SERVER", "https://ntfy.sh"), json=payload, timeout=30)
    except requests.RequestException as e:
        log.error("ntfy send failed: %s", e)
        return False
    if not resp.ok:
        log.error("ntfy send failed: %s %s", resp.status_code, resp.text[:200])
    return resp.ok


def _sheet_url() -> str | None:
    sid = os.getenv("GOOGLE_SHEET_ID")
    return f"https://docs.google.com/spreadsheets/d/{sid}/edit" if sid else None


def telegram(text: str) -> bool:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30, json={
        "chat_id": chat, "text": text[:4000], "disable_web_page_preview": True})
    if not resp.ok:
        log.error("Telegram send failed: %s %s", resp.status_code, resp.text[:200])
    return resp.ok


def send(text: str, *, title: str = "Job agent", open_path: Path | None = None) -> bool:
    log.info("notification:\n%s", text)
    shown = toast(title, text, open_path)
    sent = telegram(text) | ntfy(title, text)
    if not (shown or sent):
        log.warning("no notification channel worked — message is only in this log")
    return shown or sent


def _todays_log() -> Path | None:
    from common import DATA_DIR, today

    p = DATA_DIR / "logs" / f"{today()}.log"
    return p if p.exists() else None


def failure_alert(exc: BaseException) -> bool:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    hint = ""
    if "auth" in str(exc).lower() or "login" in str(exc).lower():
        hint = "Google login may have expired — ask Claude to 're-login Google' (see GWS_SETUP.md).\n"
    body = f"{type(exc).__name__}: {exc}\n{hint}"
    log.error("%s\n%s", ALERT_TITLE, tb)
    shown = toast(ALERT_TITLE, body, _todays_log())  # click → today's log
    sent = telegram(f"{ALERT_TITLE}\n\n{body}\n{tb[-2500:]}")
    pushed = ntfy(ALERT_TITLE, f"{body}\n{tb[-1200:]}", priority=5, tags=["rotating_light"])
    return shown or sent or pushed


def summary_message(s: dict) -> str:
    f = s["funnel"]
    lines = [f"{f['scraped']} scraped → {f['after_filters']} after filters → {f['scored']} scored → "
             f"{f['qualified']} qualified → {f['delivered']} resumes ready"]
    for i, j in enumerate(s["delivered"][:3], 1):
        lines.append(f"{i}. [{j['score']}/10] {j['title']} — {j['company']}\n{j['link']}")
    if s.get("flagged"):
        lines.append(f"⚠ {len(s['flagged'])} flagged: " +
                     "; ".join(f"{x['title']} @ {x['company']}" for x in s["flagged"][:5]))
    if s.get("warnings"):
        lines.append("⚠ " + "\n⚠ ".join(s["warnings"][:5]))
    lines.append(f"Apify ~${s['apify_usd']:.3f} · LLM calls {s['llm_calls']}")
    return "\n".join(lines)


def report(s: dict) -> bool:
    """Daily summary: toast titled with the headline, clicking opens today's Desktop folder."""
    from common import desktop_dir, load_config

    n = s["funnel"]["delivered"]
    title = f"Job agent: {n} new resume{'s' if n != 1 else ''} ready" if n else "Job agent: no new matches today"
    folder = desktop_dir() / load_config()["output"]["desktop_folder"] / s["date"]
    toast_body = "\n".join([summary_message(s).splitlines()[0]] +
                           [f"{j['title']} — {j['company']}" for j in s["delivered"][:2]])
    shown = toast(title, toast_body, folder if n else None)
    sent = telegram(f"{title}\n\n{summary_message(s)}")
    pushed = ntfy(title, summary_message(s), priority=4 if n else 2, click=_sheet_url(),
                  tags=["page_facing_up"] if n else ["zzz"])
    log.info("%s\n%s", title, summary_message(s))
    return shown or sent or pushed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from common import ROOT  # noqa: F401  (loads .env)

    sys.exit(0 if send(" ".join(sys.argv[1:]) or "Test notification from the job agent") else 1)
