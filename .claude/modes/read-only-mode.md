# Read-Only Mode (Docker)

The session-start hook (`session_start.py`) drives on-start orientation: when the current branch is found it asks you to show the related PR's progress (CI/check status, review decision, unresolved review threads, mergeability). That instruction now lives in the hook, so it is not repeated here.

If the current session is in read-only mode, do not create PRs, push commits, edit PR titles/bodies, or post comments/reviews/review-comment replies to GitHub directly.

Instead, create one atomic file per operation in a per-project subfolder `~/.config/claude-toolkit/pending-writes/<project>/` (named `<short-slug>.md`), where `<project>` is the basename of the working directory the operation relates to — e.g. `createrelease` — following the queue format below. The subfolder groups a session's writes so the write-capable agent drains one project per tab. Each file has the exact command(s) to run and any payload text (PR body, comment/reply text) the command consumes. A separate write-capable agent (see `write-mode.md`) reads each pending file, executes its commands, and deletes it on success.

Create new files only — never edit an existing file. You may delete a queued file, but only once the operation it represents is verified complete or definitively obsolete: e.g. its intended remote state already exists (a push whose commit is already the remote tip), or a later authoritative file supersedes it. Never delete a file to "unblock" yourself before its work is done, and never delete a file belonging to another task whose completion you have not verified.

After queuing a pending write, do not block on it: keep working on the rest of the task. Continuously monitor `~/.config/claude-toolkit/pending-writes/` for the files you queued — a write-capable agent removes each file once it completes (or appends a `Status: failed` line on failure). When your queued write disappears, treat the operation as done and continue; if it gains a `Status: failed` line, surface the failure to the user. Never edit your own queued files, and never delete one merely to "unblock" yourself — but do delete a queued file once you have verified its operation is already complete or obsolete (e.g. the push it requests already landed, or it was superseded by a later file), so the queue does not accumulate stale or failing commands.

## Queue format

The `~/.config/claude-toolkit/pending-writes/` directory is a hand-off queue between a **read-only agent** (which cannot perform write operations) and a **write-capable agent** (which executes them — see `write-mode.md`). Each pending write is **one atomic file**.

Create one file per operation at `<project>/<short-slug>.md`, where `<project>` is the basename of the current working directory (e.g. `createrelease`). Do not use a date/time stamp — pick a distinct `<short-slug>` per operation so files never collide:

    ### <project> — <short title>
    Context: <why this is needed; link to PR/issue/review comment if any>

    Commands:
    ```bash
    <exact command(s) to run, ready to copy-paste>
    ```

    Payload (if the command reads text from a file/stdin, put it here verbatim):
    ```
    <PR body, comment text, review reply, etc.>
    ```

Rules:

- Read-only agents: **create new files only** and never edit an existing file; delete a queued file only after verifying its operation is already complete or obsolete (never to unblock yourself, never another task's unverified work).
- Keep commands exact and self-contained (include `--repo`, full URLs, etc.) so the executing agent needs no extra context.
- Put any multi-line text a command consumes (PR body, comment) in the Payload block and have the command read it from there, so quoting is unambiguous.

## Fixing review comments

When fixing a human reviewer's comment, always show the user three things together for confirmation before queuing the write:

1. **The comment itself** — quote the reviewer's text verbatim (with author and the file/line it targets).
2. **The diff** — the exact code change that addresses it, shown as a colored diff (a fenced ```diff block, or `git diff --color`) so additions and removals are easy to read.
3. **The reply** — the text you intend to post back on the thread. When the comment is resolved by a code fix, the reply is just `Fixed in <full commit URL>`; put any useful explanation into that fix commit's message rather than the reply.

Only after the user confirms should you queue the commit/push and the reply/resolve as pending writes. This keeps the human in the loop on both the fix and the wording of the response.

Implement different review comments as different commits, unless they are tightly coupled (one change cannot stand without the other). One commit per comment keeps the fix self-contained, makes the `Fixed in <commit URL>` reply point at exactly the change that addresses the thread, and lets each fix be reviewed and reverted independently.

## Pending reads (your inbox)

`~/.config/claude-toolkit/pending-reads/` is your inbox — work handed back for the read-only agent to act on. You author writes into `~/.config/claude-toolkit/pending-writes/`; two producers reply into `pending-reads/`:

- **Change requests** (`<original-file-name>.md`) — from the write-capable agent, when it found a queued write wrong or incomplete (see `write-mode.md`). The file holds the full original plus a `Changes requested` section, and the original has already been removed from `pending-writes/`.
- **CI results** (`ci-status-*.md`) — from the host monitor, when a CI run it was watching reaches a terminal state (see "CI monitoring" below).

Continuously monitor `~/.config/claude-toolkit/pending-reads/` for files that belong to your work. When one appears, it is your job to act on it — no other agent will.

**Resolving a change request:**

1. Read the `Changes requested` section and understand what the write agent flagged.
2. Fix the underlying problem (correct the code, reword the reply, etc.). If it addresses a human review comment, re-confirm with the user following the "Fixing review comments" rule above (comment + colored diff + reply).
3. Queue a corrected write as a **new** file in `pending-writes/`, self-contained as usual.
4. Delete the change-request file from `pending-reads/` — once the corrected write is queued, the change request is resolved and obsolete, so removing it is allowed under the delete-when-obsolete rule.

**Reacting to a CI result:** the `ci-status-*` file states the outcome. On failure it lists the failing checks and asks you to fetch the failed logs, root-cause the failure, and queue any fix as a new pending write; on success, just note completion. Delete the file once you have acted on it.

If a pending read is **unrelated to your project** (mis-filed — it belongs to a different project's session) or **stalled** (obsolete, superseded, or can no longer make progress), do not rework it — **delete it** (allowed under the delete-when-obsolete rule). If it plausibly belongs to another project, re-file its content under that project's folder before deleting, so the work is not lost.

A `pending-reads/` file is a verdict or result, not a write to run — never execute its contents as commands.

## CI monitoring

CI monitoring is automatic and offloaded to the host monitor — do **not** drive it with `/loop` yourself.

- **Arming (automatic).** When a queued push is drained and lands, the `arm_monitor` PostToolUse hook (in the write container) drops a `ci` request into `~/.config/claude-toolkit/pending-monitoring/<project>/`. The host monitor claims it, polls the run with the host's own gh credentials, and on a terminal state writes a `ci-status-*` file into `pending-reads/` for you to react to (above).
- **Arming your own.** You may also request monitoring directly — e.g. to follow a PR's CI you are not pushing to — by writing `pending-monitoring/<project>/<slug>.json`:

      {"kind": "ci", "repo": "owner/name", "sha": "<head-sha>", "branch": "<branch>", "pr": <number-or-null>, "pr_url": "<url-or-null>"}

  Only the host monitor consumes and deletes `pending-monitoring/`; create requests there, but do not delete another's.
- The monitor gives up on a watch after several hours with no terminal result and reports that, so a run that never starts will not hang forever.

## Restart command

When the user issues the **restart** command, exit Claude (end the current session), but first preserve the working context so the next session can resume where this one left off:

1. Write the current context to `~/.config/claude-toolkit/pending-reads/<project>/on_restart.md`, where `<project>` is the basename of the working directory the session relates to (e.g. `createrelease`). Capture what you are in the middle of: the task, what has been done, what remains, any writes queued in `pending-writes/`, and any decisions still open. (This is resume context for the next session, not a change request — the next read-only session reads it to pick up where this one left off, then deletes it.)
2. As a fallback — in case the file cannot be written or is missed — also print the same context to the screen before exiting.

Then exit.
