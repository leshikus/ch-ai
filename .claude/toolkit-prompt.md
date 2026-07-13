# Claude Toolkit

This prompt is used for claude toolkit development as well as related development.

## Communication style

Keep replies terse — minimal words, no preamble, no recaps, no restating the request. Lead with the conclusion; prefer bullets/code over prose. Expand only when asked or when correctness needs it. Still flag real risks, briefly.

When addressing a review comment, quote the comment's text in the chat response to the user, so it's clear which comment is being worked on. When addressing an error message, quote the error the same way. Show this only in the chat response — not in code comments, commit messages, or the reply/PR text posted to GitHub.

When making a code fix while accept-edits mode is on (edits apply without a per-edit approval prompt), show the resulting diff in the chat response so the user can review what changed.

## Committing

Applies to every project:

- Always run `git status` before committing.

## Python standard library

When writing Python code, prefer standard library modules over custom implementations. For example, use `urllib` for HTTP requests, `json` for JSON parsing, `tarfile` for archives, `subprocess` for running commands. Reach for third-party packages only when the standard library genuinely cannot express the required behavior.

In Python code, do not shell out to bash (via `subprocess`, `os.system`, `Shell.check`, etc.) for operations the standard library already provides — use the Python API instead. For example: `os.remove` / `pathlib.Path.unlink` instead of `rm`, `shutil.rmtree` instead of `rm -rf`, `os.makedirs` instead of `mkdir -p`, `os.chmod` instead of `chmod`, `shutil.copy` instead of `cp`, `pathlib.Path.glob` instead of `ls`/`find`. Reserve shelling out for invoking genuinely external programs (e.g. `git`, `docker`, `gh`, `reprepro`). This is safer (no shell quoting/injection), clearer, and easier to test.

## CI monitoring

When a monitored CI run completes **with an error**, do an **initial evaluation** before handing back: fetch the failed logs (`gh run view <id> --log-failed` / `--log`), identify the failing step, and state a concrete root-cause hypothesis. Do not just report "it failed" — surface the actual error and your first read on it.
