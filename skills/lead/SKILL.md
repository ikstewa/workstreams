---
name: lead
description: Sets where a lead session and its Orca workers run when one worker per task works in other repos. Use with Orca's orchestration skill when leading a project from its basecamp workspace in Orca.
---

# Leading a project from basecamp

Orca's `orchestration` and `orca-cli` skills teach the commands. These rules decide where the lead and each worker run.

- **Run in the basecamp workspace created for your project.** Research reads the clones by the absolute path the basecamp mod's context gives; nothing is cloned into the workspace.
- **Before starting a worker in repo `owner/name`**, with the work folder the mod's context names:
  1. If `<work folder>/<name>` is missing, run `gh repo clone owner/name "<work folder>/<name>"`.
  2. If `orca repo list --json` has no project at that path, run `orca repo add --path <absolute path> --json`.
- **Start each worker** with `orca orchestration worker-start --task <id> --worktree new-child --repo path:<absolute path> --agent claude`, one worker per task. This replaces the mod's "give the person the command" for a separate session. Name its branch by that repo's own convention.
- **If a start fails at `agent_readiness` on Claude's folder-trust prompt, stop and ask the user.** Never answer a trust or permission prompt for a worker: that choice is the user's.
- **The brief says what to do when the fix is unnecessary:** if the bug is already gone, the worker reports that with evidence and opens no PR.
- **Workers do not start workers.** They use their own subagents for parallel help. Orca's nested worker depth is 1, so a nested start fails.
- **A follow-up goes to a fresh worker in the same worktree**, such as for a review comment or a failing check. Finished workers auto-close, so the old session is gone.
- **Never type into a busy agent.** The text lands in the middle of its work.
