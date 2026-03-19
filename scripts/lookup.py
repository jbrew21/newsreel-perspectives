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
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
VOICES_PATH = ROOT / "data" / "voices.json"


# Content safety: filter triggering content from search results
CONTENT_SAFETY_TERMS = [
    'pedophil', 'child abuse', 'child porn', 'child sex', 'sexual assault on minor',
    'rape of', 'molest', 'grooming children', 'sex traffick',
]


def is_content_safe(text):
    """Check if text contains potentially triggering content that should be flagged."""
    text_lower = text.lower()
    for term in CONTENT_SAFETY_TERMS:
        if term in text_lower:
            return False
    return True


def get_voice_photo(meta, voice_name):
    """Get photo URL from voice metadata, falling back to ui-avatars only if no real photo exists."""
    photo = meta.get('photo', '') if meta else ''
    # Use the real photo if it exists and isn't already a ui-avatars fallback
    if photo and 'ui-avatars.com' not in photo:
        return photo
    # Fallback: generate a ui-avatars URL from the voice name
    encoded = urllib.parse.quote(voice_name)
    return f"https://ui-avatars.com/api/?name={encoded}&background=252528&color=a1a1aa&size=96"


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


def get_merged_topic_index(max_days=3):
    """Merge topic indices from the last N days for broader coverage."""
    index_files = sorted(POSTS_DIR.glob('topic-index-*.json'), reverse=True)[:max_days]
    if not index_files:
        return None, {}
    latest_date = index_files[0].stem.replace('topic-index-', '')

    merged = {}
    seen_urls = set()  # dedup across days
    for f in index_files:
        data = json.loads(f.read_text())
        for topic, entries in data.items():
            if topic not in merged:
                merged[topic] = []
            for e in entries:
                url = e.get('sourceUrl', '')
                if url and url in seen_urls:
                    continue
                seen_urls.add(url)
                merged[topic].append(e)

    return latest_date, merged


def get_all_dates():
    """Return sorted list of available dates (most recent first)."""
    index_files = sorted(POSTS_DIR.glob('topic-index-*.json'), reverse=True)
    return [f.stem.replace('topic-index-', '') for f in index_files]


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

Which of these topic tags are DIRECTLY relevant to this specific story? Be strict.

RULES:
- Only include topics where posts tagged with it would clearly be about THIS story
- Do NOT include generic topics (politics, social-issues, media-criticism, etc.)
- Do NOT include topics that share a keyword but are about something else (e.g. "war-on-christmas" is not about actual war, "iran-womens-soccer" is not about Iran military)
- Maximum 8 topics. Quality over quantity.

Available topics:
{topics_list}

Return a JSON array of matching topic strings, most specific first. Max 8.
Example: ["iran-war", "iran-military-strike", "trump-iran", "military-casualties"]"""

    try:
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json.dumps({
                'model': 'claude-haiku-4-5-20251001',
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
            # Trust Claude's selection — don't merge with keyword flood
            return claude_topics[:8]
    except Exception as e:
        print(f"  Warning: Claude matching failed ({e}), using keyword fallback")

    return _keyword_match(headline, available_topics)


def _keyword_match(headline, available_topics):
    """Keyword matching with synonym expansion, filtering generic topics."""
    GENERIC_TOPICS = {
        'politics', 'american-politics', 'international-politics', 'global-politics',
        'social-issues', 'culture-social', 'social-commentary', 'political-commentary',
        'general-politics', 'general-media', 'media-bias', 'media-criticism',
        'government-tech', 'crime-media', 'religion', 'entertainment-news',
        'conspiracy-predictions', 'occult-conspiracy', 'propaganda-media',
    }

    # Synonym/concept expansion: map common search terms to topic slugs
    CONCEPT_MAP = {
        'black lives matter': ['racial-justice', 'police-accountability', 'criminal-justice', 'race-politics', 'blm', 'protests', 'civil-rights'],
        'blm': ['racial-justice', 'police-accountability', 'criminal-justice', 'race-politics', 'protests', 'civil-rights'],
        'police brutality': ['police-accountability', 'criminal-justice', 'racial-justice', 'civil-rights', 'law-enforcement'],
        'police': ['police-accountability', 'criminal-justice', 'law-enforcement', 'crime'],
        'reproductive rights': ['abortion', 'reproductive-rights', 'womens-rights', 'roe-wade', 'supreme-court'],
        'abortion': ['abortion', 'reproductive-rights', 'womens-rights', 'roe-wade', 'supreme-court'],
        'voting': ['elections', 'voting-rights', 'voter-fraud', 'election-integrity', 'democracy'],
        'housing': ['housing', 'housing-policy', 'homelessness', 'affordable-housing', 'economy'],
        'education': ['education', 'education-policy', 'school-choice', 'dei-education', 'higher-education'],
        'climate': ['climate', 'climate-change', 'environment', 'energy-policy', 'green-energy'],
        'guns': ['gun-control', 'gun-rights', 'second-amendment', 'mass-shootings'],
        'gun control': ['gun-control', 'gun-rights', 'second-amendment', 'mass-shootings'],
        'healthcare': ['healthcare', 'health-policy', 'medicare', 'public-health'],
        'lgbtq': ['lgbtq', 'lgbtq-rights', 'trans-rights', 'gender-identity'],
        'trans': ['trans-rights', 'lgbtq-rights', 'gender-identity'],
        'racism': ['racial-justice', 'race-politics', 'civil-rights', 'dei'],
        'immigration': ['immigration', 'border-security', 'deportation', 'ice', 'asylum'],
        'defund the police': ['police-accountability', 'criminal-justice', 'racial-justice', 'law-enforcement', 'protests'],
        'racial profiling': ['police-accountability', 'racial-justice', 'criminal-justice', 'civil-rights'],
        'stop and frisk': ['police-accountability', 'criminal-justice', 'racial-justice', 'civil-rights'],
        'affirmative action': ['racial-justice', 'dei', 'education', 'supreme-court', 'civil-rights'],
        'critical race theory': ['education', 'racial-justice', 'culture-war', 'dei'],
        'school to prison pipeline': ['criminal-justice', 'education', 'racial-justice'],
        'mass incarceration': ['criminal-justice', 'racial-justice', 'prison-reform'],
        'voter suppression': ['voting-rights', 'elections', 'racial-justice', 'democracy'],
        'gerrymandering': ['elections', 'voting-rights', 'redistricting', 'democracy'],
        'student debt': ['education', 'economic-inequality', 'student-loans'],
        'minimum wage': ['labor', 'economic-inequality', 'economy'],
        'universal healthcare': ['healthcare', 'health-policy', 'medicare', 'progressive'],
        'book bans': ['education', 'censorship', 'culture-war', 'free-speech'],
        'dei': ['dei', 'racial-justice', 'affirmative-action', 'culture-war'],
        'reparations': ['racial-justice', 'economic-inequality', 'civil-rights'],
        'redlining': ['housing', 'racial-justice', 'economic-inequality', 'civil-rights'],
        'food desert': ['public-health', 'economic-inequality', 'racial-justice', 'urban-policy'],
        'title ix': ['education', 'gender-equity', 'womens-rights', 'sports'],
        'intersectionality': ['racial-justice', 'gender-equity', 'civil-rights', 'social-justice'],
    }

    headline_lower = headline.lower()
    headline_words = set(re.findall(r'[a-z]+', headline_lower))
    matches = []

    # Check concept map first
    for concept, related_topics in CONCEPT_MAP.items():
        if concept in headline_lower:
            for t in related_topics:
                if t in available_topics and t not in matches:
                    matches.append(t)

    for topic in available_topics:
        if topic in GENERIC_TOPICS or topic in matches:
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


def fulltext_search(headline, dates):
    """Search ALL post text for keywords from the headline across multiple dates.
    Returns matching voices with quality-ranked quotes."""
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

    if isinstance(dates, str):
        dates = [dates]

    voices_found = {}
    seen_urls = set()

    for voice_dir in POSTS_DIR.iterdir():
        if not voice_dir.is_dir():
            continue

        for date in dates:
            post_file = voice_dir / f'{date}.json'
            if not post_file.exists():
                continue

            data = json.loads(post_file.read_text())
            for p in data.get('posts', []):
                url = p.get('sourceUrl', '')
                if url in seen_urls:
                    continue

                post_text = p.get('text', '')
                # Skip content that may be triggering
                if not is_content_safe(post_text):
                    continue
                text_lower = post_text.lower()
                matched_words = [w for w in keywords if w in text_lower]

                if len(keywords) <= 2:
                    # For short queries (1-2 words), require all keywords
                    if len(matched_words) < len(keywords):
                        continue
                elif len(keywords) <= 4:
                    # For medium queries, require at least half
                    if len(matched_words) < max(1, len(keywords) // 2):
                        continue
                else:
                    match_ratio = len(matched_words) / len(keywords)
                    if match_ratio < 0.4:
                        continue

                seen_urls.add(url)
                vid = voice_dir.name
                if vid not in voices_found:
                    voices_found[vid] = {
                        'voiceName': data.get('voiceName', vid),
                        'topics': [],
                        'quotes': [],
                        '_match_score': 0,
                    }

                # Score by keyword density — how much of this quote is ABOUT the story
                quote_text = p.get('quote', p['text'][:300])
                quote_score = len(matched_words)
                # Keyword density: a 20-word tweet with 3 keyword hits > a 200-word transcript with 3
                word_count = max(len(quote_text.split()), 1)
                keyword_density = len(matched_words) / word_count
                quote_score += keyword_density * 10

                voices_found[vid]['topics'].append(p.get('topic', 'matched'))
                voices_found[vid]['quotes'].append({
                    'topic': p.get('topic', 'matched'),
                    'quote': quote_text,
                    'sourceUrl': url,
                    'platform': p.get('platform', ''),
                    'timestamp': p.get('timestamp', ''),
                    '_quote_score': quote_score,
                })
                voices_found[vid]['_match_score'] += quote_score

    # Sort quotes within each voice by quality (best first)
    for vid, data in voices_found.items():
        data['quotes'].sort(key=lambda q: -q.get('_quote_score', 0))

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
- ALWAYS include a "Media Criticism" cluster if any voices are critiquing how the story is being covered (use exactly "Media Criticism", not "Media Coverage Critique" or other variants)
- Use consistent, plain-language cluster names. Avoid jargon.

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


def lookup_story(headline, days=None):
    """Main lookup: find all voices talking about a story.

    Time strategy (Option 4 — auto-expand + user override):
    - If days is set: use exactly that window
    - If days is None: auto-expand until enough voices found
      Start with 1 day, then 3, then 7, then 14, then all
      Stop expanding when we find 5+ voices
    """
    MIN_VOICES = 5  # minimum before we stop expanding

    if days is not None:
        # User specified — use exactly that window
        time_windows = [days]
    else:
        # Auto-expand: try increasingly wider windows
        time_windows = [1, 3, 7, 14, 30]

    topic_index = None
    available_dates = []
    voices_found = {}
    time_window_used = None

    for window in time_windows:
        date, topic_index = get_merged_topic_index(max_days=window)
        if not topic_index:
            continue
        available_dates = get_all_dates()[:window]

        # Quick count: how many voices match via topic index?
        available_topics = list(topic_index.keys())
        test_topics = match_story_to_topics(headline, available_topics)
        voice_count = len(set(
            e['voiceId']
            for t in test_topics
            for e in topic_index.get(t, [])
        ))

        time_window_used = f"{window}d" if window < 30 else "all"

        if voice_count >= MIN_VOICES or window == time_windows[-1]:
            print(f"\n  Searching voice database (window: {time_window_used}, {len(available_dates)} days)...")
            print(f"  Story: \"{headline}\"")
            if len(time_windows) > 1 and window > 1:
                print(f"  Auto-expanded to {window} days ({voice_count} voices found)")
            break

    if not topic_index:
        print("  No collected data found. Run: python scripts/collect.py")
        return

    # Strategy 1: Match headline to topic tags
    available_topics = list(topic_index.keys())
    matching_topics = match_story_to_topics(headline, available_topics)

    if matching_topics:
        print(f"  Matched topics: {', '.join(matching_topics)}")

    # Collect voices from topic matches
    voices_found = {}
    seen_urls = set()
    for topic in matching_topics:
        entries = topic_index.get(topic, [])
        for entry in entries:
            url = entry.get('sourceUrl', '')
            if url in seen_urls:
                continue
            seen_urls.add(url)

            vid = entry['voiceId']
            # Content safety filter
            quote_text = entry.get('quote', '')
            if not is_content_safe(quote_text):
                continue
            if vid not in voices_found:
                voices_found[vid] = {
                    'voiceName': entry['voiceName'],
                    'topics': [],
                    'quotes': [],
                }
            # Score by how topic-specific this quote is (not by length)
            quote_score = 1

            voices_found[vid]['topics'].append(topic)
            voices_found[vid]['quotes'].append({
                'topic': topic,
                'quote': quote_text,
                'sourceUrl': url,
                'platform': entry.get('platform', ''),
                'timestamp': entry.get('timestamp', ''),
                '_quote_score': quote_score,
            })

    # Strategy 2: Full-text search across ALL posts (multiple days)
    text_matches = fulltext_search(headline, available_dates)
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

    # Sort quotes within each voice (best first) and deduplicate similar quotes
    for vid, data in voices_found.items():
        data['quotes'].sort(key=lambda q: -q.get('_quote_score', 0))
        # Deduplicate: skip quotes that share 80%+ of their words with a previous one
        seen_quote_words = []
        deduped = []
        for q in data['quotes']:
            q_words = set(re.findall(r'[a-z]+', q['quote'].lower()))
            is_dup = False
            for prev_words in seen_quote_words:
                if q_words and prev_words:
                    overlap = len(q_words & prev_words) / min(len(q_words), len(prev_words))
                    if overlap > 0.8:
                        is_dup = True
                        break
            if not is_dup:
                deduped.append(q)
                seen_quote_words.append(q_words)
        data['quotes'] = deduped

    # Score voices: keyword relevance + topic specificity (not quote count or length)
    topic_rank = {t: i for i, t in enumerate(matching_topics)} if matching_topics else {}
    for vid, data in voices_found.items():
        best_rank = min((topic_rank.get(t, 999) for t in data['topics']), default=999)
        text_bonus = data.get('_match_score', 0) * 2
        quote_quality = sum(q.get('_quote_score', 0) for q in data['quotes'][:3])
        data['_score'] = -(len(data['quotes']) + text_bonus + quote_quality) + (best_rank * 0.1)

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

    # ── Match precision detection ──
    # Check if any voice's quotes actually contain the user's specific query terms
    STOP_WORDS = {
        'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'shall', 'can', 'to', 'of', 'in', 'for',
        'on', 'with', 'at', 'by', 'from', 'as', 'into', 'about', 'after',
        'and', 'but', 'or', 'nor', 'not', 'so', 'yet', 'both', 'either',
        'that', 'this', 'these', 'those', 'it', 'its', 'his', 'her', 'he',
        'she', 'they', 'them', 'we', 'us', 'you', 'your', 'our', 'their',
        'says', 'said', 'new', 'also', 'just',
    }
    query_words = [w for w in re.findall(r'[a-z]+', headline.lower()) if w not in STOP_WORDS and len(w) >= 3]
    direct_match_count = 0
    for vid, data in voices_found.items():
        for q in data['quotes']:
            quote_lower = q['quote'].lower()
            if all(w in quote_lower for w in query_words):
                direct_match_count += 1
                break

    if direct_match_count > 0:
        match_precision = 'direct'
        broadening_note = None
    else:
        match_precision = 'broadened'
        topic_display = ', '.join(t.replace('-', ' ') for t in matching_topics[:3])
        broadening_note = f"No voices specifically mention \"{headline}\" yet, but {len(voices_found)} voices are discussing {topic_display}"

    # Also output as JSON for the viewer
    output = {
        'headline': headline,
        'date': date,
        'timeWindow': time_window_used or '14d',
        'datesSearched': available_dates,
        'matchedTopics': matching_topics,
        'matchPrecision': match_precision,
        'broadeningNote': broadening_note,
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
            'photo': get_voice_photo(meta, data['voiceName']),
            'tags': meta.get('tags', []),
            'topics': list(set(data['topics'])),
            'quotes': [{k: v for k, v in q.items() if not k.startswith('_')} for q in data['quotes']],
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

    # Parse --days flag
    days = None
    if '--days' in args:
        idx = args.index('--days')
        if idx + 1 < len(args):
            days = int(args[idx + 1])
            args = args[:idx] + args[idx + 2:]

    headline = ' '.join(args)
    lookup_story(headline, days=days)


if __name__ == '__main__':
    main()
