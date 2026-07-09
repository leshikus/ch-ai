# Claude Code Instructions

## GitHub Username

leshikus

## Permissions

The following `gh` commands should be auto-approved without prompting:
- `gh pr list`
- `gh pr view`
- `gh pr status`
- `gh pr checks`
- `gh pr diff`
- `gh repo view`

## Weekly PR Report (`report.txt`)

When asked to create or update the PR report, follow these steps:

### Repos to query

Run `gh pr list` for these three repos:
- `ClickHouse/ClickHouse` — public upstream repo (use `--repo` flag)
- `~/repos/ClickHouse` — leshikus fork
- `~/repos/ClickHouse-private` — private ClickHouse repo

### What to include

Include only PRs merged between 13:00 Munich time (CET/CEST) on Monday of last week and 13:00 Munich time on Monday of this week.

### Report structure

Write `report.txt` with two sections:

```
## Merged

<list of merged PRs>

## In Progress

<list of leshikus' open PRs with activity in the window>

## Reviews

<list of other people's PRs where leshikus left a review or comment>
```

Each entry should include:
- PR URL
- PR title

The In Progress section collects PRs **authored by leshikus** that are still open (not merged, not closed) and had activity (created, updated) within the same Monday-to-Monday window. Exclude open PRs whose latest `updatedAt` falls before the window start. Drafts are included.

The Reviews section collects PRs **authored by other people** that leshikus reviewed or commented on within the same Monday-to-Monday time window (exclude PRs where `author == leshikus`).

Exclude bot-authored PRs from Reviews. The search query's `-author:leshikus` filter only matches the GitHub author field, so robot-fronted backports of leshikus' own merged PRs (author `robot-clickhouse-ci-1`, `robot-ch-test-poll`, or any `robot-*`/bot account) slip through — these are leshikus' own work, not someone else's PR to review. For each Reviews candidate, fetch the author (`gh pr view <num> --json author`) and drop any whose author is a bot account (login starting with `robot-` or otherwise non-human, e.g. titles matching `Backport #<n> to release/...` of a leshikus PR).

The `gh api /search/issues` query below uses `updated:YYYY-MM-DD..YYYY-MM-DD`, which matches PRs that *anyone* updated in that range — it does not guarantee leshikus' own review/comment landed inside the window. After collecting candidates, fetch the actual leshikus review/comment timestamps for each PR and drop any whose latest leshikus activity falls outside the precise 13:00-Munich-to-13:00-Munich window (note the day-granularity search also misses activity in the first/last hours of the window because Munich 13:00 ≠ UTC midnight).

```bash
# For each candidate PR, list leshikus' actual activity timestamps:
gh api "/repos/<owner>/<repo>/issues/<num>/comments"   --jq '[.[] | select(.user.login=="leshikus") | .created_at]'
gh api "/repos/<owner>/<repo>/pulls/<num>/reviews"     --jq '[.[] | select(.user.login=="leshikus") | .submitted_at]'
gh api "/repos/<owner>/<repo>/pulls/<num>/comments"    --jq '[.[] | select(.user.login=="leshikus") | .created_at]'
```

Keep only PRs where at least one of those timestamps falls inside the window.

### PR description quality

If a PR title is not descriptive enough, update it via `gh pr edit --title "..."`. Do not edit PR bodies.

### Commands

```bash
# List PRs authored by leshikus
gh pr list --repo ClickHouse/ClickHouse --author leshikus --state all --limit 50 --json number,title,url,state,isDraft,body,createdAt,mergedAt,updatedAt

cd ~/repos/ClickHouse && gh pr list --author leshikus --state all --limit 50 --json number,title,url,state,isDraft,body,createdAt,mergedAt,updatedAt

cd ~/repos/ClickHouse-private && gh pr list --author leshikus --state all --limit 50 --json number,title,url,state,isDraft,body,createdAt,mergedAt,updatedAt

# Find PRs reviewed or commented on by leshikus in the time window
# (search across all repos; excludes PRs authored by leshikus)
gh api -X GET "/search/issues" \
  -f q="type:pr commenter:leshikus -author:leshikus updated:YYYY-MM-DD..YYYY-MM-DD" \
  --jq '.items[] | {url: .html_url, title: .title, user: .user.login}'

gh api -X GET "/search/issues" \
  -f q="type:pr reviewed-by:leshikus -author:leshikus updated:YYYY-MM-DD..YYYY-MM-DD" \
  --jq '.items[] | {url: .html_url, title: .title, user: .user.login}'

# Update a PR title
gh pr edit <URL> --title "..."
```

## Daily Report (`daily.txt`)

When asked to create a daily report, write `daily.txt` with the following format:

```
## Daily Report YYYY-MM-DD

## Waiting for Review

<URL>
<title>
<status note>

...

## In Progress

<URL>
<title>
<status note>

...

## Merged

<URL>
<title>
```

Each entry includes:
- PR URL
- PR title
- a status note (except Merged entries); for unmerged PRs, start with one of: `Draft`, `Testing`, or nothing (use `Draft: <summary of what was done, derived from the PR body>` if the PR is a draft, `Testing` if it has a human approval, omit the status label if it is not a draft and has no human approval), followed by any additional context

### Determining review status

For every open non-draft PR, fetch its review state before writing the status note — `gh pr list` does not include this:

```bash
gh pr view <number> --repo ClickHouse/ClickHouse --json reviews \
  --jq '[.reviews[] | select(.state=="APPROVED") | .author.login]'
```

If the result contains any human login (i.e. not a bot), the status is `Testing`; otherwise omit the status label and do not write who approved the PR.

Section order: Waiting for Review (open non-draft PRs with no human approval) first, then In Progress, then Merged last.

### Time range

Include only PRs with activity (created, updated, or merged) after 13:00 Munich time (CET/CEST) of the previous day. On Mondays, use 13:00 Munich time on Friday as the cutoff instead.

This filter is strict: do **not** carry forward still-open PRs that had no activity inside the window just because they remain in flight. A PR whose latest `updatedAt`/`mergedAt`/`createdAt` is before the cutoff is excluded, even if it is still open. Check each PR's timestamps against the cutoff and drop anything outside it — the report reflects only what moved in the window.

## Updating These Instructions

When the user gives new instructions about how to work (e.g., how to format reports, what to include/exclude, how to handle edge cases), add them to this file so they persist to future conversations.

# Communication style

Keep replies terse — minimal words, no preamble, no recaps, no restating the request. Lead with the conclusion; prefer bullets/code over prose. Expand only when asked or when correctness needs it. Still flag real risks, briefly.

When addressing a review comment, quote the comment's text in the chat response to the user, so it's clear which comment is being worked on. When addressing an error message, quote the error the same way. Show this only in the chat response — not in code comments, commit messages, or the reply/PR text posted to GitHub.

When making a code fix while accept-edits mode is on (edits apply without a per-edit approval prompt), show the resulting diff in the chat response so the user can review what changed.

# Repository locations

All my working repositories live under `~/repos/` (e.g. `~/repos/release`, `~/repos/ClickHouse`, `~/repos/ClickHouse-private`). Older paths like `~/ch-*` or `~/ClickHouse` are obsolete — do not look there.

# Non-clickhouse projects

Always run `git status` before committing; always include `--author="Alexei Fedotov <alexei.fedotov@gmail.com>"` in commit commands. Never add Co-authored-by lines to commit messages.

# ClickHouse projects

Always run `git status` before committing; always include `--author="Alexei Fedotov <alexei.fedotov@clickhouse.com>"` in commit commands. Never add Co-authored-by lines to commit messages.

When creating or updating a PR description, always invoke the `clickhouse-pr-description` skill (via the Skill tool) and let it generate and apply the description — never hand-write the title/body or run `gh pr create`/`gh pr edit` for the description directly. This applies to every ClickHouse PR, including minor or CI-only ones.

Follow `.github/PULL_REQUEST_TEMPLATE.md` exactly: the body is a short description and motivation, then the Changelog category (leave one), then the Changelog entry. Do not add sections the template does not contain — in particular there is no "Documentation entry" section, so never add one. For categories whose label says the changelog entry is not required (e.g. `CI Fix or Improvement`, `Documentation`, `Not for changelog`), leave the Changelog entry empty.

When creating a PR, open it as a **Draft** — this prevents accidental merges.

When marking a PR ready for review (transitioning it out of Draft), first actualize its title and body via the `clickhouse-pr-description` skill. Both are often written against an early draft state; by the time the PR is ready the change, motivation, and CI story have usually moved on, so regenerate the subject and description before it goes in front of reviewers.

When a PR only touches files under `.claude/` (settings, tools, skills, instructions, etc.), prefix the title with `claude: ` and use the `Documentation (changelog entry is not required)` changelog category. Example: `claude: add fetch_ci_report.js to allowed commands`.

When a PR is about Darwin fast tests (e.g. adding entries to `ci/defs/darwin.skip`, fixing tests that fail only on Darwin), prefix the title with `darwin fast test: ` and use the `CI Fix or Improvement (changelog entry is not required)` changelog category. Example: `darwin fast test: skip more tests on Darwin ARM`.

When adding new prompt conventions, rules, or guidelines that apply to all ClickHouse projects, add them to `~/.claude/CLAUDE.md` in addition to any project-specific location.

When adding or updating ClickHouse-related permissions in `~/.claude/settings.json` or rules in `~/.claude/CLAUDE.md`, accumulate the changes locally. Do not open an individual PR for each change — they will be combined into a single PR once per week. Once the combined PR is merged, remove the corresponding entries from the local `~/.claude/` files.

## Containerized environment

When running in a containerized version (no GitHub credentials, working tree is a throwaway copy), do not push or otherwise mutate the tree's remote state — just prepare the PR description and hand back the commands to run. You may freely install anything you need in the container (e.g. `python`).

## Shell commands

When running Bash, prefer separate atomic invocations over chaining with `&&`, `;`, or `|`. Issue each command as its own Bash tool call (in parallel when independent) instead of joining them into one string.

**Why:** Permission rules match the literal command string. A chain like `ls *.txt | head` does not match `Bash(ls *)` or `Bash(head *)` and triggers an unnecessary prompt. Atomic calls hit the existing allowlist cleanly.

**How to apply:** Default to splitting. Chain only when later commands genuinely need the earlier command's exit status or stdout (e.g. `cmd && other`) and no equivalent split exists.

## Python standard library

When writing Python code, prefer standard library modules over custom implementations. For example, use `urllib` for HTTP requests, `json` for JSON parsing, `tarfile` for archives, `subprocess` for running commands. Reach for third-party packages only when the standard library genuinely cannot express the required behavior.

In Python code, do not shell out to bash (via `subprocess`, `os.system`, `Shell.check`, etc.) for operations the standard library already provides — use the Python API instead. For example: `os.remove` / `pathlib.Path.unlink` instead of `rm`, `shutil.rmtree` instead of `rm -rf`, `os.makedirs` instead of `mkdir -p`, `os.chmod` instead of `chmod`, `shutil.copy` instead of `cp`, `pathlib.Path.glob` instead of `ls`/`find`. Reserve shelling out for invoking genuinely external programs (e.g. `git`, `docker`, `gh`, `reprepro`). This is safer (no shell quoting/injection), clearer, and easier to test.

## Fork vs upstream

If the current repository is a fork (i.e. `git remote get-url origin` does not contain `ClickHouse/ClickHouse`), always target the upstream repository. Pass `--repo ClickHouse/ClickHouse` to `gh pr create` and set `--head <fork-owner>:<branch>` so the PR is opened against the canonical repo, not the fork.

After creating a fork-based PR, immediately add the `can be tested` label so CI is not blocked by the `can_be_tested` pre-hook:

```
gh pr edit <PR-number> --repo ClickHouse/ClickHouse --add-label "can be tested"
```

## CI monitoring

Whenever you push commits to a branch that triggers CI, or you dispatch a workflow run, **always arm a background CI monitor** for the resulting run (a `run_in_background` poll that re-invokes you on completion), so you follow the run to its conclusion instead of dropping it.

When a monitored CI run completes **with an error**, do an **initial evaluation** before handing back: fetch the failed logs (`gh run view <id> --log-failed` / `--log`, or `.claude/tools/fetch_ci_report.js` for PR CI reports), identify the failing step, and state a concrete root-cause hypothesis. Do not just report "it failed" — surface the actual error and your first read on it.
