"""Render a tailored resume to PDF through Google Docs (gws):
copy template → replace {{PLACEHOLDERS}} → export PDF → delete the temporary copy.

Usage:
    python scripts/render_gdoc.py resume.md out.pdf
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from pypdf import PdfReader

from common import base_resume_text, setup_logging
from gws import GwsError, gws
from resume_template import slot_values

log = logging.getLogger("render")


def template_id() -> str:
    tid = os.getenv("GOOGLE_RESUME_TEMPLATE_ID")
    if not tid:
        raise GwsError("setup", "GOOGLE_RESUME_TEMPLATE_ID is not set — run scripts/google_setup.py")
    return tid


def page_count(pdf: Path) -> int:
    return len(PdfReader(str(pdf)).pages)


def render_pdf(md: str, out_pdf: Path, *, copy_name: str = "tmp resume") -> int:
    """Writes out_pdf and returns its page count. The temporary Doc is always deleted."""
    values = slot_values(md, base_resume_text())  # raises TemplateMismatch before touching Google
    copy = gws("drive", "files", "copy", params={"fileId": template_id(), "fields": "id"},
               body={"name": f"[tmp] {copy_name}"})
    doc_id = copy["id"]
    try:
        requests = [{"replaceAllText": {"containsText": {"text": k, "matchCase": True}, "replaceText": v}}
                    for k, v in values.items()]
        result = gws("docs", "documents", "batchUpdate", params={"documentId": doc_id}, body={"requests": requests})
        missing = [k for k, r in zip(values, (result or {}).get("replies", []))
                   if not (r.get("replaceAllText") or {}).get("occurrencesChanged")]
        if missing:
            raise GwsError("validation", f"placeholders not found in the template (was it edited?): {missing[:5]}")
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        gws("drive", "files", "export", params={"fileId": doc_id, "mimeType": "application/pdf"}, output=out_pdf)
    finally:
        try:
            gws("drive", "files", "delete", params={"fileId": doc_id})
        except GwsError as e:  # don't mask the real error; an orphan copy is harmless
            log.warning("could not delete temporary Doc %s: %s", doc_id, e)
    if not out_pdf.exists() or out_pdf.stat().st_size == 0:
        raise GwsError("api", f"PDF export produced no file at {out_pdf}")
    return page_count(out_pdf)


def main() -> int:
    ap = argparse.ArgumentParser(description="Render resume markdown to PDF via the Google Docs template")
    ap.add_argument("markdown", type=Path)
    ap.add_argument("pdf", type=Path)
    args = ap.parse_args()
    setup_logging()
    pages = render_pdf(args.markdown.read_text(encoding="utf-8"), args.pdf)
    print(f"{args.pdf} ({pages} page{'s' if pages != 1 else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
