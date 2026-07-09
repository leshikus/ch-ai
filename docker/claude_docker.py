#!/usr/bin/env python3
"""Launch Claude Code in Docker with ~/repos mounted and all permissions granted.

    ./claude_docker.py [claude args...]            # read-only session (default)
    ./claude_docker.py --write [claude args...]    # read-write: real gh token

Our runtime state (queue, tokens, gh config, keyring copy) lives under
~/.config/claude-toolkit/ and is mounted into the container; Claude Code's own
~/.claude is mounted separately for its config.

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
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import mint_gh_token

IMAGE = "claude-toolkit:latest"
SCRIPT_DIR = Path(__file__).resolve().parent
HOME = Path.home()
# All of our runtime state lives here (mounted into the container), kept out of
# Claude Code's own ~/.claude.
APP_DIR = HOME / ".config" / "claude-toolkit"


def to_container_repo_path(host_path: Path) -> str:
    """Translate a host path under ~/repos to its container path.

    Host repos are mounted at /home/ubuntu/repos. Mirrors the shell prefix-strip
    `${p#"$HOME"/repos/}`: if the path is not under ~/repos it is left as-is
    (so a misconfigured launch fails visibly rather than silently).
    """
    prefix = f"{HOME}/repos/"
    p = str(host_path)
    sub = p[len(prefix):] if p.startswith(prefix) else p
    return f"/home/ubuntu/repos/{sub}"


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
        [sys.executable, str(SCRIPT_DIR / script)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )


def ensure_claude_links() -> None:
    """Ensure ~/.claude/CLAUDE.md and settings.json symlink to the repo copies.

    These let the host's own Claude sessions use the committed config. If a link
    is missing or broken, ask before creating it (never silently touch ~/.claude).
    """
    claude_dir = HOME / ".claude"
    links = {
        claude_dir / "CLAUDE.md": SCRIPT_DIR.parent / "CLAUDE.md",
        claude_dir / "settings.json": SCRIPT_DIR.parent / ".claude" / "settings.json",
    }
    for link, target in links.items():
        if link.exists():
            continue  # a valid file/link is already there -- leave it
        broken = link.is_symlink()
        state = "a broken link" if broken else "missing"
        if not sys.stdin.isatty():
            print(f"warning: {link} is {state}; skipping (no TTY to confirm)", file=sys.stderr)
            continue
        if input(f"{link} is {state}. Point it at {target}? [y/N] ").strip().lower() in ("y", "yes"):
            claude_dir.mkdir(parents=True, exist_ok=True)
            if broken:
                link.unlink()
            link.symlink_to(target)
            print(f"linked {link} -> {target}")
        else:
            print(f"skipped {link}")


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


def main() -> None:
    write_mode = "--write" in sys.argv[1:]
    claude_args = [a for a in sys.argv[1:] if a != "--write"]

    ensure_claude_links()
    APP_DIR.mkdir(parents=True, exist_ok=True)
    # The container mounts only these two queue dirs (never the parent, which holds
    # ro-token.pem); create them so the bind mounts attach real dirs, not new
    # root-owned ones.
    (APP_DIR / "pending-writes").mkdir(exist_ok=True)
    (APP_DIR / "change-requests").mkdir(exist_ok=True)

    # Always build: Docker's layer cache makes this a fast no-op when nothing in
    # the build context changed, and it picks up Dockerfile edits.
    subprocess.run(["docker", "build", "-t", IMAGE, str(SCRIPT_DIR)], check=True)

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
        # Soft-restart the helpers so edits to them take effect on this launch (a
        # plain relaunch would exit via the PID guard, leaving the old code running).
        restart_helper("token_refresher.py", "token-refresher.pid")
        restart_helper("pending_writes_watcher.py", "pending-writes-watcher.pid")
        gh_config_src = str(APP_DIR / "gh")

    git_helper = (
        '!f() { echo username=x-access-token; '
        'echo "password=$(cat /home/ubuntu/.config/gh/token)"; }; f'
    )

    # Container-side path to the queue_writes PreToolUse hook (it lives next to
    # this script) and the working dir, both translated through the ~/repos mount.
    hook_script = f"{to_container_repo_path(SCRIPT_DIR)}/queue_writes.py"
    workdir = to_container_repo_path(Path.cwd())

    # Inject this container's role instructions directly. Read the host-side doc
    # (write-mode for --write, read-only-mode otherwise) and append it to claude's
    # system prompt -- more reliable than the global CLAUDE.md @imports, whose host
    # paths do not resolve inside the container.
    mode_doc = SCRIPT_DIR.parent / ("write-mode.md" if write_mode else "read-only-mode.md")
    mode_prompt = mode_doc.read_text() if mode_doc.is_file() else ""

    settings = {
        "apiKeyHelper": "cat /home/ubuntu/.config/claude-toolkit/anthropic-key",
        "theme": "dark",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": f"python3 {hook_script}"}],
                }
            ]
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
    # A fixed name in write mode lets the pending-writes watcher serialize drains
    # (one tab at a time) via `docker ps`, and refuses a second concurrent drain.
    name_flags = ["--name", "claude-toolkit-drain"] if write_mode else []
    gnupg_copy = stage_gnupg()
    docker_args = [
        "docker", "run", "--rm", *name_flags, *tty_flags,
        "-e", "HOME=/home/ubuntu",
        *pending_env,
        "-w", workdir,
        "-v", f"{HOME}/repos:/home/ubuntu/repos:rw",
        # Claude Code config: mount the repo's committed CLAUDE.md + settings.json
        # (located relative to this script, so the checkout can live anywhere) into
        # ~/.claude, rather than the host's personal ~/.claude.
        "-v", f"{SCRIPT_DIR.parent}/CLAUDE.md:/home/ubuntu/.claude/CLAUDE.md:rw",
        "-v", f"{SCRIPT_DIR.parent}/.claude/settings.json:/home/ubuntu/.claude/settings.json:rw",
        "-v", f"{HOME}/.claude.json:/home/ubuntu/.claude.json:rw",
        "-v", f"{HOME}/.gitconfig:/home/ubuntu/.gitconfig:ro",
        # Our runtime state. Mount ONLY the two queue dirs and the key file -- NOT
        # the parent ~/.config/claude-toolkit, which holds ro-token.pem (the GitHub
        # App private key) that must never enter a container.
        "-v", f"{APP_DIR}/pending-writes:/home/ubuntu/.config/claude-toolkit/pending-writes:rw",
        "-v", f"{APP_DIR}/change-requests:/home/ubuntu/.config/claude-toolkit/change-requests:rw",
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
        *(["--append-system-prompt", mode_prompt] if mode_prompt else []),
        *claude_args,
    ]

    # Replace this process with docker so the interactive TTY attaches directly and
    # signals pass through. The detached refresher/watcher (new session) survive.
    os.execvp("docker", docker_args)


if __name__ == "__main__":
    main()
