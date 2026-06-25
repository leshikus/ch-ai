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
