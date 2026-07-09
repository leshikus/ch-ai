#!/usr/bin/env python3
"""Singleton host-side loop that keeps the container-shared GitHub token fresh.

Only ONE instance runs no matter how many containers launch: a PID-file guard
makes any redundant instance exit immediately. It re-mints every 50 min
(installation tokens live ~60 min) by calling mint_gh_token.mint(), which
rewrites the shared token file and gh hosts.yml under ~/.claude.

Started in the background by claude-docker.sh. Runs until killed; stop it with:
    kill "$(cat ~/.claude/.token-refresher.pid)"
"""

import os
import sys
import time
from pathlib import Path

import mint_gh_token

PIDFILE = Path(os.path.expanduser("~/.claude/.token-refresher.pid"))
INTERVAL = 3000  # 50 min


def _already_running() -> bool:
    if not PIDFILE.exists():
        return False
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, 0)  # signal 0: existence check, raises if the process is gone
        return True
    except (ValueError, ProcessLookupError):
        return False  # stale/garbage PID file -- we take over
    except PermissionError:
        return True  # process exists but owned by another user -- assume alive


def main() -> int:
    if _already_running():
        return 0

    PIDFILE.write_text(str(os.getpid()))
    try:
        while True:
            try:
                mint_gh_token.mint()
            except Exception as exc:  # keep the loop alive across transient failures
                print(f"token-refresher: mint failed, retrying next cycle: {exc}", file=sys.stderr)
            time.sleep(INTERVAL)
    finally:
        # Only remove the PID file if it is still ours.
        try:
            if PIDFILE.read_text().strip() == str(os.getpid()):
                PIDFILE.unlink()
        except (FileNotFoundError, ValueError):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
