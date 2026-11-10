# orca-lead

`orca-lead` is a Claude Code plugin with setup notes for running coding agents through [Orca](https://github.com/stablyai/orca). Each project gets a lead Claude session in its own Orca workspace of basecamp. The lead starts one Orca worker per task, each in a worktree of another repo cloned under the work folder. Orca's `orchestration` and `orca-cli` skills teach the commands; the plugin's `lead` skill adds the rules for where the lead and its workers run.

It requires the basecamp mod, installed and set up first: the mod gives every session the clones' location and the work folder.

1. Run `setup/orca-setup.sh <basecamp-path>`.
2. Set the UI-only settings in [SETUP.md](SETUP.md).
3. Install this plugin: `claude plugin marketplace add ikstewa/workstreams`, then `claude plugin install orca-lead@ikstewa`.

To start a lead, create a basecamp workspace for the project with its charter as the prompt: `orca worktree create --repo path:<basecamp-path> --name <project> --agent claude --prompt "<charter>"`.
