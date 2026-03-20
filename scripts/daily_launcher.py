#!/usr/bin/env python3
"""
Launchd-friendly launcher for the Perspectives daily pipeline.

macOS TCC blocks /bin/bash (via launchd) from accessing ~/Desktop.
This Python launcher avoids that by running the pipeline steps directly
from Python, which doesn't hit the same TCC restrictions when called
as ProgramArguments[0] in a launchd plist.
"""

import os
import sys
import subprocess
import json
import time
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
PYTHON = sys.executable
DATE = datetime.now().strftime("%Y-%m-%d")
LOG_FILE = "/tmp/perspectives-daily.log"

# Ensure PATH
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")
os.environ["PYTHONUNBUFFERED"] = "1"

# Load .env
env_file = PROJECT_DIR / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            val = val.strip().strip("'\"")
            os.environ[key.strip()] = val

SLACK_WEBHOOK = os.environ.get("PERSPECTIVES_SLACK_WEBHOOK", "")
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)


def send_slack(msg, emoji=":warning:"):
    if not SLACK_WEBHOOK:
        return
    try:
        import urllib.request
        data = json.dumps({"text": f"{emoji} *Perspectives Daily* ({DATE})\n{msg}"}).encode()
        req = urllib.request.Request(SLACK_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def run_step(name, args, timeout_sec=1200):
    """Run a subprocess with a timeout. Returns (success, elapsed_sec)."""
    log(f"Starting: {name}")
    start = time.time()
    try:
        result = subprocess.run(
            args,
            cwd=str(PROJECT_DIR),
            timeout=timeout_sec,
            capture_output=False,
        )
        elapsed = int(time.time() - start)
        if result.returncode == 0:
            log(f"  Completed: {name} ({elapsed}s)")
            return True, elapsed
        else:
            log(f"  FAILED: {name} (exit {result.returncode}, {elapsed}s)")
            return False, elapsed
    except subprocess.TimeoutExpired:
        elapsed = int(time.time() - start)
        log(f"  TIMEOUT: {name} after {elapsed}s")
        return False, elapsed


def deploy_render():
    if not RENDER_API_KEY:
        log("  No RENDER_API_KEY, skipping deploy")
        return
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.render.com/v1/services/srv-d6pitsmuk2gs73fhkj70/deploys",
            data=b"{}",
            headers={
                "Authorization": f"Bearer {RENDER_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15)
        log("  Render deploy triggered")
    except Exception as e:
        log(f"  Render deploy failed: {e}")


def git_push():
    os.chdir(str(PROJECT_DIR))
    # Check if there are changes
    result = subprocess.run(["git", "diff", "--quiet", "data/posts/"], capture_output=True)
    if result.returncode == 0:
        log("  No new data to commit")
        return
    subprocess.run(["git", "add", "data/posts/"], capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"Daily collection: {DATE}", "--no-verify"],
        capture_output=True,
    )
    push = subprocess.run(["git", "push", "origin", "main"], capture_output=True)
    if push.returncode == 0:
        log("  Pushed to GitHub")
    else:
        log(f"  Push failed (non-fatal): {push.stderr.decode()[:200]}")


def health_check():
    topic_index = PROJECT_DIR / "data" / "posts" / f"topic-index-{DATE}.json"
    if not topic_index.exists():
        send_slack(f"No topic index generated for {DATE}")
        return None

    data = json.loads(topic_index.read_text())
    topics = len(data)
    posts = sum(len(v) for v in data.values())
    voices = len(set(e["voiceId"] for entries in data.values() for e in entries))
    uncat = len(data.get("uncategorized", []))
    uncat_pct = round(uncat / max(posts, 1) * 100)

    log(f"  Posts: {posts}, Voices: {voices}, Topics: {topics}, Uncategorized: {uncat_pct}%")

    if posts < 50:
        send_slack(f"Low post count: only {posts} posts collected (expected 200+)")
    elif uncat_pct > 50:
        send_slack(f"High uncategorized rate: {uncat_pct}%")

    stories_file = PROJECT_DIR / "data" / "posts" / f"stories-{DATE}.json"
    story_count = 0
    if stories_file.exists():
        story_count = len(json.loads(stories_file.read_text()))
    log(f"  Stories: {story_count}")

    return {"posts": posts, "voices": voices, "topics": topics, "stories": story_count}


def main():
    pipeline_start = time.time()
    log("=" * 50)
    log(f"Perspectives Daily Pipeline — {DATE}")
    log("=" * 50)

    # Phase 1: Collect
    ok, _ = run_step("Phase 1: Collect posts", [PYTHON, str(SCRIPTS_DIR / "collect.py")], timeout_sec=1800)
    if not ok:
        send_slack("Collection FAILED. Check /tmp/perspectives-daily.log")
        sys.exit(1)

    # Phase 2: Enrich transcripts
    run_step("Phase 2: Enrich transcripts", [PYTHON, str(SCRIPTS_DIR / "enrich_transcripts.py")], timeout_sec=300)

    # Phase 3: Stories
    ok, _ = run_step("Phase 3: Build stories", [PYTHON, str(SCRIPTS_DIR / "stories.py")], timeout_sec=600)
    if not ok:
        log("  Falling back to fractures.py...")
        run_step("Phase 3 fallback: Fractures", [PYTHON, str(SCRIPTS_DIR / "fractures.py")], timeout_sec=300)

    # Phase 4: Health check
    log("Phase 4: Health check")
    stats = health_check()

    # Phase 5: Sync to Supabase
    log("Phase 5: Supabase sync")
    ok, _ = run_step("Supabase sync", [PYTHON, str(SCRIPTS_DIR / "pipeline.py"), "--skip-collect"], timeout_sec=120)
    if not ok:
        log("  Supabase sync failed (non-fatal)")

    # Phase 6: Deploy
    log("Phase 6: Deploy")
    deploy_render()

    # Phase 7: Git
    log("Phase 7: Git push")
    git_push()

    total = int(time.time() - pipeline_start)
    summary = f"Pipeline completed in {total}s"
    if stats:
        summary = f"{stats['posts']} posts, {stats['voices']} voices, {stats['topics']} topics, {stats['stories']} stories. {total}s."
    send_slack(summary, ":white_check_mark:")
    log(f"Done. {summary}")


if __name__ == "__main__":
    # Redirect stdout/stderr to log file
    if not sys.stdout.isatty():
        log_fh = open(LOG_FILE, "a")
        sys.stdout = log_fh
        sys.stderr = log_fh
    main()
