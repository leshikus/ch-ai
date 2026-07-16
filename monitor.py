#!/usr/bin/env python3
"""Singleton host monitor for the claude-toolkit container.

A std-lib `sched.scheduler` drives a time-ordered queue of `Event` objects. Each
event's `fire` does its work and re-arms itself (or schedules other events) on the
scheduler, so the queue never empties and the loop runs forever. Two recurring
events cover the jobs below; the monitoring event schedules a fresh CiWatchEvent
per request it discovers -- events adding events at runtime:
  1. Service the pending-monitoring queue -- each request (dispatched by `kind`;
     `ci` today) is a job to watch, e.g. a CI run armed by the arm_monitor push
     hook. Poll it to a terminal state, then hand the result back as a
     `ci-status-*` file in pending-reads for the working agent to react to.
     Requests are claimed into memory on first sight, so deleting the request file
     mid-watch cannot abort it; pending-monitoring also doubles as durable state,
     so a restart re-scans it and resumes. GitHub is polled with the host's own gh
     credentials, independent of the container, so Actions/checks are readable.
  2. Watch every open pull request (authored by you + review-requested) for a
     change that needs your attention -- CI reaching a terminal state, a new
     comment/review from someone else, or a fresh review request. Each change fires
     a macOS notification and is handed to an agent: if a project already tracks the
     PR (its dir exists under projects/, or its meta.json claims it) the change
     lands in that project's pending-reads/; otherwise a per-PR iTerm console is
     opened that clones the PR into projects/pr<N>/repo and starts a session on it.
     A periodic digest summarizes the open set. The monitor only ever touches
     projects/ -- it never learns a repo's local layout, and per-PR checkouts live
     inside projects/. Per-PR state persists to pr-state.json, so the frequent
     self-supersede restarts do not re-notify; a PR seen for the first time is
     baselined silently.

One instance runs at a time: on startup a new monitor supersedes any running
one (SIGTERMs the incumbent via the PID file, then claims it), so a relaunch
always picks up the newest code. Started detached by claude.py; runs until
killed:
    kill "$(cat ~/.config/claude-toolkit/monitor.pid)"

Host-only (opens GUI terminal tabs, polls GitHub with the host's own gh
credentials); never runs inside a container.
"""

import json
import os
import re
import sched
import signal
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(os.path.expanduser("~/.config/claude-toolkit"))
PIDFILE = APP_DIR / "monitor.pid"
LAUNCHER = Path(__file__).resolve().parent / "claude.py"
# All per-project state lives under projects/<name>/: the pending-reads /
# pending-monitoring queues plus meta.json (host_dir + any PR claim). claude.py
# mounts projects/<name>/ at the container's ~/.config/claude-toolkit/project, so the
# container queue paths are project-scoped.
PROJECTS_DIR = APP_DIR / "projects"
POLL = 2               # seconds between polls
CI_POLL_INTERVAL = 150     # seconds between polls of a single monitoring request
WATCH_EXPIRY = 6 * 3600    # give up on a watch with no terminal result after this
PR_SCAN_INTERVAL = 300     # seconds between full open-PR scans
PR_DIGEST_INTERVAL = 3600  # seconds between summary ("regular") notifications
PR_STATE_FILE = APP_DIR / "pr-state.json"  # per-PR state, so restarts don't re-notify


def _supersede_incumbent() -> None:
    """Take over from any monitor already running, so a relaunch always wins.

    The monitor owns its PID file: read the incumbent's PID, SIGTERM it, and wait
    for it to actually exit before returning, so the caller can claim the PID file
    with no overlapping poll cycle. A missing/stale PID, or a process we cannot
    signal, is left behind -- we take over regardless. The default SIGTERM
    disposition skips the incumbent's `finally` cleanup, so it leaves its (now
    stale) PID behind; the caller overwrites the file unconditionally, so that is
    harmless.
    """
    try:
        pid = int(PIDFILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return  # no incumbent (or unreadable) -- nothing to supersede
    if pid == os.getpid():
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return  # already gone, or not ours to signal -- take over anyway
    for _ in range(50):  # wait up to ~5s for the incumbent to exit
        try:
            os.kill(pid, 0)  # existence check
        except ProcessLookupError:
            return
        time.sleep(0.1)


def _shquote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _osaquote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _open_iterm_tab(title: str, launch: str) -> None:
    """Open an iTerm2 tab titled `title` whose shell runs `launch`.

    Reuses the current window (new tab) or creates one if none is open. The command
    is passed via AppleScript `write text`, so it must be a single shell line.
    """
    t = _osaquote(title)
    cmd = _osaquote(launch)
    script = (
        'tell application "iTerm2"\n'
        "  if (count of windows) = 0 then\n"
        "    create window with default profile\n"
        "    tell current session of current window\n"
        f"      set name to {t}\n"
        f"      write text {cmd}\n"
        "    end tell\n"
        "  else\n"
        "    tell current window\n"
        "      create tab with default profile\n"
        "      tell current session of current tab\n"
        f"        set name to {t}\n"
        f"        write text {cmd}\n"
        "      end tell\n"
        "    end tell\n"
        "  end if\n"
        "end tell\n"
    )
    subprocess.run(["osascript", "-e", script], check=False)


# ---- Job 3: servicing pending-monitoring -> pending-reads -------------------

# GitHub check/status verdicts grouped for terminal-state detection. A verdict
# that is not yet final counts as PENDING (the run is still going).
_PENDING_VERDICTS = {"", "PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED"}
_FAILED_VERDICTS = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}


def _gh_json(args):
    """Run a gh command with the host's credentials; return parsed JSON or None."""
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def _check_verdict(c: dict) -> str:
    """Normalize a check-run / status-context entry to an uppercase verdict.

    Handles both shapes we consume: a PR's statusCheckRollup (check runs carry
    `conclusion`/`status`, status contexts carry `state`) and the REST check-runs
    endpoint (`status`/`conclusion`). A not-yet-completed check reads as PENDING.
    """
    concl = c.get("conclusion")
    if concl:
        return concl.upper()
    state = c.get("state")
    if state:
        return state.upper()
    return "PENDING"


def _fetch_checks(repo, sha, pr):
    """Fetch a commit's check list, or None if it can't be fetched.

    Prefers the PR's statusCheckRollup (merges check runs + status contexts, the
    same source session_start uses); falls back to the commit check-runs endpoint
    when no PR is known.
    """
    if pr and repo:
        data = _gh_json(["pr", "view", str(pr), "--repo", repo, "--json", "statusCheckRollup"])
        if data is not None:
            return data.get("statusCheckRollup") or []
    if repo and sha:
        data = _gh_json(["api", f"/repos/{repo}/commits/{sha}/check-runs"])
        if data is not None:
            return data.get("check_runs") or []
    return None


def _monitor_ci(req: dict):
    """CI watch handler. Returns a result dict once terminal, else None.

    Result: {"conclusion": "success"|"failure", "total": int, "failed": [names]}.
    """
    checks = _fetch_checks(req.get("repo"), req.get("sha"), req.get("pr"))
    if not checks:  # None (fetch failed) or [] (CI not started) -> keep waiting
        return None
    verdicts = [_check_verdict(c) for c in checks]
    if any(v in _PENDING_VERDICTS for v in verdicts):
        return None  # still running
    failed = [
        (c.get("name") or c.get("context") or "?")
        for c, v in zip(checks, verdicts) if v in _FAILED_VERDICTS
    ]
    return {"conclusion": "failure" if failed else "success", "total": len(checks), "failed": failed}


_HANDLERS = {"ci": _monitor_ci}


def _ci_status_text(req: dict, result: dict) -> str:
    """Render a terminal CI result as a pending-reads message for the read agent."""
    pr = req.get("pr")
    pr_line = f"PR #{pr}: {req.get('pr_url')}" if pr else "PR: (none found)"
    label = req.get("branch") or (req.get("sha") or "")[:12]
    lines = [
        f"### CI result — {label} ({result['conclusion']})",
        f"Repo: {req.get('repo')}",
        f"Commit: {req.get('sha')}",
        pr_line,
        f"Checks: {result['total']} total, {len(result['failed'])} failing.",
        "",
    ]
    if result["conclusion"] == "failure":
        lines.append("Failing checks: " + ", ".join(result["failed"]))
        lines.append(
            "CI failed. Fetch the failed logs (`gh run view --log-failed` / "
            "`.claude/tools/fetch_ci_report.js`), identify the failing step, state a "
            "concrete root-cause hypothesis, then decide the fix and queue any writes."
        )
    else:
        lines.append("All checks passed. Note completion; no further action needed.")
    return "\n".join(lines) + "\n"


# ---- Event framework ---------------------------------------------------------


class Event:
    """One unit of scheduled monitoring work on a shared `sched.scheduler`.

    Subclasses implement `fire`, which does the work and re-arms itself (or
    schedules other events) via `arm`, so a recurring event keeps the scheduler's
    queue non-empty and the loop alive. `priority` breaks ties between events due
    at the same instant.
    """

    priority = 1

    def __init__(self, scheduler: sched.scheduler) -> None:
        self.scheduler = scheduler

    def arm(self, delay: float) -> None:
        """Queue this event's `fire` to run `delay` seconds from now."""
        self.scheduler.enter(delay, self.priority, self.fire)

    def fire(self) -> None:
        raise NotImplementedError


class ScanMonitoringEvent(Event):
    """Discover new pending-monitoring requests and add a CiWatchEvent for each.

    A request (projects/<project>/pending-monitoring/<slug>.json) is claimed on
    first sight -- its path recorded in `active` and turned into a watch event --
    so a later deletion of the request file (e.g. by an over-eager
    agent) cannot abort or re-add a watch in flight. On a monitor restart `active`
    is empty and this re-scans the dir to resume: a terminal watch already deleted
    its file, so only unfinished requests reappear (run resolution from the sha is
    stateless). Re-arms every POLL.
    """

    priority = 2

    def __init__(self, scheduler: sched.scheduler) -> None:
        super().__init__(scheduler)
        self.active: set[str] = set()

    def fire(self) -> None:
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        for path in sorted(PROJECTS_DIR.glob("*/pending-monitoring/*.json")):
            key = str(path)
            if key in self.active:
                continue
            try:
                req = json.loads(path.read_text())
                first_seen = path.stat().st_mtime
            except (json.JSONDecodeError, OSError):
                continue
            self.active.add(key)
            CiWatchEvent(
                self.scheduler, path, req, path.parent.parent.name,
                path.stem, first_seen, self.active,
            ).arm(0)
        self.arm(POLL)


class CiWatchEvent(Event):
    """Poll one armed monitoring request to a terminal state, then hand the result
    back as a pending-reads file. Re-arms every CI_POLL_INTERVAL until the run is
    terminal or the watch expires; on finishing it deletes the request file and
    drops its key from the scan event's `active` set (stopping the loop and letting
    a post-restart re-scan stay clean)."""

    priority = 3

    def __init__(self, scheduler: sched.scheduler, path: Path, req: dict,
                 project: str, slug: str, first_seen: float, active: set) -> None:
        super().__init__(scheduler)
        self.path = path
        self.req = req
        self.project = project
        self.slug = slug
        self.first_seen = first_seen
        self.active = active

    def _finish(self, text: str) -> None:
        """Write the result into pending-reads/, delete the request, drop the watch."""
        dest = PROJECTS_DIR / self.project / "pending-reads"
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"ci-status-{self.slug}.md"
        n = 2
        while out.exists():
            out = dest / f"ci-status-{self.slug}-{n}.md"
            n += 1
        out.write_text(text)
        self.path.unlink(missing_ok=True)
        self.active.discard(str(self.path))

    def fire(self) -> None:
        handler = _HANDLERS.get(self.req.get("kind"))
        if handler is None:  # unknown kind: report once and drop, don't spin
            self._finish(
                f"### Monitoring skipped — unknown kind {self.req.get('kind')!r}\n"
                f"Request: {self.req}\n"
            )
            return
        try:
            result = handler(self.req)
        except Exception as exc:  # keep the loop alive across transient failures
            print(f"monitor: {self.req.get('kind')} handler error for {self.path}: {exc}",
                  file=sys.stderr)
            result = None
        if result is not None:
            self._finish(_ci_status_text(self.req, result))
        elif time.time() - self.first_seen > WATCH_EXPIRY:
            self._finish(
                f"### CI monitoring expired — {self.req.get('branch') or ''}\n"
                f"Repo: {self.req.get('repo')}\nCommit: {self.req.get('sha')}\n"
                f"No terminal CI result after {WATCH_EXPIRY // 3600}h; check the run manually.\n"
            )
        else:
            self.arm(CI_POLL_INTERVAL)


# ---- Job 4: watching every open PR for changes that need attention ----------


def _notify(title: str, message: str) -> None:
    """Post a macOS notification (best-effort; never raises)."""
    script = (
        f"display notification {_osaquote(message)} "
        f'with title {_osaquote(title)} sound name "Glass"'
    )
    subprocess.run(["osascript", "-e", script], check=False)


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: Path, data) -> None:
    """Write `data` as JSON atomically (tmp file + rename)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def _current_login() -> str:
    """The gh account login this monitor runs as (empty string if unavailable)."""
    r = subprocess.run(["gh", "api", "user", "-q", ".login"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _pr_key(pr: dict) -> str:
    """Stable identity for a PR across scans: ``owner/name#number``."""
    return f"{pr['repository']['nameWithOwner']}#{pr['number']}"


def _search_prs(filters: list) -> list:
    """Open PRs matching `filters`, via `gh search prs` (host credentials)."""
    data = _gh_json([
        "search", "prs", "--state", "open", "--limit", "100",
        "--json", "number,repository,title,url", *filters,
    ])
    return data or []


def _ci_bucket(checks) -> str:
    """Collapse a check list to one of none/pending/failure/success."""
    if not checks:
        return "none"
    verdicts = [_check_verdict(c) for c in checks]
    if any(v in _PENDING_VERDICTS for v in verdicts):
        return "pending"
    if any(v in _FAILED_VERDICTS for v in verdicts):
        return "failure"
    return "success"


def _latest_foreign_activity(detail: dict, login: str) -> str:
    """Newest ISO timestamp of a comment/review authored by someone other than `login`.

    ISO-8601 UTC strings sort lexicographically, so ``max`` gives the latest. Empty
    string when there is none (compares less than any real timestamp).
    """
    stamps = []
    for c in detail.get("comments") or []:
        if (c.get("author") or {}).get("login") != login and c.get("createdAt"):
            stamps.append(c["createdAt"])
    for rv in detail.get("reviews") or []:
        if (rv.get("author") or {}).get("login") != login and rv.get("submittedAt"):
            stamps.append(rv["submittedAt"])
    return max(stamps) if stamps else ""


def _pr_project(pr: dict) -> str:
    """Project name for a PR: ``pr<number>``, disambiguated on a cross-repo clash.

    Per-PR state and its checkout live under projects/<name>/. Two different repos
    can share a PR number, so if projects/pr<n>/ already claims a *different* PR we
    fall back to ``pr<n>-<repo>``.
    """
    base = f"pr{pr['number']}"
    claimed = (_load_json(PROJECTS_DIR / base / "meta.json", {}).get("pr") or {}).get("key")
    if claimed and claimed != _pr_key(pr):
        return re.sub(r"[^a-zA-Z0-9_.-]", "-", f"{base}-{pr['repository']['name']}")
    return base


def _meta_pr_claims() -> dict:
    """Map pr_key -> project for every project whose meta.json claims a PR.

    Lets a manually-opened session (session_start records its branch's PR into
    meta.json) claim a PR, so a change routes to that existing agent instead of
    opening a duplicate console.
    """
    claims = {}
    if not PROJECTS_DIR.is_dir():
        return claims
    for meta in PROJECTS_DIR.glob("*/meta.json"):
        pr = (_load_json(meta, {}) or {}).get("pr")
        if isinstance(pr, dict) and pr.get("key"):
            claims[pr["key"]] = meta.parent.name
    return claims


def _pr_change_text(pr: dict, notes: list) -> str:
    """Render a PR change as a pending-reads item for a agent to act on."""
    return (
        f"### PR update — {_pr_key(pr)}\n"
        f"{pr.get('title') or ''}\n"
        f"URL: {pr.get('url')}\n\n"
        f"What changed: {'; '.join(notes)}.\n\n"
        "Act on this PR: inspect it (`gh pr view` / `gh pr diff` and its review "
        "threads), decide what is needed (reply to a reviewer, root-cause and fix "
        "failing CI, take up a review request), and queue any GitHub writes as "
        "pending writes. This is a result to act on, not a command to run.\n"
    )


def _deliver_pr_read(project: str, number: int, text: str) -> None:
    """Drop a PR-update item into a project's pending-reads inbox."""
    dest = PROJECTS_DIR / project / "pending-reads"
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"pr-{number}.md"
    n = 2
    while out.exists():
        out = dest / f"pr-{number}-{n}.md"
        n += 1
    out.write_text(text)


def _write_pr_meta(project: str, pr: dict) -> None:
    """Record the PR claim + checkout dir in the project's meta.json (merging)."""
    d = PROJECTS_DIR / project
    d.mkdir(parents=True, exist_ok=True)
    meta = _load_json(d / "meta.json", {})
    if not isinstance(meta, dict):
        meta = {}
    meta["host_dir"] = str(d / "repo")
    meta["pr"] = {
        "key": _pr_key(pr),
        "repo": pr["repository"]["nameWithOwner"],
        "number": pr["number"],
        "url": pr.get("url"),
    }
    _save_json(d / "meta.json", meta)


def _open_pr_console(pr: dict, project: str) -> None:
    """Open an iTerm tab that clones the PR into projects/<project>/repo and starts
    a session on it. The checkout stays inside the monitor's own projects/
    subtree; the repo is fetched by its GitHub coordinate, so no local repo-layout
    knowledge is needed."""
    repo = pr["repository"]["nameWithOwner"]
    checkout = PROJECTS_DIR / project / "repo"
    q = lambda s: _shquote(str(s))
    prep = (
        f"mkdir -p {q(checkout)} && cd {q(checkout)} && "
        f"{{ [ -e .git ] || gh repo clone {q(repo)} . ; }} && "
        f"gh pr checkout {pr['number']}"
    )
    launch = f"{prep} && python3 {q(LAUNCHER)}"
    _open_iterm_tab(f"PR #{pr['number']}", launch)


class PullRequestsEvent(Event):
    """Watch every open PR for a change that needs the user's attention.

    Sources: `gh search prs --author @me` and `--review-requested @me`. For each PR
    it compares CI state, latest foreign comment/review timestamp, and the review-
    requested flag against `self.state` (persisted to PR_STATE_FILE, so the monitor's
    frequent self-supersede restarts do not re-notify). A PR seen for the first time
    is baselined silently. On a transition it notifies (macOS) and routes the change
    to a agent -- an existing project's pending-reads, or a fresh per-PR
    console. A periodic digest summarizes the open set. Re-arms every PR_SCAN_INTERVAL.
    """

    priority = 4

    def __init__(self, scheduler: sched.scheduler) -> None:
        super().__init__(scheduler)
        self.state = _load_json(PR_STATE_FILE, {})
        if not isinstance(self.state, dict):
            self.state = {}
        self.login = _current_login()
        self.launched: set[str] = set()  # PRs we opened a console for this run
        self.last_digest = 0.0

    def fire(self) -> None:
        try:
            self._scan()
        except Exception as exc:  # keep the loop alive across transient failures
            print(f"monitor: PR scan failed: {exc}", file=sys.stderr)
        self.arm(PR_SCAN_INTERVAL)

    def _scan(self) -> None:
        review_prs = _search_prs(["--review-requested", "@me"])
        review_keys = {_pr_key(p) for p in review_prs}
        prs = {_pr_key(p): p for p in _search_prs(["--author", "@me"]) + review_prs}

        claims = _meta_pr_claims()
        need_action = 0
        for key, pr in prs.items():
            notes = self._evaluate(key, pr, key in review_keys)
            if notes:
                need_action += 1
                self._dispatch(key, pr, notes, claims)

        # Forget PRs that merged/closed so their state and launch guard don't linger.
        self.state = {k: v for k, v in self.state.items() if k in prs}
        self.launched &= set(prs)
        _save_json(PR_STATE_FILE, self.state)

        now = time.time()
        if now - self.last_digest >= PR_DIGEST_INTERVAL:
            self.last_digest = now
            _notify("Open pull requests", f"{len(prs)} open, {need_action} need action")

    def _evaluate(self, key: str, pr: dict, is_review_req: bool) -> list:
        """Update stored state for a PR; return the human-readable changes, if any.

        First sight baselines silently (returns []), so pre-existing comments/CI on
        a PR the monitor has never seen do not fire a notification.
        """
        repo = pr["repository"]["nameWithOwner"]
        detail = _gh_json([
            "pr", "view", str(pr["number"]), "--repo", repo,
            "--json", "statusCheckRollup,comments,reviews",
        ]) or {}
        cur = {
            "ci": _ci_bucket(detail.get("statusCheckRollup")),
            "activity": _latest_foreign_activity(detail, self.login),
            "review_requested": is_review_req,
        }
        prev = self.state.get(key)
        self.state[key] = cur
        if prev is None:
            return []
        notes = []
        if prev.get("ci") == "pending" and cur["ci"] in ("success", "failure"):
            notes.append(f"CI {cur['ci']}")
        if cur["activity"] and cur["activity"] > (prev.get("activity") or ""):
            notes.append("new comment/review")
        if is_review_req and not prev.get("review_requested"):
            notes.append("added as reviewer")
        return notes

    def _dispatch(self, key: str, pr: dict, notes: list, claims: dict) -> None:
        """Notify, and hand the change to a agent (existing or fresh)."""
        title = (pr.get("title") or "")[:50]
        _notify(f"PR #{pr['number']}: {title}", "; ".join(notes))
        text = _pr_change_text(pr, notes)

        # An agent already tracks this PR -> its pending-reads inbox. Match either an
        # explicit meta.json claim (a manual session) or the deterministic project dir
        # a prior console created.
        project = claims.get(key)
        if project is None:
            deterministic = _pr_project(pr)
            if (PROJECTS_DIR / deterministic).is_dir():
                project = deterministic
        if project is not None:
            _deliver_pr_read(project, pr["number"], text)
            return

        # No agent yet: open a per-PR console (once per run; the project dir it leaves
        # behind routes later changes to pending-reads even after a monitor restart).
        if key in self.launched:
            return
        self.launched.add(key)
        project = _pr_project(pr)
        _deliver_pr_read(project, pr["number"], text)  # pre-seed the new inbox
        _write_pr_meta(project, pr)
        _open_pr_console(pr, project)


def main() -> int:
    _supersede_incumbent()
    PIDFILE.write_text(str(os.getpid()))
    try:
        scheduler = sched.scheduler(time.time, time.sleep)
        ScanMonitoringEvent(scheduler).arm(0)  # pending-monitoring -> pending-reads
        PullRequestsEvent(scheduler).arm(0)    # open PRs -> notify + per-PR console
        # Recurring events re-arm themselves, so the queue never empties and run()
        # blocks forever -- until the process is killed.
        scheduler.run()
    finally:
        try:
            if PIDFILE.read_text().strip() == str(os.getpid()):
                PIDFILE.unlink()
        except (FileNotFoundError, ValueError):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
