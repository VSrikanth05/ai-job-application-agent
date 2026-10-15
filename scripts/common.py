"""Shared paths, config and small helpers for the pipeline scripts."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CONFIG_PATH = ROOT / "config" / "search.yaml"
TECH_TERMS_PATH = ROOT / "config" / "tech_terms.txt"
BASE_RESUME_PATH = Path(os.getenv("BASE_RESUME_PATH") or ROOT / "resume" / "base_resume.md")
# Override to keep run data elsewhere; defaults to ./output.
DATA_DIR = Path(os.getenv("JOB_AGENT_DATA_DIR") or ROOT / "output")
STATE_DIR = DATA_DIR / "state"

IST = timezone(timedelta(hours=5, minutes=30))


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def base_resume_text() -> str:
    return BASE_RESUME_PATH.read_text(encoding="utf-8")


def today() -> str:
    return datetime.now(IST).date().isoformat()


def now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def run_dir(day: str | None = None) -> Path:
    d = DATA_DIR / "runs" / (day or today())
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def slugify(text: str, maxlen: int = 50) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:maxlen].rstrip("-") or "x"


def job_folder_name(job: dict) -> str:
    return f"{slugify(job['company'], 30)}-{slugify(job['title'])}"


def norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


TRACKING_PARAM = re.compile(r"^(utm_.*|ref|refid|referrer|source|src|trk|trackingid|gh_src|lever-source.*|"
                            r"lever-origin|fbclid|gclid|mc_.*|_hsenc|_hsmi|origin)$", re.I)
COMPANY_SUFFIX = re.compile(r"(?:\s+(?:india|inc|llc|ltd|limited|pvt|private|plc|gmbh|corp|corporation|co|"
                            r"technologies|technology|labs|software|group|holdings|ai))+$")


def norm_company(s: str) -> str:
    """'AlphaSense India' / 'AlphaSense, Inc.' → 'alphasense' (same employer on different boards)."""
    n = norm_text(s)
    return COMPANY_SUFFIX.sub("", n) or n


def legacy_pair_key(job: dict) -> str:
    """company|title key as written before norm_company existed — still checked so the seen-jobs
    history recorded earlier keeps matching."""
    return f"{norm_text(job.get('company'))}|{norm_text(job.get('title'))}"


def job_keys(job: dict) -> tuple[str, str]:
    """(link key, company|title key) used for dedupe everywhere. Query params are kept unless they are
    tracking noise: some career sites identify the job only by them (…/careers/job?gh_jid=123)."""
    from urllib.parse import parse_qsl, urlencode, urlsplit

    raw = (job.get("link") or "").strip()
    # Same format as before for links without meaningful params (keeps the seen-jobs history valid).
    link = raw.split("?")[0].rstrip("/").lower()
    query = urlencode(sorted({(k, v) for k, v in parse_qsl(urlsplit(raw).query) if not TRACKING_PARAM.match(k)}))
    if query:
        link += f"?{query}"
    return link, f"{norm_company(job.get('company'))}|{norm_text(job.get('title'))}"


def desktop_dir() -> Path:
    """Real Desktop path (handles OneDrive-redirected Desktops on Windows)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0:  # CSIDL_DESKTOPDIRECTORY
            return Path(buf.value)
    return Path.home() / "Desktop"


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "googleapiclient.discovery_cache", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
