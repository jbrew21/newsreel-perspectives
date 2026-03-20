#!/usr/bin/env python3
"""
Retroactively fix YouTube quotes in topic index files.
Replaces video-title-only quotes with transcript excerpts from cache.

Usage:
  python scripts/fix_youtube_quotes.py                    # fix latest
  python scripts/fix_youtube_quotes.py --date 2026-03-13  # specific date
  python scripts/fix_youtube_quotes.py --all              # fix all dates
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
CACHE_PATH = ROOT / "data" / "transcript_cache.json"

# Load transcript cache
cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
print(f"Transcript cache: {sum(1 for v in cache.values() if v)}/{len(cache)} with text")

args = sys.argv[1:]
date = None
fix_all = '--all' in args
if '--date' in args:
    idx = args.index('--date')
    if idx + 1 < len(args):
        date = args[idx + 1]

# Find index files to fix
index_files = sorted(POSTS_DIR.glob('topic-index-*.json'), reverse=True)
if date:
    index_files = [f for f in index_files if date in f.name]
elif not fix_all:
    index_files = index_files[:1]  # just latest

for index_file in index_files:
    topic_index = json.loads(index_file.read_text())
    fixed = 0
    total_yt = 0

    for topic, entries in topic_index.items():
        for entry in entries:
            if entry.get('platform') != 'youtube':
                continue
            total_yt += 1

            # Check if quote looks like a video title (not real opinion content)
            quote = entry.get('quote', '')
            has_sentence = any(c in quote for c in '.!?') and len(quote) > 60
            is_title_only = not has_sentence or quote.startswith('[VIDEO:')

            if not is_title_only:
                continue

            # Try to find transcript in cache
            vid_match = re.search(r'(?:watch\?v=|youtu\.be/)([\w-]+)', entry.get('sourceUrl', ''))
            if not vid_match:
                continue
            video_id = vid_match.group(1)

            if video_id in cache and cache[video_id]:
                entry['quote'] = cache[video_id][:300]
                fixed += 1

    if fixed > 0:
        index_file.write_text(json.dumps(topic_index, indent=2))
        print(f"{index_file.name}: fixed {fixed}/{total_yt} YouTube quotes")
    else:
        print(f"{index_file.name}: no fixes needed ({total_yt} YouTube posts)")

# Also fix fractures/stories files
for pattern in ['fractures-*.json', 'stories-*.json']:
    for f in sorted(POSTS_DIR.glob(pattern), reverse=True)[:3]:
        data = json.loads(f.read_text())
        fixed = 0
        for story in data:
            for cluster in story.get('clusters', []):
                for voice in cluster.get('voices', []):
                    if voice.get('platform') != 'youtube':
                        continue
                    quote = voice.get('quote', '')
                    if len(quote) < 120:
                        vid_match = re.search(r'(?:watch\?v=|youtu\.be/)([\w-]+)', voice.get('sourceUrl', ''))
                        if vid_match and vid_match.group(1) in cache and cache[vid_match.group(1)]:
                            voice['quote'] = cache[vid_match.group(1)][:200]
                            fixed += 1
        if fixed > 0:
            f.write_text(json.dumps(data, indent=2))
            print(f"{f.name}: fixed {fixed} YouTube quotes in clusters")
