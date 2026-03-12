#!/usr/bin/env python3
"""
Newsreel Perspectives — Story Lookup

Given a story headline, finds matching voices from the collected database.
Uses Claude to match the story to relevant topic tags, then returns
all voices who've talked about those topics with their real quotes.

Usage:
  python scripts/lookup.py "Pentagon probe points to U.S. missile hitting Iranian school"
  python scripts/lookup.py "Epstein files released"
  python scripts/lookup.py --list-topics  # show all available topics
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
# Load env: prefer environment variable, fall back to local .env files
def load_env():
    # Check common .env locations
    for env_path in [ROOT / ".env", ROOT.parent / "newsletter" / ".env"]:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    key, _, val = line.partition('=')
                    if key.strip() not in os.environ:  # don't override existing env vars
                        os.environ[key.strip()] = val.strip()

load_env()
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')


def get_latest_topic_index():
    """Find the most recent topic index file."""
    index_files = sorted(POSTS_DIR.glob('topic-index-*.json'), reverse=True)
    if not index_files:
        return None, {}
    date = index_files[0].stem.replace('topic-index-', '')
    return date, json.loads(index_files[0].read_text())


def get_all_voice_posts(date):
    """Load all voice post files for a given date."""
    all_posts = {}
    for voice_dir in POSTS_DIR.iterdir():
        if not voice_dir.is_dir():
            continue
        post_file = voice_dir / f'{date}.json'
        if post_file.exists():
            all_posts[voice_dir.name] = json.loads(post_file.read_text())
    return all_posts


def match_story_to_topics(headline, available_topics):
    """Use Claude to match a story headline to relevant topic tags."""
    if not ANTHROPIC_API_KEY:
        # Fallback: simple keyword matching
        headline_lower = headline.lower()
        matches = []
        for topic in available_topics:
            topic_words = topic.replace('-', ' ').split()
            if any(w in headline_lower for w in topic_words):
                matches.append(topic)
        return matches

    topics_list = '\n'.join(f'  - {t}' for t in sorted(available_topics))

    prompt = f"""Given this news headline:
"{headline}"

Which of these topic tags are relevant? Include specific topics that directly relate to this story. Do NOT include vague/generic topics like "politics", "international-politics", "social-issues", "american-politics", "culture-social", "political-commentary", "religion", "entertainment-news", "media-criticism".

Available topics:
{topics_list}

Return a JSON array of matching topic strings, ordered from most specific to most broad.
Example for "Pentagon probe points to U.S. missile hitting Iranian school": ["iran-war", "military-casualties", "trump-foreign-policy", "iran-israel-conflict"]"""

    try:
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json.dumps({
                'model': 'claude-sonnet-4-20250514',
                'max_tokens': 512,
                'messages': [{'role': 'user', 'content': prompt}],
            }).encode(),
            headers={
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        result_text = data.get('content', [{}])[0].get('text', '')
        json_match = re.search(r'\[[\s\S]*?\]', result_text)
        if json_match:
            claude_topics = json.loads(json_match.group())
            # Merge with keyword fallback so we never miss obvious matches
            keyword_topics = _keyword_match(headline, available_topics)
            merged = list(dict.fromkeys(claude_topics + keyword_topics))  # dedup, preserve order
            return merged
    except Exception as e:
        print(f"  Warning: Claude matching failed ({e}), using keyword fallback")

    return _keyword_match(headline, available_topics)


def _keyword_match(headline, available_topics):
    """Simple keyword matching, filtering generic topics."""
    GENERIC_TOPICS = {
        'politics', 'american-politics', 'international-politics', 'global-politics',
        'social-issues', 'culture-social', 'social-commentary', 'political-commentary',
        'general-politics', 'general-media', 'media-bias', 'media-criticism',
        'government-tech', 'crime-media', 'religion', 'entertainment-news',
        'conspiracy-predictions', 'occult-conspiracy', 'propaganda-media',
    }
    headline_lower = headline.lower()
    headline_words = set(re.findall(r'[a-z]+', headline_lower))
    matches = []
    for topic in available_topics:
        if topic in GENERIC_TOPICS:
            continue
        topic_words = topic.replace('-', ' ').split()
        # Match if any topic word (3+ chars) appears in headline
        if any(w in headline_lower for w in topic_words if len(w) >= 3):
            matches.append(topic)
            continue
        # Also match if the topic name (without hyphens) is a substring
        topic_flat = topic.replace('-', ' ')
        if any(w in topic_flat for w in headline_words if len(w) >= 4):
            matches.append(topic)
    return matches


def fulltext_search(headline, date):
    """Search ALL post text for keywords from the headline. Returns matching voices."""
    # Extract meaningful keywords from headline (skip stop words)
    STOP_WORDS = {
        'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'shall', 'can', 'to', 'of', 'in', 'for',
        'on', 'with', 'at', 'by', 'from', 'as', 'into', 'about', 'after',
        'and', 'but', 'or', 'nor', 'not', 'so', 'yet', 'both', 'either',
        'that', 'this', 'these', 'those', 'it', 'its', 'his', 'her', 'he',
        'she', 'they', 'them', 'we', 'us', 'you', 'your', 'our', 'their',
        'what', 'which', 'who', 'whom', 'how', 'when', 'where', 'why',
        'all', 'each', 'every', 'any', 'few', 'more', 'most', 'other',
        'some', 'such', 'no', 'only', 'own', 'same', 'than', 'too', 'very',
        'just', 'says', 'said', 'new', 'also', 'back', 'even', 'still',
        'way', 'many', 'now', 'over', 'out', 'up', 'one', 'two', 'first',
        'points', 'hitting', 'get', 'gets', 'got', 'make', 'made',
    }

    words = re.findall(r'[a-z]+', headline.lower())
    keywords = [w for w in words if w not in STOP_WORDS and len(w) >= 3]

    if not keywords:
        return {}

    voices_found = {}

    for voice_dir in POSTS_DIR.iterdir():
        if not voice_dir.is_dir():
            continue
        post_file = voice_dir / f'{date}.json'
        if not post_file.exists():
            continue

        data = json.loads(post_file.read_text())
        for p in data.get('posts', []):
            text_lower = p.get('text', '').lower()
            # Count how many keywords appear in this post
            matched_words = [w for w in keywords if w in text_lower]
            # Require multiple keyword matches to reduce noise
            # For short queries (2-3 keywords), require ALL keywords present
            # For longer queries, require 50%+ overlap
            if len(keywords) <= 3:
                if len(matched_words) < len(keywords):
                    continue
            else:
                match_ratio = len(matched_words) / len(keywords)
                if match_ratio < 0.5:
                    continue

            vid = voice_dir.name
            if vid not in voices_found:
                voices_found[vid] = {
                    'voiceName': data.get('voiceName', vid),
                    'topics': [],
                    'quotes': [],
                    '_match_score': 0,
                }
            voices_found[vid]['topics'].append(p.get('topic', 'matched'))
            voices_found[vid]['quotes'].append({
                'topic': p.get('topic', 'matched'),
                'quote': p.get('quote', p['text'][:300]),
                'sourceUrl': p.get('sourceUrl', ''),
                'platform': p.get('platform', ''),
                'timestamp': p.get('timestamp', ''),
            })
            voices_found[vid]['_match_score'] += len(matched_words)

    return voices_found


def assign_argument_clusters(headline, voices_found, voices_meta):
    """Use Claude to group voices by their POSITION on this specific story.

    Instead of static left/right labels, this produces argument clusters like
    'anti-war right', 'pro-intervention', 'accountability hawks' etc.
    Returns {voiceId: cluster_label} mapping.
    """
    if not ANTHROPIC_API_KEY or not voices_found:
        return {}

    # Build a summary of each voice's quotes for Claude
    voice_summaries = []
    for vid, data in voices_found.items():
        meta = voices_meta.get(vid, {})
        quotes_text = ' | '.join(q['quote'][:200] for q in data['quotes'][:3])
        voice_summaries.append(
            f"- {data['voiceName']} (bio: {meta.get('lens', 'unknown')}): \"{quotes_text}\""
        )

    voices_block = '\n'.join(voice_summaries)

    prompt = f"""You are analyzing how different public commentators are POSITIONED on a specific news story. Your job is to identify the 4-6 major ARGUMENT CLUSTERS — groups of voices making the same core argument — and assign each voice to one.

Story: "{headline}"

Here are the voices and what they said:
{voices_block}

STEP 1: Identify exactly 4-6 argument clusters for this story. Each cluster is a distinct position or stance. Name each cluster in 2-4 words that describe the ARGUMENT (not the person).

STEP 2: Assign every voice to one of those clusters. Multiple voices MUST share clusters — that's the whole point. No voice should have a unique label.

RULES:
- Cluster names describe POSITIONS, not people: "anti-war" not "commentator", "pro-intervention" not "conservative"
- Show fractures within sides: e.g. both Tucker Carlson and Ben Shapiro are right-wing, but might be in different clusters ("anti-war right" vs "pro-intervention hawk")
- If a voice's quotes don't clearly relate to the story, put them in a cluster called "tangential"
- Aim for 4-6 clusters with 2-8 voices each

Return ONLY a JSON object mapping voice name to cluster label.
Example: {{"Tucker Carlson": "anti-war right", "Ben Shapiro": "pro-intervention hawk", "Dan Crenshaw": "pro-intervention hawk", "Jon Stewart": "anti-war left", "Pod Save America": "anti-war left"}}"""

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
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        result_text = data.get('content', [{}])[0].get('text', '')
        json_match = re.search(r'\{[\s\S]*\}', result_text)
        if json_match:
            name_to_cluster = json.loads(json_match.group())
            # Map back to voice IDs
            name_to_id = {d['voiceName']: vid for vid, d in voices_found.items()}
            clusters = {}
            for name, cluster in name_to_cluster.items():
                vid = name_to_id.get(name)
                if vid:
                    clusters[vid] = cluster
            return clusters
    except Exception as e:
        print(f"  Warning: Argument clustering failed ({e})")

    return {}


def lookup_story(headline):
    """Main lookup: find all voices talking about a story."""
    date, topic_index = get_latest_topic_index()
    if not topic_index:
        print("  No collected data found. Run: python scripts/collect.py")
        return

    print(f"\n  Searching voice database ({date})...")
    print(f"  Story: \"{headline}\"")

    # Strategy 1: Match headline to topic tags
    available_topics = list(topic_index.keys())
    matching_topics = match_story_to_topics(headline, available_topics)

    if matching_topics:
        print(f"  Matched topics: {', '.join(matching_topics)}")

    # Collect voices from topic matches
    voices_found = {}
    for topic in matching_topics:
        entries = topic_index.get(topic, [])
        for entry in entries:
            vid = entry['voiceId']
            if vid not in voices_found:
                voices_found[vid] = {
                    'voiceName': entry['voiceName'],
                    'topics': [],
                    'quotes': [],
                }
            voices_found[vid]['topics'].append(topic)
            voices_found[vid]['quotes'].append({
                'topic': topic,
                'quote': entry.get('quote', ''),
                'sourceUrl': entry.get('sourceUrl', ''),
                'platform': entry.get('platform', ''),
                'timestamp': entry.get('timestamp', ''),
            })

    # Strategy 2: Full-text search across ALL posts
    text_matches = fulltext_search(headline, date)
    for vid, data in text_matches.items():
        if vid not in voices_found:
            voices_found[vid] = data
        else:
            # Add any new quotes not already found via topic matching
            existing_urls = {q['sourceUrl'] for q in voices_found[vid]['quotes']}
            for q in data['quotes']:
                if q['sourceUrl'] not in existing_urls:
                    voices_found[vid]['quotes'].append(q)
                    voices_found[vid]['topics'].append(q['topic'])

    if not voices_found:
        print(f"\n  No voices found for these topics.")
        return

    # Score voices: more quotes + specific topic matches + text matches = better
    topic_rank = {t: i for i, t in enumerate(matching_topics)} if matching_topics else {}
    for vid, data in voices_found.items():
        best_rank = min((topic_rank.get(t, 999) for t in data['topics']), default=999)
        text_bonus = data.get('_match_score', 0) * 2  # text matches weighted heavily
        data['_score'] = -(len(data['quotes']) + text_bonus) + (best_rank * 0.1)

    # Load voice metadata for photos/lean
    voices_meta = {}
    try:
        voices_list = json.loads(VOICES_PATH.read_text())
        voices_meta = {v['id']: v for v in voices_list}
    except:
        pass

    # Assign argument clusters (per-story position labels)
    print(f"\n  Clustering voices by position...")
    clusters = assign_argument_clusters(headline, voices_found, voices_meta)

    # Display results
    print(f"\n  ╔══════════════════════════════════════════════╗")
    print(f"  ║   {len(voices_found)} VOICES ON THIS STORY{' ' * (25 - len(str(len(voices_found))))}║")
    print(f"  ╚══════════════════════════════════════════════╝")

    for vid, data in sorted(voices_found.items(), key=lambda x: x[1]['_score']):
        meta = voices_meta.get(vid, {})
        cluster = clusters.get(vid, '')
        cluster_label = f" [{cluster}]" if cluster else ''

        print(f"\n  {data['voiceName']}{cluster_label}")
        print(f"  Topics: {', '.join(set(data['topics']))}")

        for q in data['quotes'][:3]:  # Max 3 quotes per voice
            platform_icon = {'x': 'X', 'youtube': 'YT', 'bluesky': 'BS'}.get(q['platform'], q['platform'])
            quote_text = q['quote'][:200]
            print(f"    [{platform_icon}] \"{quote_text}\"")
            print(f"        {q['sourceUrl']}")

    # Also output as JSON for the viewer
    output = {
        'headline': headline,
        'date': date,
        'matchedTopics': matching_topics,
        'voices': [],
    }

    for vid, data in sorted(voices_found.items(), key=lambda x: x[1]['_score']):
        meta = voices_meta.get(vid, {})
        output['voices'].append({
            'voiceId': vid,
            'voiceName': data['voiceName'],
            'argumentCluster': clusters.get(vid, ''),
            'lean': meta.get('lean', ''),
            'lens': meta.get('lens', ''),
            'photo': meta.get('photo', ''),
            'topics': list(set(data['topics'])),
            'quotes': data['quotes'],
        })

    # Save result
    results_dir = ROOT / "data" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r'[^a-z0-9]+', '-', headline.lower())[:50]
    result_path = results_dir / f'{slug}.json'
    result_path.write_text(json.dumps(output, indent=2))
    print(f"\n  Result saved: {result_path}")

    return output


def list_topics():
    """Show all available topics with counts."""
    date, topic_index = get_latest_topic_index()
    if not topic_index:
        print("  No collected data found.")
        return

    print(f"\n  Topics from {date}:")
    print(f"  {'─' * 50}")
    for topic, entries in sorted(topic_index.items(), key=lambda x: -len(x[1])):
        names = list(set(e['voiceName'] for e in entries))[:4]
        more = f" +{len(names) - 4} more" if len(set(e['voiceName'] for e in entries)) > 4 else ""
        print(f"  [{len(entries):2d}] {topic}: {', '.join(names)}{more}")


def main():
    args = sys.argv[1:]

    if not args:
        print(__doc__)
        return

    if args[0] == '--list-topics':
        list_topics()
        return

    headline = ' '.join(args)
    lookup_story(headline)


if __name__ == '__main__':
    main()
