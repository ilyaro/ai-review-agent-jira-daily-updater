#!/usr/bin/env bash
# deploy.sh — copy changed files from repo to /Users/$USER/Deploy
# Usage: bash deploy.sh [-h] [repos-root-dir]
# Compares checksums; only copies files that are new or changed.
set -euo pipefail

usage() {
    echo "Usage: bash deploy.sh [repos-root-dir]"
    echo "  repos-root-dir  Directory containing git repos (default: /Volumes/RAMDisk)"
    exit 0
}

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && usage

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_DIR="/Users/${USER}/Deploy"
REPOS_ROOT="${1:-/Volumes/RAMDisk}"
 
PLIST_TEMPLATE="com.user.codereview.plist"
PLIST_NAME="com.${USER}.codereview.plist"
 
# Files managed by this deploy script
FILES=(
   agent.py
   "$PLIST_TEMPLATE"
   requirements.txt
)

mkdir -p "$DEPLOY_DIR"

deployed=0
skipped=0
plist_changed=false

for file in "${FILES[@]}"; do
    src="$REPO_DIR/$file"
    dst="$DEPLOY_DIR/$file"
    
    # Use real plist name in output
    display_name="${file/$PLIST_TEMPLATE/$PLIST_NAME}"
 
    if [[ ! -f "$src" ]]; then
        echo "  MISSING  $display_name (not found in repo, skipping)"
        continue
    fi

    # Copy if destination doesn't exist or checksum differs
    if [[ ! -f "$dst" ]] || ! shasum -a 256 -s --check <(shasum -a 256 "$src" | sed "s|$src|$dst|") 2>/dev/null; then
        cp "$src" "$dst"
        echo "  DEPLOYED $display_name"
        if [[ "$file" == "$PLIST_TEMPLATE" ]]; then
            plist_changed=true
        fi
        (( deployed++ )) || true
    else
        echo "  OK       $display_name (unchanged)"
        (( skipped++ )) || true
    fi
done

echo ""
 echo "Done — $deployed deployed, $skipped unchanged → $DEPLOY_DIR"

# Re-install plist if it changed
if $plist_changed; then
    VENV="$DEPLOY_DIR/.venv"
    PLIST_DST="$HOME/Library/LaunchAgents/com.${USER}.codereview.plist"
    echo "Plist changed — reinstalling launchd schedule…"
    sed \
      -e "s|__VENV__|$VENV|g" \
      -e "s|__AGENT_DIR__|$DEPLOY_DIR|g" \
      -e "s|__HOME__|$HOME|g" \
      -e "s|__REPOS_ROOT__|$REPOS_ROOT|g" \
      -e "s|__USER__|$USER|g" \
      "$DEPLOY_DIR/$PLIST_TEMPLATE" > "$PLIST_DST"
    launchctl unload "$PLIST_DST" &>/dev/null || true
    launchctl load -w "$PLIST_DST"
    echo "  ✓ plist reloaded → $PLIST_DST"
fi

# Create/update virtual environment
VENV="$DEPLOY_DIR/.venv"
if [[ ! -d "$VENV" ]]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$DEPLOY_DIR/requirements.txt"
