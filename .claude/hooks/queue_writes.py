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

QUEUE_DIR = Path(os.path.expanduser("~/.config/claude-toolkit/pending-writes"))


def is_remote_write(cmd: str) -> bool:
    """True only for commands that are *certainly* remote writes.

    Anything ambiguous returns False so the read-only agent just tries it -- a
    real write then 403s under the read-only token. We only block/queue when
    there is no doubt the command mutates remote state.
    """
    # git push (covers `git -C <dir> push ...`), except --dry-run (non-mutating)
    if re.search(r"\bgit\b.*\bpush\b", cmd) and not re.search(r"\B--dry-run\b", cmd):
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

    # gh api: block only when it is *certainly* a write. graphql (query vs
    # mutation is indistinguishable here) and a bare -f/-F (forces POST for REST
    # but is also used by reads, e.g. `-X GET -f q=`) are NOT certain, so let the
    # read-only agent try them; a real write just 403s. Two certain signals:
    #   * an explicit write method (-X/--method POST|PATCH|PUT|DELETE), and
    #   * a `body=` field -- forces POST and carries a write payload
    #     (comment/reply/review/...), so it is ~always a mutation (the frequent
    #     `gh api .../comments -f body=...` case). graphql uses `query=`, not
    #     `body=`, so this never catches a graphql read.
    if re.search(r"\bgh\s+api\b", cmd) and not re.search(r"\bgraphql\b", cmd):
        m = re.search(r"(?:-X|--method)\s+([A-Za-z]+)", cmd)
        if m:
            return m.group(1).upper() in {"POST", "PATCH", "PUT", "DELETE"}
        # No explicit method: a body field forces POST with a write payload.
        return bool(re.search(r"""(?:-f|-F|--field|--raw-field)\s*['"]?body=""", cmd))

    return False


def slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text or "write")[:48]


def queue(cmd: str, description: str, cwd: str = "") -> Path:
    # The container mounts exactly one checkout at /home/ubuntu/<project> and the
    # session runs there, so the cwd basename is the project. Grouping writes under
    # it keeps their order and lets the host watcher open one drain tab per project.
    qdir = QUEUE_DIR / Path(cwd).name
    qdir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d-%H%M")
    header_ts = now.strftime("%Y-%m-%d %H:%M")
    if description:
        slug = slugify(description)
    else:
        parts = cmd.split()
        slug = slugify(parts[0] if parts else "write")

    path = qdir / f"{stamp}-{slug}.md"
    n = 2
    while path.exists():
        path = qdir / f"{stamp}-{slug}-{n}.md"
        n += 1

    title = description.strip() if description else cmd.strip().splitlines()[0][:80]
    # Prefix a `cd` and note the directory so the command runs in the right repo.
    # Paths stay container-absolute on purpose: the write-capable executor runs in
    # the same container image and mounts, so they resolve unchanged -- no host
    # translation needed.
    workdir_note = f"Working directory: {cwd}\n" if cwd else ""
    command_block = f'cd "{cwd}" &&\n{cmd.rstrip()}' if cwd else cmd.rstrip()
    body = (
        f"### {header_ts} — {title}\n"
        f"Context: Auto-queued by the queue_writes hook (read-only session); "
        f"a write-capable agent should run the command below.\n"
        f"{workdir_note}\n"
        f"Commands:\n"
        f"```bash\n{command_block}\n```\n"
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

    path = queue(cmd, tool_input.get("description", ""), event.get("cwd", "/home/ubuntu/misc"))
    decision = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Read-only session: this write was queued to {path} instead of "
                f"running. Do not retry the command; continue with other work and "
                f"monitor ~/.config/claude-toolkit/pending-writes/ — a write-capable agent will "
                f"execute it and remove the file (or append a Status: failed line)."
            ),
        }
    }
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
