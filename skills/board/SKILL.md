---
name: board
description: Use when the user types /workstreams:board, asks what is running, what is waiting, what to pick up next, or wants to dispatch, stop or sweep a workstream session. Reads the local board, the session registry and the workstream charters; --deep adds GitHub PRs and Jira.
---

# /workstreams:board

Board dir: `board_dir()` in `hooks/board.py`. Workstream records: `~/.claude/projects/<slug>/workstreams/<KEY>.md`, charter frontmatter plus handoff body. Never read `memory/` for workstream state, except in the sweep's migration below, and never write a workstream fact into `MEMORY.md`.

## Render (default)

Run, from the project directory:
```bash
uv run --no-project "${CLAUDE_PLUGIN_ROOT}/hooks/board.py" render --no-color
```
Present it with the template below. The render decides *what* is on the board: every group, row, `note:` and `last:` line, child and the stale line it prints appears, in its order, and nothing it does not print is added. The template decides only *how* it looks. The tmux sidebar is the same board, narrow, so the two always agree.

Template (groups in the render's order, Control, Active, Blocked, Idle, Unassigned, Done; omit a group the render omits; `…` repeats):
```markdown
## Workstream board · <weekday> <d> <Mon>, <HH:MM> <local tz>

**<Group>**
- **<KEY>** `<session8>` (`–` for a row with no live session) · <state · detail> · <focus, or _unassigned_> <[duplicate] if flagged>
  - note: <Ian's note> (rows he has noted only)
  - likely: <KEY> (unassigned rows the render hints only)
  - last: <last message>
  - ↳ <child type> `<id8>` · <state>
  - …
…

<stale line, if any, in _italics_>
```
Filled:
```markdown
## Workstream board · Sat 26 Sep, 08:31 PDT

**Control**
- **control** `db9b4d1c` · waiting · unread

**Active**
- **ONBOARDING** `91d5bdf5` · waiting · unread · 1 goal left · 0.0h · Signup flow shipped 09-26; next is the welcome emails.
  - note: parked until the design review lands
  - last: The signup flow is built, and its tests pass.
  - ↳ general-purpose `ad5e58a8` · running

**Blocked**
- **PROJ-12** `–` · stack review · 2d · Four stacked PRs open; next is the merge.
  - last: Pushed the last fix; the stack waits on review.

**Unassigned**
- **myproject-e3** `432788f0` · idle · 0.0h · _unassigned_
  - likely: DOCS_SITE

**Done**
- **DOCS_SITE** `–` · all done · 20h · New docs site shipped on master.

_stale (38, no session in 3d): PAYMENTS_API, PROJ-31, PROJ-44, …_
```
A Blocked row waits on something outside its session, such as a review or a merge, and is not Ian's move. A Done row has every goal ticked and no live session, or is a `done` workstream whose live session is neither working nor asking Ian.

The filled example truncates the stale line for space; yours never does. Grouping, liveness, labels, the goal count, the duplicate flag and the stale line are `place()` in `hooks/board.py` (spec §6; a dormant row's goal label, §2), not something to recompute here.

## What now (interpretation)

Below the block, under the heading **What now**, the decisions the board raises, ranked by spec §8 (Guidance). This part is your judgment, not the board: you may read the `last_message` fields of the `board/*.json` records and the charters to form it.

Argue each decision before you recommend it — what to answer, what to pick up, what to stop:
```markdown
**<n>. <the decision, as a question>**
- For: <the strongest case for doing it now>
- Against: <the strongest case for leaving it>
- → <your recommendation, one line>
```
Keep each side to one or two lines, and give each side its best case, not a straw man. A decision with no real case against is a one-line item with no For/Against.

A row that reads `all done` raises one decision: close the workstream. Propose `status: done` on its charter and set it only on Ian's yes; the board never closes a workstream on its own, as the sweep never archives one.

Then ask Ian about the first decision only, and stop.

## `/workstreams:board --deep`
Add: `gh search prs --author @me --state open --json number,title,reviewDecision,statusCheckRollup`; Jira, through an MCP server when one is configured, for tickets in each charter's `refs.tickets`. Re-rank.

## `/workstreams:board <WORKSTREAM>`
Show what spec §8 lists for one workstream, with the actions below.

## Dispatch an active session
From Bash; the session runs on the default model:
```bash
claude --bg -n "<WORKSTREAM>" --permission-mode auto "<one concrete first step from the record's next line>"
```
The plugin's mod appends the operating rules, the charter and the record's newest part as a conversation row at session start. If the dispatched session does not know its charter, the `-n` name does not match a charter's `workstream:` key; fix the name.

A workstream whose latest session is still in `claude agents --json --all` is reopened from its sidebar row, not dispatched again: a click revives a retired session, and replacing it is Ian's choice, made with a cold row's `↻` (spec §4). `claude rm` on that session makes the row's next click start fresh.

## Stop / sweep
`claude stop <job>` and `claude rm <job>` take the short job id: the registry's `jobId`, or else the row's 8-character session id. A full session UUID fails with "no job matching". `claude stop` ends a running session, and for a background session waiting on input it is the only thing that ends it cleanly. `claude rm` clears a stale `blocked` entry from `claude agents`; the plugin prunes only its own board. Stop what should be gone; never wait on reaping. Delete `board/*.json` with `state: ended` and `updated_at` older than 7 days, and any record whose pid has no registry file and whose `updated_at` is older than 1 day.

The sweep offers to archive the stale set and never archives on its own. Show the keys on the render's stale line with each charter's `focus` line and ask; only on a yes set `status: archived` on the records named, and only on those — Ian keeps the ones he intends to come back to. An archived workstream leaves the board altogether; a stale one is still on it, one word wide.

The sweep also migrates project memory: for every `memory/project-*.md` or `memory/project_*.md`, add `workstream:`, `purpose:`, `focus:`, `status:` and `goals:` frontmatter from its content, move it to `workstreams/<KEY>.md` (merge into an existing record if the key already has one), and delete its line from `MEMORY.md`. Take the goals from the file's checklist if it carries one; otherwise draft them from the outcomes it says are still open, with finished ones as `[x]`. Report each move in one line.
