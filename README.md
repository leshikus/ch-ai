# claude-toolkit

**Claude Code in Docker with a read-only GitHub token — every push, PR, and comment waits in a human-approved queue.**

Other "sandbox your agent" tools protect your *files*. This one protects your *GitHub*: the agent works on your real repos and edits, builds, and commits freely — but it can't push, open PRs, or post comments. Those writes are captured to a queue and held until you approve them.

## Use it

```bash
./claude.py            # read-only session — GitHub writes get queued
./claude.py --write    # drain the queue — you approve each write
```

You mostly just run the read-only session. When writes pile up, a watcher pops open a `--write` tab per project (projects drain concurrently, a tab each) where you approve them; close each when done. After an approved push lands, that same watcher follows its CI to conclusion and drops the result back into the read-only session's queue to react to — no `/loop` babysitting.

## How the safety works

The container's GitHub token is **read-only**, so real writes simply fail — the queue is the only way anything reaches GitHub, and it runs under your approval. The App private key never enters a container. Commits are GPG-signed with your key (via a private keyring copy).

Details: [`read-only-mode.md`](.claude/modes/read-only-mode.md) (authoring the queue) and [`write-mode.md`](.claude/modes/write-mode.md) (draining it).

## Needs

macOS + Docker Desktop, iTerm2, `gh`/`gpg`, and a GitHub App with a read-only install (key at `~/.config/claude-toolkit/ro-token.pem`).
