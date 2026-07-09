#!/usr/bin/env python3
"""Mint a short-lived, read-only GitHub App installation token and write it
where the containers read it:

  * ~/.claude/.docker-gh-token  -- raw token, for git's credential helper
  * ~/.claude/gh/hosts.yml      -- gh config, so `gh` uses it with no GH_TOKEN

Runs on the HOST (it needs the App private key). Both outputs live under the
mounted ~/.claude, so every container sees the current token. Installation
tokens live ~1 h, so token_refresher.py calls mint() periodically; this file is
also runnable directly for the initial synchronous mint at launch.
"""

import base64
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

GH_APP_ID = os.environ.get("GH_APP_ID", "4250913")
_SCRIPT_DIR = Path(__file__).resolve().parent
GH_APP_PEM = Path(os.environ.get("GH_APP_PEM", _SCRIPT_DIR.parent / "ro-token.pem"))

CLAUDE_DIR = Path(os.path.expanduser("~/.claude"))
TOKEN_FILE = CLAUDE_DIR / ".docker-gh-token"
GH_CONFIG_DIR = CLAUDE_DIR / "gh"
HOSTS_YML = GH_CONFIG_DIR / "hosts.yml"

API = "https://api.github.com"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _app_jwt() -> str:
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps({"iat": now - 60, "exp": now + 540, "iss": GH_APP_ID}, separators=(",", ":")).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    # RSA (RS256) signing is not in the stdlib; shell out to openssl, an external tool.
    proc = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(GH_APP_PEM)],
        input=signing_input, capture_output=True, check=True,
    )
    return f"{header}.{payload}.{_b64url(proc.stdout)}"


def _api(path: str, jwt: str, method: str = "GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {jwt}")
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def mint() -> str:
    """Mint a fresh installation token and write the shared token/config files."""
    if not GH_APP_PEM.is_file():
        raise SystemExit(f"error: cannot read GitHub App key: {GH_APP_PEM}")

    jwt = _app_jwt()
    installs = _api("/app/installations", jwt)
    if not installs:
        raise SystemExit("error: no App installation found (install the App on your account).")
    install_id = installs[0]["id"]

    result = _api(
        f"/app/installations/{install_id}/access_tokens", jwt, method="POST",
        body={"permissions": {"contents": "read", "pull_requests": "read", "metadata": "read"}},
    )
    token = result.get("token")
    if not token:
        raise SystemExit("error: failed to mint installation token.")

    old_umask = os.umask(0o077)
    try:
        GH_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(token)
        HOSTS_YML.write_text(
            "github.com:\n"
            f"    oauth_token: {token}\n"
            "    git_protocol: https\n"
            "    user: x-access-token\n"
        )
    finally:
        os.umask(old_umask)
    return token


if __name__ == "__main__":
    mint()
