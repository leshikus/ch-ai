# Write Mode — Processing Pending Writes

This describes the **write-capable agent** that drains the `~/.config/claude-toolkit/project/pending-writes/` hand-off queue produced by read-only sessions (see `read-only-mode.md` for how those files are authored and for the queue format). Each pending write is one atomic file in that format: a `Context`, a `Commands` block, and optionally a `Payload` block.

## Executing pending writes

When executing pending writes, first give Alexei a short summary of the pending operations (what each file does) before running any commands.

For each new pending-writes item, in order:

1. **Confirm** — summarize the operation for Alexei and get confirmation before acting.
2. **Safety check** — arm a parallel agent to check whether the action is safe to perform. Wait for that check to pass; if it flags the action as unsafe, do not execute — surface the concern to Alexei instead.
3. **Contents review** — review the *contents the write will produce*, not just that the command is well-formed. The read-only agent that queued it could not run code, post to GitHub, or see CI, so it may have made a wrong call; verify the substance:
   - **If it pushes code** (a commit or push): review the actual diff/code being committed. Is it correct, does it do what the file's `Context` claims, and does it introduce no obvious bug, regression, or unintended change? For a fix addressing a review comment, confirm the change actually resolves the reviewer's point. For **any** push, review all new code the push introduces relative to the remote tip — read the full diff of every new commit, not just a `--stat` summary or file list. This holds regardless of how the task file frames the write: a re-sign, a rebase, or a "clean fast-forward" whose tree is claimed identical to some prior commit is still new code arriving on the remote, so review it as such rather than trusting the `Context`'s characterization.
   - **If it posts a comment or review reply**: review whether the reply genuinely answers the review comment it responds to — does it address the reviewer's actual point, and is it factually accurate (e.g. it claims a change was made that really was made)?
   - For other operations (PR body, label, dispatch), sanity-check that the payload matches the stated intent.

   Proceed only once the contents review passes. If the contents are wrong or incomplete, do not execute — instead **request changes** (see below) so the read-only agent that queued it can fix and re-submit.

Then, each task file ends in one of three outcomes:

- **On error-less completion:** delete the file — successful completion removes it from the queue.
- **On failure** (a command errored while executing): keep the file, append a `Status: failed <YYYY-MM-DD HH:MM>` line with the error, and do not delete it.
- **On changes requested** (the contents review rejected it before executing): do not run the commands; hand it back for revision as described next.

## Requesting changes (`changes_requested`)

The queue is a hand-off between agents:

- `~/.config/claude-toolkit/project/pending-writes/` — read-only agent → write agent (writes to execute).
- `~/.config/claude-toolkit/project/pending-reads/` — the read-only agent's inbox. You author **change requests** here (review verdicts to resolve); the host monitor also drops CI results (`ci-status-*`) here. You do **not** consume this directory.

When the contents review finds the write is wrong or incomplete, do not execute it and do not silently fix it yourself. Replace the write request with a **change request** so the read-only agent that has the task context reworks it:

1. Create a file in `~/.config/claude-toolkit/project/pending-reads/` named after the original request (`<original-file-name>.md` — it keeps the original's timestamped name, so it never collides with the monitor's `ci-status-*` results).
2. That file must contain **the entire original request file verbatim**, followed by a `Changes requested` section that states, specifically, what is wrong and what must change — quote the offending diff/reply text and the reviewer's point it fails to address, so the read-only agent can act without re-deriving context.
3. Delete the original file from `pending-writes/` — the change-request file carries the full original, so nothing is lost.

Beyond a failed contents review, convert a pending write into a change request — a **clarification change request** — in two more cases, even when its commands are well-formed:

- **Unrelated to the project.** The write does not belong to the project whose queue you drain (e.g. it targets a different repository, PR, or checkout than this drain's project). Do not execute it; hand it back as a clarification change request naming the project it appears to belong to, so the read-only agent re-files it under the correct queue.
- **Stalled.** The write cannot make progress — it is gated on a dependency that never arrives, references a path/file/commit that does not exist here, or has been superseded by later work. Do not execute it; hand it back as a clarification change request describing what blocks it and what the read-only agent must supply or decide.

A clarification change request uses the same format and directory as above (the full original verbatim, followed by a `Changes requested` section that here poses the clarifying question), and likewise removes the original from `pending-writes/`.

Do not execute a change request yourself; it is the read-only agent's job to resolve it (see `read-only-mode.md`). Files under `pending-reads/` are never run as commands — they are verdicts and results, not writes.

CI monitoring is armed for you automatically: after a successful `git push`, the `arm_monitor` PostToolUse hook drops a `ci` request into `pending-monitoring/`, and the host monitor polls that run to conclusion and reports it back as a `ci-status-*` file in `pending-reads/`. You do **not** need to launch or `/loop` a CI monitor yourself. If you triggered a run some other way the push hook cannot see, you may drop your own `pending-monitoring/<slug>.json` request (schema in `read-only-mode.md`).

## Continuous monitoring

Continuously monitor the `~/.config/claude-toolkit/project/pending-writes/` directory. When a new task file appears, start processing it right away following the rules above (summarize for Alexei, safety-check, contents-review, then execute-and-delete, mark failed, or request changes). Do not wait to be asked — as soon as a new pending write shows up, pick it up and process it. (You do not consume `pending-reads/` or `pending-monitoring/`; the read-only agent resolves `pending-reads/` and the host monitor services `pending-monitoring/`.)

Also check the queue whenever you finish a piece of work: as soon as any task completes, immediately re-scan `~/.config/claude-toolkit/project/pending-writes/` and report any pending writes to Alexei before moving on.
