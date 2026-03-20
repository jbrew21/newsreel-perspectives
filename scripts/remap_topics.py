#!/usr/bin/env python3
"""
Remap topic index files from legacy slugs to the fixed taxonomy.

Usage:
  python scripts/remap_topics.py                    # remap latest
  python scripts/remap_topics.py --date 2026-03-13  # specific date
  python scripts/remap_topics.py --all              # all dates
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
MAPPING_PATH = ROOT / "data" / "topic-mapping.json"

# Load mapping
mapping = json.loads(MAPPING_PATH.read_text())
print(f"Loaded mapping: {len(mapping)} legacy slugs -> canonical topics")

args = sys.argv[1:]
fix_all = '--all' in args
date = None
if '--date' in args:
    idx = args.index('--date')
    if idx + 1 < len(args):
        date = args[idx + 1]

index_files = sorted(POSTS_DIR.glob('topic-index-*.json'), reverse=True)
if date:
    index_files = [f for f in index_files if date in f.name]
elif not fix_all:
    index_files = index_files[:1]

for index_file in index_files:
    topic_index = json.loads(index_file.read_text())
    new_index = {}
    remapped = 0
    kept = 0

    for topic, entries in topic_index.items():
        canonical = mapping.get(topic, topic)
        if canonical != topic:
            remapped += 1
        else:
            kept += 1

        if canonical not in new_index:
            new_index[canonical] = []
        new_index[canonical].extend(entries)

    # Deduplicate within each topic (same voiceId + sourceUrl)
    for topic in new_index:
        seen = set()
        deduped = []
        for e in new_index[topic]:
            key = (e['voiceId'], e.get('sourceUrl', ''))
            if key not in seen:
                seen.add(key)
                deduped.append(e)
        new_index[topic] = deduped

    old_count = len(topic_index)
    new_count = len(new_index)
    total_posts = sum(len(v) for v in new_index.values())

    index_file.write_text(json.dumps(new_index, indent=2))
    print(f"{index_file.name}: {old_count} topics -> {new_count} topics ({remapped} remapped, {total_posts} posts)")
