#!/bin/sh
# The Orca setup a CLI can do. SETUP.md lists the settings only the UI can change.
set -eu

[ $# -eq 1 ] || { echo "usage: $0 <home-repo-path>" >&2; exit 2; }
repo=$(cd "$1" && pwd)
work=${WORK_DIR:-$HOME/work}

orca skills install --skill orca-cli --skill orchestration
mkdir -p "$work/workspaces"
orca repo list --json | grep -qF "\"path\": \"$repo\"" || orca repo add --path "$repo" --json
gh config set -h github.com git_protocol ssh

cat <<'EOF'

Not done by this script:
- If gh has no keyring login: gh auth login -h github.com -s project,read:org,workflow
- On a headless server: set ORCA_TELEMETRY_DISABLED=1 in the Orca service's environment.
EOF
