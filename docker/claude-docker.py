#!/usr/bin/env python3
"""Launch Claude Code in Docker with ~/repos mounted and all permissions granted.

    ./claude-docker.py [claude args...]

Read-only session: a PreToolUse hook queues GitHub writes instead of running
them, and the GitHub token is read-only and kept fresh by a background refresher.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import mint_gh_token

IMAGE = "claude-docker:latest"
SCRIPT_DIR = Path(__file__).resolve().parent
HOME = Path.home()


def to_container_repo_path(host_path: Path) -> str:
    """Translate a host path under ~/repos to its container path.

    Host repos are mounted at /home/agent/repos. Mirrors the shell prefix-strip
    `${p#"$HOME"/repos/}`: if the path is not under ~/repos it is left as-is
    (so a misconfigured launch fails visibly rather than silently).
    """
    prefix = f"{HOME}/repos/"
    p = str(host_path)
    sub = p[len(prefix):] if p.startswith(prefix) else p
    return f"/home/agent/repos/{sub}"


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
    # Always build: Docker's layer cache makes this a fast no-op when nothing in
    # the build context changed, and it picks up Dockerfile edits.
    subprocess.run(["docker", "build", "-t", IMAGE, str(SCRIPT_DIR)], check=True)

    # Persist onboarding state (theme, bypass-permissions acceptance, per-project
    # trust) so the initial prompts are answered once. This lives in ~/.claude.json
    # (a file in $HOME, not inside ~/.claude); ensure it exists so the bind mount
    # attaches a file rather than Docker creating an empty directory.
    claude_json = HOME / ".claude.json"
    if not claude_json.exists():
        claude_json.write_text("{}")

    # macOS stores the Claude Code credential in the login Keychain, not on disk,
    # so mounting ~/.claude alone leaves the container unauthenticated. This account
    # uses a raw API key (not OAuth), so .credentials.json can't carry it. Instead,
    # drop the key into a 0600 file under the mounted ~/.claude and point
    # apiKeyHelper at it -- the key never enters the env or `docker inspect`.
    key_file = HOME / ".claude" / ".docker-anthropic-key"
    old_umask = os.umask(0o077)
    try:
        key_file.write_bytes(read_keychain_api_key())
    finally:
        os.umask(old_umask)
    key_file.chmod(0o600)

    # GitHub auth: mint a short-lived, read-only App installation token now (so it
    # exists before the container starts), then keep it fresh with a detached,
    # self-deduping background refresher (only one runs across all containers). The
    # token is written where every container reads it -- a raw token file for git's
    # credential helper and a gh hosts.yml (surfaced at gh's default config dir via
    # the mount below), so no GH_TOKEN env is needed.
    mint_gh_token.mint()
    subprocess.Popen(
        [sys.executable, str(SCRIPT_DIR / "token_refresher.py")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )

    # Container-side path to the queue-writes PreToolUse hook (it lives next to
    # this script) and the working dir, both translated through the ~/repos mount.
    hook_script = f"{to_container_repo_path(SCRIPT_DIR)}/queue-writes.py"
    workdir = to_container_repo_path(Path.cwd())

    settings = {
        "apiKeyHelper": "cat /home/agent/.claude/.docker-anthropic-key",
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

    docker_args = [
        "docker", "run", "--rm", "-it",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/home/agent",
        "-e", "CLAUDE_PENDING_WRITES=1",
        "-w", workdir,
        "-v", f"{HOME}/repos:/home/agent/repos:rw",
        "-v", f"{HOME}/.claude:/home/agent/.claude:rw",
        "-v", f"{HOME}/.claude.json:/home/agent/.claude.json:rw",
        "-v", f"{HOME}/.gitconfig:/home/agent/.gitconfig:ro",
        # A dedicated gh config dir mounted at gh's default location. NOT the host's
        # real ~/.config/gh -- that would leak a full-scope personal token.
        "-v", f"{HOME}/.claude/gh:/home/agent/.config/gh:rw",
        "-e", "GIT_CONFIG_COUNT=1",
        "-e", "GIT_CONFIG_KEY_0=credential.https://github.com.helper",
        # Passed literally; the $(cat ...) is evaluated by git inside the container.
        "-e", 'GIT_CONFIG_VALUE_0=!f() { echo username=x-access-token; '
              'echo "password=$(cat /home/agent/.claude/.docker-gh-token)"; }; f',
        IMAGE, "--dangerously-skip-permissions",
        "--settings", json.dumps(settings),
        *sys.argv[1:],
    ]

    # Replace this process with docker so the interactive TTY attaches directly and
    # signals pass through. The detached refresher (new session) survives.
    os.execvp("docker", docker_args)


if __name__ == "__main__":
    main()
