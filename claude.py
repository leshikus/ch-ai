#!/usr/bin/env python3
"""Launch Claude Code in Docker with the current directory mounted and all permissions granted.

    ./claude.py [claude args...]            # read-only session (default)
    ./claude.py --write [claude args...]    # read-write: real gh token

Our runtime state (queue, tokens, gh config, keyring copy) and toolkit code
(hooks, mode docs) live under ~/.config/claude-toolkit/ and are mounted into the
container. The host's own ~/.claude is mounted as-is, so the session runs in the
user's real Claude environment (their CLAUDE.md, skills, plugins, history); our
sandbox behavior is layered on via --settings (hooks) and --append-system-prompt.

Read-only (default): a PreToolUse hook queues GitHub writes to
~/.config/claude-toolkit/pending-writes instead of running them, and the GitHub
token is a scoped read-only App token kept fresh by a background refresher.

--write: extract the host's real gh token (from the login keychain via
`gh auth token`) into a generated hosts.yml + token file, mount those into the
container, and leave the queue hook inactive -- so the session executes GitHub
writes directly, used to drain the pending-writes queue.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import mint_gh_token

IMAGE = "claude-toolkit:latest"
REPO_DIR = Path(__file__).resolve().parent  # claude.py is the repo-root entry point
HOME = Path.home()
# All of our runtime state lives here (mounted into the container), kept out of
# Claude Code's own ~/.claude.
APP_DIR = HOME / ".config" / "claude-toolkit"


def container_name(project: str) -> str:
    """Docker container name for a project's --write drain.

    Per-project so multiple drains run concurrently -- one tab (and one container)
    each. Sanitized to Docker's allowed name charset [a-zA-Z0-9_.-]. monitor.py
    imports this so the name it filters `docker ps` on matches the one claude.py sets.
    """
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", project)
    return f"claude-toolkit-drain-{safe}"


def write_mode_gh_config() -> str:
    """Materialize a gh config dir carrying the host's real token, for --write.

    On macOS the real gh token lives in the login keychain, not in
    ~/.config/gh/hosts.yml, so mounting that dir gives the container no token.
    Extract it with `gh auth token` and write a hosts.yml (for gh) plus a raw
    token file (for git's credential helper).
    """
    dest = APP_DIR / "write-gh"
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


def restart_helper(script: str, pidfile_name: str) -> None:
    """Soft-restart a background helper so edits to it take effect on launch.

    The helpers guard themselves with a PID file, so a plain relaunch would exit
    as a redundant instance and keep the old code running. SIGTERM the old
    instance first, then start a fresh one.
    """
    pidfile = APP_DIR / pidfile_name
    try:
        os.kill(int(pidfile.read_text().strip()), signal.SIGTERM)
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        pass
    pidfile.unlink(missing_ok=True)
    subprocess.Popen(
        [sys.executable, str(REPO_DIR / script)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )


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
    write_mode = "--write" in sys.argv[1:]
    claude_args = [a for a in sys.argv[1:] if a != "--write"]

    pull_toolkit()
    APP_DIR.mkdir(parents=True, exist_ok=True)
    # The container mounts only these queue dirs (never the parent, which holds
    # ro-token.pem); create them so the bind mounts attach real dirs, not new
    # root-owned ones.
    (APP_DIR / "pending-writes").mkdir(exist_ok=True)
    (APP_DIR / "pending-reads").mkdir(exist_ok=True)
    (APP_DIR / "pending-monitoring").mkdir(exist_ok=True)
    (APP_DIR / "projects").mkdir(exist_ok=True)

    # Always build: Docker's layer cache makes this a fast no-op when nothing in
    # the build context changed, and it picks up Dockerfile edits.
    subprocess.run(["docker", "build", "-t", IMAGE, str(REPO_DIR)], check=True)

    # Persist onboarding state (theme, bypass-permissions acceptance, per-project
    # trust). This lives in ~/.claude.json (a file in $HOME, not inside ~/.claude);
    # ensure it exists so the bind mount attaches a file, not a new empty directory.
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

    # GitHub auth. Read-only (default): mint a scoped read-only App token now, plus
    # a detached self-deduping token refresher and the pending-writes watcher.
    # --write: extract the host's real gh token. Either way the gh config dir
    # (hosts.yml + token) is mounted at gh's default location, so git and gh share
    # one token and the git credential helper is identical.
    if write_mode:
        gh_config_src = write_mode_gh_config()
    else:
        mint_gh_token.mint()
        # Soft-restart the monitor (token refresh + pending-writes drain tabs) so
        # edits to it take effect on this launch -- a plain relaunch would exit via
        # the PID guard, leaving the old code running.
        restart_helper("monitor.py", "monitor.pid")
        gh_config_src = str(APP_DIR / "gh")

    git_helper = (
        '!f() { echo username=x-access-token; '
        'echo "password=$(cat /home/ubuntu/.config/gh/token)"; }; f'
    )

    # Toolkit code (queue_writes, session-start orientation, arm_monitor) is mounted
    # under ~/.config/claude-toolkit/ below -- NOT into ~/.claude, so we no longer
    # overwrite the user's ~/.claude dir. Fixed paths, independent of where the
    # checkout lives.
    hook_script = "/home/ubuntu/.config/claude-toolkit/hooks/queue_writes.py"
    session_start_script = "/home/ubuntu/.config/claude-toolkit/hooks/session_start.py"
    arm_monitor_script = "/home/ubuntu/.config/claude-toolkit/hooks/arm_monitor.py"
    # Mount just the current directory at /home/ubuntu/<cwd-name> and work there.
    # No ~/repos assumption: the session sees only the checkout it was launched from.
    cwd = Path.cwd()
    workdir = f"/home/ubuntu/{cwd.name}"
    # Record this project's host checkout so the monitor's drain tab can cd into it,
    # keyed by the mount basename. Replaces the old ~/repos lookup (see monitor.py).
    (APP_DIR / "projects" / f"{cwd.name}.json").write_text(
        json.dumps({"host_dir": str(cwd)}) + "\n"
    )

    # Point this container's session at its role doc (mounted rw under
    # ~/.config/claude-toolkit/modes below, so the agent can refine it). The doc is
    # guidance -- the read-only guarantee is the queue_writes hook -- so a pointer
    # (vs injecting a snapshot) is safe and keeps one live, editable source of truth.
    mode_name = "write-mode" if write_mode else "read-only-mode"
    mode_prompt = (
        f"Follow the {mode_name} workflow in ~/.config/claude-toolkit/modes/{mode_name}.md. "
        f"That file is the source of truth; if its guidance is wrong or incomplete (e.g. it "
        f"did not prevent a mistake you just made), edit it to improve it."
    )

    # The generic toolkit prompt is read from the checkout on the host and injected
    # into both containers (read-only and --write) as an appended system prompt.
    # Nothing from .claude/ is mounted into ~/.claude anymore, so this is the only
    # channel for the prompt. Combine it with the mode pointer so a single
    # --append-system-prompt carries both.
    generic_prompt = (REPO_DIR / ".claude" / "toolkit-prompt.md").read_text().strip()
    append_prompt = f"{generic_prompt}\n\n{mode_prompt}"

    settings = {
        "apiKeyHelper": "cat /home/ubuntu/.config/claude-toolkit/anthropic-key",
        "theme": "dark",
        # Read-only mode runs with --dangerously-skip-permissions, which otherwise
        # shows an interactive one-time "Bypass Permissions mode" warning on every
        # fresh container. Pre-accept it here so headless sessions don't hang on it.
        "skipDangerousModePermissionPrompt": True,
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [{"type": "command", "command": f"python3 {session_start_script}"}],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": f"python3 {hook_script}"}],
                }
            ],
            # After a Bash command runs, arm_monitor arms the host monitor for any
            # successful `git push` (drops a pending-monitoring request). It self-gates:
            # a push denied by the queue hook in read-only mode never reaches PostToolUse,
            # so this only fires on real pushes in the write drain container.
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": f"python3 {arm_monitor_script}"}],
                }
            ],
        },
    }

    # CLAUDE_PENDING_WRITES arms the queue hook; omit it in write mode so writes run.
    pending_env = [] if write_mode else ["-e", "CLAUDE_PENDING_WRITES=1"]
    # Read-only mode bypasses permission prompts (its token can't do real writes and
    # the hook queues write commands anyway, so prompts would only add friction).
    # Write mode keeps prompts on so a human approves each write -- which needs an
    # interactive TTY to answer them.
    perms_flag = [] if write_mode else ["--dangerously-skip-permissions"]
    if write_mode and not sys.stdin.isatty():
        sys.exit("error: --write needs an interactive terminal to answer permission prompts.")
    # Only allocate a TTY when we actually have one, so read-only mode also works
    # headless (e.g. `-p "..."` driven from another process).
    tty_flags = ["-it"] if sys.stdin.isatty() else []
    # A per-project name in write mode lets the pending-writes watcher run one
    # drain per project concurrently (a tab each) and, via `docker ps`, avoid
    # opening a second tab for a project already draining. Derived from the cwd
    # basename, which is the project the drain tab cd'd into.
    name_flags = ["--name", container_name(Path.cwd().name)] if write_mode else []
    # --write only: overlay the committed settings.json onto ~/.claude/settings.json
    # (a symlink to a host path that doesn't resolve in the container) for its
    # permissions allowlist + enabledPlugins. rw so Claude can persist acceptance
    # state. Read-only doesn't mount it -- the bypass warning is suppressed via the
    # skipDangerousModePermissionPrompt flag in the inline --settings above.
    settings_mount = (
        ["-v", f"{REPO_DIR}/.claude/settings.json:/home/ubuntu/.claude/settings.json:rw"]
        if write_mode else []
    )
    gnupg_copy = stage_gnupg()
    docker_args = [
        "docker", "run", "--rm", *name_flags, *tty_flags,
        "-e", "HOME=/home/ubuntu",
        *pending_env,
        "-w", workdir,
        "-v", f"{cwd}:{workdir}:rw",
        # Mount the host's real ~/.claude as-is: the session runs in the user's own
        # Claude environment -- their CLAUDE.md (memory), skills, commands, plugins,
        # and history. We impose nothing here; our sandbox behavior arrives via
        # --settings (hooks) and --append-system-prompt (the generic prompt) instead.
        # rw because Claude writes its runtime state (history, projects/, todos/).
        "-v", f"{HOME}/.claude:/home/ubuntu/.claude:rw",
        # Toolkit code lives under ~/.config/claude-toolkit/, referenced by the hook
        # commands and the mode pointer -- kept out of ~/.claude so it shadows nothing.
        # Hooks are read-only; modes are rw so the agent can refine the role docs and
        # the edits land back in the checkout.
        "-v", f"{REPO_DIR}/.claude/hooks:/home/ubuntu/.config/claude-toolkit/hooks:ro",
        "-v", f"{REPO_DIR}/.claude/modes:/home/ubuntu/.config/claude-toolkit/modes:rw",
        *settings_mount,
        "-v", f"{HOME}/.claude.json:/home/ubuntu/.claude.json:rw",
        "-v", f"{HOME}/.gitconfig:/home/ubuntu/.gitconfig:ro",
        # Our runtime state. Mount ONLY these queue dirs and the key file -- NOT
        # the parent ~/.config/claude-toolkit, which holds ro-token.pem (the GitHub
        # App private key) that must never enter a container.
        "-v", f"{APP_DIR}/pending-writes:/home/ubuntu/.config/claude-toolkit/pending-writes:rw",
        "-v", f"{APP_DIR}/pending-reads:/home/ubuntu/.config/claude-toolkit/pending-reads:rw",
        "-v", f"{APP_DIR}/pending-monitoring:/home/ubuntu/.config/claude-toolkit/pending-monitoring:rw",
        "-v", f"{APP_DIR}/anthropic-key:/home/ubuntu/.config/claude-toolkit/anthropic-key:ro",
        # Private copy of the GPG keyring so the container can sign commits without
        # touching the host keyring.
        "-v", f"{gnupg_copy}:/home/ubuntu/.gnupg:rw",
        # gh config (hosts.yml + token) at gh's default location: read-only mode uses
        # the scoped App token, --write uses the host's real token.
        "-v", f"{gh_config_src}:/home/ubuntu/.config/gh:rw",
        "-e", "GIT_CONFIG_COUNT=1",
        "-e", "GIT_CONFIG_KEY_0=credential.https://github.com.helper",
        "-e", f"GIT_CONFIG_VALUE_0={git_helper}",
        IMAGE, *perms_flag,
        "--settings", json.dumps(settings),
        "--append-system-prompt", append_prompt,
        *claude_args,
    ]

    # Replace this process with docker so the interactive TTY attaches directly and
    # signals pass through. The detached refresher/watcher (new session) survive.
    os.execvp("docker", docker_args)


if __name__ == "__main__":
    main()
