# workstreams

A Claude Code plugin for people who run several sessions at once. It groups sessions into workstreams, gives each workstream a charter file, and shows all of them on a live board in a tmux sidebar. The plugin is a Claude Code mod (TypeScript) plus a stdlib-only Python board script. It is for one person working across many long-running efforts, not for teams.

## Key ideas

- **Workstream.** A named effort that runs through many sessions. Its charter is one Markdown file: frontmatter for `workstream` (the key), `purpose`, `scope`, `focus`, `goals`, `status`, `blocked`, `blocked_since`, `note`, `pinned` and `snoozed_until`, then a record of dated entries as the body.
- **Binding.** A session belongs to the workstream whose `workstream:` key equals the session's name. A session named `control` is the control session. A name of the form `KEY work: <task>` is a worker of `KEY`: it reads a short brief instead of the charter, and the sidebar draws it under its workstream's row.
- **The mod.** In a bound session it appends the charter and the newest part of the record as a conversation row at start, after `/clear` and after a compaction. Rows over the record budget roll to `<KEY>/record-archive.md` beside the charter, and done goals are left out of what a session reads. It records board state from turn, ask, answer and sub-agent events. It registers the `mcp__workstreams__charter` tool, the session's only way to write its charter (focus, record entries, goals, block), and the `mcp__workstreams__worker` tool, which starts a worker (`claude --bg`, named `KEY work: <task>`, from the project root) for work in a repo or worktree of its own, and closes one of the session's workers by id once it has reported: `claude stop` then `claude rm`, which deletes its session and worktree, and, when it keeps the worktree, removes it only if the worker's changes are committed and its commits are on the default branch or its branch has a merged PR. It then deletes the worker's local branch. It keeps the charter's goals in step with the session's task list: completing a goal's task ticks the goal.
- **The board.** `board.py render` prints every workstream grouped as Control, Pinned, Active, Blocked, Idle, Unassigned, Done and Snoozed, with a stale line for workstreams that have had no session for a while. `bin/ws` runs it in a tmux sidebar; the `/workstreams:board` skill prints it in a session and ranks what to do next.

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

Set `status: done` or `status: archived` to take a workstream off the board's active groups; an archived workstream leaves the board. Do not edit a bound session's charter by hand while the session runs; the session writes it through its charter tool. Only you write `note:`, `pinned:` and `snoozed_until:`. You set the note from the sidebar. You set the pin by clicking the ☆/★ on the key row of the sidebar's panel, or with `board.py pin <KEY>`, and a pinned workstream sits in the board's Pinned group. You snooze a workstream by clicking the ☾ beside the pin, which offers 1h, 3h or 08:00 tomorrow, or with `board.py snooze <KEY> <length>` (such as `90m`, `3h` or `1d`). A snoozed workstream sits as one dim line in the board's Snoozed group until the time passes, while its sessions keep running; a permission or a question from any of them ends the snooze, and `board.py unsnooze <KEY>` or a click on the ☾ ends it by hand.

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
| Option+n | Open the next workstream session that needs you, never a worker |
| Option+m | Menu of every row |
| Option+e | Edit the note on the open workstream |
| Click a session id in a reply | Open that session in the right pane, as a click on its row does. Only an id with a sidebar row is a link: an 8-character id or a full one, bare or alone in inline code |

Outside `ws` these keys pass through to the pane unchanged.

In a session, `/workstreams:board` prints the same board, ranks the decisions it raises and asks about the first. `/workstreams:board --deep` adds open pull requests from `gh`; `/workstreams:board <WORKSTREAM>` shows one workstream.

### Jira and GitHub links

The mod can also draw Jira issue keys and GitHub refs written as `<repo> #<n>` in Claude's replies as links to their issues or pull requests. It changes only what the terminal draws: the stored message and the model's context keep the bare text. A key or ref inside code, an existing link or a URL is left as written. Only the listed projects' keys and the listed repositories' refs link, so `UTF-8`, `SHA-256`, `PR #198` and a bare `#197` stay as written.

It links nothing until you set your Jira site and project keys, or your repositories as `owner/repo`, in `/config` or in `~/.claude/settings.json`:

```json
"pluginConfigs": {
  "workstreams@ikstewa": {
    "options": {
      "jiraUrl": "https://example.atlassian.net",
      "jiraProjectKeys": ["PROJ", "OPS"],
      "githubRepos": ["octo-org/widgets"]
    }
  }
}
```

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
