#!/usr/bin/env python3
"""Session-start orientation for the claude-toolkit container.

Wired as a Claude Code ``SessionStart`` hook by ``run.py`` (the hook
is part of the container's inline settings, so it applies only inside the
toolkit container -- not to host sessions). The harness runs this at the start
of every session and injects its stdout into the model's context, so the "on
start" orientation happens deterministically instead of relying on the model to
remember it.

When the current branch is found, it runs ``gh pr view`` to report the related
pull request's progress (CI/check status, review decision, unresolved review
threads, mergeability) and asks the assistant to set its session goal to
completing that pull request. It highlights **unanswered reviewer comments** --
unresolved threads whose latest comment is from someone other than the PR author,
i.e. the ones awaiting your reply. When
the repo is a fork the PR is opened against the upstream parent with the branch
as head, so the lookup falls back to searching the parent. New orientation steps
can be added as further blocks in ``main``.

The full orientation text is both printed (the harness injects stdout into the
model's context) and written to ``.claude/start-session.md`` so a human can open
that file to read the outstanding review comments directly.
"""

import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

PR_FIELDS = "number,url,state,isDraft,statusCheckRollup,reviewDecision,mergeable,author"

# Where the rendered orientation is dumped for a human to read (git-ignored via
# the .claude whitelist). __file__ is .claude/hooks/session_start.py, so
# parent.parent is .claude/.
DUMP_FILE = Path(__file__).resolve().parent.parent / "start-session.md"

# The per-project meta.json (mounted at the container's project dir) records the
# host checkout dir; its basename is the real project name -- the container cwd is
# the generic /home/ubuntu/project, so basename(cwd) alone would just say "project".
META_FILE = Path("/home/ubuntu/.config/claude-toolkit/project/meta.json")


def project_name() -> str:
    """Real project name: basename of host_dir in meta.json, else the cwd basename."""
    try:
        host_dir = json.loads(META_FILE.read_text())["host_dir"]
        return os.path.basename(host_dir.rstrip("/")) or os.path.basename(os.getcwd())
    except (OSError, ValueError, KeyError):
        return os.path.basename(os.getcwd())

# Fetches every review thread with its full comment chain, so the hook can both
# surface which conversations a reviewer still has open and tell whether the
# latest word on each is the reviewer's (awaiting your reply) or yours.
REVIEW_THREADS_QUERY = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      reviewThreads(first:100){
        nodes{
          isResolved
          comments(first:100){ nodes{ author{login} path line body } }
        }
      }
    }
  }
}
"""


def run(cmd):
    """Run an external command, returning (exit_code, stdout, stderr)."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def json_out(cmd):
    """Run ``cmd`` and return its parsed JSON stdout, or None on any failure."""
    code, out, _ = run(cmd)
    if code != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def current_branch():
    code, out, _ = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    return out if code == 0 else None


def upstream_parent():
    """Return ``owner/name`` of the upstream parent if this is a fork, else None."""
    data = json_out(["gh", "repo", "view", "--json", "isFork,parent"])
    if not data or not data.get("isFork") or not data.get("parent"):
        return None
    parent = data["parent"]
    return f"{parent['owner']['login']}/{parent['name']}"


def pr_view(branch):
    """Return the PR dict for ``branch``, or None if there is none.

    Looks first at the current repo (``gh pr view``). When that finds nothing and
    the repo is a fork, searches the upstream parent for an open PR whose head is
    ``branch`` -- that is where a fork's PRs actually live.
    """
    pr = json_out(["gh", "pr", "view", "--json", PR_FIELDS])
    if pr is not None:
        return pr

    parent = upstream_parent()
    if parent is None:
        return None

    prs = json_out(
        ["gh", "pr", "list", "--repo", parent, "--head", branch, "--json", "number"]
    )
    if not prs:
        return None

    return json_out(
        ["gh", "pr", "view", str(prs[0]["number"]), "--repo", parent, "--json", PR_FIELDS]
    )


def report_pr(pr, lines):
    """Summarize the PR's progress into ``lines``."""
    draft = " (draft)" if pr.get("isDraft") else ""
    lines.append(f"PR #{pr.get('number')}{draft}: {pr.get('url')}")
    lines.append(f"  state: {pr.get('state')}")
    lines.append(f"  review decision: {pr.get('reviewDecision') or 'none'}")
    lines.append(f"  mergeable: {pr.get('mergeable')}")

    checks = pr.get("statusCheckRollup") or []
    if checks:
        counts = Counter()
        for c in checks:
            # Check runs use `conclusion`/`status`; status contexts use `state`.
            status = c.get("conclusion") or c.get("state") or c.get("status") or "UNKNOWN"
            counts[status] += 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        lines.append(f"  checks ({len(checks)}): {summary}")

        failed = [
            c.get("name") or c.get("context") or "?"
            for c in checks
            if (c.get("conclusion") or c.get("state")) in {"FAILURE", "ERROR", "CANCELLED"}
        ]
        if failed:
            lines.append(f"  failing: {', '.join(failed)}")
    else:
        lines.append("  checks: none reported")


def _thread_location(thread):
    """``path:line`` (or just ``path``) of a review thread's first comment."""
    comments = (thread.get("comments") or {}).get("nodes") or []
    if not comments:
        return "?"
    first = comments[0]
    path = first.get("path") or "?"
    line = first.get("line")
    return f"{path}:{line}" if line is not None else path


def _render_thread(thread, lines):
    """Append a review thread -- its location and every comment -- to ``lines``."""
    lines.append(f"    - {_thread_location(thread)}")
    for c in (thread.get("comments") or {}).get("nodes") or []:
        author = (c.get("author") or {}).get("login") or "?"
        body = " ".join((c.get("body") or "").split())[:300]
        lines.append(f"        {author}: {body}")


def report_review_threads(pr, lines):
    """Summarize the PR's unresolved review threads into ``lines``.

    Splits unresolved threads into those whose latest comment is from someone
    other than the PR author -- **unanswered reviewer comments** awaiting your
    reply -- and the rest. The PR's location (owner/name/number) is parsed from
    its URL so this works identically for fork PRs (which live on the upstream
    parent) and same-repo PRs.
    """
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr.get("url") or "")
    if not m:
        return
    owner, name, number = m.group(1), m.group(2), m.group(3)
    pr_author = (pr.get("author") or {}).get("login")

    data = json_out(
        [
            "gh", "api", "graphql",
            "-f", f"query={REVIEW_THREADS_QUERY}",
            "-f", f"owner={owner}",
            "-f", f"name={name}",
            "-F", f"number={number}",
        ]
    )
    try:
        nodes = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (KeyError, TypeError):
        return

    unresolved = [n for n in nodes if not n.get("isResolved")]
    if not unresolved:
        lines.append("  unresolved review threads: none")
        return

    # "Unanswered" = a reviewer had the last word (latest comment not by the PR
    # author), so a reply from you is owed. Without a known author, treat all as
    # unanswered rather than silently hiding them.
    unanswered, answered = [], []
    for n in unresolved:
        comments = (n.get("comments") or {}).get("nodes") or []
        if not comments:
            continue
        last_author = (comments[-1].get("author") or {}).get("login")
        (answered if pr_author and last_author == pr_author else unanswered).append(n)

    if unanswered:
        lines.append(f"  unanswered reviewer comments -- awaiting your reply ({len(unanswered)}):")
        for n in unanswered:
            _render_thread(n, lines)
    if answered:
        lines.append(f"  other unresolved review threads ({len(answered)}):")
        for n in answered:
            _render_thread(n, lines)


def finish(lines):
    """Print the orientation (into the model's context) and dump it to a file.

    The file lets a human open ``.claude/start-session.md`` to read the
    outstanding review comments directly, without scrolling the session log.
    """
    text = "\n".join(lines)
    print(text)
    try:
        DUMP_FILE.write_text(text + "\n")
    except OSError as exc:
        print(f"(could not write {DUMP_FILE}: {exc})", file=sys.stderr)
    return 0


def main():
    lines = ["=== session-start orientation ==="]
    # Always surface the project name so the session knows which project it is in,
    # even outside a git repo. The repo mounts at the fixed /home/ubuntu/project, so
    # the cwd basename is generic ("project"); read the real name from the recorded
    # host checkout in meta.json (basename of host_dir), falling back to the cwd.
    lines.append(f"Project name: {project_name()}")

    branch = current_branch()
    if branch is None:
        lines.append("(not a git repository, or git unavailable)")
        return finish(lines)

    lines.append(f"branch: {branch}")

    pr = pr_view(branch)
    if pr is None:
        lines.append("PR: none for this branch")
        return finish(lines)

    report_pr(pr, lines)
    report_review_threads(pr, lines)
    lines.append(
        f"Set your goal for this session to "
        f"`complete PR #{pr.get('number')} {pr.get('url')}`."
    )
    return finish(lines)


if __name__ == "__main__":
    sys.exit(main())
