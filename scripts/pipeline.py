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


def sync_to_supabase():
    """Sync today's data to Supabase (single source of truth)."""
    log("Supabase: Starting sync")
    try:
        from supabase import create_client
    except ImportError:
        log("Supabase: supabase package not installed, skipping")
        return False

    url = os.environ.get('SUPABASE_URL', '')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not url or not key:
        log("Supabase: No credentials, skipping")
        return False

    try:
        client = create_client(url, key)

        # Import and run migration (idempotent -- deterministic UUIDs)
        sys.path.insert(0, str(SCRIPTS))
        from migrate_to_supabase import (
            migrate_topics, migrate_voices, migrate_posts,
            migrate_stories, refresh_views, connect
        )

        # Override the module's connect to reuse our client
        import migrate_to_supabase as mig
        mig.DRY_RUN = False

        valid_slugs = migrate_topics(client)
        valid_voices = migrate_voices(client)
        migrate_posts(client, valid_slugs, valid_voices)
        migrate_stories(client, valid_voices)
        refresh_views(client)

        log("Supabase: Sync complete")
        return True
    except Exception as e:
        log(f"Supabase: Sync failed (non-fatal): {e}")
        return False


def record_pipeline_run(status, stats=None, errors=None):
    """Write to pipeline_runs table in Supabase for monitoring."""
    url = os.environ.get('SUPABASE_URL', '')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not url or not key:
        return

    try:
        from supabase import create_client
        client = create_client(url, key)

        elapsed = int(time.time() - START)
        row = {
            'run_date': DATE,
            'started_at': datetime.fromtimestamp(START).isoformat(),
            'finished_at': datetime.now().isoformat(),
            'status': status,
            'posts_collected': (stats or {}).get('posts', 0),
            'posts_categorized': (stats or {}).get('categorized', 0),
            'stories_created': (stats or {}).get('stories', 0),
            'clusters_created': (stats or {}).get('clusters', 0),
            'errors': errors or [],
            'cost_usd': (stats or {}).get('cost', 0),
        }
        client.table('pipeline_runs').insert(row).execute()
        log(f"Monitoring: Pipeline run recorded ({status}, {elapsed}s)")
    except Exception as e:
        log(f"Monitoring: Failed to record run: {e}")


def main():
    skip_collect = '--skip-collect' in sys.argv
    errors = []

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
            timeout_sec=1800,
            required=True,
        )
        if not ok:
            log("ABORT: Collection failed")
            errors.append("Collection failed")
            record_pipeline_run('failed', errors=errors)
            sys.exit(1)
    else:
        log("Skipping collection (--skip-collect)")

    # Step 2: Build stories
    ok = run_step(
        "Build stories feed",
        [python, str(SCRIPTS / "stories.py")],
        timeout_sec=600,
        required=False,
    )
    if not ok:
        errors.append("Stories build failed")

    # Step 3: Health check
    healthy = health_check()

    # Step 4: Sync to Supabase (single source of truth)
    supabase_ok = sync_to_supabase()
    if not supabase_ok:
        errors.append("Supabase sync failed")

    # Step 5: Git push
    git_push()

    # Step 6: Deploy
    trigger_deploy()

    # Step 7: Record pipeline run
    stats = None
    topic_index = POSTS_DIR / f"topic-index-{DATE}.json"
    if topic_index.exists():
        ti = json.loads(topic_index.read_text())
        stories_path = POSTS_DIR / f"stories-{DATE}.json"
        story_count = 0
        cluster_count = 0
        if stories_path.exists():
            stories = json.loads(stories_path.read_text())
            story_count = len(stories)
            cluster_count = sum(len(s.get('clusters', [])) for s in stories)
        stats = {
            'posts': sum(len(v) for v in ti.values()),
            'categorized': sum(len(v) for v in ti.values()),
            'stories': story_count,
            'clusters': cluster_count,
        }

    status = "completed" if healthy and not errors else "degraded" if healthy else "failed"
    record_pipeline_run(status, stats=stats, errors=errors)

    # Summary
    elapsed = int(time.time() - START)
    print(f"\n  {'='*40}")
    print(f"  Pipeline {status.upper()} in {elapsed}s")
    print(f"  Completed: {datetime.now().strftime('%H:%M:%S')}\n")

    sys.exit(0 if healthy else 1)


if __name__ == '__main__':
    main()
