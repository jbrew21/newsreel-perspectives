#!/usr/bin/env python3
"""
Newsreel Perspectives -- Embedding-Based Story Detection

Replaces topic-tag counting with real statistical clustering.
Uses sentence-transformers (free, local) + UMAP + HDBSCAN via BERTopic.

Input:  All categorized posts from today's collection
Output: Story candidates with grouped voice posts

How it works:
  1. Load all posts from today (already collected by collect.py)
  2. Embed post text using all-MiniLM-L6-v2 (free, local, no API)
  3. BERTopic clusters posts into natural story groups
  4. Filter: only keep clusters with 4+ unique voices
  5. Generate headline + summary for each story via Claude
  6. Output: stories-{date}.json ready for the clustering pipeline

No predefined taxonomy needed. New stories are discovered automatically.
A massive earthquake, a new scandal, a viral moment -- all detected
without updating any category list.

Usage:
  python scripts/detect_stories.py                    # today
  python scripts/detect_stories.py --date 2026-03-19  # specific date
  python scripts/detect_stories.py --min-voices 3     # lower threshold
"""

import json
import os
import re
import sys
import logging
import urllib.request
from pathlib import Path
from datetime import datetime
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('detect_stories')

ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / 'data' / 'posts'

# Load env for Claude API key
for env_path in [ROOT / '.env', ROOT.parent / 'newsletter' / '.env']:
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, val = line.partition('=')
                if key.strip() not in os.environ:
                    os.environ[key.strip()] = val.strip()

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# Content safety filter
SAFETY_TERMS = ['pedophil', 'child abuse', 'child porn', 'child sex', 'molest', 'sex traffick']


def is_safe(text):
    t = text.lower()
    return not any(term in t for term in SAFETY_TERMS)


def load_todays_posts(date_str):
    """Load all categorized posts from today's collection."""
    posts = []
    voices_path = ROOT / 'data' / 'voices.json'
    voice_names = {}
    if voices_path.exists():
        for v in json.loads(voices_path.read_text()):
            voice_names[v['id']] = v['name']

    for voice_dir in sorted(POSTS_DIR.iterdir()):
        if not voice_dir.is_dir():
            continue
        day_file = voice_dir / f'{date_str}.json'
        if not day_file.exists():
            continue

        try:
            data = json.loads(day_file.read_text())
        except Exception:
            continue

        voice_id = voice_dir.name
        for p in data.get('posts', []):
            text = (p.get('text') or '').strip()
            if len(text) < 30:
                continue
            if not is_safe(text):
                continue
            # Only use posts that have been categorized (have topic + stance)
            if not p.get('topic') or p['topic'] == 'uncategorized':
                continue
            if p.get('relevance') not in ('high', 'medium'):
                continue

            posts.append({
                'voice_id': voice_id,
                'voice_name': voice_names.get(voice_id, voice_id),
                'text': text[:500],
                'quote': (p.get('quote') or text)[:300],
                'platform': p.get('platform', ''),
                'source_url': p.get('sourceUrl', ''),
                'topic': p.get('topic', ''),
                'stance': p.get('stance', ''),
                'timestamp': p.get('timestamp', ''),
            })

    return posts


def cluster_posts(posts, min_voices=4):
    """Use BERTopic to find natural story clusters in posts."""
    if len(posts) < 10:
        log.warning(f'Only {len(posts)} posts, too few to cluster')
        return []

    log.info(f'Embedding {len(posts)} posts...')

    from bertopic import BERTopic
    from sentence_transformers import SentenceTransformer

    # Use a small, fast model that runs on CPU
    embedding_model = SentenceTransformer('all-MiniLM-L6-v2')

    # Configure BERTopic
    model = BERTopic(
        embedding_model=embedding_model,
        min_topic_size=min_voices,      # minimum posts per cluster
        nr_topics='auto',               # let HDBSCAN decide
        verbose=False,
    )

    texts = [p['text'] for p in posts]
    topics, probs = model.fit_transform(texts)

    log.info(f'Found {len(set(topics)) - (1 if -1 in topics else 0)} story clusters')

    # Group posts by cluster
    clusters = defaultdict(list)
    for i, topic_id in enumerate(topics):
        if topic_id == -1:
            continue  # noise / one-off posts
        clusters[topic_id].append(posts[i])

    # Filter: only keep clusters with min_voices unique voices
    story_candidates = []
    for topic_id, cluster_posts in sorted(clusters.items(), key=lambda x: -len(x[1])):
        unique_voices = set(p['voice_id'] for p in cluster_posts)
        if len(unique_voices) < min_voices:
            continue

        # Get BERTopic's auto-generated topic keywords
        topic_info = model.get_topic(topic_id)
        keywords = [word for word, score in (topic_info or [])[:5]]

        story_candidates.append({
            'topic_id': topic_id,
            'posts': cluster_posts,
            'voice_count': len(unique_voices),
            'post_count': len(cluster_posts),
            'keywords': keywords,
            'voices': list(unique_voices),
        })

    log.info(f'{len(story_candidates)} stories with {min_voices}+ voices')
    return story_candidates


def generate_headline(candidate):
    """Use Claude to generate a headline + summary from the cluster's posts."""
    if not ANTHROPIC_API_KEY:
        # Fallback: use keywords
        kw = ' '.join(candidate.get('keywords', []))
        return kw.title() or 'Unnamed Story', ''

    # Sample quotes from different voices
    seen = set()
    samples = []
    for p in candidate['posts']:
        if p['voice_id'] not in seen and len(samples) < 8:
            seen.add(p['voice_id'])
            samples.append(f"- {p['voice_name']} ({p['platform']}): \"{p['quote'][:150]}\"")

    samples_text = '\n'.join(samples)

    prompt = f"""These posts from different public commentators are all about the same news story. Generate:
1. A headline (max 12 words, newspaper style, factual not editorial)
2. A one-sentence summary of what the story is about

Posts:
{samples_text}

Return ONLY JSON: {{"headline": "...", "summary": "..."}}"""

    try:
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json.dumps({
                'model': 'claude-haiku-4-5-20251001',
                'max_tokens': 128,
                'messages': [{'role': 'user', 'content': prompt}],
            }).encode(),
            headers={
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        text = data.get('content', [{}])[0].get('text', '')
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            result = json.loads(match.group())
            return result.get('headline', 'Unnamed Story'), result.get('summary', '')
    except Exception as e:
        log.warning(f'Headline generation failed: {e}')

    kw = ' '.join(candidate.get('keywords', []))
    return kw.title() or 'Unnamed Story', ''


def build_story_candidates(date_str, min_voices=4):
    """Full pipeline: load posts, cluster, generate headlines."""
    log.info(f'Detecting stories for {date_str}')

    posts = load_todays_posts(date_str)
    log.info(f'Loaded {len(posts)} categorized posts from {len(set(p["voice_id"] for p in posts))} voices')

    if len(posts) < 10:
        log.warning('Not enough posts to detect stories')
        return []

    candidates = cluster_posts(posts, min_voices=min_voices)

    stories = []
    for c in candidates:
        headline, summary = generate_headline(c)
        log.info(f'  Story: "{headline}" ({c["voice_count"]} voices, {c["post_count"]} posts)')

        # Build voice data for the stories pipeline
        voice_data = {}
        for p in c['posts']:
            vid = p['voice_id']
            if vid not in voice_data:
                voice_data[vid] = {
                    'voiceName': p['voice_name'],
                    'quote': p['quote'],
                    'sourceUrl': p['source_url'],
                    'platform': p['platform'],
                    'text': p['text'],
                }
            # Keep the best (longest) quote per voice
            elif len(p['quote']) > len(voice_data[vid]['quote']):
                voice_data[vid]['quote'] = p['quote']
                voice_data[vid]['sourceUrl'] = p['source_url']
                voice_data[vid]['platform'] = p['platform']

        stories.append({
            'headline': headline,
            'summary': summary,
            'keywords': c['keywords'],
            'voiceCount': c['voice_count'],
            'postCount': c['post_count'],
            'voices': voice_data,
            'topicSlugs': list(set(p['topic'] for p in c['posts'] if p.get('topic'))),
        })

    # Sort by voice count (most-discussed first)
    stories.sort(key=lambda s: -s['voiceCount'])

    return stories


def main():
    args = sys.argv[1:]

    date_str = datetime.now().strftime('%Y-%m-%d')
    min_voices = 4

    for i, arg in enumerate(args):
        if arg == '--date' and i + 1 < len(args):
            date_str = args[i + 1]
        if arg == '--min-voices' and i + 1 < len(args):
            min_voices = int(args[i + 1])

    stories = build_story_candidates(date_str, min_voices=min_voices)

    if stories:
        log.info(f'\nDetected {len(stories)} stories:')
        for s in stories:
            log.info(f'  [{s["voiceCount"]} voices] {s["headline"]}')
            log.info(f'    Keywords: {", ".join(s["keywords"])}')
    else:
        log.info('No stories detected')


if __name__ == '__main__':
    main()
