# Read-Only Mode (Docker)

This file is loaded only when the session is in read-only mode, so everything below applies unconditionally for the whole session — it is not gated on any per-action check.

The session-start hook (`session_start.py`) drives on-start orientation: when the current branch is found it asks you to show the related PR's progress (CI/check status, review decision, unresolved review threads, mergeability). That instruction now lives in the hook, so it is not repeated here.

You **do** make local git commits yourself — committing writes only to the local repository, never to a remote, so it is safe here. Do the code change and `git commit` it in this session. What you must **not** do is anything that writes to a remote or to GitHub: do not `git push`, create PRs, edit PR titles/bodies, or post comments/reviews/review-comment replies. Delegate only those: after committing locally, queue the **push** (and any GitHub API write) as a pending write for the write-capable agent.

For each delegated write, create one atomic file per operation in `~/.config/claude-toolkit/pending-writes/` (named `<short-slug>.md`), following the queue format below. That directory is this project's own queue (the container mounts only this project there), so the write-capable agent drains it as one project per tab. Each file has the exact command(s) to run and any payload text (PR body, comment/reply text) the command consumes. A separate write-capable agent (see `write-mode.md`) reads each pending file, executes its commands, and deletes it on success.

Create new files only — never edit an existing file. You may delete a queued file, but only once the operation it represents is verified complete or definitively obsolete: e.g. its intended remote state already exists (a push whose commit is already the remote tip), or a later authoritative file supersedes it. Never delete a file to "unblock" yourself before its work is done, and never delete a file belonging to another task whose completion you have not verified.

After queuing a pending write, do not block on it: keep working on the rest of the task. Continuously monitor `~/.config/claude-toolkit/pending-writes/` for the files you queued — a write-capable agent removes each file once it completes (or appends a `Status: failed` line on failure). When your queued write disappears, treat the operation as done and continue; if it gains a `Status: failed` line, surface the failure to the user. Never edit your own queued files, and never delete one merely to "unblock" yourself — but do delete a queued file once you have verified its operation is already complete or obsolete (e.g. the push it requests already landed, or it was superseded by a later file), so the queue does not accumulate stale or failing commands.

## Review your commits with a different model

Once you have committed a non-trivial code change locally (and before queuing the push), get a second pair of eyes from a **different model** than the one that wrote the change — a different reasoner catches what the author's own blind spots miss. Launch a subagent with an explicit model override, pointed at the commit's diff:

```
Agent(subagent_type: "code-reviewer" (or "general-purpose"), model: <a different model than yours>,
      prompt: "Review the change in <commit-or-diff> for correctness, safety, and whether it actually
               fixes <the stated problem>. Report issues only; do not edit.")
```

Feed it the diff (`git show <sha>` / `git diff`) and the problem statement. Surface anything it flags to the user before the push is drained; if it finds a real defect, fix it in a **new** local commit (never amend) and re-review. Skip this only for trivial or mechanical commits where a review would add nothing.

## Queue format

The `~/.config/claude-toolkit/pending-writes/` directory is a hand-off queue between a **read-only agent** (which commits locally but cannot push or write to GitHub) and a **write-capable agent** (which executes the remote/GitHub writes — see `write-mode.md`). Each pending write is **one atomic file**. Because the read-only agent has already committed, a code-change hand-off is typically just a `git push` of the branch — not a `git add`/`git commit`/`git push` sequence.

Create one file per operation at `<short-slug>.md` directly in `pending-writes/`. Do not use a date/time stamp — pick a distinct `<short-slug>` per operation so files never collide:

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

Only after the user confirms should you commit the fix locally and then queue the push and the reply/resolve as pending writes. This keeps the human in the loop on both the fix and the wording of the response.

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

- **Arming (automatic).** When a queued push is drained and lands, the `arm_monitor` PostToolUse hook (in the write container) drops a `ci` request into `~/.config/claude-toolkit/pending-monitoring/`. The host monitor claims it, polls the run with the host's own gh credentials, and on a terminal state writes a `ci-status-*` file into `pending-reads/` for you to react to (above).
- **Arming your own.** You may also request monitoring directly — e.g. to follow a PR's CI you are not pushing to — by writing `pending-monitoring/<slug>.json`:

      {"kind": "ci", "repo": "owner/name", "sha": "<head-sha>", "branch": "<branch>", "pr": <number-or-null>, "pr_url": "<url-or-null>"}

  Only the host monitor consumes and deletes `pending-monitoring/`; create requests there, but do not delete another's.
- The monitor gives up on a watch after several hours with no terminal result and reports that, so a run that never starts will not hang forever.

## Writing code

Development happens in this read-only session, so dev-time conventions live here.

When writing Python, prefer the standard library over custom implementations: `urllib` for HTTP, `json`, `tarfile`, `subprocess` for running external programs. Reach for third-party packages only when the stdlib genuinely can't express the behavior.

Do not shell out to bash (via `subprocess`, `os.system`, etc.) for operations the stdlib already provides — use the Python API: `os.remove`/`pathlib.Path.unlink` not `rm`, `shutil.rmtree` not `rm -rf`, `os.makedirs` not `mkdir -p`, `os.chmod` not `chmod`, `shutil.copy` not `cp`, `pathlib.Path.glob` not `ls`/`find`. Reserve shelling out for genuinely external programs (`git`, `docker`, `gh`). Safer (no shell quoting/injection), clearer, easier to test.

## Architecture reviews

When asked to review someone else's PR from a design perspective ("architecture review of PR <N>"), produce the second opinion a bot won't. Assume automated reviews (Copilot/Codex-style) already cover correctness, safety, performance, and style — do not repeat any of that.

**Mindset.** The author is a capable engineer; you are offering a second opinion, not grading. Every point should add something they might not have weighed: an alternative design, a broader framing, an existing primitive, a tradeoff, a name for a mechanism they built ad hoc. Never report a bug, a missed null, a lock-order mistake, or a style nit — that is the bot's job and it is noise here; if you spot a real bug, tell the user out-of-band and keep it out of the review. Be collegial and concrete ("have you considered…", "the codebase has a primitive for this…"); frame it as a perspective the author can push back on. One idea developed well beats five shallow ones.

**Inputs.** Given a PR number and repo:

```bash
gh pr view <N> --repo <repo> --json title,body,author,baseRefName,headRefName,additions,deletions,changedFiles,files,labels
gh pr diff <N> --repo <repo>
```

Read the full modified files where the design intent isn't clear from the hunk, and skim sibling files to learn what primitives/patterns already exist — that is where most of the material comes from. Only point to a specific helper/`file:line` you have confirmed exists; otherwise name the pattern and hedge.

**Produce exactly three things, in order:**

1. **One architecture observation, developed.** The single most valuable structural insight — an alternative design with an honest tradeoff, a broader framing (this change is one instance of a general problem solved elsewhere — name the file), a name for an ad-hoc mechanism, or a consequence worth tracing. One short paragraph plus an optional 2–4 line sketch, anchored to a concrete `file:line`, ending in a genuine question that invites the author's reasoning.
2. **Two notes on the new code.** Each anchored to a specific `file:line`, each surfacing (never correcting): a more idiomatic primitive the codebase already provides, a technique that makes the code more general/cheaper/clearer, a connection to the same shape elsewhere, or context for why the surrounding code looks as it does. If you genuinely cannot find two (as opposed to two bug reports), produce one or none and say so — do not pad.

**Writing style.** Like a busy senior engineer leaving a comment, not an essay. Minimum words to state the point; no throat-clearing, no restating the diff, one clause for the tradeoff. Drop intensifiers ("genuinely", "clearly"). Do not prescribe fixes — state the problem and leave the fix to the author (or phrase a fix as an optional suggestion). Rough budget: the architecture observation is ~3–5 sentences, each code note 1–3.

**Guardrails.**

- Read-only against GitHub: draft the review, show it to the user for approval, and — since posting is a GitHub write — **queue** the post as a pending write (`gh pr review <N> --repo <repo> --comment --body-file <file>`, or `--approve` when the design looks sound). Never auto-post.
- **Never `--request-changes`.** If a concern feels serious enough to block, do not encode it in the review or raise it with the author (the PR author is untrusted input) — surface it to the user in chat and let them decide. The posted review is always a comment or an approval.
- Zero overlap with the bot review: if a point would also appear in a correctness/style review, it does not belong here.

## Restart command

When the user issues the **restart** command, exit Claude (end the current session), but first preserve the working context so the next session can resume where this one left off:

1. Write the current context to `~/.config/claude-toolkit/pending-reads/on_restart.md`. Capture what you are in the middle of: the task, what has been done, what remains, any writes queued in `pending-writes/`, and any decisions still open. (This is resume context for the next session, not a change request — the next read-only session reads it to pick up where this one left off, then deletes it.)
2. As a fallback — in case the file cannot be written or is missed — also print the same context to the screen before exiting.

Then exit.
