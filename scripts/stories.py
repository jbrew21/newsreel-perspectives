#!/usr/bin/env python3
"""
Newsreel Perspectives — Unified Daily Stories

Produces a single homepage feed by:
1. Pulling today's CMS editorial stories (what the newsroom picked)
2. Finding auto-detected topics from voice data (what voices are buzzing about)
3. For each, matching voice posts and clustering arguments
4. Scoring & ranking by how interesting the voice coverage is

Output: data/posts/stories-YYYY-MM-DD.json

Usage:
  python scripts/stories.py                    # today
  python scripts/stories.py --date 2026-03-13  # specific date
"""

import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
VOICES_PATH = ROOT / "data" / "voices.json"
CMS_API = "https://newsreel-cms.onrender.com/api"

# Canonical voice-profile access layer (loading, photo/lens rules, validation).
sys.path.append(str(Path(__file__).parent))
from voices_lib import load_voices, index_by_id, voice_photo, voice_lens, VoicesError  # noqa: E402


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

# Homepage story floor: on thin news days the quality gate can collapse the
# feed to 2-3 stories. Aim for at least this many by backfilling from genuine
# near-misses (real but weaker engagement) -- never from true garbage
# (no confidence / zero on-topic voices).
MIN_STORIES = 5


# Transient API failures that are worth retrying (rate limits, overload,
# gateway and timeout errors). A failed call here silently drops a story from
# the homepage feed (the Jun 29 "Validation: skipped" incident), so retry
# before giving up.
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}
MAX_API_RETRIES = 3

# Clustering model. Sonnet 5 ($2/$10 intro pricing vs 4.6's $3/$15):
# - We never send temperature/top_p/top_k (Sonnet 5 rejects non-default values).
# - Omitting 'thinking' runs adaptive thinking by default on Sonnet 5, and
#   thinking tokens count against max_tokens — disable it explicitly so the
#   tight budgets below behave as they did on 4.6.
# - Sonnet 5's tokenizer yields ~30% more tokens for the same text, so the
#   analyze/validate budgets below got headroom bumps.
CLAUDE_MODEL = 'claude-sonnet-5'

# Output budget for the per-story analyze pass (was 1024 on sonnet-4-6; the
# clusters JSON repeats up to 30 voice names, so give tokenizer headroom).
ANALYZE_MAX_TOKENS = 1536


def _claude_request_body(prompt, max_tokens):
    """Request body for /v1/messages — shared by single calls and batches."""
    return {
        'model': CLAUDE_MODEL,
        'max_tokens': max_tokens,
        'thinking': {'type': 'disabled'},
        'messages': [{'role': 'user', 'content': prompt}],
    }


def _parse_claude_message(message):
    """Extract the first {...} JSON blob from a /v1/messages message dict.

    Returns the parsed dict, or None when the response has no JSON object.
    Raises on malformed JSON (callers treat that as a transient failure).
    """
    result_text = message.get('content', [{}])[0].get('text', '')
    json_match = re.search(r'\{[\s\S]*\}', result_text)
    if json_match:
        return json.loads(json_match.group())
    return None


def call_claude(prompt, max_tokens=1024):
    """Call Claude API and return parsed JSON, retrying transient failures."""
    if not ANTHROPIC_API_KEY:
        return None

    payload = json.dumps(_claude_request_body(prompt, max_tokens)).encode()

    for attempt in range(1, MAX_API_RETRIES + 1):
        try:
            req = urllib.request.Request(
                'https://api.anthropic.com/v1/messages',
                data=payload,
                headers={
                    'x-api-key': ANTHROPIC_API_KEY,
                    'anthropic-version': '2023-06-01',
                    'content-type': 'application/json',
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
            # None = valid HTTP response but no JSON object — retrying won't
            # help. Malformed JSON raises and is retried below.
            return _parse_claude_message(data)
        except urllib.error.HTTPError as e:
            transient = e.code in RETRYABLE_STATUS
            print(f"  Claude API error (attempt {attempt}/{MAX_API_RETRIES}): HTTP {e.code}")
            if not transient or attempt == MAX_API_RETRIES:
                return None
        except Exception as e:
            # Network/timeout errors are transient — retry.
            print(f"  Claude API error (attempt {attempt}/{MAX_API_RETRIES}): {e}")
            if attempt == MAX_API_RETRIES:
                return None
        # Exponential backoff between attempts (2s, 4s) — not after the last.
        if attempt < MAX_API_RETRIES:
            time.sleep(2 ** attempt)

    return None


# Cluster name normalization: standardize synonyms
CLUSTER_NAME_MAP = {
    'media coverage critique': 'Media Criticism',
    'media critique': 'Media Criticism',
    'media accountability': 'Media Criticism',
    'press criticism': 'Media Criticism',
    'anti-media': 'Media Criticism',
    'media skepticism': 'Media Criticism',
}


def strip_quoted_tweet(text):
    """Keep only the author's own words, dropping any embedded quote-tweet.

    Mirrors collect.py so already-collected posts are cleaned at build time too:
    a quoted tweet is embedded as `<own text>\\n\\n\\n<Name> (@handle)\\n\\n<quoted>`.
    Classifying/displaying the quoted account's words was mislabeling voices.
    """
    if not text:
        return text
    t = text
    # Bare requote: the whole post is a quoted tweet (starts with "Name (@handle)").
    if re.match(r"^\s*[^\n]{1,60}\s\(@[A-Za-z0-9_]{1,20}\)", t):
        return ""
    # Otherwise cut at the quoted tweet's attribution line.
    m = re.search(r"\n\s*\n\s*[^\n]{1,60}\s\(@[A-Za-z0-9_]{1,20}\)", t)
    if m:
        t = t[:m.start()]
    t = re.sub(r'\s*—\s*https?://\S+\s*$', '', t)
    t = re.sub(r'(?:\n+\s*(?:Video|GIF|Link|Show this thread|Watch)\s*)+$', '', t, flags=re.I)
    return t.strip()


def voice_quote(entry, limit=250):
    """Author's own quote text, quote-tweet stripped, truncated to `limit`."""
    return strip_quoted_tweet(entry.get('quote', entry.get('text', '')))[:limit]


def normalize_cluster_name(name):
    """Normalize cluster name for consistency across stories."""
    low = name.strip().lower()
    if low in CLUSTER_NAME_MAP:
        return CLUSTER_NAME_MAP[low]
    # Title case
    return name.strip().title() if name == name.lower() else name.strip()
    return None


# Catch-all bucket names the model invents for voices that aren't actually about
# this story — topic-adjacent posts (same broad topic tag, different event) it
# couldn't fit into a real position. Step 3 of the analysis prompt tells it to
# mark these "unrelated" in RELEVANCE "rather than forcing it into a cluster,"
# but the output has no per-voice relevance field (only aggregate counts), so its
# only lever is to bucket them — and that bucket then renders and counts as a
# fake argument position (e.g. "Unrelated Commentary (9)" on the June CPI story).
# Drop these buckets entirely: they are not a stance anyone is taking.
TANGENTIAL_CLUSTER_RE = re.compile(
    r'\b(unrelated|tangential|off[\s-]?topic|not[\s-]?related|'
    r'no[\s-]?(clear[\s-]?)?position|miscellaneous)\b',
    re.I,
)


def is_tangential_cluster(name):
    """True if a cluster name is a catch-all for off-topic voices, not a stance."""
    return bool(TANGENTIAL_CLUSTER_RE.search(name or ''))


# Per-slug snapshot archive. A story's only addressable id is a slug derived
# from its AI-generated headline, and every rebuild regenerates that headline
# (so the slug/URL changes) AND overwrites the same-day stories-*.json. Any link
# captured before a rebuild then 404s as "may have rotated out." Snapshotting
# each built story here — keyed by the exact slug the client/serve use — keeps
# every headline variant ever shown resolvable (serve.find_story_by_slug falls
# back to this dir). Lives under data/posts/ so the pipeline commits it, in a
# subdirectory so the top-level dated-file prune leaves it intact.
STORY_ARCHIVE_DIR = POSTS_DIR / "story-archive"


def story_slug(headline):
    """Slugify a headline. MUST match serve.story_slug and story.html slugify()
    exactly, or archived snapshots won't resolve."""
    return re.sub(r'[^a-z0-9]+', '-', (headline or '').lower()).strip('-')[:60]


def carry_forward_slugs(stories, prev_stories):
    """Give each story a stable ``slug``, reusing the previous build's.

    Every rebuild regenerates headlines, so slugify(headline) changes and the
    canonical URL churns 3x/day (the archive keeps old links resolving, but
    the story's address still moves). If a prior story shares the lead topic
    and ≥40% of this story's voices, it's the same ongoing story — reuse its
    slug. Clients link story.slug first, falling back to slugify(headline).
    Mutates ``stories`` in place.
    """
    def _voice_ids(s):
        return {v.get('voiceId') for c in s.get('clusters', []) for v in c.get('voices', [])}

    taken = set()
    for s in stories:
        own = story_slug(s.get('headline', ''))
        carried = None
        lead_topic = (s.get('topicSlugs') or [''])[0]
        mine = _voice_ids(s)
        for p in prev_stories:
            if (p.get('topicSlugs') or [''])[0] != lead_topic or not mine:
                continue
            theirs = _voice_ids(p)
            if theirs and len(mine & theirs) / max(len(mine), 1) >= 0.4:
                carried = p.get('slug') or story_slug(p.get('headline', ''))
                break
        slug = carried if carried and carried not in taken else own
        if slug in taken:  # two stories collapsing to one slug — keep both reachable
            slug = own
        s['slug'] = slug
        taken.add(slug)
    return stories


# ── Step 1: Gather candidate stories ────────────────────────────

def get_cms_stories(date):
    """Pull today's editorial stories from CMS."""
    stories = []
    for endpoint in [
        f"{CMS_API}/newsreels/{date}",
        f"{CMS_API}/stories?status=published&date={date}&sort=newest&limit=10",
    ]:
        try:
            req = urllib.request.Request(endpoint)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            # newsreels endpoint wraps in { stories: [...] }
            raw = data.get('stories', [data] if isinstance(data, dict) else data)
            if isinstance(raw, list) and len(raw) > 0:
                for s in raw:
                    if isinstance(s, dict) and s.get('headline', s.get('story_headline', '')):
                        stories.append({
                            'headline': s.get('headline', s.get('story_headline', '')),
                            'subhead': s.get('subhead', ''),
                            'cover_url': s.get('cover_url', ''),
                            'story_type': s.get('story_type', s.get('type', '')),
                            'source': 'cms',
                        })
                if stories:
                    break
        except Exception as e:
            print(f"  CMS fetch failed ({endpoint}): {e}")
            continue
    return stories


def get_voice_topics(date, min_voices=4, max_topics=8):
    """Find topics with enough voice coverage from recent data (48h window)."""
    index_path = POSTS_DIR / f'topic-index-{date}.json'
    if not index_path.exists():
        return {}, {}

    topic_index = json.loads(index_path.read_text())

    # Time-scope: only include posts from last 48 hours
    try:
        cutoff = datetime.strptime(date, '%Y-%m-%d') - timedelta(hours=48)
    except:
        cutoff = datetime.now() - timedelta(hours=48)

    SKIP = {'uncategorized', 'other'}

    topics = {}
    filtered_index = {}
    for topic, entries in topic_index.items():
        if topic in SKIP:
            continue

        # Filter to recent posts only
        recent = []
        for e in entries:
            ts = e.get('timestamp', '')
            if ts:
                try:
                    # Handle various timestamp formats
                    post_time = datetime.fromisoformat(ts.replace('Z', '+00:00').replace('+00:00', ''))
                    if post_time.replace(tzinfo=None) < cutoff:
                        continue
                except:
                    pass  # Keep posts with unparseable timestamps
            recent.append(e)

        if not recent:
            continue

        filtered_index[topic] = recent

        unique_voices = {}
        for e in recent:
            vid = e['voiceId']
            if vid not in unique_voices:
                unique_voices[vid] = e
        if len(unique_voices) >= min_voices:
            topics[topic] = unique_voices

    return topics, filtered_index


# ── Step 2: Match CMS stories to voice topics ──────────────────

def match_cms_to_voices(cms_stories, voice_topics):
    """Use Claude to match CMS headlines to voice topic slugs."""
    if not cms_stories or not voice_topics:
        return {}

    headlines = [s['headline'] for s in cms_stories]
    topic_list = list(voice_topics.keys())

    prompt = f"""Match these news headlines to the most relevant topic slugs.

Headlines:
{json.dumps(headlines, indent=2)}

Available topic slugs:
{json.dumps(topic_list, indent=2)}

For each headline, return the 1-3 most relevant topic slugs (or empty array if none match).

Return ONLY this JSON:
{{
  "matches": {{
    "headline text": ["topic-slug-1", "topic-slug-2"],
    "headline text 2": []
  }}
}}"""

    result = call_claude(prompt, max_tokens=1024)
    if result and 'matches' in result:
        return result['matches']
    return {}


# ── Step 3: Analyze voices on a story ───────────────────────────

def build_analysis_prompt(headline, voices_data, voices_meta):
    """Build the clustering prompt for one story (shared by call + batch)."""
    summaries = []
    for vid, entry in voices_data.items():
        meta = voices_meta.get(vid, {})
        quote = voice_quote(entry, 250)
        bio = voice_lens(meta)
        name = entry.get('voiceName', vid)
        platform = entry.get('platform', '')
        # Flag YouTube title-only posts so Claude knows they lack opinion text
        is_title_only = (platform == 'youtube' and len(quote) < 100
                         and not any(c in quote for c in '.!?'))
        if is_title_only:
            summaries.append(f"- {name} ({bio}): [VIDEO TITLE: \"{quote}\"] (covering this topic, but no direct quote available)")
        else:
            summaries.append(f"- {name} ({bio}): \"{quote}\"")

    voices_block = '\n'.join(summaries[:30])

    prompt = f"""Analyze what these public voices are saying about this story: "{headline}"

Voices and their quotes:
{voices_block}

Each quote is the voice's OWN words (an embedded quote-tweet or article they linked has been removed). Judge each voice ONLY by what they themselves said, never by a topic you infer from context that isn't in their quote.

Do these things:

1. HEADLINE: Write a short, specific news headline (under 12 words) summarizing what is actually happening. Ground the reader in the current story.

2. CLUSTER: Group these voices into 2-5 argument clusters. Each cluster is a distinct position or reaction. Name each in 2-4 words describing the ARGUMENT (not ideology). If there's no real split, use descriptive groupings like "Cautious Support" or "Demanding Action."
   CRITICAL: Name each cluster using language its MEMBERS would use to describe themselves, not language their opponents would use. "Deterrence Advocates" not "War Hawks". "Abortion Rights Defenders" not "Baby Killers". "Immigration Enforcement" not "Xenophobes". Always use neutral-to-sympathetic framing for every cluster.

3. ASSIGN: Put every voice in exactly one cluster. For voices marked [VIDEO TITLE], you can still assign them based on who they are and what the title suggests, but weight voices with actual quotes more heavily when determining cluster names and the summary. If a voice's quote states no clear position on THIS story (a bare reaction like "Thoughts?" or "Disgusting.", or an unrelated topic), mark it "unrelated" in RELEVANCE rather than forcing it into a cluster.

4. SUMMARY: Write ONE sentence (under 20 words) capturing the most interesting thing about how voices are reacting. This could be:
   - A surprising split: "Left and right unite against the bill"
   - A consensus: "Rare agreement across the spectrum"
   - An interesting reaction: "12 voices weigh in, most demanding accountability"
   Don't force a "split" framing if there isn't one. Just describe what's happening.

5. TYPE: Classify as one of: "split" (clear opposing camps), "spectrum" (range of views), "consensus" (broad agreement), "reaction" (mostly one-directional response)

6. RELEVANCE: For EACH voice, rate whether their quote is actually about this specific story:
   - "direct" = clearly discussing this exact story/event
   - "related" = discussing the broader topic but not this specific story
   - "unrelated" = not relevant at all
   Count how many are "direct" vs total. This is critical for data quality.

7. CONFIDENCE: Rate 1-10 how confident you are that these voices are genuinely reacting to the SAME story (not just the same broad topic). 1 = voices are scattered across unrelated topics. 10 = every voice is clearly discussing the same event.

Return ONLY this JSON:
{{
  "headline": "The specific news headline",
  "clusters": {{
    "cluster name": ["Voice Name 1", "Voice Name 2"],
    "cluster name 2": ["Voice Name 3"]
  }},
  "summary": "The one-liner about the conversation",
  "type": "split|spectrum|consensus|reaction",
  "relevance": {{"direct": 8, "related": 12, "unrelated": 3}},
  "confidence": 7
}}"""

    return prompt


def analyze_voices(headline, voices_data, voices_meta):
    """Cluster voices and generate insight for a story."""
    prompt = build_analysis_prompt(headline, voices_data, voices_meta)
    return call_claude(prompt, max_tokens=ANALYZE_MAX_TOKENS)


# ── Message Batches: run the independent per-story analyze calls in one
# batch at 50% token cost. Every input to analyze_voices is known before the
# candidate loop starts, so the calls have no cross-story dependency. Any
# batch-level failure, timeout, or per-story errored/missing result falls
# back to the existing sequential call_claude path for that story.
BATCHES_URL = 'https://api.anthropic.com/v1/messages/batches'
BATCH_POLL_SECONDS = 15
BATCH_TIMEOUT_SECONDS = 900  # hard cap, well under the nightly Action limit


def _batch_api(url, payload=None, method='GET'):
    """Raw HTTP call against the Message Batches endpoints. Returns body text."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read().decode()


def batch_analyze_voices(candidates, voices_meta):
    """Analyze all candidates via the Message Batches API.

    Returns {candidate_index: parsed_result} for the stories that succeeded.
    Indices that are missing (errored/expired/canceled result, unparseable
    output, or any batch-level failure) are handled by the caller with the
    sequential analyze_voices call — identical failure semantics, lower cost.
    """
    if not ANTHROPIC_API_KEY or len(candidates) < 2:
        return {}

    requests_payload = [
        {
            'custom_id': f'analyze-{i}',
            'params': _claude_request_body(
                build_analysis_prompt(c['headline'], c['voices'], voices_meta),
                ANALYZE_MAX_TOKENS,
            ),
        }
        for i, c in enumerate(candidates)
    ]

    try:
        batch = json.loads(_batch_api(
            BATCHES_URL, payload={'requests': requests_payload}, method='POST'))
        batch_id = batch['id']
    except Exception as e:
        print(f"  Batch create failed ({e}) — falling back to sequential calls")
        return {}

    print(f"  Batch {batch_id}: analyzing {len(candidates)} candidates...")

    ended = None
    deadline = time.time() + BATCH_TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(BATCH_POLL_SECONDS)
        try:
            snapshot = json.loads(_batch_api(f'{BATCHES_URL}/{batch_id}'))
        except Exception as e:
            print(f"  Batch poll error ({e}) — retrying")
            continue
        if snapshot.get('processing_status') == 'ended':
            ended = snapshot
            break

    if ended is None:
        print(f"  Batch {batch_id} not done after {BATCH_TIMEOUT_SECONDS}s — "
              f"canceling, falling back to sequential calls")
        try:
            _batch_api(f'{BATCHES_URL}/{batch_id}/cancel', payload={}, method='POST')
        except Exception:
            pass
        return {}

    results_url = ended.get('results_url')
    if not results_url:
        print("  Batch ended without results_url — falling back to sequential calls")
        return {}

    try:
        lines = _batch_api(results_url).splitlines()
    except Exception as e:
        print(f"  Batch results fetch failed ({e}) — falling back to sequential calls")
        return {}

    # Results arrive UNORDERED — key strictly by custom_id, never by position.
    results = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get('result', {}).get('type') != 'succeeded':
                continue
            idx = int(entry['custom_id'].rsplit('-', 1)[-1])
            parsed = _parse_claude_message(entry['result']['message'])
            if parsed is not None:
                results[idx] = parsed
        except Exception:
            continue  # malformed entry -> sequential fallback for that story

    print(f"  Batch complete: {len(results)}/{len(candidates)} analyses succeeded")
    return results


def validate_clusters(headline, clusters, voices_data, voices_meta):
    """Second-pass validation: rate how well each voice fits its assigned cluster."""
    # Build voice-cluster pairs with quotes
    assignments = []
    for cluster_name, voice_names in clusters.items():
        for name in voice_names:
            quote = ''
            for vid, entry in voices_data.items():
                entry_name = entry.get('voiceName', vid)
                if entry_name.lower() == name.lower() or vid == name.lower().replace(' ', '-'):
                    quote = voice_quote(entry, 250)
                    break
            assignments.append(f'- {name} -> cluster "{cluster_name}": "{quote}"')

    assignments_block = '\n'.join(assignments)

    prompt = f"""Here are voice quotes assigned to argument clusters about this story: "{headline}"

For each voice, rate how well their quote actually supports their cluster assignment.
A voice saying "this is terrible" assigned to "Supporters" would be a 1.
A voice clearly arguing the cluster's position would be a 10.

Assignments:
{assignments_block}

For each voice, return a fit score (1-10) and a one-line reason.

Return ONLY this JSON:
{{
  "validations": [
    {{"voice": "Voice Name", "cluster": "cluster name", "fit": 7, "reason": "quote clearly argues this position"}}
  ]
}}"""

    # Was 1024 on sonnet-4-6: up to 30 validation entries with per-voice
    # reasons could already brush that limit, and Sonnet 5's tokenizer yields
    # ~30% more tokens for the same text.
    return call_claude(prompt, max_tokens=2048)


def update_cluster_history(stories, date):
    """Append voice-cluster assignments to cluster-history.json for temporal tracking."""
    history_path = ROOT / "data" / "cluster-history.json"

    # Load existing history
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text())
        except Exception:
            history = {}
    else:
        history = {}

    for story in stories:
        headline = story.get('headline', '')
        topic_slugs = story.get('topicSlugs', [])
        topic_slug = topic_slugs[0] if topic_slugs else 'unknown'

        for cluster in story.get('clusters', []):
            cluster_name = cluster.get('name', '')
            for voice in cluster.get('voices', []):
                voice_id = voice.get('voiceId', '')
                if not voice_id:
                    continue

                if voice_id not in history:
                    history[voice_id] = {}
                if topic_slug not in history[voice_id]:
                    history[voice_id][topic_slug] = []

                # Find fit score from validation data if available
                fit = voice.get('fit', None)

                entry = {
                    'date': date,
                    'cluster': cluster_name,
                    'headline': headline,
                }
                if fit is not None:
                    entry['fit'] = fit

                history[voice_id][topic_slug].append(entry)

    history_path.write_text(json.dumps(history, indent=2))
    print(f"  Updated cluster history: {history_path}")


# ── Step 4: Build the unified feed ──────────────────────────────

def build_stories(date=None):
    """Build the unified daily stories feed."""
    if not date:
        date = datetime.now().strftime('%Y-%m-%d')

    print(f"\n  Building stories feed for {date}...")

    # Load voice metadata
    voices_meta = {}
    try:
        voices_meta = index_by_id(load_voices())
    except VoicesError as e:
        print(f"  ⚠ Could not load voice metadata: {e}")

    # 1. Try embedding-based story detection (enterprise), fall back to topic counting
    cms_stories = get_cms_stories(date)
    print(f"  CMS stories: {len(cms_stories)}")

    embedding_candidates = []
    try:
        from detect_stories import build_story_candidates
        print(f"\n  Using BERTopic embedding-based story detection...")
        embedding_stories = build_story_candidates(date, min_voices=4)
        for es in embedding_stories:
            embedding_candidates.append({
                'headline': es['headline'],
                'cover_url': '',
                'story_type': '',
                'source': 'voices',
                'topic_slugs': es.get('topicSlugs', []),
                'voices': es['voices'],
                'voice_count': es['voiceCount'],
            })
        print(f"  BERTopic detected {len(embedding_candidates)} stories")
    except ImportError:
        print(f"  BERTopic not installed, using topic-tag fallback")
    except Exception as e:
        print(f"  BERTopic failed ({e}), using topic-tag fallback")

    # Fallback: topic counting (if BERTopic didn't find enough)
    voice_topics, topic_index = get_voice_topics(date, min_voices=4)
    print(f"  Voice topics (4+ voices): {len(voice_topics)}")

    if not voice_topics and not embedding_candidates:
        print("  No stories found. Exiting.")
        return

    # Build candidate list
    candidates = list(embedding_candidates)  # start with embedding results
    used_headlines = {c['headline'] for c in candidates}

    # Add CMS stories matched to voice topics
    BROAD_TOPICS = {'other', 'culture-war', 'media-press', 'celebrity-entertainment'}
    cms_matches = {}
    if cms_stories and voice_topics:
        cms_matches = match_cms_to_voices(cms_stories, voice_topics)

    used_topics = set()
    for story in cms_stories:
        if story['headline'] in used_headlines:
            continue
        matched_topics = cms_matches.get(story['headline'], [])
        matched_topics = [t for t in matched_topics if t not in BROAD_TOPICS]
        if not matched_topics:
            continue

        merged_voices = {}
        for topic_slug in matched_topics:
            if topic_slug in voice_topics:
                merged_voices.update(voice_topics[topic_slug])
                used_topics.add(topic_slug)

        if len(merged_voices) >= 3:
            candidates.append({
                'headline': story['headline'],
                'cover_url': story.get('cover_url', ''),
                'story_type': story.get('story_type', ''),
                'source': 'editorial',
                'topic_slugs': matched_topics,
                'voices': merged_voices,
                'voice_count': len(merged_voices),
            })
            used_headlines.add(story['headline'])

    # Add voice-only topics not covered by embedding or CMS
    if not embedding_candidates:
        VOICE_SKIP = BROAD_TOPICS | {'sports', 'education', 'healthcare'}
        for topic, voices in voice_topics.items():
            if topic in used_topics or topic in VOICE_SKIP:
                continue
            candidates.append({
                'headline': topic.replace('-', ' ').title(),
                'cover_url': '',
                'story_type': '',
                'source': 'voices',
                'topic_slugs': [topic],
                'voices': voices,
                'voice_count': len(voices),
                })

    # Sort by voice count, take top 14
    candidates.sort(key=lambda c: -c['voice_count'])
    candidates = candidates[:14]

    print(f"\n  Analyzing {len(candidates)} candidates:")
    for c in candidates:
        src = '[CMS]' if c['source'] == 'editorial' else '[voices]'
        print(f"    {src} {c['headline'][:60]} ({c['voice_count']} voices)")

    # 4. Analyze each candidate. The independent analyze calls run as one
    # Message Batch (50% token cost); any story missing from the batch result
    # falls back to the original sequential call.
    batch_results = batch_analyze_voices(candidates, voices_meta)

    stories = []
    for idx, candidate in enumerate(candidates):
        print(f"\n  Clustering: {candidate['headline'][:50]}... ({candidate['voice_count']} voices)")

        result = batch_results.get(idx)
        if result is None:
            result = analyze_voices(candidate['headline'], candidate['voices'], voices_meta)
        if not result or 'clusters' not in result:
            print(f"    Skipped (analysis failed)")
            continue

        # Quality gate: drop stories where voices aren't actually about this story
        confidence = result.get('confidence', 5)
        relevance = result.get('relevance', {})
        direct = relevance.get('direct', 0)
        related = relevance.get('related', 0)
        unrelated = relevance.get('unrelated', 0)
        total_rated = direct + related + unrelated or 1
        direct_pct = direct / total_rated

        print(f"    Quality: confidence={confidence}/10, direct={direct}/{total_rated} ({direct_pct:.0%})")

        # Tiered gate (instead of a hard drop):
        #  - PASS: clears the original quality bar -> always shown.
        #  - BACKFILL: real but weaker engagement -> shown only to reach
        #    MIN_STORIES on thin days, and flagged belowThreshold.
        #  - REJECT: no confidence or zero on-topic voices -> never shown.
        passes_gate = confidence > 3 and (direct_pct >= 0.2 or direct >= 3)
        backfill_ok = (not passes_gate) and confidence >= 2 and direct >= 2

        if not passes_gate and not backfill_ok:
            print(f"    DROPPED: no genuine debate (confidence {confidence}/10, {direct} direct)")
            continue
        if not passes_gate:
            print(f"    BACKFILL-ELIGIBLE: below bar (confidence {confidence}/10, {direct} direct) — used only to reach floor")

        # Validation pass: check how well each voice fits its cluster
        validation_result = validate_clusters(
            candidate['headline'], result['clusters'],
            candidate['voices'], voices_meta
        )
        fit_scores = {}  # (voice_name_lower, cluster_name) -> {fit, reason}
        if validation_result and 'validations' in validation_result:
            for v in validation_result['validations']:
                key = (v.get('voice', '').lower(), v.get('cluster', ''))
                fit_scores[key] = {'fit': v.get('fit', 5), 'reason': v.get('reason', '')}
            low_fit = [v for v in validation_result['validations'] if v.get('fit', 5) < 4]
            if low_fit:
                print(f"    Validation: {len(low_fit)} voices dropped (fit < 4)")
                for v in low_fit:
                    print(f"      - {v.get('voice')} in '{v.get('cluster')}' (fit={v.get('fit')}): {v.get('reason', '')}")
        else:
            print(f"    Validation: skipped (API call failed)")

        # Remove low-fit voices from clusters before building output
        validated_clusters = {}
        for cluster_name, voice_names in result['clusters'].items():
            kept = []
            for name in voice_names:
                key = (name.lower(), cluster_name)
                score = fit_scores.get(key, {}).get('fit', 5)
                if score >= 4:
                    kept.append(name)
            if kept:
                validated_clusters[cluster_name] = kept
        result['clusters'] = validated_clusters

        # Build cluster objects with full voice data
        cluster_list = []
        for cluster_name, voice_names in result['clusters'].items():
            # Skip the model's off-topic catch-all bucket. These voices matched
            # the story's broad topic tag but their posts are about a different
            # event; showing them as a "position" is misleading (see
            # is_tangential_cluster).
            if is_tangential_cluster(cluster_name):
                print(f"    Dropped off-topic bucket '{cluster_name}' ({len(voice_names)} voices) — not a position")
                continue
            cluster_voices = []
            for name in voice_names:
                for vid, entry in candidate['voices'].items():
                    entry_name = entry.get('voiceName', vid)
                    if entry_name.lower() == name.lower() or vid == name.lower().replace(' ', '-'):
                        meta = voices_meta.get(vid, {})
                        q = voice_quote(entry, 200)
                        # A bare requote (quoted tweet only, no author comment) strips to
                        # empty — don't show a voice with none of its own words.
                        if not q:
                            break
                        voice_obj = {
                            'voiceId': vid,
                            'voiceName': entry_name,
                            'photo': voice_photo(meta, entry_name),
                            'quote': q,
                            'sourceUrl': entry.get('sourceUrl', ''),
                            'platform': entry.get('platform', ''),
                        }
                        # Attach fit score from validation
                        key = (name.lower(), cluster_name)
                        if key in fit_scores:
                            voice_obj['fit'] = fit_scores[key]['fit']
                        cluster_voices.append(voice_obj)
                        break

            if cluster_voices:
                cluster_list.append({
                    'name': normalize_cluster_name(cluster_name),
                    'voices': cluster_voices,
                    'voiceCount': len(cluster_voices),
                })

        cluster_list.sort(key=lambda c: -c['voiceCount'])

        # Guard: if validation stripped every voice from every cluster, there is
        # no story to show. Drop it — never append a 0-cluster story, which the
        # floor could otherwise backfill onto the homepage as "0 positions".
        if not cluster_list:
            print(f"    DROPPED: all voices failed cluster validation (fit < 4)")
            continue

        # ── Apply editorial overrides from review dashboard ──
        overrides_path = ROOT / "data" / "editorial-overrides.json"
        if overrides_path.exists():
            try:
                overrides = json.loads(overrides_path.read_text())
                # Overrides keyed by headline -> {old_name: new_name}
                story_overrides = overrides.get(candidate['headline'], {})
                if story_overrides:
                    for cluster in cluster_list:
                        if cluster['name'] in story_overrides:
                            old_name = cluster['name']
                            cluster['name'] = story_overrides[old_name]
                            cluster['overridden'] = True
                            print(f"    Editorial override: '{old_name}' -> '{cluster['name']}'")
            except Exception:
                pass

        # ── Quote Quality Ranking ──
        # Rank voices within each cluster by quote quality tier
        PLATFORM_QUALITY = {
            'substack': 5,   # Long-form written opinion
            'bluesky': 4,    # Written post with stance
            'x': 3,          # Tweet — short but direct
            'instagram': 2,  # Caption, often visual-first
            'youtube': 1,    # Often just video title
            'tiktok': 1,     # Often just video title
            'podcast': 4,    # Transcript excerpt
        }
        for cluster in cluster_list:
            for voice in cluster['voices']:
                plat = voice.get('platform', '').lower()
                quote = voice.get('quote', '')
                # Base score from platform
                tier = PLATFORM_QUALITY.get(plat, 2)
                # Boost for longer, more substantive quotes
                if len(quote) > 100:
                    tier += 1
                # Boost for quotes with clear opinion markers
                if any(w in quote.lower() for w in ['because', 'should', 'must', 'wrong', 'right', 'dangerous', 'important']):
                    tier += 1
                voice['quoteQuality'] = min(tier, 7)
            # Sort voices by quality (best first)
            cluster['voices'].sort(key=lambda v: -v.get('quoteQuality', 0))
            # Surface best quote for the cluster
            if cluster['voices']:
                best = cluster['voices'][0]
                cluster['bestQuote'] = {
                    'voiceName': best['voiceName'],
                    'quote': best['quote'],
                    'platform': best['platform'],
                    'quality': best.get('quoteQuality', 0),
                }

        # ── Counter-Narrative Detection (semantic) ──
        # Find the two clusters most in tension, not just largest vs smallest
        counter_narrative = None
        if len(cluster_list) >= 2:
            cluster_names = [c['name'] for c in cluster_list]
            cluster_sizes = {c['name']: c['voiceCount'] for c in cluster_list}
            tension_prompt = f"""Given these argument clusters about "{result.get('headline', candidate['headline'])}":
{json.dumps(cluster_names)}

Which two clusters are most directly in TENSION or OPPOSITION to each other?
Not just different topics — actual disagreement on the same question.

Return ONLY this JSON:
{{"clusterA": "name", "clusterB": "name", "axis": "what they disagree about in 3-5 words", "tension": 8}}
tension = 1-10 how opposed they are (1=tangential, 10=direct opposition)
If no real tension exists, return {{"tension": 0}}"""

            tension_result = call_claude(tension_prompt, max_tokens=256)
            if tension_result and tension_result.get('tension', 0) >= 5:
                a_name = tension_result.get('clusterA', '')
                b_name = tension_result.get('clusterB', '')
                a_count = cluster_sizes.get(a_name, 0)
                b_count = cluster_sizes.get(b_name, 0)
                # Dominant = larger cluster, counter = smaller
                if a_count >= b_count:
                    dom_name, dom_count = a_name, a_count
                    ctr_name, ctr_count = b_name, b_count
                else:
                    dom_name, dom_count = b_name, b_count
                    ctr_name, ctr_count = a_name, a_count
                if ctr_count >= 2:
                    counter_narrative = {
                        'dominantCluster': dom_name,
                        'dominantCount': dom_count,
                        'counterCluster': ctr_name,
                        'counterCount': ctr_count,
                        'axis': tension_result.get('axis', ''),
                        'tensionScore': tension_result.get('tension', 0),
                        'tension': f"{dom_count} voices say \"{dom_name}\" — but {ctr_count} push back: \"{ctr_name}\"",
                    }

        # ── Story Heat Score ──
        # Composite: voice density + disagreement + cross-pollination + directness + confidence
        total_voices = sum(c['voiceCount'] for c in cluster_list)

        # Log scale for voice count (differentiates 10 vs 50 voices)
        voice_score = min(math.log(total_voices + 1) / math.log(50), 1.0)

        # Shannon entropy of cluster sizes (higher = more disagreement)
        if total_voices > 0 and len(cluster_list) > 1:
            proportions = [c['voiceCount'] / total_voices for c in cluster_list]
            entropy = -sum(p * math.log2(p) for p in proportions if p > 0)
            max_entropy = math.log2(len(cluster_list))
            disagreement = entropy / max_entropy if max_entropy > 0 else 0
        else:
            disagreement = 0

        # Ideological cross-pollination: do voices with opposing tags share a cluster?
        cross_score = 0.0
        if voices_meta:
            for cluster in cluster_list:
                cluster_tags = set()
                for voice in cluster.get('voices', []):
                    vid = voice.get('voiceId', '')
                    meta = voices_meta.get(vid, {})
                    for tag in meta.get('tags', []):
                        cluster_tags.add(tag.lower())
                # Check for ideological opposites in same cluster
                opposites = [
                    ({'conservative', 'right-leaning', 'maga', 'republican'}, {'progressive', 'left-leaning', 'democrat', 'liberal'}),
                    ({'pro-trump', 'trump-supporter'}, {'anti-trump', 'trump-critic'}),
                    ({'libertarian', 'libertarian-leaning'}, {'socialist', 'democratic-socialist'}),
                ]
                for set_a, set_b in opposites:
                    if cluster_tags & set_a and cluster_tags & set_b:
                        cross_score = 1.0  # Strange bedfellows found
                        break
                if cross_score > 0:
                    break

        directness_score = direct_pct  # from relevance check above
        conf_factor = min(confidence / 10.0, 1.0)

        heat_score = round(
            (voice_score * 0.25 + disagreement * 0.25 + cross_score * 0.2 + directness_score * 0.15 + conf_factor * 0.15) * 100
        )

        story = {
            'headline': result.get('headline', candidate['headline']),
            'summary': result.get('summary', ''),
            'type': result.get('type', 'spectrum'),
            'source': candidate['source'],
            'coverUrl': candidate.get('cover_url', ''),
            'storyType': candidate.get('story_type', ''),
            'topicSlugs': candidate['topic_slugs'],
            # Count only voices shown in a real position — total_voices is the
            # sum of the surviving clusters (after validation + off-topic drop),
            # so the "N voices" header matches what the position cards add up to.
            'voiceCount': total_voices,
            'clusterCount': len(cluster_list),
            'clusters': cluster_list,
            'confidence': confidence,
            'relevance': relevance,
            'validated': bool(validation_result and 'validations' in validation_result),
            'heatScore': heat_score,
            'belowThreshold': not passes_gate,
            '_directPct': round(direct_pct, 3),
        }
        if counter_narrative:
            story['counterNarrative'] = counter_narrative

        stories.append(story)
        print(f"    [{result.get('type', '?')}] {len(cluster_list)} clusters: {', '.join(c['name'] for c in cluster_list)}")
        print(f"    Heat: {heat_score}/100 | {result.get('summary', '')}")
        if counter_narrative:
            print(f"    Counter: {counter_narrative['tension']}")

    # ── Apply the story floor ──
    # Always keep gate-passers. If that's fewer than MIN_STORIES, backfill from
    # the strongest near-misses (highest confidence, then most on-topic) so the
    # homepage doesn't collapse to 2-3 on thin days. Garbage was already
    # rejected above, so backfills are weak-but-real, not off-topic noise.
    passers = [s for s in stories if not s.get('belowThreshold')]
    backfills = [s for s in stories if s.get('belowThreshold')]
    backfills.sort(key=lambda s: (-s.get('confidence', 0), -s.get('_directPct', 0)))

    if len(passers) >= MIN_STORIES:
        stories = passers
        print(f"\n  Story floor: {len(passers)} passed the gate (no backfill needed).")
    else:
        need = MIN_STORIES - len(passers)
        used = backfills[:need]
        stories = passers + used
        print(f"\n  Story floor: {len(passers)} passed + {len(used)} backfilled "
              f"(below threshold) to reach {len(stories)} "
              f"[{len(backfills) - len(used)} near-misses left unused].")

    # Strip internal-only fields before saving
    for s in stories:
        s.pop('_directPct', None)

    # Sort stories by heat score (hottest first)
    stories.sort(key=lambda s: -s.get('heatScore', 0))

    # ── Stable slugs ──
    prev_stories = []
    prev_files = sorted(POSTS_DIR.glob('stories-*.json'), reverse=True)
    for pf in prev_files[:2]:  # today's earlier run and/or yesterday
        try:
            prev_stories.extend(json.loads(pf.read_text()))
        except Exception:
            continue
    carry_forward_slugs(stories, prev_stories)

    # Save
    output_path = POSTS_DIR / f'stories-{date}.json'
    output_path.write_text(json.dumps(stories, indent=2))
    print(f"\n  Saved {len(stories)} stories to {output_path}")

    # Durable per-slug snapshots so a link survives the next rebuild's headline
    # change (see STORY_ARCHIVE_DIR). Write every story under its own slug; an
    # unchanged headline just overwrites its own snapshot, a changed one adds a
    # new slug alongside the old, and both keep resolving.
    STORY_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archived = 0
    for s in stories:
        # Snapshot under the canonical slug AND the headline-derived one when
        # they differ, so links minted under either keep resolving.
        for slug in {s.get('slug', ''), story_slug(s.get('headline', ''))}:
            if not slug:
                continue
            (STORY_ARCHIVE_DIR / f'{slug}.json').write_text(json.dumps(s, indent=2))
            archived += 1
    print(f"  Archived {archived} story snapshots to {STORY_ARCHIVE_DIR}")

    # Also save as fractures for backward compat
    compat_path = POSTS_DIR / f'fractures-{date}.json'
    compat = []
    for s in stories:
        compat.append({
            'topic': (s['topicSlugs'] or [''])[0],
            'topicDisplay': (s['topicSlugs'] or [''])[0].replace('-', ' ').title(),
            'headline': s['headline'],
            'voiceCount': s['voiceCount'],
            'clusterCount': s['clusterCount'],
            'insight': s['summary'],
            'clusters': s['clusters'],
            'belowThreshold': s.get('belowThreshold', False),
        })
    compat_path.write_text(json.dumps(compat, indent=2))

    # Update temporal cluster history
    update_cluster_history(stories, date)

    return stories


def main():
    args = sys.argv[1:]
    date = None
    if '--date' in args:
        idx = args.index('--date')
        if idx + 1 < len(args):
            date = args[idx + 1]
    build_stories(date)


if __name__ == '__main__':
    main()
