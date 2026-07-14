#!/usr/bin/env python3
"""PostToolUse hook: after a successful `git push`, arm the host monitor to
follow the resulting CI run.

It drops a `kind: ci` request into ~/.config/claude-toolkit/pending-monitoring/,
which the host `monitor.py` consumes: it polls the run to conclusion and writes a
`ci-status-*` result into pending-reads/ for the read-only agent to react to.

Self-gating: in a read-only session the queue_writes PreToolUse hook DENIES a
`git push`, so it never executes and PostToolUse never fires for it. This hook
therefore only ever arms on a real push -- i.e. inside the write drain container.

Success is judged offline: a successful `git push` advances the local
remote-tracking ref, so HEAD == the push/upstream ref afterwards. A failed push
leaves the tracking ref behind, so we skip. When no tracking ref is configured
the check is indeterminate; we arm anyway (the monitor simply expires the watch
if no run ever appears). Fails open (exit 0) on any unexpected error.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

MONITOR_DIR = Path(os.path.expanduser("~/.config/claude-toolkit/pending-monitoring"))


def git(cwd: str, *args: str):
    """Run a git command in `cwd`, returning stripped stdout or None on failure."""
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def gh_json(*args: str):
    """Run a gh command and return parsed JSON stdout, or None on any failure."""
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def is_push(cmd: str) -> bool:
    """True for a real `git push` (covers `git -C <dir> push`), not a --dry-run."""
    return bool(re.search(r"\bgit\b.*\bpush\b", cmd)) and not re.search(r"\B--dry-run\b", cmd)


def push_succeeded(cwd: str):
    """True/False if determinable from the local remote-tracking ref, else None.

    A successful push fast-forwards the tracking ref to HEAD; a failed one does
    not. `@{push}` is the branch we push to, `@{upstream}` the one we track.
    """
    head = git(cwd, "rev-parse", "HEAD")
    if head is None:
        return False
    for ref in ("@{push}", "@{upstream}"):
        tip = git(cwd, "rev-parse", ref)
        if tip is not None:
            return tip == head
    return None  # no tracking ref configured -- indeterminate


def target_repo(cwd: str):
    """`owner/name` where CI runs: the upstream parent for a fork, else this repo."""
    data = gh_json("repo", "view", "--json", "nameWithOwner,isFork,parent")
    if not data:
        return None
    if data.get("isFork") and data.get("parent"):
        p = data["parent"]
        return f"{p['owner']['login']}/{p['name']}"
    return data.get("nameWithOwner")


def find_pr(branch: str, repo):
    """Return (number, url) of the PR for `branch`, or (None, None).

    Looks at the current repo first; for a fork PR (which lives on the upstream
    parent) falls back to searching `repo` by head branch.
    """
    pr = gh_json("pr", "view", "--json", "number,url")
    if pr:
        return pr.get("number"), pr.get("url")
    if repo:
        prs = gh_json("pr", "list", "--repo", repo, "--head", branch, "--json", "number,url")
        if prs:
            return prs[0].get("number"), prs[0].get("url")
    return None, None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # fail open

    if event.get("tool_name") != "Bash":
        return 0
    cmd = (event.get("tool_input") or {}).get("command", "")
    if not cmd or not is_push(cmd):
        return 0

    cwd = event.get("cwd") or os.getcwd()
    if push_succeeded(cwd) is False:
        return 0  # push failed -- nothing to monitor

    sha = git(cwd, "rev-parse", "HEAD")
    if not sha:
        return 0
    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or ""
    repo = target_repo(cwd)
    pr_number, pr_url = find_pr(branch, repo)

    request = {
        "kind": "ci",
        "repo": repo,
        "sha": sha,
        "branch": branch,
        "pr": pr_number,
        "pr_url": pr_url,
        "context": "Armed by arm_monitor after a successful git push.",
    }

    MONITOR_DIR.mkdir(parents=True, exist_ok=True)
    path = MONITOR_DIR / f"ci-{sha[:12]}.json"
    path.write_text(json.dumps(request, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
