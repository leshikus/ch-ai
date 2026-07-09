# Read-Only Mode (Docker)

If the current session is in read-only mode, do not create PRs, push commits, edit PR titles/bodies, or post comments/reviews/review-comment replies to GitHub directly.

Instead, create one atomic file per operation in a per-project subfolder `~/.config/claude-toolkit/pending-writes/<project>/` (named `<short-slug>.md`), where `<project>` is the basename of the working directory the operation relates to — e.g. `createrelease` — following the queue format below. The subfolder groups a session's writes so the write-capable agent drains one project per tab. Each file has the exact command(s) to run and any payload text (PR body, comment/reply text) the command consumes. A separate write-capable agent (see `write-mode.md`) reads each pending file, executes its commands, and deletes it on success.

Create new files only — never edit an existing file. You may delete a queued file, but only once the operation it represents is verified complete or definitively obsolete: e.g. its intended remote state already exists (a push whose commit is already the remote tip), or a later authoritative file supersedes it. Never delete a file to "unblock" yourself before its work is done, and never delete a file belonging to another task whose completion you have not verified.

After queuing a pending write, do not block on it: keep working on the rest of the task. Continuously monitor `~/.config/claude-toolkit/pending-writes/` for the files you queued — a write-capable agent removes each file once it completes (or appends a `Status: failed` line on failure). When your queued write disappears, treat the operation as done and continue; if it gains a `Status: failed` line, surface the failure to Alexei. Never edit your own queued files, and never delete one merely to "unblock" yourself — but do delete a queued file once you have verified its operation is already complete or obsolete (e.g. the push it requests already landed, or it was superseded by a later file), so the queue does not accumulate stale or failing commands.

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

When fixing a human reviewer's comment, always show Alexei three things together for confirmation before queuing the write:

1. **The comment itself** — quote the reviewer's text verbatim (with author and the file/line it targets).
2. **The diff** — the exact code change that addresses it, shown as a colored diff (a fenced ```diff block, or `git diff --color`) so additions and removals are easy to read.
3. **The reply** — the text you intend to post back on the thread.

Only after Alexei confirms should you queue the commit/push and the reply/resolve as pending writes. This keeps the human in the loop on both the fix and the wording of the response.

## Resolving change requests

The queue is a two-directory handshake: you author into `~/.config/claude-toolkit/pending-writes/`, and the write-capable agent replies into `~/.config/claude-toolkit/change-requests/`.

The write-capable agent reviews the contents of each pending write before executing it (see `write-mode.md`). If it finds a write wrong or incomplete, it does not execute — instead it drops a **change request** into `change-requests/`: a file named after your original request (`<original-file-name>.md`) that contains the full original plus a `Changes requested` section, and it removes the original from `pending-writes/`.

Continuously monitor `~/.config/claude-toolkit/change-requests/` for files that belong to your work (matching the original file name you queued). When one appears, it is your job to resolve it — the write agent will not:

1. Read the `Changes requested` section and understand what the write agent flagged.
2. Fix the underlying problem (correct the code, reword the reply, etc.). If it addresses a human review comment, re-confirm with Alexei following the "Fixing review comments" rule above (comment + colored diff + reply).
3. Queue a corrected write as a **new** file in `pending-writes/`, self-contained as usual.
4. Delete the change-request file from `change-requests/` — once the corrected write is queued, the change request is resolved and obsolete, so removing it is allowed under the delete-when-obsolete rule.

If a change request is itself **unrelated to your project** (mis-filed — it belongs to a different project's session) or **stalled** (its underlying operation is obsolete, superseded, or can no longer make progress and is not worth reworking), do not rework it — **delete it**. It is not your task to resolve, and leaving it lets the queue accumulate stale verdicts; this is allowed under the delete-when-obsolete rule. If it plausibly belongs to another project, re-file its content under that project's folder before deleting, so the work is not lost.

A `change-requests/` file is a review verdict describing required changes, not a write to run — never execute its contents as commands.

## CI monitoring

When a read-only session needs to follow a CI run to its conclusion (e.g. after queuing a push or a `can be tested` label that triggers CI):

- **Drive it with `/loop`, not a one-shot background poll.** A `/loop` (self-paced, or with an interval) survives across turns and re-enters on each tick, so the run is followed even after context is summarized or the session is relaunched — a plain `run_in_background` poll dies on relaunch and drops the run.
- **Post a timestamped progress update to the console on every tick.** Include the UTC time and the current job states, e.g. `[04:50Z] ClickBench amd=in_progress arm=queued`.
- **Update in place instead of scrolling.** Where the terminal allows it, rewrite the same status line with a carriage return (`\r` / `^M`) rather than printing a fresh line each tick, so the console shows one live-updating line. This also surfaces the current status in the terminal tab title, giving at-a-glance progress without reading the log.
- Keep the tick interval sane (poll every few minutes, not seconds) and stop the loop once every watched job reaches a terminal state, then report the outcome.
