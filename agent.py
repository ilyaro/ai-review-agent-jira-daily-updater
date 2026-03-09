#!/usr/bin/env python3
"""
review-agent: Daily code review agent for local git repos.
Finds branches with open PRs, posts AI code reviews to GitHub and summaries to Jira.

Secrets are read from macOS Keychain — never stored in plaintext.
"""
from __future__ import annotations

import os
import re
import sys
import logging
import logging.handlers
import argparse
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from openai import AzureOpenAI
import urllib.request
from github import Auth, Github, GithubException
from jira import JIRA

# ── Config ────────────────────────────────────────────────────────────────────

WORKING_HOUR    = 23                    # runs once at 23:00
AGENT_MARKER    = "<!-- review-agent -->"  # fingerprint injected into every review
LOG_DIR         = Path.home() / "Library/Logs"
LOG_FILE        = LOG_DIR / f"review-agent.{datetime.now().strftime('%Y-%m-%d')}.log"
LOG_RETENTION   = 30

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
       logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("review-agent")

def cleanup_old_logs():
    cutoff = datetime.now() - timedelta(days=LOG_RETENTION)
    for f in LOG_DIR.glob("review-agent.*.log"):
        try:
            date_str = f.stem.split(".", 1)[1]
            if datetime.strptime(date_str, "%Y-%m-%d") < cutoff:
                f.unlink()
                log.info("Deleted old log: %s", f.name)
        except (ValueError, IndexError):
            pass

# ── Secrets ───────────────────────────────────────────────────────────────────

def keychain_get(service: str) -> str:
    """Retrieve a secret from macOS Keychain.  Dies with a clear message if missing."""
    try:
        value = subprocess.check_output(
            ["security", "find-generic-password",
             "-a", os.environ["USER"], "-s", service, "-w"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        if not value:
            raise ValueError("empty")
        return value
    except subprocess.CalledProcessError:
        log.error(
            "Keychain secret '%s' not found.\n"
            "  Store it once with:\n"
            "    security add-generic-password -a \"$USER\" -s \"%s\" -w \"<token>\"",
            service, service,
        )
        sys.exit(1)


def load_secrets() -> dict:
     return {
         "github_token":    keychain_get("GITHUB_TOKEN"),
         "jira_token":      keychain_get("JIRA_TOKEN"),
         "jira_server":     keychain_get("JIRA_SERVER"),   # e.g. https://yourco.atlassian.net
         "openai_key":      keychain_get("OPENAI_KEY"),
         "azure_endpoint":  keychain_get("AZURE_ENDPOINT"),
     }

# ── Guard: working hours ──────────────────────────────────────────────────────

def is_working_time() -> bool:
    now = datetime.now()
    return now.weekday() < 5 and now.hour == WORKING_HOUR

# ── Git helpers ───────────────────────────────────────────────────────────────

def git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git"] + args, cwd=cwd,
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def local_branches(repo_path: Path) -> list[str]:
    out = git(["branch", "--format=%(refname:short)"], cwd=repo_path)
    return [b.strip() for b in out.splitlines() if b.strip()]


def get_remote_url(repo_path: Path) -> Optional[str]:
    try:
        return git(["remote", "get-url", "origin"], cwd=repo_path)
    except RuntimeError:
        return None


def parse_github_repo(remote_url: str) -> Optional[str]:
    """Return 'owner/repo' from an SSH or HTTPS remote URL."""
    patterns = [
        r"github\.com[:/](.+?/[^/]+?)(?:\.git)?$",
    ]
    for p in patterns:
        m = re.search(p, remote_url)
        if m:
            return m.group(1)
    return None

# ── Jira helpers ─────────────────────────────────────────────────────────────

def extract_jira_key(branch: str) -> Optional[str]:
     m = re.search(r'([A-Z]{2,10}-\d+)', branch.upper())
     return m.group(1) if m else None

def resolve_jira_field_id(jira_client: JIRA, field_name: str) -> Optional[str]:
    """Look up the custom field ID for a given field name (e.g. 'Internal Notes').
    Queries GET /rest/api/2/field once and caches nothing — called once per run."""
    for field in jira_client.fields():
        if field["name"] == field_name:
            return field["id"]
    return None

# ── GitHub helpers ────────────────────────────────────────────────────────────

def find_open_pr(gh_repo, branch: str):
    """Return the first open PR for this branch, or None."""
    prs = gh_repo.get_pulls(state="open", head=f"{gh_repo.owner.login}:{branch}")
    for pr in prs:
        return pr
    return None


def pr_diff(pr, token: str) -> str:
    """Fetch unified diff of the full PR (uses raw GitHub API)."""
    req = urllib.request.Request(
        pr.url,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3.diff",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return resp.read().decode("utf-8", errors="replace")


def commits_since(pr, since: datetime) -> list:
    """Return PR commits pushed after `since` (UTC)."""
    since_utc = since.astimezone(timezone.utc)
    return [c for c in pr.get_commits()
            if c.commit.author.date.replace(tzinfo=timezone.utc) >= since_utc]


def agent_review_exists(pr) -> tuple[bool, str | None, datetime | None]:
    """Return (found, last_review_body, last_review_time) for reviews already posted by this agent."""
    last_body = None
    last_time = None
    for review in pr.get_reviews():
        if AGENT_MARKER in (review.body or ""):
            last_body = review.body
            last_time = review.submitted_at
    return (last_body is not None), last_body, last_time

# ── AI review ────────────────────────────────────────────────────────────────

REVIEW_SYSTEM = """\
You are a senior software engineer doing a pull-request code review.
Be concise, constructive, and specific.
Structure your output with these sections:
## Summary
## Issues  (Critical / Major / Minor)
## Suggestions
## Positive highlights
Keep the total review under 600 words."""


def build_review_prompt(
    diff: str,
    jira_summary: str,
    jira_description: str,
    pr_title: str,
    new_commits: list,
    previous_review: str | None,
) -> str:
    commit_list = "\n".join(f"- {c.commit.message.splitlines()[0]}" for c in new_commits) or "none"
    prev_section = (
        f"Previous agent review (for context — do NOT repeat already-raised issues):\n{previous_review[:1500]}"
        if previous_review else "No previous review from this agent."
    )

    # Truncate diff to avoid token limits (~12k chars ≈ ~3k tokens)
    diff_excerpt = diff[:12_000] + ("\n... [diff truncated]" if len(diff) > 12_000 else "")

    return f"""\
PR title: {pr_title}

Jira task: {jira_summary}
{jira_description[:800]}

New commits since last run:
{commit_list}

{prev_section}

Diff to review:
```diff
{diff_excerpt}
```

Write the code review now."""


def ai_review(client: OpenAI, prompt: str) -> str:
    response = client.chat.completions.create(
        model="gpt-5.2-2025-12-11",
        max_completion_tokens=1024,
        messages=[
            {"role": "system", "content": REVIEW_SYSTEM},
            {"role": "user",   "content": prompt},
        ],
    )
    return response.choices[0].message.content
 
 
JIRA_SUMMARY_SYSTEM = """\
You are a DevOps engineer writing a brief daily status update for a Jira ticket.
Based on the commit messages, summarize WHAT WAS DONE today in 2-4 sentences.
Focus on deliverables and progress, not code quality.
Do not review or critique the code. Just state what changed."""

def ai_work_summary(client, commits: list, pr_title: str) -> str:
    commit_list = "\n".join(
        f"- {c.commit.message.splitlines()[0]}" for c in commits
    )
    response = client.chat.completions.create(
        model="gpt-5.2-2025-12-11",
        max_completion_tokens=256,
        messages=[
            {"role": "system", "content": JIRA_SUMMARY_SYSTEM},
            {"role": "user", "content": f"PR: {pr_title}\n\nCommits:\n{commit_list}"},
        ],
    )
    return response.choices[0].message.content

def build_jira_comment(pr_url: str, pr_title: str, new_commits: list, work_summary: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    commit_lines = "\n".join(
        f"* {c.commit.message.splitlines()[0]}" for c in new_commits
    ) or "* (no new commits today)"
 
    return (
        f"*Automated daily update — {today}*\n\n"
        f"*PR:* [{pr_title}|{pr_url}]\n\n"
        f"*Commits today:*\n{commit_lines}\n\n"
        f"*What was done:*\n{work_summary}\n"
     )
 
 # ── Main loop ─────────────────────────────────────────────────────────────────

def process_repo(
    repo_path: Path,
    gh: Github,
    jira_client: JIRA,
    ai_client: OpenAI,
    dry_run: bool,
    github_token: str,
) -> None:
    remote_url = get_remote_url(repo_path)
    if not remote_url or "github.com" not in remote_url:
        log.debug("%s — no GitHub remote, skipping", repo_path.name)
        return

    slug = parse_github_repo(remote_url)
    if not slug:
        log.warning("%s — could not parse remote URL: %s", repo_path.name, remote_url)
        return

    try:
        gh_repo = gh.get_repo(slug)
    except GithubException as e:
        log.warning("%s — GitHub repo not found (%s)", slug, e.status)
        return

    log.info("Scanning %s (%s)", repo_path.name, slug)

    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    for branch in local_branches(repo_path):
        jira_key = extract_jira_key(branch)
        if not jira_key:
            log.debug("  branch %s — no Jira key, skipping", branch)
            continue

        pr = find_open_pr(gh_repo, branch)
        if not pr:
            log.debug("  branch %s — no open PR", branch)
            continue

        log.info("  branch %s → PR #%d  Jira %s", branch, pr.number, jira_key)

        # Check for new commits today
        already_reviewed, prev_review, last_review_time = agent_review_exists(pr)
        since = last_review_time if last_review_time else today_start
        new_commits = commits_since(pr, since)

        if not new_commits and already_reviewed:
            log.info("    no new commits and already reviewed — skipping")
            continue

        # Fetch Jira issue
        try:
            issue = jira_client.issue(jira_key)
            jira_summary     = issue.fields.summary or ""
            jira_description = getattr(issue.fields, "description", "") or ""
        except Exception as e:
            log.warning("    Jira issue %s not found: %s", jira_key, e)
            jira_summary = jira_description = ""

        # Get diff
        try:
            diff = pr_diff(pr, github_token)
        except Exception as e:
            log.warning("    Could not fetch diff: %s", e)
            continue

        if not diff.strip():
            log.info("    empty diff — skipping")
            continue

        # Generate AI review
        prompt = build_review_prompt(
            diff=diff,
            jira_summary=jira_summary,
            jira_description=jira_description,
            pr_title=pr.title,
            new_commits=new_commits,
            previous_review=prev_review,
        )

        log.info("    generating review with openai…")
        review_text = ai_review(ai_client, prompt)
        stamped_review = f"{AGENT_MARKER}\n\n{review_text}"

        # Post review to PR
        if dry_run:
            log.info("    [DRY RUN] would post review to PR #%d:\n%s", pr.number, review_text[:300])
        else:
            pr.create_review(body=stamped_review, event="COMMENT")
            log.info("    ✓ review posted to PR #%d", pr.number)

        # Post comment to Jira
        if new_commits:
            log.info("    generating work summary for Jira...")
            work_summary = ai_work_summary(ai_client, new_commits, pr.title)
            jira_comment = build_jira_comment(pr.html_url, pr.title, new_commits, work_summary)

            # Update "Internal Notes" custom field
            internal_notes_id = resolve_jira_field_id(jira_client, "Internal Notes")
            if not dry_run and internal_notes_id:
                jira_client.issue(jira_key).update(fields={internal_notes_id: work_summary})
                log.info("    ✓ Jira Internal Notes (%s) updated for %s", internal_notes_id, jira_key)
            elif not internal_notes_id:
                log.warning("    field 'Internal Notes' not found in Jira — skipping field update")
            else:
                 log.info("    [DRY RUN] would update Internal Notes on %s", jira_key)

            # Post Jira comment
            if dry_run:
                log.info("    [DRY RUN] would post Jira comment to %s:\n%s", jira_key, jira_comment[:300])
            else:
                jira_client.add_comment(jira_key, jira_comment)
                log.info("    ✓ Jira comment posted to %s", jira_key)


def main():
    parser = argparse.ArgumentParser(description="Daily code review agent")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print actions without posting anything")
    parser.add_argument("--force", action="store_true",
                        help="Run even outside working hours")
    parser.add_argument("--repo", type=str,
                        help="Process only this repo name (for testing)")
    parser.add_argument("--repos-root", type=str, required=True,
                        help="Root directory containing git repos to scan")
    args = parser.parse_args()

    REPOS_ROOT = Path(args.repos_root)

    if not args.force and not is_working_time():
        log.info("Outside working hours — exiting (use --force to override)")
        sys.exit(0)

    log.info("=== review-agent starting (dry_run=%s) ===", args.dry_run)
    cleanup_old_logs()

    secrets = load_secrets()

    gh          = Github(auth=Auth.Token(secrets["github_token"]))
    jira_client = JIRA(
        server=secrets["jira_server"],
        token_auth=secrets["jira_token"],
     )
    ai_client   = AzureOpenAI(
        azure_endpoint=secrets["azure_endpoint"],
        api_key=secrets["openai_key"],
        api_version="2025-04-01-preview",
    )

    if not REPOS_ROOT.exists():
        log.error("REPOS_ROOT %s does not exist", REPOS_ROOT)
        sys.exit(1)

    repos = (
        [REPOS_ROOT / args.repo] if args.repo
        else [p for p in REPOS_ROOT.iterdir() if p.is_dir() and (p / ".git").exists()]
    )

    if not repos:
        log.warning("No git repos found under %s", REPOS_ROOT)

    for repo_path in repos:
        try:
            process_repo(repo_path, gh, jira_client, ai_client, dry_run=args.dry_run, github_token=secrets["github_token"])
        except Exception as e:
            log.exception("Unhandled error processing %s: %s", repo_path.name, e)

    log.info("=== review-agent done ===")


if __name__ == "__main__":
    main()