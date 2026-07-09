#!/usr/bin/env python3
"""PreToolUse hook: in a read-only session, redirect GitHub/remote write
commands into the pending-writes queue instead of executing them.

Active only when CLAUDE_PENDING_WRITES is set (the read-only Docker launcher
sets it). On the write-capable host the env var is unset, so this is a no-op.

Contract: Claude pipes a PreToolUse JSON event on stdin. We inspect the Bash
command; if it is a remote write, we write a queue file and emit a `deny`
decision so the command never runs. Otherwise we exit 0 and let the normal
permission flow proceed. Fails open (exit 0) on any unexpected error.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

QUEUE_DIR = Path(os.path.expanduser("~/.claude/pending-writes"))


def is_remote_write(cmd: str) -> bool:
    """True if the command mutates remote state (push / gh write op)."""
    # git push (covers `git -C <dir> push ...`)
    if re.search(r"\bgit\b.*\bpush\b", cmd):
        return True

    # gh <group> <write-verb>
    pr_verbs = "create|edit|merge|close|comment|review|ready|reopen|lock|unlock"
    if re.search(rf"\bgh\s+pr\s+({pr_verbs})\b", cmd):
        return True
    issue_verbs = "create|edit|comment|close|reopen|delete|lock|unlock|pin|unpin|transfer"
    if re.search(rf"\bgh\s+issue\s+({issue_verbs})\b", cmd):
        return True
    if re.search(r"\bgh\s+release\s+(create|edit|delete|upload)\b", cmd):
        return True
    if re.search(r"\bgh\s+(label|secret|variable)\s+(create|edit|set|delete)\b", cmd):
        return True
    if re.search(r"\bgh\s+workflow\s+(run|enable|disable)\b", cmd):
        return True

    # gh api: GET by default. A write requires an explicit write method, OR a
    # field flag (which forces POST) *unless* the method is explicitly GET
    # (the report search uses `gh api -X GET ... -f q=...`, a read).
    if re.search(r"\bgh\s+api\b", cmd):
        m = re.search(r"(?:-X|--method)\s+([A-Za-z]+)", cmd)
        if m:
            return m.group(1).upper() in {"POST", "PATCH", "PUT", "DELETE"}
        return bool(re.search(r"(^|\s)(-f|-F|--field|--raw-field|--input)\b", cmd))

    return False


def slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text or "write")[:48]


def queue(cmd: str, description: str) -> Path:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d-%H%M")
    header_ts = now.strftime("%Y-%m-%d %H:%M")
    if description:
        slug = slugify(description)
    else:
        parts = cmd.split()
        slug = slugify(parts[0] if parts else "write")

    path = QUEUE_DIR / f"{stamp}-{slug}.md"
    n = 2
    while path.exists():
        path = QUEUE_DIR / f"{stamp}-{slug}-{n}.md"
        n += 1

    title = description.strip() if description else cmd.strip().splitlines()[0][:80]
    body = (
        f"### {header_ts} — {title}\n"
        f"Context: Auto-queued by the queue-writes hook (read-only session); "
        f"a write-capable agent should run the command below.\n\n"
        f"Commands:\n"
        f"```bash\n{cmd.rstrip()}\n```\n"
    )
    path.write_text(body)
    return path


def main() -> int:
    if not os.environ.get("CLAUDE_PENDING_WRITES"):
        return 0  # write-capable host: do nothing

    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # fail open

    if event.get("tool_name") != "Bash":
        return 0

    tool_input = event.get("tool_input") or {}
    cmd = tool_input.get("command", "")
    if not cmd or not is_remote_write(cmd):
        return 0  # not a remote write — normal permission flow

    path = queue(cmd, tool_input.get("description", ""))
    decision = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Read-only session: this write was queued to {path} instead of "
                f"running. Do not retry the command; continue with other work and "
                f"monitor ~/.claude/pending-writes/ — a write-capable agent will "
                f"execute it and remove the file (or append a Status: failed line)."
            ),
        }
    }
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
