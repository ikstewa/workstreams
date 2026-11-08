# Orca settings

Orca has no settings file or settings CLI, so these are set by hand in its UI, page by page in sidebar order. Each value applies to both a laptop and a server unless marked *laptop* or *server*.

**Onboarding:** default agent Claude; agents open in Terminal UI; Yolo mode off.

**Agents:** Default Agent Claude. Permissions "Each agent's own settings", so the `defaultMode` in Claude's `~/.claude/settings.json` governs. Installed: Claude only, with Claude Agent Teams off. Agent status hooks on. Keep computer awake "Agent" (*laptop*).

**AI Provider Accounts:** none.

**Orchestration:** nested worker depth 1; auto-close finished workers on.

**Computer Use:** reset access; leave the `orca-computer-use` skill uninstalled.

**Voice:** off.

**Orca Account:** not signed in.

**General:** workspace directory `~/work/workspaces`; nest workspaces on; ask before deleting on; Open In apps none.

**Integrations:** GitHub through `gh`, a keyring login with the `project` scope. Jira is optional: a scoped Atlassian token with `read:jira-work`, `write:jira-work` and `read:jira-user`, plus the optional board scopes `read:board-scope:jira-software`, `read:board-scope.admin:jira-software` and `read:project:jira`, with the email left empty. Everything else unconnected.

**Mobile:** LAN, never Relay; a real machine name. On a server the phone joins over Tailscale (*server*).

**Automations:** none.

**Artifacts and Share Skills:** off.

**Agent Session Search:** on for a personal laptop (*laptop*). On a shared server, decide deliberately: the index keeps a copy of transcripts (*server*).

**Git & Source Control:** branch prefix None; Jira key Uppercase; keep local main up to date on; compare base repository default; auto-rename branch off; Show Source Control AI actions off; draft by default on; use review template on; generate details off.

**Task Sources:** GitHub and Jira shown; GitLab and Linear hidden.

**Terminal:** system zsh; TUI clipboard writes on; scrollback 25k.

**Quick Commands:** `Neovim` = `nvim .`, Global.

**Browser:** Agent Browser Use off, because a signed-in browser carries admin logins; offer to save passwords off; no cookie or password imports.

**Mobile Emulator:** off.

**Chat UI:** off.

**Notifications:** agent finished on; PR check failures on.

**Stats & Usage:** prompt cache timer on, 1 hour.

**Privacy & Telemetry:** usage data off.

**Experimental:** Agent Dashboard on; Terminal attention on; Agent sleep off; Cloud VM off.

**Plugins:** off.
