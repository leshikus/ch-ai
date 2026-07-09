#!/usr/bin/env bash
#
# Launch Claude Code in Docker with ~/repos mounted and all permissions granted.
#   ./claude-docker.sh [claude args...]
#
set -euo pipefail

IMAGE="claude-docker:latest"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Always build: Docker's layer cache makes this a fast no-op when nothing in the
# build context (Dockerfile included) changed, and it picks up Dockerfile edits.
docker build -t "$IMAGE" "$SCRIPT_DIR"

# Persist onboarding state (theme, bypass-permissions acceptance, per-project trust)
# so the initial prompts are answered once, not on every launch. This state lives in
# ~/.claude.json (a file in $HOME, not inside ~/.claude). Ensure it exists so the
# bind mount below attaches a file rather than Docker creating an empty directory.
[ -e "$HOME/.claude.json" ] || echo '{}' > "$HOME/.claude.json"

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

# Mint a short-lived, read-only GitHub token on the HOST from a GitHub App private
# key, and pass only the token (never the key) into the container. The key stays on
# the host; the token is scoped read-only and expires in 1 hour.
GH_APP_ID="${GH_APP_ID:-4250913}"
GH_APP_PEM="${GH_APP_PEM:-$SCRIPT_DIR/../ro-token.pem}"

command -v jq >/dev/null 2>&1 || { echo "error: jq is required (brew install jq)." >&2; exit 1; }
[ -r "$GH_APP_PEM" ] || { echo "error: cannot read GitHub App key: $GH_APP_PEM" >&2; exit 1; }

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
now=$(date +%s)
jwt_h=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
jwt_p=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((now - 60)) $((now + 540)) "$GH_APP_ID" | b64url)
jwt_s=$(printf '%s.%s' "$jwt_h" "$jwt_p" | openssl dgst -sha256 -sign "$GH_APP_PEM" | b64url)
app_jwt="$jwt_h.$jwt_p.$jwt_s"

api="https://api.github.com"
gh_hdr=(-H "Authorization: Bearer $app_jwt" -H "Accept: application/vnd.github+json")
install_id=$(curl -fsS "${gh_hdr[@]}" "$api/app/installations" | jq -r '.[0].id')
[ -n "$install_id" ] && [ "$install_id" != "null" ] || { echo "error: no App installation found (install the App on your account)." >&2; exit 1; }

GH_TOKEN=$(curl -fsS -X POST "${gh_hdr[@]}" \
    -d '{"permissions":{"contents":"read","pull_requests":"read","metadata":"read"}}' \
    "$api/app/installations/$install_id/access_tokens" | jq -r '.token')
[ -n "$GH_TOKEN" ] && [ "$GH_TOKEN" != "null" ] || { echo "error: failed to mint installation token." >&2; exit 1; }

# Host repos are mounted under /home/agent/repos so ~ resolves consistently inside
# the container. Translate the host $PWD (under $HOME/repos) to the container path so
# Claude opens the same project. Note: because the absolute path differs from the host,
# `--resume`/`--continue` key sessions to the container path, not the host path.
exec docker run --rm -it \
    --user "$(id -u):$(id -g)" \
    -e HOME=/home/agent \
    -w "/home/agent/repos/${PWD#"$HOME"/repos/}" \
    -v "$HOME/repos:/home/agent/repos:rw" \
    -v "$HOME/.claude:/home/agent/.claude:rw" \
    -v "$HOME/.claude.json:/home/agent/.claude.json:rw" \
    -v "$HOME/.gitconfig:/home/agent/.gitconfig:ro" \
    -e GH_TOKEN="$GH_TOKEN" \
    -e GIT_CONFIG_COUNT=1 \
    -e GIT_CONFIG_KEY_0="credential.https://github.com.helper" \
    -e GIT_CONFIG_VALUE_0='!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f' \
    "$IMAGE" --dangerously-skip-permissions \
    --settings '{"apiKeyHelper":"cat /home/agent/.claude/.docker-anthropic-key","theme":"dark"}' \
    "$@"
