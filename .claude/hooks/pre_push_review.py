#!/usr/bin/env python3
"""PreToolUse hook: review the commits a `git push` would send, before it runs.

A separate reviewer agent -- a different model, headless `claude -p` -- inspects
the exact commits about to be pushed. If it finds a concrete defect, the push is
denied and the findings are handed back to the working agent (fix in a NEW commit,
push again). Clean commits pass straight through. Purpose: keep broken changes out
of the PR so the author does not end up in a long back-and-forth with a reviewer
bot.

The reviewer is best-effort quality control, not a security boundary (auto mode's
classifier is that). So it fails OPEN: any reviewer error, timeout, or
unavailability allows the push with a warning rather than wedging the working
agent. It only ever *denies* -- it never forces an allow, so auto mode's classifier
still gates the push (force push, routing around review, ...) as usual.

Contract: Claude pipes a PreToolUse JSON event on stdin. Exit 0 with no output to
let the normal permission flow proceed; print a `deny` decision to block.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REVIEW_DOC = Path("/home/ubuntu/.config/claude-toolkit/modes/pre-push-review.md")
KEY_FILE = Path("/home/ubuntu/.config/claude-toolkit/anthropic-key")
# A different model than the working agent (Opus), for a genuine second opinion.
REVIEWER_MODEL = os.environ.get("CLAUDE_REVIEW_MODEL", "claude-sonnet-5")
REVIEW_TIMEOUT = 300      # seconds to wait for the reviewer before failing open
MAX_DIFF_CHARS = 100_000  # cap the diff fed to the reviewer; truncate beyond this


def is_push(cmd: str) -> bool:
    """True for a real `git push` (covers `git -C <dir> push`), not a --dry-run."""
    return bool(re.search(r"\bgit\b.*\bpush\b", cmd)) and not re.search(r"\B--dry-run\b", cmd)


def git(cwd: str, *args: str):
    """Run a git command in `cwd`, returning stripped stdout or None on failure."""
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def push_range(cwd: str):
    """(base, [shas]) for commits HEAD is ahead of the remote, or (None, []).

    Prefers the branch's push/upstream tracking ref. For a brand-new branch with
    no tracking ref, falls back to the merge-base with the remote's default branch,
    so the first push is still reviewed.
    """
    for ref in ("@{push}", "@{upstream}"):
        if git(cwd, "rev-parse", ref) is not None:
            shas = git(cwd, "log", "--format=%H", f"{ref}..HEAD")
            return ref, (shas.split() if shas else [])
    for ref in ("origin/HEAD", "origin/main", "origin/master"):
        base = git(cwd, "merge-base", ref, "HEAD")
        if base:
            shas = git(cwd, "log", "--format=%H", f"{base}..HEAD")
            return base, (shas.split() if shas else [])
    return None, []


def allow() -> int:
    return 0  # no output -> normal permission flow (auto-mode classifier) proceeds


def deny(reason: str) -> int:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    return 0


def run_reviewer(prompt: str):
    """Return the reviewer's stdout, or None on any failure (caller fails open)."""
    env = dict(os.environ)
    try:
        env["ANTHROPIC_API_KEY"] = KEY_FILE.read_text().strip()
    except OSError:
        return None  # no key to run a separate claude with
    try:
        r = subprocess.run(
            ["claude", "-p", "--model", REVIEWER_MODEL],
            input=prompt, capture_output=True, text=True,
            timeout=REVIEW_TIMEOUT, env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return allow()  # fail open

    if event.get("tool_name") != "Bash":
        return allow()
    cmd = (event.get("tool_input") or {}).get("command", "")
    if not cmd or not is_push(cmd):
        return allow()

    cwd = event.get("cwd") or os.getcwd()
    base, shas = push_range(cwd)
    if not shas:
        return allow()  # nothing new to push, or range undeterminable -> don't gate

    diff = git(cwd, "diff", f"{base}..HEAD") or ""
    if not diff.strip():
        return allow()
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n[... diff truncated for review ...]\n"
    log = git(cwd, "log", "--format=%H%n%an%n%s%n%b%n---", f"{base}..HEAD") or ""

    try:
        instructions = REVIEW_DOC.read_text()
    except OSError:
        instructions = (
            "Review the commits about to be pushed for concrete defects (bugs, "
            "regressions, or a change that does not do what its commit message "
            "claims). First line: exactly PASS or BLOCK; if BLOCK, list the "
            "specific findings. Ignore style/tests/preferences. When in doubt, PASS."
        )
    prompt = (
        f"{instructions}\n\n"
        f"===== commit log (to be pushed) =====\n{log}\n\n"
        f"===== diff ({base}..HEAD) =====\n{diff}\n"
    )

    out = run_reviewer(prompt)
    if out is None:
        print("pre_push_review: reviewer unavailable; allowing push", file=sys.stderr)
        return allow()  # fail open -- best-effort quality control, not a hard gate

    # Verdict is the first non-empty line; block only on an explicit BLOCK.
    first = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
    if first.upper().startswith("BLOCK"):
        return deny(
            "Pre-push review blocked this push. Fix the findings below in a NEW "
            "commit (do not amend an already-pushed commit), then push again. If a "
            "finding is wrong, address it in your next message before retrying.\n\n"
            + out
        )
    return allow()


if __name__ == "__main__":
    sys.exit(main())
