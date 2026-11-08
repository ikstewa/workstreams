---
name: lead
description: Sets where a lead session and its Orca workers run when one worker per task works in other repos. Use with Orca's orchestration skill when leading a project from a home-repo workspace in Orca.
---

# Leading from the home repo

Orca's `orchestration` and `orca-cli` skills teach the commands. These rules decide where the lead and each worker run.

- **Stay in the workspace you started in:** the home repo's main checkout, never an Orca worktree of it. A worktree holds only the home repo's tracked files, not the checkouts nested in it.
- **Before starting a worker in repo `owner/name`**, with `WORK_DIR` defaulting to `~/work`:
  1. If `$WORK_DIR/<name>` is missing, run `gh repo clone owner/name "$WORK_DIR/<name>"`.
  2. If `orca repo list --json` has no project at that path, run `orca repo add --path <absolute path> --json`.
- **Start each worker** with `orca orchestration worker-start --task <id> --worktree new-child --repo path:<absolute path> --agent claude`, one worker per task. Name its branch by that repo's own convention.
- **Workers do not start workers.** They use their own subagents for parallel help. Orca's nested worker depth is 1, so a nested start fails.
- **A follow-up goes to a fresh worker in the same worktree**, such as for a review comment or a failing check. Finished workers auto-close, so the old session is gone.
- **Never type into a busy agent.** The text lands in the middle of its work.
