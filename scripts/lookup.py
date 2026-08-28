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
TAXONOMY_PATH = ROOT / "data" / "taxonomy.json"


def load_topic_descriptions():
    """Map topic slug -> taxonomy description, so the matcher sees what a slug
    actually covers (e.g. ai-technology includes social media platforms)."""
    try:
        taxonomy = json.loads(TAXONOMY_PATH.read_text())
        return {t['slug']: t.get('description', '') for t in taxonomy.get('topics', [])}
    except (OSError, ValueError, KeyError):
        return {}

# Canonical voice-profile access layer (loading, photo/lens rules, validation).
sys.path.append(str(Path(__file__).parent))
from voices_lib import load_voices, index_by_id, voice_photo, voice_lens, VoicesError  # noqa: E402
import search_helpers  # noqa: E402  (recency timestamp helper)


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


# Promo/solicitation quotes are on-topic but useless as a voice card ("Read the
# full newsletter and consider becoming a paid subscriber..."). Penalize them so
# a substantive quote from the same voice ranks first.
PROMO_TERMS = [
    'paid subscriber', 'become a subscriber', 'consider subscribing', 'subscribe',
    'link in bio', 'link in our bio', 'full episode', 'full newsletter',
    'promo code', 'use code', 'sign up at', 'join us at', 'watch the full',
    'read the full', 'available now at', 'pre-order', 'buy my', 'my new book',
]


def is_promo_quote(text):
    """True if a quote is primarily a subscription/product solicitation."""
    text_lower = text.lower()
    return any(term in text_lower for term in PROMO_TERMS)


# Stop words for extracting the meaningful keywords from a story headline.
# Used to rank quotes by on-topic relevance and to detect match precision.
QUERY_STOP_WORDS = {
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'shall', 'can', 'to', 'of', 'in', 'for',
    'on', 'with', 'at', 'by', 'from', 'as', 'into', 'about', 'after',
    'and', 'but', 'or', 'nor', 'not', 'so', 'yet', 'both', 'either',
    'that', 'this', 'these', 'those', 'it', 'its', 'his', 'her', 'he',
    'she', 'they', 'them', 'we', 'us', 'you', 'your', 'our', 'their',
    'says', 'said', 'new', 'also', 'just', 'try', 'tries',
}


def extract_query_keywords(headline):
    """Return the meaningful (non-stop-word, 3+ char) keywords from a headline."""
    return [w for w in re.findall(r'[a-z]+', headline.lower())
            if w not in QUERY_STOP_WORDS and len(w) >= 3]


# Umbrella topics whose posts span many unrelated stories. A search may only
# match one of these when the query itself names it ("congress budget" ->
# congress-legislation). Without this guard, the topic matcher padded searches
# with adjacent umbrellas and every voice in the bucket flooded the results
# (Aug 28 2026: "immigration" matched foreign-policy-diplomacy +
# congress-legislation and returned podcast blurbs and Census posts).
BROAD_TOPICS = {
    'trump-administration', 'trump-policy', 'congress-legislation',
    'foreign-policy-diplomacy', 'economy-trade', 'supreme-court-legal',
    'elections', 'culture-war', 'media-press', 'media-personalities',
    'state-local-politics', 'uk-world-politics', 'celebrity-entertainment',
    'free-speech-censorship', 'protests-activism', 'billionaires-wealth',
}


def drop_unnamed_broad_topics(topics, headline):
    """Keep an umbrella topic only if a query word appears in its slug."""
    query_words = set(re.findall(r'[a-z]+', headline.lower()))
    return [t for t in topics
            if t not in BROAD_TOPICS or (set(t.split('-')) & query_words)]


# Words in taxonomy descriptions that describe every topic rather than one.
_GENERIC_DESC_WORDS = {
    'policy', 'policies', 'political', 'politics', 'issues', 'news',
    'related', 'major', 'general', 'american', 'national', 'coverage',
    'debates', 'actions', 'battles', 'stories',
}


def topic_bonus_terms(topics, limit=2):
    """Signature words for the top matched topics, from their slug + taxonomy
    description. Lets a quote that says "ICE arrests" score as on-topic for an
    "immigration" search even though it never uses the query word."""
    descriptions = load_topic_descriptions()
    terms = set()
    for t in topics[:limit]:
        text = t.replace('-', ' ') + ' ' + descriptions.get(t, '')
        for w in re.findall(r'[a-z]+', text.lower()):
            if w not in QUERY_STOP_WORDS and w not in _GENERIC_DESC_WORDS and len(w) >= 3:
                terms.add(w)
    return terms


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

# Single source of truth for the model id. Keeping it in one place avoids the
# stale-id-in-one-call-site bug that has bitten this pipeline before.
CLAUDE_MODEL = 'claude-haiku-4-5-20251001'


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


# Memo for topic matching. lookup_story calls match_story_to_topics twice per
# lookup (auto-expand probe + final match), and the web UI's two-phase search
# runs the whole lookup twice (fast paint, then clustered). Without this memo
# that's up to 4 identical Haiku calls per search; with it, 1.
_topic_match_memo = {}
_TOPIC_MEMO_MAX = 200


def match_story_to_topics(headline, available_topics):
    """Use Claude to match a story headline to relevant topic tags."""
    memo_key = (headline.strip().lower(), len(available_topics))
    if memo_key in _topic_match_memo:
        return _topic_match_memo[memo_key]
    result = _match_story_to_topics_uncached(headline, available_topics)
    if len(_topic_match_memo) >= _TOPIC_MEMO_MAX:
        _topic_match_memo.clear()
    _topic_match_memo[memo_key] = result
    return result


def _match_story_to_topics_uncached(headline, available_topics):
    if not ANTHROPIC_API_KEY:
        # Fallback: simple keyword matching
        headline_lower = headline.lower()
        matches = []
        for topic in available_topics:
            topic_words = topic.replace('-', ' ').split()
            if any(w in headline_lower for w in topic_words):
                matches.append(topic)
        return matches

    descriptions = load_topic_descriptions()
    topics_list = '\n'.join(
        f'  - {t} ({descriptions[t]})' if descriptions.get(t) else f'  - {t}'
        for t in sorted(available_topics)
    )

    prompt = f"""Given this news headline:
"{headline}"

Which of these topic tags are DIRECTLY relevant to this specific story? Be strict.

RULES:
- Only include topics where posts tagged with it would clearly be about THIS story
- Do NOT include generic topics (politics, social-issues, media-criticism, etc.)
- Do NOT include topics that share a keyword but are about something else (e.g. "war-on-christmas" is not about actual war, "iran-womens-soccer" is not about Iran military)
- Do NOT pad with broad umbrella topics (congress-legislation, foreign-policy-diplomacy, trump-administration, economy-trade, supreme-court-legal) unless the story is DIRECTLY about them. A search for "immigration" wants immigration posts, not everything Congress did this week.
- Maximum 8 topics. Quality over quantity.

Available topics:
{topics_list}

Topics are listed as "slug (description)". Return ONLY the slug, never the description.
Return a JSON array of matching topic strings, most specific first. Max 8.
Example: ["iran-war", "iran-military-strike", "trump-iran", "military-casualties"]"""

    try:
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json.dumps({
                'model': CLAUDE_MODEL,
                'max_tokens': 512,
                'messages': [{'role': 'user', 'content': prompt}],
            }).encode(),
            headers={
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
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
                # Sink subscription/product solicitations below any real quote
                if is_promo_quote(quote_text):
                    quote_score -= 100

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

    # Cap how many voices go to the model. On big stories (100+ voices) the
    # full roster made the prompt huge AND the response JSON overflow
    # max_tokens, so the parse failed and NO clusters shipped at all. Cluster
    # the top voices by relevance score; the tail renders uncluttered.
    MAX_CLUSTER_VOICES = 60
    ranked = sorted(voices_found.items(), key=lambda x: x[1].get('_score', 0))[:MAX_CLUSTER_VOICES]

    # Build a summary of each voice's quotes for Claude
    voice_summaries = []
    for vid, data in ranked:
        meta = voices_meta.get(vid, {})
        quotes_text = ' | '.join(q['quote'][:200] for q in data['quotes'][:3])
        voice_summaries.append(
            f"- {data['voiceName']} (bio: {voice_lens(meta, 'unknown')}): \"{quotes_text}\""
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
                # Haiku 4.5 per project config ("Perspectives: Haiku 4.5, not
                # Sonnet") — and it cuts cluster latency to ~1/4 of Sonnet's.
                # max_tokens must fit the full {name: cluster} mapping for up
                # to MAX_CLUSTER_VOICES voices; 1024 silently truncated it.
                'model': CLAUDE_MODEL,
                'max_tokens': 4000,
                'messages': [{'role': 'user', 'content': prompt}],
            }).encode(),
            headers={
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())

        result_text = data.get('content', [{}])[0].get('text', '')
        json_match = re.search(r'\{[\s\S]*\}', result_text)
        if json_match:
            name_to_cluster = json.loads(json_match.group())

            # Enforce the 4-6 cluster instruction. The model routinely sprawls
            # (17 labels for 24 voices, Aug 28 2026) and a wall of singleton
            # pills defeats the whole Ground News-style grouping. One retry
            # with explicit feedback; if it still sprawls, fold the singleton
            # clusters into "Other viewpoints" (never fold "Media Criticism"
            # or "tangential" — those labels carry meaning on their own).
            labels = set(name_to_cluster.values())
            if len(labels) > 7:
                consolidate = (
                    f"You returned {len(labels)} clusters — far too many. "
                    "Consolidate into AT MOST 6 clusters by merging similar "
                    "positions. Same JSON format, every voice assigned.\n\n"
                    f"Your previous answer:\n{json.dumps(name_to_cluster)}"
                )
                try:
                    req2 = urllib.request.Request(
                        'https://api.anthropic.com/v1/messages',
                        data=json.dumps({
                            'model': CLAUDE_MODEL,
                            'max_tokens': 4000,
                            'messages': [
                                {'role': 'user', 'content': prompt},
                                {'role': 'assistant', 'content': result_text},
                                {'role': 'user', 'content': consolidate},
                            ],
                        }).encode(),
                        headers={
                            'x-api-key': ANTHROPIC_API_KEY,
                            'anthropic-version': '2023-06-01',
                            'content-type': 'application/json',
                        },
                    )
                    with urllib.request.urlopen(req2, timeout=20) as resp2:
                        data2 = json.loads(resp2.read().decode())
                    retry_text = data2.get('content', [{}])[0].get('text', '')
                    retry_match = re.search(r'\{[\s\S]*\}', retry_text)
                    if retry_match:
                        retried = json.loads(retry_match.group())
                        # Only accept a retry that actually consolidated and
                        # still covers (most of) the same voices.
                        if (len(set(retried.values())) < len(labels)
                                and len(retried) >= len(name_to_cluster) * 0.8):
                            name_to_cluster = retried
                            labels = set(name_to_cluster.values())
                except Exception as e:
                    print(f"  Warning: cluster consolidation retry failed ({e})")

            if len(labels) > 7:
                from collections import Counter
                counts = Counter(name_to_cluster.values())
                keep = {'Media Criticism', 'tangential', 'Tangential'}
                for name, cluster in list(name_to_cluster.items()):
                    if counts[cluster] == 1 and cluster not in keep:
                        name_to_cluster[name] = 'Other viewpoints'

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


def lookup_story(headline, days=None, skip_clusters=False):
    """Main lookup: find all voices talking about a story.

    Time strategy (Option 4 — auto-expand + user override):
    - If days is set: use exactly that window
    - If days is None: auto-expand until enough voices found
      Start with 1 day, then 3, then 7, then 14, then all
      Stop expanding when we find 5+ voices

    skip_clusters=True skips the assign_argument_clusters Claude call (the
    slowest step, ~3-8s of Sonnet latency) and returns voices immediately with
    empty argumentCluster labels + clustersPending=True. The web UI uses this
    for a fast first paint, then fetches the full (clustered) result behind it.
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

    # Match topics ONCE against the widest window's topic set, then intersect
    # with each probe window's index. The old per-window match cost one Haiku
    # round-trip per expansion — a niche query that kept expanding serialized
    # up to 5 Claude calls before anything painted.
    _, widest_index = get_merged_topic_index(max_days=time_windows[-1])
    widest_topics = match_story_to_topics(headline, list(widest_index.keys())) if widest_index else []
    # Deterministic guard on top of the model's judgment: umbrella topics only
    # match when the query names them (see BROAD_TOPICS).
    widest_topics = drop_unnamed_broad_topics(widest_topics, headline)
    # Zero-match fallback: when the model declines every topic (bare umbrella
    # words like "congress" post-guard), take topics the query literally names.
    # Full-text still carries the search either way; this restores topic
    # ranking and the pinned matchedStories join.
    if not widest_topics and widest_index:
        query_words = set(re.findall(r'[a-z]+', headline.lower()))
        widest_topics = [t for t in widest_index if set(t.split('-')) & query_words]

    for window in time_windows:
        date, topic_index = get_merged_topic_index(max_days=window)
        if not topic_index:
            continue
        available_dates = get_all_dates()[:window]

        # Quick count: how many voices match via topic index?
        test_topics = [t for t in widest_topics if t in topic_index]
        voice_count = len(set(
            e['voiceId']
            for t in test_topics
            for e in topic_index.get(t, [])
        ))

        time_window_used = f"{window}d" if window < 30 else "all"

        if voice_count >= MIN_VOICES or window == time_windows[-1]:
            n_dates = len(available_dates)
            print(f"\n  Searching voice database (window: {time_window_used}, {n_dates} day{'s' if n_dates != 1 else ''})...")
            print(f"  Story: \"{headline}\"")
            if len(time_windows) > 1 and window > 1:
                print(f"  Auto-expanded to {window} days ({voice_count} voices found)")
            break

    if not topic_index:
        print("  No collected data found. Run: python scripts/collect.py")
        return

    # Staleness guard: warn loudly if the newest index is old (Apr 2026 incident:
    # quotes from an 8-week-old index nearly shipped in the newsletter).
    newest = get_all_dates()[0] if get_all_dates() else None
    if newest:
        from datetime import datetime as _dt
        age_days = (_dt.now() - _dt.strptime(newest, '%Y-%m-%d')).days
        if age_days > 2:
            print(f"\n  {'!'*60}")
            print(f"  ⚠ STALE DATA: newest topic index is {newest} ({age_days} days old).")
            print(f"  ⚠ Quotes below are NOT current. Check the GitHub Action:")
            print(f"  ⚠ https://github.com/jbrew21/newsreel-perspectives/actions")
            print(f"  ⚠ Then run: git pull origin main")
            print(f"  {'!'*60}\n")

    # Strategy 1: Match headline to topic tags (reuse the single widest-window
    # match from above — no second Claude call)
    matching_topics = [t for t in widest_topics if t in topic_index]

    if matching_topics:
        print(f"  Matched topics: {', '.join(matching_topics)}")

    # Collect voices from topic matches
    # Keywords from the headline let us rank each voice's quotes by how much
    # they're actually about THIS story, so a voice tagged with a matching
    # topic surfaces its on-topic quote rather than an unrelated post that
    # happens to share the same topic tag.
    query_keywords = extract_query_keywords(headline)
    # Signature terms of the top matched topics ("immigration" -> ice,
    # deportation, border, asylum...). Worth less than a literal query-word
    # hit, but they let genuinely on-topic quotes clear the relevance gate.
    bonus_terms = topic_bonus_terms(matching_topics) - set(query_keywords)
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
            # Score by how many of the story's keywords this quote actually
            # contains. Off-topic posts under a matching topic tag score 1 and
            # sink below genuinely on-topic quotes in the top-3 display.
            quote_lower = quote_text.lower()
            quote_score = 1 + 2 * sum(1 for w in query_keywords if w in quote_lower)
            quote_score += min(sum(1 for w in bonus_terms if w in quote_lower), 2)
            # Sink subscription/product solicitations below any real quote
            if is_promo_quote(quote_text):
                quote_score -= 100

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

    # Relevance gate (Aug 28 2026): a voice must either quote the story's own
    # words / a matched topic's signature terms (_quote_score > 1), or have a
    # post filed under the search's PRIMARY topic. Without this, a voice tagged
    # only with an adjacent topic renders posts about something else entirely.
    if query_keywords and matching_topics:
        primary = matching_topics[0]
        gated = {}
        for vid, data in voices_found.items():
            best = max((q.get('_quote_score', 0) for q in data['quotes']), default=0)
            if best > 1 or any(q.get('topic') == primary for q in data['quotes']):
                gated[vid] = data
        if len(gated) < len(voices_found):
            print(f"  Relevance gate: dropped {len(voices_found) - len(gated)} voices with no on-topic quote")
        voices_found = gated

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
        # Relevance must dominate volume. A _quote_score of 1 means "topic-adjacent but
        # mentions none of the query keywords"; >1 means the quote actually names the
        # story's terms. Rank on the single BEST quote (volume-independent) so a prolific
        # voice with many off-topic posts can't outrank a voice with one dead-on quote.
        # Raw quote count survives only as a small tiebreaker. (Fixed Jul 2 2026 — the old
        # formula summed quote counts and let off-topic high-volume accounts float to top.)
        best_quote_score = max((q.get('_quote_score', 0) for q in data['quotes']), default=0)
        on_topic_quotes = sum(1 for q in data['quotes'] if q.get('_quote_score', 0) > 1)
        volume_tiebreak = min(len(data['quotes']), 3) * 0.25
        data['_score'] = -(best_quote_score * 3 + on_topic_quotes + volume_tiebreak) + (best_rank * 0.1)

    # Load voice metadata for photos/lens. Degrade gracefully (voices still
    # render, just without photo/lens/tags) but surface the failure loudly.
    voices_meta = {}
    try:
        voices_meta = index_by_id(load_voices())
    except VoicesError as e:
        print(f"  ⚠ Could not load voice metadata: {e}")

    # Assign argument clusters (per-story position labels)
    if skip_clusters:
        clusters = {}
    else:
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
    query_words = extract_query_keywords(headline)
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
        'clustersPending': bool(skip_clusters),
        'voices': [],
    }

    for vid, data in sorted(voices_found.items(), key=lambda x: x[1]['_score']):
        meta = voices_meta.get(vid, {})
        clean_quotes = [{k: v for k, v in q.items() if not k.startswith('_')} for q in data['quotes']]
        output['voices'].append({
            'voiceId': vid,
            'voiceName': data['voiceName'],
            'argumentCluster': clusters.get(vid, ''),
            'lens': meta.get('lens', ''),
            'photo': voice_photo(meta, data['voiceName']),
            'tags': meta.get('tags', []),
            'topics': list(set(data['topics'])),
            'quotes': clean_quotes,
            # Most recent quote timestamp, so the client can offer a recency sort.
            'latestTimestamp': search_helpers.voice_latest_timestamp({'quotes': clean_quotes}).isoformat(),
        })

    # Save result — but never persist a fast-phase (unclustered) result, or the
    # pre-rendered fallback cache would serve permanently cluster-less pages.
    if not skip_clusters:
        results_dir = ROOT / "data" / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        slug = search_helpers.result_slug(headline)
        result_path = results_dir / f'{slug}.json'
        result_path.write_text(json.dumps(output, indent=2))
        print(f"\n  Result saved: {result_path}")

    return output


# ─── AI SEARCH (natural-language questions) ──────────────────────────────────
# Keyword search answers "iran strikes". AI search answers "what do Republicans
# think about the latest Iran strikes?" — it parses the question into a topic +
# an optional audience filter (a KIND of voice: medical experts, Republicans,
# economists…), retrieves voices via the normal lookup, keeps only those that
# fit the audience, and writes a grounded summary from what they actually said.

def _claude_message(prompt, max_tokens=800, model=CLAUDE_MODEL, timeout=20):
    """Single-shot Claude call returning raw text, or '' on any failure."""
    if not ANTHROPIC_API_KEY:
        return ''
    try:
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json.dumps({
                'model': model,
                'max_tokens': max_tokens,
                'messages': [{'role': 'user', 'content': prompt}],
            }).encode(),
            headers={
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        return data.get('content', [{}])[0].get('text', '') or ''
    except Exception as e:
        print(f"  Warning: Claude call failed ({e})")
        return ''


def _parse_ai_question(question):
    """NL question -> {'searchQuery': keywords, 'audience': phrase|None}."""
    prompt = f'''A user asked a search engine (over a database of public commentators' recent posts):

"{question}"

Return JSON with two fields:
1. "searchQuery": the news topic/event to search for, as 2-6 plain keywords — no question words, no filter words. Examples: "what do Republicans think about the latest Iran strikes?" -> "Iran strikes"; "have any doctors weighed in on the Hegseth testosterone testing?" -> "Hegseth testosterone military".
2. "audience": if the question restricts WHO should answer (a TYPE of person — "medical experts", "Republicans", "economists", "members of Congress"), return that short phrase; otherwise null.

Return ONLY JSON: {{"searchQuery": "...", "audience": "..." or null}}'''
    txt = _claude_message(prompt, max_tokens=200)
    m = re.search(r'\{[\s\S]*\}', txt)
    if m:
        try:
            d = json.loads(m.group())
            aud = d.get('audience')
            if isinstance(aud, str) and not aud.strip():
                aud = None
            return {'searchQuery': (d.get('searchQuery') or '').strip(), 'audience': aud}
        except Exception:
            pass
    return {'searchQuery': question, 'audience': None}


def _filter_and_summarize(question, audience, voices):
    """Select voices fitting the audience filter and write a grounded summary.

    The model returns compact 0-based INDICES into ``candidates`` (not slugs) so
    the response stays small and can't truncate mid-JSON. Returns
    {'matchedVoiceIds': [...], 'summary': str, 'audienceLabel': str|None}.
    """
    candidates = voices[:24]  # already ranked by relevance; bound prompt + output
    lines = []
    for i, v in enumerate(candidates):
        tags = ', '.join(v.get('tags', []) or [])
        quote = ((v.get('quotes') or [{}])[0].get('quote', '') or '')[:180]
        lines.append(
            f'[{i}] {v.get("voiceName")} | {(v.get("lens") or "")[:110]} | '
            f'tags: {tags} | said: "{quote}"'
        )
    block = '\n'.join(lines)
    audience_rule = (
        f'   b. AND who fit this description: "{audience}" (judge by their bio/tags). '
        f'A voice must satisfy BOTH a and b.'
        if audience else
        '   (No restriction on WHO — judge on topical relevance only.)'
    )
    prompt = f'''A user asked: "{question}"

These commentators were retrieved because they post in a related topic area. Some are
genuinely reacting to the specific story in the question; others only touch the broad
area and are not really about it. Each is numbered.
{block}

1. "matched": the INDEX NUMBERS of voices whose quote is genuinely relevant —
   a. engaging with the story/subject in the question from ANY angle (reaction,
      analysis, mockery, defense — all count). Exclude only voices whose quote is
      clearly about a DIFFERENT story. When in doubt, include. AND
{audience_rule}
   Use an empty list only if truly none fit.

2. "summary": 2-3 sentences answering the user's question using ONLY what the matched
   voices actually said. Name the split or consensus among them; never invent positions.
   If "matched" is empty, make it one sentence saying no matching voices have weighed in yet.

Return ONLY JSON (indices, not names):
{{"matched": [0, 3, 5], "summary": "..."}}'''

    for _attempt in range(2):
        txt = _claude_message(prompt, max_tokens=1200)
        m = re.search(r'\{[\s\S]*\}', txt)
        if not m:
            continue
        try:
            d = json.loads(m.group())
        except Exception:
            continue
        idxs = d.get('matched')
        if not isinstance(idxs, list):
            idxs = []
        ids = [candidates[i]['voiceId'] for i in idxs
               if isinstance(i, int) and 0 <= i < len(candidates)]
        return {
            'matchedVoiceIds': ids,
            'summary': (d.get('summary') or '').strip(),
            'audienceLabel': audience,
        }

    # Both attempts failed to parse — degrade safely: no audience -> show the
    # top relevant few; audience set -> empty (better than a mislabeled dump).
    return {
        'matchedVoiceIds': [] if audience else [v['voiceId'] for v in candidates[:8]],
        'summary': '',
        'audienceLabel': audience,
    }


def ai_search(question, days=None):
    """Natural-language search: parse the question, retrieve voices, optionally
    filter to an audience, and return a grounded summary plus the voices."""
    # AI search is meaningless without the model — parsing the question, the
    # audience filter, and the summary all require it. Fail honestly rather than
    # dumping unfiltered voices under a wrong label (e.g. Democrats for a
    # "what are Republicans saying" query).
    if not ANTHROPIC_API_KEY:
        return {
            'mode': 'ai', 'question': question,
            'error': "AI search isn't available on this server yet — the language "
                     "model key isn't configured.",
            'code': 'no_model',
        }

    parsed = _parse_ai_question(question)
    search_query = parsed['searchQuery'] or question
    audience = parsed['audience']

    # Retrieval is topic-driven, and an audience implies a domain the bare
    # searchQuery may not: "doctors on Lindsey Graham's death" parses to
    # "Lindsey Graham death", which matches political topics only — the
    # healthcare topics where the doctors live never enter the pool. Feeding
    # the audience into the retrieval query biases topic matching toward its
    # domain; the filter stage still enforces who actually fits.
    retrieval_query = f'{search_query} {audience}' if audience else search_query
    result = lookup_story(retrieval_query, days=days, skip_clusters=True) or {}
    voices = result.get('voices', [])
    base = {
        'mode': 'ai',
        'question': question,
        'searchQuery': search_query,
        'audience': audience,
        'matchedTopics': result.get('matchedTopics', []),
        'timeWindow': result.get('timeWindow'),
    }
    if not voices:
        return {**base, 'summary': None, 'voices': [], 'totalRetrieved': 0, 'noResults': True}

    fs = _filter_and_summarize(question, audience, voices)
    matched = set(fs['matchedVoiceIds'])
    filtered = [v for v in voices if v.get('voiceId') in matched]
    if not filtered and not audience:
        # No audience restriction and the model matched nothing on-topic: rather
        # than a blank result for a valid topic, fall back to the most relevant
        # retrieved voices (already ranked by relevance) so there's still a feed.
        filtered = voices[:12]

    summary = fs['summary']
    degraded = not summary  # model output unusable — the summary below is a stub
    if not summary:
        # Safety net so the UI never shows a blank answer card.
        if filtered:
            aud = f'{audience} ' if audience else ''
            summary = f'{len(filtered)} {aud}voice{"" if len(filtered) == 1 else "s"} are discussing this. See their takes below.'
        elif audience:
            summary = f'No {audience} have weighed in on this yet.'
        else:
            summary = 'No voices have weighed in on this yet.'

    return {
        **base,
        'audienceLabel': fs['audienceLabel'],
        'summary': summary,
        'degraded': degraded,
        'voices': filtered,
        'totalRetrieved': len(voices),
    }


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
