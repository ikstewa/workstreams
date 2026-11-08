# orca-lead

`orca-lead` is a Claude Code plugin with setup notes for running coding agents through [Orca](https://github.com/stablyai/orca). A lead Claude session runs in your home repo, a multi-repo checkout opened in Orca as a workspace. It starts one Orca worker per task, each in a worktree of another repo cloned under `~/work`. Orca's `orchestration` and `orca-cli` skills teach the commands; the plugin's `lead` skill adds the rules for where the lead and its workers run.

1. Run `setup/orca-setup.sh <home-repo-path>`. Set `WORK_DIR` to clone somewhere other than `~/work`.
2. Set the UI-only settings in [SETUP.md](SETUP.md).
3. Load this plugin in the lead session: `claude --plugin-dir <this checkout>`.
