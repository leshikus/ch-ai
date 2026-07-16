# claude-toolkit

**Claude Code in Docker, in auto mode — writes flow, but a different-model reviewer gates every push and every write is logged for you to review.**

The agent works on your real repos with your real GitHub token and runs
autonomously (Claude Code's auto-mode classifier blocks the dangerous stuff —
force pushes, exfiltration, prod deploys). Two things keep you in control without
slowing it down: before any `git push` completes, a separate reviewer agent (a
different model) inspects the commits and blocks concrete defects so junk never
reaches the PR; and every remote write (push, PR, comment) is recorded to a global
log you walk afterward in a single review window.

## Use it

```bash
./claude.py            # working session (auto mode)
./claude.py --review   # one window over the writes log — review everything after the fact
```

Run the working session and let it work. A push goes out the moment it passes the
pre-push review, so CI starts immediately — no approval queue in the hot path. When
you want oversight, open `--review`: it walks every logged write one at a time
(diff, PR, CI), reports, and lets you act. After a push lands, a host monitor
follows its CI to conclusion and drops the result back for the session to react to,
and it watches your open PRs for changes — no `/loop` babysitting.

## How the safety works

- **Auto mode** (`--permission-mode auto`): no routine prompts, but a classifier
  blocks irreversible / destructive / external actions before they run.
- **Pre-push gate**: a `PreToolUse` hook runs a separate reviewer agent (a different
  model) over the exact commits about to be pushed; a concrete defect blocks the
  push and the findings go back to the working agent to fix. It fails open, so it
  never wedges you.
- **Write capture + review**: a `PostToolUse` hook logs every remote write to
  `~/.config/claude-toolkit/writes-log/`; the `--review` session is your
  after-the-fact audit over all of it.

Commits are GPG-signed with your key (via a private keyring copy); the container
never touches your host keyring.

Details: [`working-mode.md`](.claude/modes/working-mode.md),
[`pre-push-review.md`](.claude/modes/pre-push-review.md),
[`review-mode.md`](.claude/modes/review-mode.md).

## Needs

macOS + Docker Desktop, iTerm2, `gh`/`gpg`, and a Claude account with auto mode
available (Opus/Sonnet 4.6+ on the Anthropic API).
