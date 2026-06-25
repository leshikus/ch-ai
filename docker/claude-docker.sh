#!/usr/bin/env bash
#
# Launch Claude Code in Docker with ~/repos mounted and all permissions granted.
#   ./claude-docker.sh [claude args...]
#
set -euo pipefail

IMAGE="claude-docker:latest"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -t "$IMAGE" "$SCRIPT_DIR"

# macOS stores the Claude Code credential in the login Keychain, not on disk, so
# mounting ~/.claude alone leaves the container unauthenticated. This account uses
# a raw API key (not OAuth), so .credentials.json can't carry it. Instead, drop the
# key into a file under the mounted ~/.claude and point apiKeyHelper at it via
# --settings -- the key stays in a 0600 file, never in the env or `docker inspect`.
KEY_FILE="$HOME/.claude/.docker-anthropic-key"
(umask 077; security find-generic-password -s "Claude Code" -w 2>/dev/null > "$KEY_FILE") || {
    echo "error: could not read 'Claude Code' credential from the macOS Keychain." >&2
    echo "       Log in on the host first (run 'claude' and authenticate), then retry." >&2
    rm -f "$KEY_FILE"
    exit 1
}

# Launch in the host's current directory (already mounted via ~/repos at the same
# absolute path) so Claude keys sessions to the same project as the host -- this is
# what lets `--resume`/`--continue` find sessions started on the host or a prior run.
exec docker run --rm -it \
    --user "$(id -u):$(id -g)" \
    -e HOME=/home/agent \
    -w "$PWD" \
    -v "$HOME/repos:$HOME/repos:rw" \
    -v "$HOME/.claude:/home/agent/.claude:rw" \
    -v "$HOME/.gitconfig:/home/agent/.gitconfig:ro" \
    "$IMAGE" --dangerously-skip-permissions \
    --settings '{"apiKeyHelper":"cat /home/agent/.claude/.docker-anthropic-key"}' \
    "$@"
