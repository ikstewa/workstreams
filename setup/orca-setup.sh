#!/bin/sh
# The Orca setup a CLI can do. SETUP.md lists the settings only the UI can change.
set -eu

[ $# -eq 1 ] || { echo "usage: $0 <basecamp-path>" >&2; exit 2; }
basecamp=$(cd "$1" && pwd)
# The basecamp mod's workPath defaults to basecamp's parent folder.
work=$(dirname "$basecamp")

orca skills install --skill orca-cli --skill orchestration
mkdir -p "$work/workspaces"
orca repo list --json | grep -qF "\"path\": \"$basecamp\"" || orca repo add --path "$basecamp" --json
gh config set -h github.com git_protocol ssh

cat <<EOF

Not done by this script:
- Install and set up the basecamp mod first (basecamp's README).
- Run claude once in $work and accept the trust prompt. Worktrees under it then start without the prompt, in your default permission mode.
- Install the lead plugin. Skip the first line if the ikstewa marketplace is already added:
    claude plugin marketplace add ikstewa/workstreams
    claude plugin install orca-lead@ikstewa
- If gh has no keyring login: gh auth login -h github.com -s project,read:org,workflow
- On a headless server: set ORCA_TELEMETRY_DISABLED=1 in the Orca service's environment.
EOF
