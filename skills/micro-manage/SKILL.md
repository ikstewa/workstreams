---
name: micro-manage
description: "Watches a project's members for its manager session: starts board.py watch once and acts on each line it prints, nudging an idle session onto a settled step, passing on a merged PR, telling a member to merge master, restarting a manager whose context runs high and holding new workers when the quota runs low. Use only when Ian asks a project's manager to watch or monitor its members, or types /workstreams:micro-manage."
---

# /workstreams:micro-manage

You are a project's manager (`/workstreams:project`), and Ian has asked you to watch. Run every `board.py` command from the project directory. `<P>` below is your session's name, the project's key.

## The watch

When this skill is invoked: if no watch is running, start `uv run --no-project "${CLAUDE_PLUGIN_ROOT}/hooks/board.py" watch <P>` with the Monitor tool. It prints one line per change in the members' managers and workers and in the PRs their work waits on, and a `QUOTA` line, and keeps running until Ian has typed no prompt in any session for 20 minutes; then it prints `PAUSED: …` and exits. Each line it prints starts a turn of yours. Start it only here: after `PAUSED` it stays off until Ian runs `/workstreams:micro-manage` again, and his next prompt does not restart it.

- `armed: …` comes first. A watch started after a pause prints what changed in the gap before it. Your first reply after a pause leads with that gap, then the Needs-Ian list.
- `PAUSED: …`: end the turn without messaging anyone. Ian is away; nothing you send is seen until he is back.

## Acting on a line

Each reply follows the project skill's "Every reply", and you act only on what a charter or Ian already settled.

- **`IDLE <who> <N>m: <last message>`**: when the session's own last message, or its charter's focus `Next:`, names a step that needs nothing from Ian, send it "Go ahead with <step>." once for that idle stretch. A session still idle after the nudge goes on the list as "nudged, still idle". A step that needs Ian goes on the list instead.
- **`PR <ref> → MERGED`**: tell the manager whose charter or record says what follows that merge.
- **`PR <ref> DIRTY after <ref> merged`** or **`BEHIND`**: tell each member manager whose watched PR in that repo went DIRTY or BEHIND to merge master. The push stays its ask to Ian.
- **`CONTEXT <who> at 70%`** or more, for a member's manager that is idle (not working, not asking Ian, no sub-agent running): restart it from its charter as the project skill says. Workers are never restarted: each is fresh for its task.
- **`QUOTA 5h N% …` at 90% or more**: start no new workers, ask the managers to hold new ones, batch your messages harder, and tell Ian before work stalls.
- **A PR Ian names** to watch: `uv run --no-project "${CLAUDE_PLUGIN_ROOT}/hooks/board.py" pr <P> <owner/repo#n or its URL>`. The watch also reads the PRs named in the `blocked:` lines of your charter and the members'. A merge marks siblings only in a repo your charter's `repos:` lists.
