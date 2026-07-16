#!/usr/bin/env python3
"""Launch Claude Code in Docker with the current directory mounted, in auto mode.

    ./claude.py [claude args...]            # working session
    ./claude.py --review [claude args...]   # review the writes log (one window)

--review opens a session pointed at the global writes log (all projects) with the
review-mode role doc: it walks the writes one at a time, fetching each diff/PR/CI
and reporting to you, after the fact. It never gates the working sessions.

The working session runs with the host's real GitHub token and
`--permission-mode auto`: it works autonomously (no routine permission prompts)
while Claude Code's auto-mode classifier gates dangerous actions (force push,
exfiltration, production deploys, routing around a review). Routine pushes and PR
creation flow directly, so CI starts as soon as a push lands.

Our runtime state (gh config, api key, per-project queues, writes log) and toolkit
code (hooks, mode docs) live under ~/.config/claude-toolkit/ and are mounted into
the container. The host's own ~/.claude is mounted as-is, so the session runs in
the user's real Claude environment (their CLAUDE.md, skills, plugins, history);
our behavior is layered on via --settings (hooks) and --append-system-prompt.

Hooks layer our behavior on: pre_push_review (PreToolUse) gates a `git push` on a
separate reviewer agent (a different model) that inspects the commits about to be
pushed and blocks concrete defects before they reach the PR; capture_writes
(PostToolUse) logs every remote write to the global writes log
(~/.config/claude-toolkit/writes-log/) for a separate --review session to analyze
after the fact; and arm_monitor (PostToolUse) arms the host monitor's CI watch
after a successful push. The host monitor also watches open PRs.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

IMAGE = "claude-toolkit:latest"
REPO_DIR = Path(__file__).resolve().parent  # claude.py is the repo-root entry point
HOME = Path.home()
# All of our runtime state lives here (mounted into the container), kept out of
# Claude Code's own ~/.claude.
APP_DIR = HOME / ".config" / "claude-toolkit"


def real_gh_config() -> str:
    """Materialize a gh config dir carrying the host's real token.

    On macOS the real gh token lives in the login keychain, not in
    ~/.config/gh/hosts.yml, so mounting that dir gives the container no token.
    Extract it with `gh auth token` and write a hosts.yml (for gh) plus a raw
    token file (for git's credential helper). Kept in its own dir (not APP_DIR/gh,
    which the monitor's token minter still writes to) so nothing clobbers it.
    """
    dest = APP_DIR / "real-gh"
    try:
        token = subprocess.run(
            ["gh", "auth", "token", "-h", "github.com"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit("error: `gh auth token` failed; run `gh auth login` on the host first.")
    if not token:
        sys.exit("error: gh returned an empty token; run `gh auth login` on the host.")

    login = subprocess.run(
        ["gh", "api", "user", "-q", ".login"], capture_output=True, text=True,
    ).stdout.strip() or "x-access-token"

    old_umask = os.umask(0o077)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "hosts.yml").write_text(
            "github.com:\n"
            f"    oauth_token: {token}\n"
            "    git_protocol: https\n"
            f"    user: {login}\n"
        )
        (dest / "token").write_text(token)
    finally:
        os.umask(old_umask)
    return str(dest)


def stage_gnupg() -> str:
    """Copy the host GPG keyring to a private dir and return its path.

    gpg needs a writable GNUPGHOME even to read the keyring (it writes a lockfile
    and trustdb), so a read-only mount cannot sign. Mounting a copy (rw) instead
    of ~/.gnupg lets the container sign commits and write its own agent sockets
    without being able to modify the host keyring. Refreshed each launch; agent
    sockets and lockfiles are skipped (uncopyable / stale).
    """
    src = HOME / ".gnupg"
    dest = APP_DIR / "gnupg"
    if dest.exists():
        shutil.rmtree(dest)
    if src.is_dir():
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("S.*", "*.lock", ".#*"))
    else:
        dest.mkdir(parents=True)
    dest.chmod(0o700)
    return str(dest)


def read_keychain_api_key() -> bytes:
    """Read the Claude Code API key from the macOS login Keychain."""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code", "-w"],
            capture_output=True, check=True,
        )
    except subprocess.CalledProcessError:
        sys.exit(
            "error: could not read 'Claude Code' credential from the macOS Keychain.\n"
            "       Log in on the host first (run 'claude' and authenticate), then retry."
        )
    return proc.stdout


def pull_toolkit() -> None:
    """Fast-forward the toolkit checkout so committed updates apply on launch.

    Best-effort: a failure (offline, diverged, or local changes in the way) warns
    but does not block the session -- launching with slightly stale tooling beats
    refusing to start.
    """
    result = subprocess.run(
        ["git", "-C", str(REPO_DIR), "pull", "--ff-only"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"warning: git pull failed; using the current checkout:\n"
              f"{result.stderr.strip()}", file=sys.stderr)


def main() -> None:
    review_mode = "--review" in sys.argv[1:]
    claude_args = [a for a in sys.argv[1:] if a != "--review"]

    pull_toolkit()
    APP_DIR.mkdir(parents=True, exist_ok=True)
    # Per-project state lives under projects/<name>/: the pending-reads /
    # pending-monitoring queues plus meta.json (the host checkout dir, read by the
    # host monitor). The whole dir is mounted at the container's
    # ~/.config/claude-toolkit/project, so the hooks need no project logic. Create the
    # queue dirs so the bind mount attaches real dirs, not new root-owned ones.
    cwd = Path.cwd()
    projects_dir = APP_DIR / "projects"
    # A monitor-bootstrapped per-PR checkout lives at projects/<name>/repo; its
    # project state (queues, meta.json) is the parent dir, so the project name is
    # that parent -- not the generic "repo". Any other cwd names its project by its
    # own basename, as before.
    if cwd.name == "repo" and cwd.parent.parent == projects_dir:
        project = cwd.parent.name
    else:
        project = cwd.name
    proj_dir = projects_dir / project
    # No pending-writes queue anymore: writes execute directly (auto mode). We keep
    # pending-reads (the monitor's CI/PR results inbox) and pending-monitoring (the
    # CI-watch arm requests dropped by arm_monitor).
    for sub in ("pending-reads", "pending-monitoring"):
        (proj_dir / sub).mkdir(parents=True, exist_ok=True)
    # Global writes log: one consolidated store of every remote write across all
    # projects, mounted into the container so capture_writes appends here and the
    # separate --review session walks one list.
    writes_log = APP_DIR / "writes-log"
    writes_log.mkdir(parents=True, exist_ok=True)
    # Merge, not overwrite: preserve a `pr` claim the monitor (or a prior
    # session_start) recorded, so an open PR stays associated with this project.
    meta_file = proj_dir / "meta.json"
    try:
        meta = json.loads(meta_file.read_text())
        if not isinstance(meta, dict):
            meta = {}
    except (OSError, ValueError):
        meta = {}
    meta["host_dir"] = str(cwd)
    meta_file.write_text(json.dumps(meta) + "\n")

    # Always build: Docker's layer cache makes this a fast no-op when nothing in
    # the build context changed, and it picks up Dockerfile edits.
    subprocess.run(["docker", "build", "-t", IMAGE, str(REPO_DIR)], check=True)

    # Persist onboarding state (theme, per-project trust). This lives in
    # ~/.claude.json (a file in $HOME, not inside ~/.claude); ensure it exists so the
    # bind mount attaches a file, not a new empty directory.
    claude_json = HOME / ".claude.json"
    if not claude_json.exists():
        claude_json.write_text("{}")

    # macOS keeps the Claude Code credential in the login Keychain, not on disk.
    # This account uses a raw API key (not OAuth), so drop it into a 0600 file that
    # apiKeyHelper reads -- the key never enters the env or `docker inspect`.
    key_file = APP_DIR / "anthropic-key"
    old_umask = os.umask(0o077)
    try:
        key_file.write_bytes(read_keychain_api_key())
    finally:
        os.umask(old_umask)
    key_file.chmod(0o600)

    # GitHub auth: the working session uses the host's real token (auto mode gates
    # dangerous writes; the token lets routine writes execute). The gh config dir
    # (hosts.yml + token) mounts at gh's default location, so git and gh share one
    # token and the git credential helper is identical.
    gh_config_src = real_gh_config()
    # Launch the host monitor (open-PR + CI watch). On startup it supersedes any
    # running instance -- SIGTERMs the incumbent via the PID file, then claims it --
    # so this launch always picks up the newest code without leaving a stale monitor.
    subprocess.Popen(
        [sys.executable, str(REPO_DIR / "monitor.py")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )

    git_helper = (
        '!f() { echo username=x-access-token; '
        'echo "password=$(cat /home/ubuntu/.config/gh/token)"; }; f'
    )

    # Toolkit code (capture_writes, session-start orientation, arm_monitor) is mounted
    # under ~/.config/claude-toolkit/ below -- NOT into ~/.claude, so we no longer
    # overwrite the user's ~/.claude dir. Fixed paths, independent of where the
    # checkout lives.
    pre_push_script = "/home/ubuntu/.config/claude-toolkit/hooks/pre_push_review.py"
    capture_script = "/home/ubuntu/.config/claude-toolkit/hooks/capture_writes.py"
    session_start_script = "/home/ubuntu/.config/claude-toolkit/hooks/session_start.py"
    arm_monitor_script = "/home/ubuntu/.config/claude-toolkit/hooks/arm_monitor.py"
    # Mount the current directory at the fixed /home/ubuntu/project and work there.
    # The container is project-agnostic: no repo name appears in any container path
    # (the name lives only host-side, under projects/<name>/). No ~/repos assumption;
    # the session sees only the checkout it was launched from. (cwd and proj_dir were
    # computed above, with meta.json already recorded.)
    workdir = "/home/ubuntu/project"

    # Point this session at its role doc (mounted rw under
    # ~/.config/claude-toolkit/modes below, so the agent can refine it). A pointer
    # (vs injecting a snapshot) keeps one live, editable source of truth. --review
    # runs the review agent over the writes log; the default is the working agent.
    mode_doc = "review-mode" if review_mode else "working-mode"
    mode_prompt = (
        f"Follow the {mode_doc} workflow in ~/.config/claude-toolkit/modes/{mode_doc}.md. "
        "That file is the source of truth; if its guidance is wrong or incomplete (e.g. it "
        "did not prevent a mistake you just made), edit it to improve it."
    )

    # The generic toolkit prompt is read from the checkout on the host and injected
    # as an appended system prompt. Nothing from .claude/ is mounted into ~/.claude,
    # so this is the only channel for the prompt. Combine it with the mode pointer so
    # a single --append-system-prompt carries both.
    generic_prompt = (REPO_DIR / ".claude" / "toolkit-prompt.md").read_text().strip()
    append_prompt = f"{generic_prompt}\n\n{mode_prompt}"

    settings = {
        "apiKeyHelper": "cat /home/ubuntu/.config/claude-toolkit/anthropic-key",
        "theme": "dark",
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [{"type": "command", "command": f"python3 {session_start_script}"}],
                }
            ],
            # Before a Bash command runs: pre_push_review gates a `git push` on a
            # separate reviewer agent (a different model). It denies the push only on
            # a concrete defect and fails open otherwise, so it never wedges the
            # session; non-push commands pass straight through.
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": f"python3 {pre_push_script}"}],
                }
            ],
            # After a Bash command runs: capture_writes logs any remote write to the
            # global writes log (observe-only, never a gate), and arm_monitor arms the
            # host monitor's CI watch on a successful `git push`.
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": f"python3 {capture_script}"},
                        {"type": "command", "command": f"python3 {arm_monitor_script}"},
                    ],
                }
            ],
        },
    }

    # Only allocate a TTY when we actually have one, so the session also works
    # headless (e.g. `-p "..."` driven from another process).
    tty_flags = ["-it"] if sys.stdin.isatty() else []
    # --review needs every project's dir (to read each meta.json for repo/PR, and any
    # per-PR checkout under projects/<name>/repo). A working session sees only its own
    # project, so this all-projects mount is review-only. The container target
    # (.../projects) is a sibling of the per-project .../project mount, not nested.
    review_mount = (
        ["-v", f"{projects_dir}:/home/ubuntu/.config/claude-toolkit/projects:rw"]
        if review_mode else []
    )
    gnupg_copy = stage_gnupg()
    docker_args = [
        "docker", "run", "--rm", *tty_flags,
        "-e", "HOME=/home/ubuntu",
        "-w", workdir,
        "-v", f"{cwd}:{workdir}:rw",
        # Mount the host's real ~/.claude as-is: the session runs in the user's own
        # Claude environment -- their CLAUDE.md (memory), skills, commands, plugins,
        # and history. We impose nothing here; our behavior arrives via --settings
        # (hooks) and --append-system-prompt (the generic prompt) instead.
        # rw because Claude writes its runtime state (history, projects/, todos/).
        "-v", f"{HOME}/.claude:/home/ubuntu/.claude:rw",
        # Toolkit code lives under ~/.config/claude-toolkit/, referenced by the hook
        # commands and the mode pointer -- kept out of ~/.claude so it shadows nothing.
        # Hooks are read-only; modes are rw so the agent can refine the role docs and
        # the edits land back in the checkout.
        "-v", f"{REPO_DIR}/.claude/hooks:/home/ubuntu/.config/claude-toolkit/hooks:ro",
        "-v", f"{REPO_DIR}/.claude/modes:/home/ubuntu/.config/claude-toolkit/modes:rw",
        # settings.json (permissions allowlist + enabledPlugins) at ~/.claude's default
        # location. Read-only so the container cannot clobber the committed repo source
        # (see the write-eacces-mounted-settings note). In auto mode narrow allow rules
        # speed up routine reads; the classifier gates everything else.
        "-v", f"{REPO_DIR}/.claude/settings.json:/home/ubuntu/.claude/settings.json:ro",
        "-v", f"{HOME}/.claude.json:/home/ubuntu/.claude.json:rw",
        "-v", f"{HOME}/.gitconfig:/home/ubuntu/.gitconfig:ro",
        # Mount THIS project's own dir (projects/<name>/) at a fixed container path,
        # ~/.config/claude-toolkit/project/, so the hooks see project/pending-reads/...
        # with no project name. It is a fresh subtree -- nothing else mounts under it --
        # so it sidesteps the Docker Desktop virtiofs failure you get from mounting
        # proj_dir AS ~/.config/claude-toolkit and nesting anthropic-key/hooks/modes on top.
        "-v", f"{proj_dir}:/home/ubuntu/.config/claude-toolkit/project:rw",
        # Global writes log (all projects) so capture_writes records here and the
        # --review session reads one consolidated list.
        "-v", f"{writes_log}:/home/ubuntu/.config/claude-toolkit/writes-log:rw",
        # --review only: every project's dir (meta.json + per-PR checkouts).
        *review_mount,
        "-v", f"{APP_DIR}/anthropic-key:/home/ubuntu/.config/claude-toolkit/anthropic-key:ro",
        # Private copy of the GPG keyring so the container can sign commits without
        # touching the host keyring.
        "-v", f"{gnupg_copy}:/home/ubuntu/.gnupg:rw",
        # gh config (hosts.yml + token) at gh's default location: the host's real token.
        "-v", f"{gh_config_src}:/home/ubuntu/.config/gh:rw",
        "-e", "GIT_CONFIG_COUNT=1",
        "-e", "GIT_CONFIG_KEY_0=credential.https://github.com.helper",
        "-e", f"GIT_CONFIG_VALUE_0={git_helper}",
        IMAGE, "--permission-mode", "auto",
        "--settings", json.dumps(settings),
        "--append-system-prompt", append_prompt,
        *claude_args,
    ]

    # Replace this process with docker so the interactive TTY attaches directly and
    # signals pass through. The detached monitor (new session) survives.
    os.execvp("docker", docker_args)


if __name__ == "__main__":
    main()
