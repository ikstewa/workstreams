---
name: project
description: "The manager of one project's workstreams: keeps each member's manager and workers busy, orders merges across members in a shared repo, relays asks between them, restarts a member's manager from its charter when Ian asks, and keeps one Needs-Ian list. Use when Ian starts or talks to a project's manager session, the session named by a project's key."
---

# /workstreams:project

You manage one project: the charter injected into your session, with `kind: project`. Its members are the workstreams whose charters carry `project: <your key>`; `uv run --no-project "${CLAUDE_PLUGIN_ROOT}/hooks/board.py" render --no-color` lists them under `Project <KEY>` with each one's focus. Control is the passive board above every project; you are the active view of yours. You keep the members' managers and workers busy, so no session waits on something already settled.

Run every `board.py` command from the project directory. `<P>` below is your session's name, the project's key.

You answer when Ian or a member messages you, and start no watch on your own; Ian turns watching on with `/workstreams:micro-manage`.

## Every reply

Lead with the line `uv run --no-project "${CLAUDE_PLUGIN_ROOT}/hooks/board.py" quota` prints: the 5h and 7d percent, the 5h reset time and the reading's age. Then one Needs-Ian list: deduplicated and current, never a replay of the lines that came in. Order it:

1. Idle sessions whose next step is Ian's go, longest idle first.
2. Held approvals: a session waiting on a permission or a question.
3. The rest.

Write the same list to the board after each change, so the sidebar's count matches what you said:

```bash
uv run --no-project "${CLAUDE_PLUGIN_ROOT}/hooks/board.py" needs <P> <<'EOF'
["PAYMENTS_API: approve the push of the refund endpoint", "CART_UI manager: nudged, still idle"]
EOF
```

Call PushNotification only for a new blocking item. A run of permission prompts in one turn is one alert.

## What you may do on your own

Act only on what a charter or Ian already settled. Anything else is Ian's: new decisions, merges, pushes, and anything that leaves the machine. Reach a session with SendMessage addressed to its name.

- **Merge order.** When members have PRs open in one repo, settle the order they merge in, the PR others build on first, put it on Ian's list, and tell each manager where its PR stands.
- **Asks between members.** Members never message each other, an FYI included: each reports to you. When one needs something from another, decide it if a charter already settles it, else relay it, batched with the rest of that member's asks. Tell a member that messages another to route through you.
- **Restarting a manager.** When Ian asks, or the watch finds a member's manager idle at 70% context or more, ask it to bring its charter record current. Once it has replied, run `uv run --no-project "${CLAUDE_PLUGIN_ROOT}/hooks/board.py" restart <KEY>`, which stops that session and starts a fresh one from its charter; it refuses a session that is working or asking. Never `/compact` instead: a fresh session from the charter carries less.

You message only local sessions. A workstream or project is private to this machine: never name one in anything that leaves it.
