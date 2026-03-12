#!/usr/bin/env python3
"""
Newsreel Perspectives — Daily Automation

Pulls today's stories from the Newsreel CMS, runs the multi-platform
search pipeline on each, and saves story JSONs for the web app.

Usage:
  python scripts/daily.py              # Run on today's stories
  python scripts/daily.py --dry-run    # Show what would run without searching
"""

import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
ENV_PATH = ROOT.parent / "newsletter" / ".env"
STORIES_DIR = ROOT / "data" / "stories"

# Load env
def load_env():
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                key, _, val = line.partition('=')
                os.environ[key.strip()] = val.strip()

load_env()

CMS_API = os.environ.get('CMS_API_URL', 'https://newsreel-cms.onrender.com/api')


def fetch_todays_stories():
    """Pull today's stories from the Newsreel CMS."""
    today = datetime.now().strftime('%Y-%m-%d')

    try:
        url = f'{CMS_API}/stories?status=published&date={today}&sort=newest&limit=10'
        req = urllib.request.Request(url, headers={'User-Agent': 'Newsreel-Perspectives/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        raw = data.get('stories', data if isinstance(data, list) else [])

        stories = []
        for s in raw:
            headline = s.get('story_headline', s.get('headline', s.get('title', '')))
            summary = s.get('subhead', s.get('summary', ''))
            if headline:
                stories.append({
                    'headline': headline,
                    'summary': summary[:300] if summary else '',
                })

        # If no stories for today, try latest published regardless of date
        if not stories:
            url = f'{CMS_API}/stories?status=published&sort=newest&limit=5'
            req = urllib.request.Request(url, headers={'User-Agent': 'Newsreel-Perspectives/1.0'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())

            raw = data.get('stories', data if isinstance(data, list) else [])
            for s in raw:
                headline = s.get('story_headline', s.get('headline', s.get('title', '')))
                summary = s.get('subhead', s.get('summary', ''))
                if headline:
                    stories.append({
                        'headline': headline,
                        'summary': summary[:300] if summary else '',
                    })

        return stories

    except Exception as e:
        print(f"  ⚠ CMS error: {e}")
        return []


async def run_search(headline, summary, topic):
    """Run the search pipeline for a single story."""
    # Import the search module
    sys.path.insert(0, str(ROOT / 'scripts'))
    import search

    # Override sys.argv for the search module
    original_argv = sys.argv
    sys.argv = ['search.py', topic, '--headline', headline, '--summary', summary]

    try:
        await search.main()
    finally:
        sys.argv = original_argv


async def main():
    dry_run = '--dry-run' in sys.argv

    print(f"\n  ╔══════════════════════════════════════════════╗")
    print(f"  ║   NEWSREEL PERSPECTIVES — Daily Run            ║")
    print(f"  ╚══════════════════════════════════════════════╝")
    print(f"\n  Date: {datetime.now().strftime('%A, %B %d, %Y')}")

    stories = fetch_todays_stories()

    if not stories:
        print(f"\n  No stories found in CMS for today.")
        print(f"  You can run manually: python scripts/search.py \"topic\" --headline \"...\"")
        return

    print(f"  Found {len(stories)} stories in CMS\n")

    for i, s in enumerate(stories):
        print(f"  [{i+1}] {s['headline'][:70]}")

    if dry_run:
        print(f"\n  --dry-run: Would search {len(stories)} stories. Exiting.")
        return

    print(f"\n  Running search pipeline on each story...\n")
    print(f"  {'='*60}")

    for i, s in enumerate(stories):
        print(f"\n  Story {i+1}/{len(stories)}: {s['headline'][:60]}")
        print(f"  {'-'*60}")

        # Extract key topic words from headline (drop common words)
        stop_words = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
                      'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by', 'from',
                      'as', 'and', 'or', 'but', 'not', 'that', 'this', 'it', 'its',
                      'his', 'her', 'he', 'she', 'they', 'we', 'you', 'i', 'my',
                      'says', 'said', 'could', 'would', 'will', 'may', 'might',
                      'has', 'have', 'had', 'do', 'does', 'did', 'can', 'very'}
        words = [w for w in s['headline'].split() if w.lower() not in stop_words]
        topic = ' '.join(words[:5])  # Use top 5 meaningful words as search topic

        try:
            await run_search(s['headline'], s['summary'], topic)
        except Exception as e:
            print(f"  ⚠ Error on story {i+1}: {e}")

    print(f"\n  {'='*60}")
    print(f"  ✓ Daily run complete! Check data/stories/ for results.")
    print(f"  ✓ Web app: http://localhost:8888")
    print()


if __name__ == '__main__':
    asyncio.run(main())
