# review-agent
 
Daily AI code-review agent that runs on your Mac.
Every weekday at 19:00 it scans your local git repos, finds branches with
open PRs, and automatically:
 
1. Posts an AI code review to the GitHub PR
2. Updates the **Internal Notes** field on the linked Jira ticket
3. Posts a daily summary comment to the Jira ticket
 
No servers, no Docker — just a Python script scheduled via macOS launchd.

---

## Prerequisites

- macOS (uses Keychain and launchd)
- Python 3.10+
- Git repos cloned under a local directory (passed as argument)
- Branch names containing a Jira key (e.g. `feature/PROJ-1234-add-widget`)

## Secrets

All secrets are stored in macOS Keychain — never in plaintext.

| Keychain service | What it is |
|---|---|
| `GITHUB_TOKEN` | GitHub PAT with `repo` + `pull_requests` scopes |
| `JIRA_TOKEN` | Jira Personal Access Token (on-premises Jira Server) |
| `JIRA_SERVER` | Jira server URL, e.g. `https://jira.yourcompany.com` |
| `OPENAI_KEY` | Azure OpenAI API key |
| `AZURE_ENDPOINT` | Azure OpenAI endpoint URL |

## Installation on macOS

### Quick setup (recommended)

Run the setup script — it creates the venv, prompts for secrets,
installs the launchd schedule, and runs a dry-run test:

```bash
git clone https://github.com/ilyaro/ai-review-agent-jira-daily-updater
cd ai-review-agent-jira-daily-updater
bash setup.sh [repos-root-dir]
```

That's it. The agent will run automatically every weekday at 19:00.

### Manual setup

**1. Deploy files and install dependencies**

```bash
bash deploy.sh [repos-root-dir]
```

This copies `agent.py`, `requirements.txt`, and the plist template to
`/Users/${USER}/Deploy`, creates a virtualenv, and installs dependencies.

**2. Store secrets in Keychain**

```bash
security add-generic-password -a "$USER" -s "GITHUB_TOKEN" -w "ghp_..."
security add-generic-password -a "$USER" -s "JIRA_TOKEN"   -w "your-pat"
security add-generic-password -a "$USER" -s "JIRA_SERVER"  -w "https://jira.yourcompany.com"
security add-generic-password -a "$USER" -s "OPENAI_KEY"   -w "sk-..."
security add-generic-password -a "$USER" -s "AZURE_ENDPOINT" -w "https://your-instance.openai.azure.com/"
```

**3. Install the launchd schedule**

```bash
DEPLOY_DIR="/Users/$USER/Deploy"
PLIST_DST="$HOME/Library/LaunchAgents/com.${USER}.codereview.plist"
 
 sed \
   -e "s|__VENV__|$DEPLOY_DIR/.venv|g" \
   -e "s|__AGENT_DIR__|$DEPLOY_DIR|g" \
   -e "s|__HOME__|$HOME|g" \
   -e "s|__USER__|$USER|g" \
   "$DEPLOY_DIR/com.user.codereview.plist" > "$PLIST_DST"

launchctl load -w "$PLIST_DST"
```

**4. Verify with a dry run**

```bash
/Users/$USER/Deploy/.venv/bin/python /Users/$USER/Deploy/agent.py --dry-run --force --repos-root /Volumes/RAMDisk
```

## Usage

The agent runs automatically via launchd. For manual runs:

```bash
# Dry run (prints actions, posts nothing)
python agent.py --dry-run --force --repos-root /Volumes/RAMDisk

# Process a single repo
python agent.py --force --repos-root /Volumes/RAMDisk --repo my-repo-name

# Normal run (outside working hours needs --force)
python agent.py --force --repos-root /Volumes/RAMDisk

## Run deployed agent manually
~/Deploy/.venv/bin/python ~/Deploy/agent.py --force --repos-root /Volumes/RAMDisk
```

## How it works

For each git repo under `/Volumes/RAMDisk`:

1. Extracts the Jira key from the branch name (e.g. `feature/PROJ-1234` -> `PROJ-1234`)
2. Finds the open PR on GitHub for that branch
3. Checks for new commits since the last review
4. Fetches the Jira ticket summary and description for context
5. Sends the PR diff + Jira context to Azure OpenAI for a code review
6. Posts the review to the PR (tagged `<!-- review-agent -->` to track duplicates)
7. Updates the Jira "Internal Notes" field with a short work summary
8. Posts a full daily update comment to the Jira ticket

## Logs

Logs are written to `~/Library/Logs/review-agent.YYYY-MM-DD.log`.
Old logs are automatically deleted after 30 days.

## Files

| File | Purpose |
|---|---|
| `agent.py` | Main agent script |
| `requirements.txt` | Python dependencies |
| `com.ilya.codereview.plist` | launchd schedule template |
| `setup.sh` | One-time setup (venv + secrets + launchd) |
| `deploy.sh` | Deploys changed files to `/Users/${USER}/Deploy` |
