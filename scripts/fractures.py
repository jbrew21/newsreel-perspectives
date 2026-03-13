#!/usr/bin/env python3
"""
Newsreel Perspectives — Daily Fracture Computation

Finds today's most interesting story fractures — topics where voices split
into distinct argument clusters. Pre-computes everything so the homepage
loads instantly.

Usage:
  python scripts/fractures.py           # compute today's fractures
  python scripts/fractures.py --date 2026-03-12  # specific date
"""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
VOICES_PATH = ROOT / "data" / "voices.json"

# Load env
def load_env():
    for env_path in [ROOT / ".env", ROOT.parent / "newsletter" / ".env"]:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    key, _, val = line.partition('=')
                    if key.strip() not in os.environ:
                        os.environ[key.strip()] = val.strip()

load_env()
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')


def get_top_topics(date, min_voices=4, max_topics=4):
    """Find the most interesting topics from today's data."""
    index_path = POSTS_DIR / f'topic-index-{date}.json'
    if not index_path.exists():
        return []

    topic_index = json.loads(index_path.read_text())

    # Generic topics to skip
    SKIP = {
        'uncategorized', 'politics', 'american-politics', 'international-politics',
        'social-issues', 'culture-social', 'social-commentary', 'political-commentary',
        'general-politics', 'media-bias', 'media-criticism', 'entertainment-news',
        'conspiracy-predictions', 'religion', 'health-nutrition', 'media-platforms',
        'college-basketball', 'sports', 'nba', 'nfl', 'mma', 'fitness',
    }

    topics = []
    for topic, entries in topic_index.items():
        if topic in SKIP:
            continue
        unique_voices = {}
        for e in entries:
            vid = e['voiceId']
            if vid not in unique_voices:
                unique_voices[vid] = e
        if len(unique_voices) >= min_voices:
            topics.append({
                'topic': topic,
                'voice_count': len(unique_voices),
                'voices': unique_voices,
            })

    # Sort by voice count, take top N
    topics.sort(key=lambda x: -x['voice_count'])
    return topics[:max_topics]


def cluster_voices_for_topic(topic_name, voices_data, voices_meta):
    """Use Claude to cluster voices on a specific topic and generate insight."""
    if not ANTHROPIC_API_KEY:
        return None

    # Build voice summaries
    summaries = []
    for vid, entry in voices_data.items():
        meta = voices_meta.get(vid, {})
        quote = entry.get('quote', entry.get('text', ''))[:250]
        bio = meta.get('lens', 'commentator')
        name = entry.get('voiceName', vid)
        summaries.append(f"- {name} ({bio}): \"{quote}\"")

    voices_block = '\n'.join(summaries[:30])  # Cap at 30 voices for prompt size

    prompt = f"""Analyze how these public voices are POSITIONED on this story topic: "{topic_name.replace('-', ' ')}"

Voices and their quotes:
{voices_block}

Do THREE things:

1. CLUSTER: Group these voices into 3-5 argument clusters. Each cluster is a distinct position. Name each in 2-4 words describing the ARGUMENT (not ideology).

2. ASSIGN: Put every voice in exactly one cluster. Multiple voices MUST share clusters.

3. INSIGHT: Write ONE punchy sentence (under 15 words) that captures the most interesting fracture. Focus on unexpected splits or surprising agreements.

Good insight examples:
- "The right is split 3 ways on this"
- "Anti-war left and anti-war right find common ground"
- "Everyone agrees except the establishment"
- "Hawks vs doves crosses party lines"

Bad insight examples (too generic):
- "Voices disagree on this topic"
- "There are different perspectives"

Return ONLY this JSON:
{{
  "clusters": {{
    "cluster name": ["Voice Name 1", "Voice Name 2"],
    "cluster name 2": ["Voice Name 3", "Voice Name 4"]
  }},
  "insight": "The punchy one-liner"
}}"""

    try:
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json.dumps({
                'model': 'claude-sonnet-4-20250514',
                'max_tokens': 1024,
                'messages': [{'role': 'user', 'content': prompt}],
            }).encode(),
            headers={
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode())

        result_text = data.get('content', [{}])[0].get('text', '')
        json_match = re.search(r'\{[\s\S]*\}', result_text)
        if json_match:
            return json.loads(json_match.group())
    except Exception as e:
        print(f"  Warning: Clustering failed for {topic_name}: {e}")

    return None


def compute_fractures(date=None):
    """Compute today's top fractures and save to JSON."""
    if not date:
        date = datetime.now().strftime('%Y-%m-%d')

    print(f"\n  Computing fractures for {date}...")

    # Load voice metadata
    voices_meta = {}
    try:
        voices_list = json.loads(VOICES_PATH.read_text())
        voices_meta = {v['id']: v for v in voices_list}
    except Exception:
        pass

    # Find top topics
    top_topics = get_top_topics(date, min_voices=4, max_topics=4)
    if not top_topics:
        print("  No topics with enough voices found.")
        return

    print(f"  Found {len(top_topics)} candidate topics:")
    for t in top_topics:
        print(f"    {t['topic']}: {t['voice_count']} voices")

    fractures = []

    for topic_data in top_topics:
        topic_name = topic_data['topic']
        voices = topic_data['voices']
        print(f"\n  Clustering: {topic_name} ({len(voices)} voices)...")

        result = cluster_voices_for_topic(topic_name, voices, voices_meta)
        if not result or 'clusters' not in result:
            print(f"    Skipped (clustering failed)")
            continue

        # Build fracture object with full voice data
        cluster_list = []
        for cluster_name, voice_names in result['clusters'].items():
            cluster_voices = []
            for name in voice_names:
                # Find voice by name
                for vid, entry in voices.items():
                    entry_name = entry.get('voiceName', vid)
                    if entry_name.lower() == name.lower() or vid == name.lower().replace(' ', '-'):
                        meta = voices_meta.get(vid, {})
                        cluster_voices.append({
                            'voiceId': vid,
                            'voiceName': entry_name,
                            'photo': meta.get('photo', ''),
                            'quote': entry.get('quote', entry.get('text', ''))[:200],
                            'sourceUrl': entry.get('sourceUrl', ''),
                            'platform': entry.get('platform', ''),
                        })
                        break

            if cluster_voices:
                cluster_list.append({
                    'name': cluster_name,
                    'voices': cluster_voices,
                    'voiceCount': len(cluster_voices),
                })

        # Sort clusters by size (largest first)
        cluster_list.sort(key=lambda c: -c['voiceCount'])

        fracture = {
            'topic': topic_name,
            'topicDisplay': topic_name.replace('-', ' ').title(),
            'voiceCount': len(voices),
            'clusterCount': len(cluster_list),
            'insight': result.get('insight', ''),
            'clusters': cluster_list,
        }
        fractures.append(fracture)
        print(f"    {len(cluster_list)} clusters: {', '.join(c['name'] for c in cluster_list)}")
        print(f"    Insight: {result.get('insight', 'none')}")

    # Save
    output_path = POSTS_DIR / f'fractures-{date}.json'
    output_path.write_text(json.dumps(fractures, indent=2))
    print(f"\n  Saved {len(fractures)} fractures to {output_path}")

    return fractures


def main():
    args = sys.argv[1:]
    date = None

    if '--date' in args:
        idx = args.index('--date')
        if idx + 1 < len(args):
            date = args[idx + 1]

    compute_fractures(date)


if __name__ == '__main__':
    main()
