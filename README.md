# workstreams

A Claude Code plugin for people who run several sessions at once. It groups sessions into workstreams, gives each workstream a charter file, and shows all of them on a live board in a tmux sidebar. The plugin is a Claude Code mod (TypeScript) plus a stdlib-only Python board script. It is for one person working across many long-running efforts, not for teams.

## Key ideas

- **Workstream.** A named effort that runs through many sessions. Its charter is one Markdown file: frontmatter for `workstream` (the key), `purpose`, `scope`, `focus`, `goals`, `status`, `blocked`, `blocked_since`, `note` and `pinned`, then a record of dated entries as the body.
- **Binding.** A session belongs to the workstream whose `workstream:` key equals the session's name. A session named `control` is the control session. A name of the form `KEY work: <task>` is a worker of `KEY`.
- **The mod.** In a bound session it appends the charter and the newest part of the record as a conversation row at start, after `/clear` and after a compaction. It records board state from turn, ask, answer and sub-agent events. It registers the `mcp__workstreams__charter` tool, the session's only way to write its charter (focus, record entries, goals, block). It keeps the charter's goals in step with the session's task list: completing a goal's task ticks the goal.
- **The board.** `board.py render` prints every workstream grouped as Control, Pinned, Active, Blocked, Idle, Unassigned and Done, with a stale line for workstreams that have had no session for a while. `bin/ws` runs it in a tmux sidebar; the `/workstreams:board` skill prints it in a session and ranks what to do next.

## Requirements

- Claude Code with the mods API (`$.tool.register` and the session, turn and tool events). It is developed against 2.1.289 to 2.1.291.
- `tmux`, for the sidebar.
- `uv`, which runs `board.py` with its own Python (`uv run --no-project`). The script uses only the standard library.
- `gh`, only for `/workstreams:board --deep`, which also reads Jira through an MCP server when one is configured.

## Install

```sh
claude plugin marketplace add ikstewa/workstreams
claude plugin install workstreams@ikstewa
```

The `/plugin` command inside Claude Code does the same.

## Setup and use

### Charters

Charters live in `~/.claude/projects/<project>/workstreams/<KEY>.md`. `<project>` is the project directory with every character other than letters, digits and `-` replaced by `-`, so `/Users/me/dev/myproject` becomes `-Users-me-dev-myproject`. A `.worktrees/<name>` suffix is dropped, so a worktree shares its project's charters. The board finds a charter by its `workstream:` line; the file name is a convention.

A minimal charter:

```markdown
---
workstream: PAYMENTS_API
purpose: Move invoice creation to the new payments API.
scope:
  in: [invoice creation, refunds]
  out: [subscription billing]
focus: Client library merged; next is the refund endpoint.
status: active
goals:
  - "[x] Client library for the payments API"
  - "[ ] Refund endpoint"
  - "[ ] Retire the old invoice path"
---
- **10-02** Client library merged. Refund endpoint design agreed.
```

Set `status: done` or `status: archived` to take a workstream off the board's active groups; an archived workstream leaves the board. Do not edit a bound session's charter by hand while the session runs; the session writes it through its charter tool. Only you write `note:` and `pinned:`. You set the note from the sidebar. You set the pin by clicking the ☆/★ on the key row of the sidebar's panel, or with `board.py pin <KEY>`, and a pinned workstream sits in the board's Pinned group.

### Start a bound session

Name the session after the charter key. From the project directory:

```sh
claude -n PAYMENTS_API
```

`/rename` in a running session binds it the same way. If the session does not show its charter, its name does not match any charter's `workstream:` key.

### Open the board

`bin/ws` creates a tmux session named `ws`: the board's tree sidebar (40 columns) on the left and `claude agents --permission-mode auto` on the right, then attaches to it. Run it from the project directory, since the sidebar's directory selects the project:

```sh
/path/to/workstreams/bin/ws
```

Put it on your `PATH` or alias it. It re-applies its tmux bindings on every run, so a reattach picks up changes.

Inside the `ws` session:

| Input | Action |
| --- | --- |
| Click a sidebar row | Open the row's session in the right pane, or reset a stale one from its `↻` |
| Option+j / Option+k | Open the next or previous session without starting one |
| Option+n | Open the session that most needs you |
| Option+m | Menu of every row |
| Option+e | Edit the note on the open workstream |

Outside `ws` these keys pass through to the pane unchanged.

In a session, `/workstreams:board` prints the same board, ranks the decisions it raises and asks about the first. `/workstreams:board --deep` adds open pull requests from `gh`; `/workstreams:board <WORKSTREAM>` shows one workstream.

## Development

```sh
uv run --no-project -m unittest discover -s tests
claude plugin test .
claude plugin validate .
```

`tsconfig.json` extends `./.claude-plugin/types/tsconfig.json`, which is not committed. `claude plugin test` does not create it, so editor type checking of `hooks/mod.ts` needs that directory from elsewhere. `claude plugin validate .` passes with warnings about hooks in `mod.ts` that have no `.catch`.

The design is in [`docs/spec.html`](docs/spec.html).

## Status

A personal tool built on Claude Code's early-access mods API, which can change between releases.
