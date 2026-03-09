#!/usr/bin/env bash
# setup.sh — one-time setup for review-agent
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ $# -lt 1 ]]; then
    echo "Usage: bash setup.sh <repos-root-dir>"
    echo "  e.g. bash setup.sh /Volumes/RAMDisk"
    exit 1
fi
REPOS_ROOT="$1"

DEPLOY_DIR="/Users/${USER}/Deploy"
VENV="$DEPLOY_DIR/.venv"

echo "=== review-agent setup ==="

# 0. Deploy files, install deps, and install plist
echo "[0/3] Deploying files to ${DEPLOY_DIR} …"
bash "$REPO_DIR/deploy.sh" "$REPOS_ROOT"

# 1. Store secrets in Keychain
echo ""
echo "[1/3] Storing secrets in macOS Keychain"
echo "       (existing entries will be updated)"

store_secret() {
    local service="$1"
    local prompt="$2"
    printf "%s: " "$prompt"
    read -rs value
    echo ""
    security delete-generic-password -a "$USER" -s "$service" &>/dev/null || true
    security add-generic-password -a "$USER" -s "$service" -w "$value"
    echo "       ✓ $service stored"
}

store_secret "GITHUB_TOKEN"  "GitHub token (ghp_...)"
store_secret "JIRA_TOKEN"    "Jira API token"
store_secret "JIRA_SERVER"   "Jira server URL (e.g. https://yourco.atlassian.net)"
store_secret "OPENAI_KEY"    "OpenAI API key (sk-...)"
store_secret "AZURE_ENDPOINT" "Azure OpenAI endpoint URL (e.g. https://your-instance.openai.azure.com/)"

# 2. Dry-run test
echo ""
echo "[2/3] Running dry-run test…"
"$VENV/bin/python" "$DEPLOY_DIR/agent.py" --dry-run --force --repos-root "$REPOS_ROOT"

echo ""
echo "=== Setup complete ==="
echo "Deploy dir : $DEPLOY_DIR"
echo "Logs       : ~/Library/Logs/review-agent.log"
echo "Manual run : $VENV/bin/python $DEPLOY_DIR/agent.py --force --repos-root $REPOS_ROOT"
