"""Claude Code hook: validate tailored resumes (output/runs/<date>/jobs/<folder>/resume.md).

PreToolUse(Write)  → validates the content about to be written; exit 2 blocks the write.
PostToolUse(Edit)  → validates the edited file; exit 2 reports problems back to Claude.
Other files pass through untouched. Stdlib only (runs outside the venv).
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_resume import validate  # noqa: E402

TARGET = re.compile(r"[\\/]jobs[\\/][^\\/]+[\\/]resume\.md$")


def main() -> int:
    sys.stderr.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")  # Windows defaults to cp1252 and garbles "—" in the payload
    data = json.load(sys.stdin)
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path", "")
    if not TARGET.search(path):
        return 0
    if data.get("hook_event_name") == "PreToolUse":
        content = tool_input.get("content", "")
    else:
        content = Path(path).read_text(encoding="utf-8")

    errors = validate(content, (ROOT / "resume" / "base_resume.md").read_text(encoding="utf-8"))
    if not errors:
        return 0
    print("Tailored resume failed validation (reorder/reword only — no fabrication):\n- " + "\n- ".join(errors),
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
