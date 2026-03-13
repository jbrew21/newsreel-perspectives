#!/usr/bin/env python3
"""Retroactively enrich existing YouTube posts with transcripts."""

import json
import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
CACHE_PATH = ROOT / "data" / "transcript_cache.json"

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import IpBlocked
except ImportError:
    print("Install: pip3 install youtube-transcript-api")
    exit(1)

import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--retry-failed', action='store_true', help='Clear failed cache entries and retry')
args = parser.parse_args()

# Load cache
cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}

# If --retry-failed, clear empty cache entries so we re-attempt
if args.retry_failed:
    before = len(cache)
    cache = {k: v for k, v in cache.items() if v}
    print(f"Cleared {before - len(cache)} failed cache entries, retrying...")

ytt_api = YouTubeTranscriptApi()
fetched = 0
enriched = 0
updated_files = 0
ip_blocked = False

for voice_dir in sorted(POSTS_DIR.iterdir()):
    if not voice_dir.is_dir():
        continue
    for post_file in sorted(voice_dir.glob('*.json')):
        data = json.loads(post_file.read_text())
        file_changed = False

        for p in data.get('posts', []):
            if p.get('platform') != 'youtube':
                continue
            if p.get('type') == 'video_transcript':
                continue

            vid_match = re.search(r'(?:watch\?v=|youtu\.be/)([\w-]+)', p.get('sourceUrl', ''))
            if not vid_match:
                continue
            video_id = vid_match.group(1)

            # Check cache first
            if video_id in cache:
                if cache[video_id]:
                    title = p['text'][:100]
                    p['text'] = f"[VIDEO: {title}] {cache[video_id]}"
                    p['type'] = 'video_transcript'
                    p['quote'] = cache[video_id][:300]
                    file_changed = True
                    enriched += 1
                continue

            # Fetch fresh transcript
            if ip_blocked or fetched >= 500:
                continue
            try:
                time.sleep(0.3)
                transcript = ytt_api.fetch(video_id, languages=['en'])
                text_parts = []
                for snippet in transcript.snippets:
                    if snippet.start > 300:  # first 5 min
                        break
                    text_parts.append(snippet.text)
                if text_parts:
                    transcript_text = ' '.join(text_parts)[:800]
                    cache[video_id] = transcript_text
                    title = p['text'][:100]
                    p['text'] = f"[VIDEO: {title}] {transcript_text}"
                    p['type'] = 'video_transcript'
                    p['quote'] = transcript_text[:300]
                    file_changed = True
                    enriched += 1
                    fetched += 1
                    if fetched % 10 == 0:
                        print(f"  Fetched {fetched} transcripts so far...")
                else:
                    cache[video_id] = ''
                    fetched += 1
            except IpBlocked:
                print("  YouTube IP rate limited — will retry next run")
                ip_blocked = True
            except Exception:
                cache[video_id] = ''
                fetched += 1

        if file_changed:
            # Rebuild topic summary
            data['topicSummary'] = {}
            for p in data.get('posts', []):
                topic = p.get('topic', 'uncategorized')
                if topic not in data['topicSummary']:
                    data['topicSummary'][topic] = []
                data['topicSummary'][topic].append({
                    'quote': p.get('quote', p['text'][:200]),
                    'sourceUrl': p['sourceUrl'],
                    'platform': p['platform'],
                    'timestamp': p['timestamp'],
                })

            post_file.write_text(json.dumps(data, indent=2))
            updated_files += 1

# Save cache
CACHE_PATH.write_text(json.dumps(cache))

has_text = sum(1 for v in cache.values() if v)
print(f"\nDone!")
print(f"Fetched: {fetched} new transcripts")
print(f"Enriched: {enriched} posts total")
print(f"Updated: {updated_files} files")
print(f"Cache: {has_text}/{len(cache)} with text")
