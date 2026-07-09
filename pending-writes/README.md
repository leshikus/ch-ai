# Pending Writes Queue

This directory is a hand-off queue between a **read-only agent** (which cannot
perform write operations) and a **write-capable agent** (which executes them).

Each pending write is **one atomic file**. A read-only agent that needs to run a
command requiring write permissions — e.g. creating a PR, pushing commits,
editing a PR title/body, or posting a reply to a review comment — must **not**
attempt to run it. Instead it creates a new file in this directory. A separate
write-capable agent later reads each pending file, runs its commands, and
**deletes the file on error-less completion**.

## Format

Create one file per operation, named `<YYYY-MM-DD-HHMM>-<short-slug>.md`:

```
### <YYYY-MM-DD HH:MM> — <short title>
Context: <why this is needed; link to PR/issue/review comment if any>

Commands:
​```bash
<exact command(s) to run, ready to copy-paste>
​```

Payload (if the command reads text from a file/stdin, put it here verbatim):
​```
<PR body, comment text, review reply, etc.>
​```
```

## Rules

- Read-only agents: **create new files only**. Never edit or delete existing files.
- Write-capable agents: before executing, give Alexei a short summary of the
  pending operations. Run each file's commands, then:
  - **On success:** delete the file (error-less completion = removed from queue).
  - **On failure:** keep the file, append a `Status: failed <YYYY-MM-DD HH:MM>`
    line and record the error, and do not delete it.
- Keep commands exact and self-contained (include `--repo`, full URLs, etc.) so
  the executing agent needs no extra context.
- Put any multi-line text a command consumes (PR body, comment) in the Payload
  block and have the command read it from there, so quoting is unambiguous.
- `README.md` is documentation, not a task — never execute or delete it.
