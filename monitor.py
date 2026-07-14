#!/usr/bin/env python3
"""Singleton host monitor for the claude-toolkit container.

One poll loop with three jobs:
  1. Keep the read-only GitHub token fresh -- re-mint when the token file is older
     than ~50 min (installation tokens live ~60 min).
  2. Drain the pending-writes queue -- for each project that has pending writes and
     no drain already running, open a terminal tab (interactive `claude.py --write`,
     titled by project) to process it. One tab PER PROJECT, run concurrently, so
     several projects drain in parallel instead of waiting in a single line. Each
     project's drain is guarded by `docker ps` on its own
     `claude-toolkit-drain-<project>` container, so a restart cannot spawn a
     duplicate for a project already draining.
  3. Service the pending-monitoring queue -- each request (dispatched by `kind`;
     `ci` today) is a job to watch, e.g. a CI run armed by the arm_monitor push
     hook. Poll it to a terminal state, then hand the result back as a
     `ci-status-*` file in pending-reads for the read-only agent to react to.
     Requests are claimed into memory on first sight, so deleting the request file
     mid-watch cannot abort it; pending-monitoring also doubles as durable state,
     so a restart re-scans it and resumes. GitHub is polled with the host's own gh
     credentials (not the container's read-only token), so Actions/checks are
     readable.

One instance runs regardless of how many containers launch (PID-file guard).
Started detached by claude.py; runs until killed:
    kill "$(cat ~/.config/claude-toolkit/monitor.pid)"

Host-only (mints tokens, opens GUI terminal tabs); never runs inside a container.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import mint_gh_token
from claude import container_name

APP_DIR = Path(os.path.expanduser("~/.config/claude-toolkit"))
PIDFILE = APP_DIR / "monitor.pid"
LAUNCHER = Path(__file__).resolve().parent / "claude.py"
# All per-project state lives under projects/<name>/: the pending-writes /
# pending-reads / pending-monitoring queues plus meta.json (host_dir, so the drain
# tab can cd into the right repo -- no ~/repos assumption). claude.py mounts
# projects/<name>/ at the container's ~/.config/claude-toolkit/project, so the
# container queue paths are project-scoped. See _project_host_dir.
PROJECTS_DIR = APP_DIR / "projects"
# Where the queue folder appears inside the container (project-scoped mount, so no
# per-project subfolder; ~ resolves to the container home).
CONTAINER_QUEUE = "~/.config/claude-toolkit/project/pending-writes"
POLL = 2               # seconds between polls
MINT_MAX_AGE = 3000    # re-mint the token when older than this (50 min)
CI_POLL_INTERVAL = 150     # seconds between polls of a single monitoring request
WATCH_EXPIRY = 6 * 3600    # give up on a watch with no terminal result after this


def _already_running() -> bool:
    if not PIDFILE.exists():
        return False
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, 0)  # existence check
        return True
    except (ValueError, ProcessLookupError):
        return False  # stale PID file -- take over
    except PermissionError:
        return True


def _token_age() -> float:
    """Seconds since the token was last minted (inf if it does not exist yet)."""
    try:
        return time.time() - mint_gh_token.HOSTS_YML.stat().st_mtime
    except FileNotFoundError:
        return float("inf")


def _has_tasks(folder: Path) -> bool:
    return any(p.is_file() and p.name != "README.md" for p in folder.iterdir())


def _drain_running(project: str) -> bool:
    """True if a --write drain container for `project` is currently running."""
    r = subprocess.run(
        ["docker", "ps", "--filter", f"name=^{container_name(project)}$", "-q"],
        capture_output=True, text=True,
    )
    return bool(r.stdout.strip())


def _shquote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _osaquote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _build_prompt(project: str) -> str:
    """Assemble the exact queued tasks for `project` into one drain prompt."""
    host_folder = PROJECTS_DIR / project / "pending-writes"
    container_folder = CONTAINER_QUEUE
    files = sorted(p for p in host_folder.iterdir() if p.is_file() and p.name != "README.md")
    mds = [p for p in files if p.suffix == ".md"]
    others = [p.name for p in files if p.suffix != ".md"]
    parts = [
        f"You are the write-capable agent draining the pending-writes queue for "
        f"project '{project}'. The task files are in {container_folder}. Process each "
        f"task in order: summarize it, honor its guards, run its commands (approving "
        f"prompts as needed), and delete the file on error-less success.",
        "Before executing any task, review the contents it will produce -- not just "
        "that the command is well-formed. The read-only agent that queued it could "
        "not run code, post to GitHub, or see CI, so verify the substance. For a "
        "push, review ALL the code it introduces relative to the remote tip: read "
        "the full diff of every new commit (not a --stat summary or file list), "
        "confirm it does what the task's Context claims and introduces no bug, "
        "regression, or unintended change; for a fix addressing a review comment, "
        "confirm it actually resolves the reviewer's point. If the project provides "
        "a review skill or command (e.g. ClickHouse's `/review` under "
        "`.claude/skills/review`), run it on the pushed diff or PR and fold its "
        "findings into your decision. Execute only once the review passes; if the "
        "contents are wrong or incomplete, do not run it -- request changes instead "
        "(see write-mode.md).",
    ]
    if others:
        parts.append(
            "Companion payload files in that folder, read as referenced: "
            + ", ".join(others) + "."
        )
    parts.append("The exact queued tasks follow.")
    parts += [f"===== {p.name} =====\n{p.read_text().rstrip()}" for p in mds]
    return "\n\n".join(parts)


def _project_host_dir(project: str) -> Path:
    """Host checkout dir for a drained project, recorded by claude.py at launch.

    claude.py writes projects/<project>.json = {"host_dir": ...} for each session; the
    drain tab cd's there before launching claude.py --write. Falls back to $HOME if the
    record is missing or stale so the tab still opens visibly rather than failing.
    """
    try:
        data = json.loads((PROJECTS_DIR / project / "meta.json").read_text())
        host_dir = Path(data["host_dir"])
        if host_dir.is_dir():
            return host_dir
    except (OSError, ValueError, KeyError):
        pass
    return Path.home()


def _open_terminal_tab(project: str) -> None:
    """Open a terminal tab running an interactive --write drain for this project.

    Defaults to iTerm2 (swap the AppleScript here for Terminal.app or another
    emulator if needed). The exact queued task contents are handed to the session
    via a prompt file the tab's shell reads with $(cat ...), so no multi-line text
    goes through AppleScript `write text` (which would submit it line by line).
    """
    cwd = _project_host_dir(project)
    prompt_file = PROJECTS_DIR / project / "drain-prompt.md"
    prompt_file.write_text(_build_prompt(project))
    launch = (
        f"cd {_shquote(str(cwd))} && "
        f'python3 {_shquote(str(LAUNCHER))} --write "$(cat {_shquote(str(prompt_file))})"'
    )
    title = _osaquote(project)
    cmd = _osaquote(launch)
    script = (
        'tell application "iTerm2"\n'
        "  if (count of windows) = 0 then\n"
        "    create window with default profile\n"
        "    tell current session of current window\n"
        f"      set name to {title}\n"
        f"      write text {cmd}\n"
        "    end tell\n"
        "  else\n"
        "    tell current window\n"
        "      create tab with default profile\n"
        "      tell current session of current tab\n"
        f"        set name to {title}\n"
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


def _finish_watch(watches: dict, key: str, w: dict, text: str) -> None:
    """Write the result into pending-reads/, delete the request, drop the watch."""
    dest = PROJECTS_DIR / w["project"] / "pending-reads"
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"ci-status-{w['slug']}.md"
    n = 2
    while out.exists():
        out = dest / f"ci-status-{w['slug']}-{n}.md"
        n += 1
    out.write_text(text)
    Path(key).unlink(missing_ok=True)
    watches.pop(key, None)


def _service_monitoring(watches: dict) -> None:
    """Claim and poll pending-monitoring requests; hand terminal results back.

    Each request is read into `watches` on first sight so a later deletion of the
    request file (e.g. by an over-eager read-only agent) cannot abort a watch in
    flight; `first_seen` is captured then too, so expiry survives the file's
    removal. On a monitor restart `watches` is empty and this re-scans the dir to
    resume (run resolution from the sha is stateless).
    """
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()

    # Claim any new request files into memory. Requests live at
    # projects/<project>/pending-monitoring/<slug>.json.
    for path in sorted(PROJECTS_DIR.glob("*/pending-monitoring/*.json")):
        key = str(path)
        if key in watches:
            continue
        try:
            req = json.loads(path.read_text())
            first_seen = path.stat().st_mtime
        except (json.JSONDecodeError, OSError):
            continue
        watches[key] = {
            "req": req, "project": path.parent.parent.name, "slug": path.stem,
            "first_seen": first_seen, "last_poll": 0.0,
        }

    # Poll each claimed watch on its own cadence.
    for key, w in list(watches.items()):
        if now - w["last_poll"] < CI_POLL_INTERVAL:
            continue
        w["last_poll"] = now
        req = w["req"]
        handler = _HANDLERS.get(req.get("kind"))
        if handler is None:  # unknown kind: report once and drop, don't spin
            _finish_watch(
                watches, key, w,
                f"### Monitoring skipped — unknown kind {req.get('kind')!r}\n"
                f"Request: {req}\n",
            )
            continue
        try:
            result = handler(req)
        except Exception as exc:  # keep the loop alive across transient failures
            print(f"monitor: {req.get('kind')} handler error for {key}: {exc}", file=sys.stderr)
            result = None
        if result is not None:
            _finish_watch(watches, key, w, _ci_status_text(req, result))
        elif now - w["first_seen"] > WATCH_EXPIRY:
            _finish_watch(
                watches, key, w,
                f"### CI monitoring expired — {req.get('branch') or ''}\n"
                f"Repo: {req.get('repo')}\nCommit: {req.get('sha')}\n"
                f"No terminal CI result after {WATCH_EXPIRY // 3600}h; check the run manually.\n",
            )


def main() -> int:
    if _already_running():
        return 0
    PIDFILE.write_text(str(os.getpid()))
    # Projects we have already opened a tab for in the current batch of pending
    # writes. A project stays here until its queue actually drains, so we open at
    # most one tab per project per batch -- never more open tabs than projects with
    # pending writes, and no continuous reopening when a drain leaves work behind
    # (e.g. a `Status: failed` file) and its container has exited.
    launched: set[str] = set()
    # Armed monitoring requests claimed into memory (request-path -> watch state),
    # so a request file deleted mid-watch cannot abort it.
    watches: dict[str, dict] = {}
    try:
        while True:
            # 1) Keep the read-only token fresh.
            if _token_age() > MINT_MAX_AGE:
                try:
                    mint_gh_token.mint()
                except Exception as exc:  # keep the loop alive across transient failures
                    print(f"monitor: token mint failed, retrying next cycle: {exc}", file=sys.stderr)

            # 2) Drain the queue -- one tab per project, run concurrently. Open a tab
            # for each project with pending writes we have not already opened one for
            # and whose drain container is not already running. `_drain_running`
            # covers the case where our in-memory `launched` set was lost to a monitor
            # restart; `launched` covers the gap before the container shows up in
            # `docker ps` and stops us reopening while a drain still has work queued.
            PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
            projects = {
                p.name for p in PROJECTS_DIR.iterdir()
                if (p / "pending-writes").is_dir() and _has_tasks(p / "pending-writes")
            }
            # A project no longer in `projects` has drained; forget it so a later
            # batch of pending writes reopens a tab for it.
            launched &= projects
            for project in sorted(projects):
                if project in launched:
                    continue
                if _drain_running(project):
                    launched.add(project)
                    continue
                _open_terminal_tab(project)
                launched.add(project)

            # 3) Service the pending-monitoring queue (poll CI watches, hand back
            # results). Cheap: each watch only actually polls GitHub on its own
            # CI_POLL_INTERVAL cadence, not every POLL.
            _service_monitoring(watches)

            time.sleep(POLL)
    finally:
        try:
            if PIDFILE.read_text().strip() == str(os.getpid()):
                PIDFILE.unlink()
        except (FileNotFoundError, ValueError):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
