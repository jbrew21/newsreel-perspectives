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


def split_oversized_cluster(cluster_posts, embeddings, max_posts=40, min_voices=4):
    """Recursively split clusters that are too large using k-means."""
    from sklearn.cluster import KMeans
    import numpy as np

    if len(cluster_posts) <= max_posts:
        return [cluster_posts]

    # Split into 2-4 sub-clusters based on size
    n_splits = min(4, max(2, len(cluster_posts) // max_posts + 1))
    emb_array = np.array(embeddings)

    kmeans = KMeans(n_clusters=n_splits, random_state=42, n_init=10)
    labels = kmeans.fit_predict(emb_array)

    sub_clusters = []
    for label in range(n_splits):
        sub = [cluster_posts[i] for i in range(len(cluster_posts)) if labels[i] == label]
        sub_emb = [embeddings[i] for i in range(len(cluster_posts)) if labels[i] == label]
        unique_voices = set(p['voice_id'] for p in sub)
        if len(unique_voices) >= min_voices:
            # Recursively split if still too large
            if len(sub) > max_posts:
                sub_clusters.extend(split_oversized_cluster(sub, sub_emb, max_posts, min_voices))
            else:
                sub_clusters.append(sub)

    return sub_clusters


def cluster_posts(posts, min_voices=4, max_cluster_posts=40):
    """Use BERTopic to find natural story clusters in posts."""
    if len(posts) < 10:
        log.warning(f'Only {len(posts)} posts, too few to cluster')
        return []

    log.info(f'Embedding {len(posts)} posts...')

    from bertopic import BERTopic
    from sentence_transformers import SentenceTransformer

    # Use a small, fast model that runs on CPU
    embedding_model = SentenceTransformer('all-MiniLM-L6-v2')

    # Embed first (we need raw embeddings for splitting)
    texts = [p['text'] for p in posts]
    embeddings = embedding_model.encode(texts, show_progress_bar=False)

    # Configure BERTopic
    model = BERTopic(
        embedding_model=embedding_model,
        min_topic_size=min_voices,
        nr_topics='auto',
        verbose=False,
    )

    topics, probs = model.fit_transform(texts, embeddings)

    log.info(f'Found {len(set(topics)) - (1 if -1 in topics else 0)} initial clusters')

    # Group posts + embeddings by cluster
    raw_clusters = defaultdict(lambda: {'posts': [], 'embeddings': []})
    for i, topic_id in enumerate(topics):
        if topic_id == -1:
            continue
        raw_clusters[topic_id]['posts'].append(posts[i])
        raw_clusters[topic_id]['embeddings'].append(embeddings[i].tolist())

    # Split oversized clusters and collect final candidates
    story_candidates = []
    split_count = 0

    for topic_id, data in sorted(raw_clusters.items(), key=lambda x: -len(x[1]['posts'])):
        cluster_posts_list = data['posts']
        cluster_embeddings = data['embeddings']

        if len(cluster_posts_list) > max_cluster_posts:
            sub_clusters = split_oversized_cluster(
                cluster_posts_list, cluster_embeddings,
                max_posts=max_cluster_posts, min_voices=min_voices
            )
            split_count += 1
            log.info(f'  Split cluster ({len(cluster_posts_list)} posts) into {len(sub_clusters)} sub-stories')
        else:
            sub_clusters = [cluster_posts_list]

        for sub in sub_clusters:
            unique_voices = set(p['voice_id'] for p in sub)
            if len(unique_voices) < min_voices:
                continue

            # Get keywords from most common words
            from collections import Counter
            all_words = ' '.join(p['text'][:200].lower() for p in sub).split()
            stop = {'the','a','an','is','are','was','were','be','been','to','of','in','for',
                    'on','with','at','by','from','as','and','but','or','not','this','that',
                    'it','he','she','they','we','you','his','her','its','our','my','your',
                    'has','have','had','do','does','did','will','would','can','could','may',
                    'just','about','up','out','so','if','what','who','how','when','where',
                    'no','all','more','than','very','new','also','like','get','one','two',
                    'said','says','now','https','http','com','amp','rt'}
            word_counts = Counter(w for w in all_words if len(w) > 2 and w not in stop)
            keywords = [w for w, c in word_counts.most_common(5)]

            story_candidates.append({
                'topic_id': topic_id,
                'posts': sub,
                'voice_count': len(unique_voices),
                'post_count': len(sub),
                'keywords': keywords,
                'voices': list(unique_voices),
            })

    if split_count:
        log.info(f'Split {split_count} oversized clusters')
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

    prompt = f"""These {len(samples)} posts from different public commentators are all about the same news story.

Posts:
{samples_text}

Generate:
1. A headline (max 12 words, newspaper style, factual not editorial)
2. A one-sentence summary
3. A confidence score (1-10): how confident are you that these posts are ALL about the SAME specific story? 10 = clearly one event. 1 = posts are about different things lumped together.

If confidence is below 5, the posts are probably about a broad topic (like "Iran" or "politics") rather than a specific story. In that case, identify the MOST SPECIFIC story that the majority of posts are about and write the headline for THAT.

Return ONLY JSON: {{"headline": "...", "summary": "...", "confidence": 8}}"""

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
            headline = result.get('headline', 'Unnamed Story')
            summary = result.get('summary', '')
            confidence = result.get('confidence', 5)

            if confidence < 5:
                log.info(f'    Low confidence ({confidence}/10), headline may be imprecise: "{headline}"')

            return headline, summary
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
