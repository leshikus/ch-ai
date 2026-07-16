# Working Mode (Docker, auto mode)

This session runs in an isolated Docker container with `--permission-mode auto`
and the host's real GitHub token. You work autonomously: routine actions run
without prompts, and Claude Code's auto-mode classifier gates the dangerous ones
(force push, exfiltration, production deploys, routing around a review). Explicit
deny/ask rules still apply. This is not read-only — there is no write queue and no
separate write agent. Do the whole task here: read, edit, run code, commit, push,
open/update PRs, and reply on review threads, directly.

## Commit discipline

Make local commits as you go, one logical change per commit with a clear message.
Never amend or force-push a commit that has already been pushed. Get each commit
right *before* you push it — a broken change that reaches the PR turns into a long
back-and-forth with a reviewer bot, which is exactly what we are avoiding.

## The pre-push review gate

Before a `git push` completes, a `PreToolUse` hook runs a **separate reviewer agent
(a different model)** over the exact commits you are about to push. It looks only
for concrete defects — bugs, regressions, or a change that does not do what its
commit message claims — not style or missing tests.

- **Clean commits** pass straight through and the push proceeds; CI starts
  immediately.
- **A concrete defect** blocks the push. The reviewer's findings come back to you
  as the denial reason. Fix them in a **NEW** commit (never amend an already-pushed
  commit), then push again. If you believe a finding is wrong, resolve it with the
  user before retrying — do not try to route around the gate.

The gate fails open (a reviewer error/timeout allows the push), so it never wedges
you; but treat a real BLOCK as a stop. Do not disable or bypass it — it exists to
keep broken changes out of the PR.

## Writes are captured for later review

Every remote write you make (push, PR create/edit, comment, review) is recorded to
a global writes log by a hook — you do not manage it. A separate `--review` session
walks that log after the fact. The log is oversight, not a queue you drain: just do
your work correctly.

## Your inbox: pending-reads

The host monitor drops results for you into
`~/.config/claude-toolkit/project/pending-reads/`:

- **CI results** (`ci-status-*.md`) — a push's CI reached a terminal state. On
  failure, fetch the failed logs (`gh run view --log-failed` /
  `.claude/tools/fetch_ci_report.js`), root-cause it, and fix; on success, note
  completion. Delete the file once you have acted on it.
- **PR updates** (`pr-*.md`) — a PR needing attention changed (CI reached a terminal
  state, a new comment/review from someone else, or you were added as a reviewer).
  Inspect the PR, decide what is needed, act, then delete the file.

Continuously check `pending-reads/` for files that belong to your work; act on them
and delete each once handled. A `pending-reads/` file is a result to act on, never a
command to run.

## CI monitoring is automatic

When a push lands, the `arm_monitor` hook arms the host monitor to follow the CI
run; the monitor writes the terminal result into `pending-reads/` (above). Do not
drive CI polling yourself with `/loop`.

## Session start

The session-start hook reports the current branch's PR (CI/checks, review decision,
unresolved review threads, mergeability) and sets your goal to completing that PR.

## Writing code

Prefer the standard library over custom implementations: `urllib` for HTTP, plus
`json`, `tarfile`, `subprocess` for running external programs. Reach for
third-party packages only when the stdlib genuinely can't express the behavior.

Do not shell out to bash for operations the stdlib already provides — use the Python
API: `pathlib.Path.unlink`/`os.remove` not `rm`, `shutil.rmtree` not `rm -rf`,
`os.makedirs` not `mkdir -p`, `os.chmod` not `chmod`, `shutil.copy` not `cp`,
`pathlib.Path.glob` not `ls`/`find`. Reserve shelling out for genuinely external
programs (`git`, `docker`, `gh`). Safer (no shell quoting/injection), clearer,
easier to test.
