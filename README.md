# claude-toolkit

**Claude Code in Docker with a read-only GitHub token — every push, PR, and comment waits in a human-approved queue.**

Other "sandbox your agent" tools protect your *files*. This one protects your *GitHub*: the agent works on your real repos and edits, builds, and commits freely — but it can't push, open PRs, or post comments. Those writes are captured to a queue and held until you approve them.

## Use it

```bash
docker/claude_docker.py            # read-only session — GitHub writes get queued
docker/claude_docker.py --write    # drain the queue — you approve each write
```

You mostly just run the read-only session. When writes pile up, a watcher pops open a `--write` tab (one project at a time) where you approve them; close it when done.

## How the safety works

The container's GitHub token is **read-only**, so real writes simply fail — the queue is the only way anything reaches GitHub, and it runs under your approval. The App private key never enters a container. Commits are GPG-signed with your key (via a private keyring copy).

Details: [`read-only-mode.md`](read-only-mode.md) (authoring the queue) and [`write-mode.md`](write-mode.md) (draining it).

## Needs

macOS + Docker Desktop, iTerm2, `gh`/`gpg`, and a GitHub App with a read-only install (key at `~/.config/claude-toolkit/ro-token.pem`).
