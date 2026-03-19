#!/usr/bin/env python3
"""
Newsreel Perspectives -- Daily Pipeline

Single entry point for the full daily pipeline.
Designed to run as a Render cron job (no local Mac dependencies).

Steps:
  1. Collect posts from all voices (7 platforms)
  2. Enrich YouTube transcripts
  3. Categorize with Claude Haiku
  4. Build unified stories feed
  5. Git commit + push data
  6. Trigger Render web service deploy

Usage:
  python scripts/pipeline.py              # full pipeline
  python scripts/pipeline.py --skip-collect  # just rebuild stories from existing data
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
SCRIPTS = ROOT / "scripts"
POSTS_DIR = ROOT / "data" / "posts"

# Load env
for env_path in [ROOT / ".env", ROOT.parent / "newsletter" / ".env"]:
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                key, _, val = line.partition('=')
                if key.strip() not in os.environ:
                    os.environ[key.strip()] = val.strip()

DATE = datetime.now().strftime('%Y-%m-%d')
START = time.time()


def log(msg):
    elapsed = int(time.time() - START)
    print(f"  [{elapsed:>4}s] {msg}", flush=True)


def run_step(name, cmd, timeout_sec=1200, required=True):
    """Run a pipeline step with timeout and error handling."""
    log(f"Starting: {name}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout_sec, cwd=str(ROOT),
        )
        if result.returncode != 0:
            log(f"FAILED: {name} (exit {result.returncode})")
            if result.stderr:
                for line in result.stderr.strip().split('\n')[-5:]:
                    log(f"  stderr: {line}")
            if required:
                return False
        else:
            log(f"Done: {name}")
        # Print last few lines of stdout for monitoring
        if result.stdout:
            for line in result.stdout.strip().split('\n')[-3:]:
                log(f"  {line}")
        return True
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT: {name} (>{timeout_sec}s)")
        return not required
    except Exception as e:
        log(f"ERROR: {name}: {e}")
        return not required


def health_check():
    """Verify the pipeline produced good data."""
    topic_index = POSTS_DIR / f"topic-index-{DATE}.json"
    if not topic_index.exists():
        log("HEALTH FAIL: No topic index generated")
        return False

    ti = json.loads(topic_index.read_text())
    total_entries = sum(len(v) for v in ti.values())
    total_topics = len(ti)
    voices = len(set(e['voiceId'] for entries in ti.values() for e in entries))

    log(f"Health: {total_entries} posts, {voices} voices, {total_topics} topics")

    if total_entries < 50:
        log(f"HEALTH WARN: Low post count ({total_entries}). Expected 200+.")
    if total_topics < 5:
        log(f"HEALTH WARN: Low topic count ({total_topics}). Expected 10+.")

    # Check stories
    stories_path = POSTS_DIR / f"stories-{DATE}.json"
    if stories_path.exists():
        stories = json.loads(stories_path.read_text())
        log(f"Stories: {len(stories)} generated")
    else:
        log("HEALTH WARN: No stories file generated")

    return total_entries > 0


def git_push():
    """Commit and push new data to GitHub."""
    try:
        # Check for changes
        result = subprocess.run(
            ['git', 'diff', '--quiet', 'data/posts/'],
            cwd=str(ROOT), capture_output=True,
        )
        if result.returncode == 0:
            log("Git: No new data to commit")
            return True

        # Stage and commit
        subprocess.run(
            ['git', 'add', 'data/posts/'],
            cwd=str(ROOT), capture_output=True,
        )

        topic_index = POSTS_DIR / f"topic-index-{DATE}.json"
        ti = json.loads(topic_index.read_text()) if topic_index.exists() else {}
        total = sum(len(v) for v in ti.values())
        voices = len(set(e['voiceId'] for entries in ti.values() for e in entries))
        topics = len(ti)

        msg = f"Daily collection: {DATE} -- {total} posts, {voices} voices, {topics} topics"
        subprocess.run(
            ['git', 'commit', '-m', msg, '--no-verify'],
            cwd=str(ROOT), capture_output=True,
        )

        result = subprocess.run(
            ['git', 'push', 'origin', 'main'],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            log("Git: Pushed to GitHub")
        else:
            log(f"Git: Push failed (non-fatal): {result.stderr[:100]}")
        return True
    except Exception as e:
        log(f"Git: Error (non-fatal): {e}")
        return True


def trigger_deploy():
    """Trigger a Render web service deploy."""
    render_key = os.environ.get('RENDER_API_KEY', '')
    if not render_key:
        log("Deploy: No RENDER_API_KEY, skipping")
        return True

    try:
        import urllib.request
        req = urllib.request.Request(
            'https://api.render.com/v1/services/srv-d6pitsmuk2gs73fhkj70/deploys',
            data=b'{}',
            headers={
                'Authorization': f'Bearer {render_key}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        urllib.request.urlopen(req, timeout=10)
        log("Deploy: Render deploy triggered")
        return True
    except Exception as e:
        log(f"Deploy: Failed (non-fatal): {e}")
        return True


def main():
    skip_collect = '--skip-collect' in sys.argv

    print(f"\n  Perspectives Daily Pipeline")
    print(f"  Date: {DATE}")
    print(f"  Started: {datetime.now().strftime('%H:%M:%S')}")
    print(f"  {'='*40}\n")

    python = sys.executable

    # Step 1: Collect posts
    if not skip_collect:
        ok = run_step(
            "Collect posts from 257 voices",
            [python, str(SCRIPTS / "collect.py")],
            timeout_sec=1200,  # 20 min max
            required=True,
        )
        if not ok:
            log("ABORT: Collection failed")
            sys.exit(1)
    else:
        log("Skipping collection (--skip-collect)")

    # Step 2: Build stories
    run_step(
        "Build stories feed",
        [python, str(SCRIPTS / "stories.py")],
        timeout_sec=600,  # 10 min max
        required=False,  # stories failure shouldn't block data push
    )

    # Step 3: Health check
    healthy = health_check()

    # Step 4: Git push
    git_push()

    # Step 5: Deploy
    trigger_deploy()

    # Summary
    elapsed = int(time.time() - START)
    status = "OK" if healthy else "DEGRADED"
    print(f"\n  {'='*40}")
    print(f"  Pipeline {status} in {elapsed}s")
    print(f"  Completed: {datetime.now().strftime('%H:%M:%S')}\n")

    sys.exit(0 if healthy else 1)


if __name__ == '__main__':
    main()
