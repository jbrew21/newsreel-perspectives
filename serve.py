#!/usr/bin/env python3
"""
Newsreel Perspectives -- Production Server

Enterprise-grade HTTP server with:
- In-process search (no subprocess spawning)
- In-memory caching with TTL
- Rate limiting per IP
- Input validation and sanitization
- Security headers (CSP, CORS)
- Health endpoint for monitoring
- Structured JSON logging
- Gzip compression for API responses
"""

import gzip
import hashlib
import html as html_lib
import http.server
import io
import json
import logging
import os
import re
import sys
import threading
import time
from collections import Counter, defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs

# ─── CONFIGURATION ──────────────────────────────────────────────────────────

PORT = int(os.environ.get('PORT', 8888))
ROOT = os.path.dirname(os.path.abspath(__file__))

# Rate limiting
RATE_LIMIT_SEARCH = 20       # max search requests per IP per minute (each user
                             # search = 2 requests: fast phase + clustered phase)
RATE_LIMIT_GENERAL = 120     # max requests per IP per minute
RATE_LIMIT_PHOTOS = 360      # photos get their own bucket: a single homepage
                             # load fetches dozens of avatar/chip images, and
                             # NAT'd institutions (libraries, schools) share
                             # one IP — charging photos to the general bucket
                             # starves the API for co-located readers

# Cache TTL (seconds)
CACHE_TTL_STORIES = 300      # 5 min for stories
CACHE_TTL_TOPICS = 300       # 5 min for topics
CACHE_TTL_WIRE = 120         # 2 min for wire
WIRE_MAX_PER_VOICE = 2       # keep at most N most-recent posts per voice
WIRE_MIN_POSTS_BETWEEN = 3   # require >= N other posts between two from one voice
WIRE_MAX_ITEMS = 100         # max entries returned in the wire feed
CACHE_TTL_SEARCH = 600       # 10 min for search results
CACHE_TTL_STATIC = 3600      # 1 hour for static pages
CACHE_TTL_AGENDA = 300       # 5 min for the Split Screen agenda
AGENDA_WINDOW_HOURS = 48     # topic-index is a rolling window; 24h leaves the
                             # right column too thin, 48h gives both sides depth
AGENDA_FALLBACK_HOURS = 168  # widen once if either column comes up short
AGENDA_TOP_N = 5             # topics per column
AGENDA_SHARED_MIN_VOICES = 4  # min distinct voices per side for "shared attention"
LEAN_LEFT_MIN = 0.60         # build_voice_profile overall >= this -> left
LEAN_RIGHT_MAX = 0.40        # overall <= this -> right; between (or unscored) -> center

# Content safety
SAFETY_TERMS = ['pedophil', 'child abuse', 'child porn', 'child sex',
                'molest', 'sex traffick', 'sexual assault on minor']

# Search input limits
MAX_QUERY_LENGTH = 200
MIN_QUERY_LENGTH = 2

# ─── LOGGING ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('perspectives')

# ─── CACHING ─────────────────────────────────────────────────────────────────

_cache = {}
_cache_lock = threading.Lock()
# Single-flight guard for the wire rebuild: without it, every request that
# arrives after the 2-min TTL expires re-walks every voice directory at once.
_wire_build_lock = threading.Lock()
# Same thundering-herd guard for the agenda rebuild (5-min TTL).
_agenda_build_lock = threading.Lock()


def cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() < entry['expires']:
            return entry['data']
        if entry:
            del _cache[key]
    return None


def cache_set(key, data, ttl):
    with _cache_lock:
        _cache[key] = {'data': data, 'expires': time.time() + ttl}
        # Evict old entries if cache grows too large
        if len(_cache) > 500:
            now = time.time()
            expired = [k for k, v in _cache.items() if now >= v['expires']]
            for k in expired:
                del _cache[k]


# ─── RATE LIMITING ───────────────────────────────────────────────────────────

_rate_buckets = defaultdict(list)
_rate_lock = threading.Lock()


def is_rate_limited(ip, limit, window=60, bucket='general'):
    # Each (ip, bucket) pair gets its own counter. Searches and general traffic
    # MUST use separate buckets — otherwise page-load/photo requests (which are
    # far more numerous) burn through the tight search budget and a user trips
    # "Too many searches" after a single real query. See RATE_LIMIT_* below.
    now = time.time()
    key = (ip, bucket)
    with _rate_lock:
        hits = _rate_buckets[key]
        # Prune old entries
        _rate_buckets[key] = [t for t in hits if now - t < window]
        if len(_rate_buckets[key]) >= limit:
            return True
        _rate_buckets[key].append(now)
    return False


# ─── DATA LOADING ────────────────────────────────────────────────────────────

def load_json_file(path):
    try:
        with open(path) as f:
            return json.loads(f.read())
    except Exception:
        return None


def get_latest_file(directory, prefix):
    """Find the most recent file matching a prefix in a directory."""
    try:
        files = sorted(
            [f for f in os.listdir(directory) if f.startswith(prefix) and f.endswith('.json')],
            reverse=True
        )
        return os.path.join(directory, files[0]) if files else None
    except Exception:
        return None


def story_slug(headline):
    """Slugify a headline to match the client's slugify() exactly (story.html)."""
    s = re.sub(r'[^a-z0-9]+', '-', (headline or '').lower())
    return s.strip('-')[:60]


def find_story_by_slug(slug, max_files=14):
    """Resolve a specific story (with its real clusters) by slug across recent
    dated stories-*.json files, so a story that has rotated off /api/stories
    still opens with the correct headline and positions."""
    posts_dir = os.path.join(ROOT, 'data', 'posts')
    try:
        files = sorted(
            [f for f in os.listdir(posts_dir) if f.startswith('stories-') and f.endswith('.json')],
            reverse=True
        )[:max_files]
    except Exception:
        return None
    for fname in files:
        data = load_json_file(os.path.join(posts_dir, fname))
        if not isinstance(data, list):
            continue
        for s in data:
            if story_slug(s.get('headline', '')) == slug:
                return s
    return None


def is_content_safe(text):
    text_lower = text.lower()
    return not any(term in text_lower for term in SAFETY_TERMS)


def wire_ts_key(post):
    """Sort key that turns a post's timestamp into a real UTC instant.

    Posts arrive with mixed ISO-8601 formats (``...+00:00``, ``...Z``,
    varying fractional-second widths, and occasionally a non-UTC offset).
    A plain string sort orders those by byte value, which is only
    chronologically correct while every source happens to emit zero-offset
    UTC — an undocumented, unenforced invariant. Parsing to an instant makes
    ordering correct for any offset; missing/unparseable timestamps sort
    oldest so they never jump to the top of the feed.
    """
    try:
        # The extraction stays inside the try: a truthy non-string timestamp
        # (e.g. a numeric epoch from a scraper change) must degrade to
        # oldest, not raise — build_agenda calls this per-post across the
        # whole topic-index, so one bad record would otherwise 500 the
        # endpoint for as long as that file is the latest index.
        ts = (post.get('timestamp') or '').strip()
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def decluster(posts, max_per_voice=WIRE_MAX_PER_VOICE,
              min_gap=WIRE_MIN_POSTS_BETWEEN, limit=WIRE_MAX_ITEMS):
    """Turn a raw post list into a diverse, chronological wire feed.

    A single voice posting a burst (e.g. an 8-tweet thread inside one minute)
    used to monopolize the top of the wire, so it read as "bunched by author"
    instead of a diverse newswire. This:

      1. sorts newest-first by real instant (see ``wire_ts_key``),
      2. keeps at most ``max_per_voice`` most-recent posts per voice, then
      3. greedily emits the newest remaining post whose voice has NOT appeared
         in the last ``min_gap`` slots, so the same voice is spread out.

    Newest-first order is preserved except where a burst is deliberately
    spaced. The ``next(..., 0)`` fallback places an otherwise-blocked post when
    every remaining candidate is still inside the gap window (e.g. a
    single-voice input) — this guarantees termination; do not "fix" it into a
    loop. Input size is bounded (``max_per_voice`` × number of voices), so the
    linear scan is not a concern at any realistic scale.
    """
    ordered = sorted(posts, key=wire_ts_key, reverse=True)

    counts = {}
    capped = []
    for p in ordered:
        vid = p.get('voiceId')
        if counts.get(vid, 0) >= max_per_voice:
            continue
        counts[vid] = counts.get(vid, 0) + 1
        capped.append(p)

    wire = []
    recent = deque(maxlen=max(1, min_gap))
    while capped:
        idx = next((i for i, p in enumerate(capped)
                    if p.get('voiceId') not in recent), 0)
        p = capped.pop(idx)
        wire.append(p)
        recent.append(p.get('voiceId'))

    return wire[:limit]


def build_wire(root=None):
    """Build the de-clustered wire feed by scanning every voice's posts.

    Pure of request state so it can be unit-tested and called under the
    single-flight lock. Reads ``data/posts/<voice>/<day>.json`` for today AND
    yesterday (UTC): the pipeline commits each day's files around 06-08 UTC,
    so a today-only wire went completely dark from 00:00 UTC (8pm ET) until
    the morning commit. Posts are deduped across the two files, then decoded,
    filtered, and handed to ``decluster`` for ordering and diversity.
    """
    root = root or ROOT
    days = [(date.today() - timedelta(days=n)).isoformat() for n in range(2)]
    posts_dir = os.path.join(root, 'data', 'posts')

    voice_meta, leans = load_voice_meta(root)

    all_posts = []
    seen = set()
    try:
        for voice_dir in os.listdir(posts_dir):
            meta = voice_meta.get(voice_dir, {})
            for day in days:
                day_file = os.path.join(posts_dir, voice_dir, f'{day}.json')
                if not os.path.isfile(day_file):
                    continue
                data = load_json_file(day_file)
                if not data:
                    continue
                posts = data.get('posts', []) if isinstance(data, dict) else data
                if not isinstance(posts, list):
                    continue
                for p in posts:
                    # One malformed record must not drop the rest of a voice's
                    # posts, so guard each one individually.
                    if not isinstance(p, dict):
                        continue
                    # Source text is scraped and often carries HTML entities
                    # (e.g. "Beef &amp; Draws"). Decode once here; the client
                    # re-escapes for safe display, so the reader sees "&".
                    text = html_lib.unescape(str(p.get('text') or '')).strip()
                    if len(text) < 30:
                        continue
                    if not is_content_safe(text):
                        continue
                    # The same post can appear in consecutive day files when
                    # scrapes overlap — keep the first occurrence (newest day
                    # is scanned first).
                    key = (voice_dir, p.get('sourceUrl') or (p.get('timestamp'), text[:80]))
                    if key in seen:
                        continue
                    seen.add(key)
                    all_posts.append({
                        'voiceId': voice_dir,
                        'voiceName': meta.get('name', p.get('voiceName', voice_dir)),
                        'photo': meta.get('photo', ''),
                        'platform': p.get('platform', ''),
                        'text': text[:200],
                        'sourceUrl': p.get('sourceUrl', ''),
                        'timestamp': p.get('timestamp', ''),
                        'lean': leans.get(voice_dir, 'center'),
                    })
    except OSError as e:
        log.error(f"Wire error: {e}")

    return decluster(all_posts)


# {voices.json path: (mtime, voice_meta, leans)} — leans derive solely from
# voices.json, which changes at most daily, so re-scoring every voice on each
# wire (2-min) and agenda (5-min) cache miss is wasted work. Benign under
# races: worst case two threads compute the same result.
_voice_meta_cache = {}


def load_voice_meta(root=None):
    """(voice_meta, leans) for data/voices.json, cached on file mtime.

    Shared by build_wire and build_agenda so the two features can never
    disagree about which voices exist or how they lean.
    """
    root = root or ROOT
    path = os.path.join(root, 'data', 'voices.json')
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    cached = _voice_meta_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1], cached[2]
    voices_data = load_json_file(path) or []
    voice_meta = {v['id']: v for v in voices_data
                  if isinstance(v, dict) and v.get('id')}
    leans = {vid: voice_lean(v) for vid, v in voice_meta.items()}
    _voice_meta_cache[path] = (mtime, voice_meta, leans)
    return voice_meta, leans


def voice_lean(voice):
    """Bucket a voice as 'left' | 'right' | 'center' from its tags.

    Uses the same scorer that powers the mirror profiles
    (perspective_profiles.build_voice_profile, 0=conservative..1=progressive).
    Unscored voices and everything between the thresholds land in 'center' —
    borderline voices are never conscripted into a side.
    """
    profile = build_voice_profile(voice or {})
    if not profile.get('classified'):
        return 'center'
    overall = profile.get('overall', 0.5)
    if overall >= LEAN_LEFT_MIN:
        return 'left'
    if overall <= LEAN_RIGHT_MAX:
        return 'right'
    return 'center'


def build_agenda(root=None, now=None):
    """Build the Split Screen payload: what left- vs right-leaning voices are
    each talking about, ranked by distinct-voice attention per topic.

    Pure of request state (like build_wire) so it can be unit-tested against a
    fixture root. The topic-index is a rolling window, so posts are filtered to
    AGENDA_WINDOW_HOURS; if either column comes up short the window widens once
    to AGENDA_FALLBACK_HOURS.
    """
    root = root or ROOT
    now = now or datetime.now(timezone.utc)
    posts_dir = os.path.join(root, 'data', 'posts')

    voice_meta, leans = load_voice_meta(root)
    classified = {'left': sum(1 for b in leans.values() if b == 'left'),
                  'right': sum(1 for b in leans.values() if b == 'right')}
    center_count = len(leans) - classified['left'] - classified['right']

    topic_index = {}
    index_path = get_latest_file(posts_dir, 'topic-index-')
    if index_path:
        topic_index = load_json_file(index_path) or {}

    display_names = {}
    taxonomy = load_json_file(os.path.join(root, 'data', 'taxonomy.json')) or {}
    for t in taxonomy.get('topics', []):
        if isinstance(t, dict) and t.get('slug'):
            display_names[t['slug']] = t.get('display', t['slug'])

    def disp(slug):
        return display_names.get(slug, slug.replace('-', ' ').title())

    # Map topic slug -> today's story page, when one exists
    story_slugs = {}
    stories_path = get_latest_file(posts_dir, 'stories-')
    stories = load_json_file(stories_path) if stories_path else None
    for s in stories or []:
        if not isinstance(s, dict):
            continue
        slug = story_slug(s.get('headline', ''))
        for topic in s.get('topicSlugs', []) or []:
            story_slugs.setdefault(topic, slug)

    def tally(window_hours):
        cutoff = now - timedelta(hours=window_hours)
        per_topic = {}
        for topic, posts in topic_index.items():
            if topic == 'uncategorized' or not isinstance(posts, list):
                continue
            sides = {'left': {'voices': Counter(), 'posts': 0},
                     'right': {'voices': Counter(), 'posts': 0}}
            for p in posts:
                if not isinstance(p, dict):
                    continue
                if wire_ts_key(p) < cutoff:
                    continue
                bucket = leans.get(p.get('voiceId'), 'center')
                if bucket == 'center':
                    continue
                sides[bucket]['voices'][p['voiceId']] += 1
                sides[bucket]['posts'] += 1
            per_topic[topic] = sides
        return per_topic

    def column(per_topic, side):
        ranked = sorted(
            ((t, s[side]) for t, s in per_topic.items() if s[side]['voices']),
            key=lambda kv: (len(kv[1]['voices']), kv[1]['posts']),
            reverse=True,
        )[:AGENDA_TOP_N]
        topics = []
        for topic, stats in ranked:
            top_voices = []
            for vid, _count in stats['voices'].most_common(3):
                meta = voice_meta.get(vid, {})
                top_voices.append({
                    'voiceId': vid,
                    'name': meta.get('name', vid),
                    'photo': meta.get('photo', ''),
                })
            topics.append({
                'slug': topic,
                'display': disp(topic),
                'voices': len(stats['voices']),
                'posts': stats['posts'],
                'storySlug': story_slugs.get(topic),
                'topVoices': top_voices,
            })
        return topics

    def healthy(topics):
        return sum(1 for t in topics if t['voices'] >= 2) >= 3

    window = AGENDA_WINDOW_HOURS
    per_topic = tally(window)
    left, right = column(per_topic, 'left'), column(per_topic, 'right')
    if not (healthy(left) and healthy(right)):
        window = AGENDA_FALLBACK_HOURS
        per_topic = tally(window)
        left, right = column(per_topic, 'left'), column(per_topic, 'right')

    shared = sorted(
        (
            {'slug': t,
             'display': disp(t),
             'leftVoices': len(s['left']['voices']),
             'rightVoices': len(s['right']['voices'])}
            for t, s in per_topic.items()
            if min(len(s['left']['voices']), len(s['right']['voices'])) >= AGENDA_SHARED_MIN_VOICES
        ),
        key=lambda x: min(x['leftVoices'], x['rightVoices']),
        reverse=True,
    )[:4]

    return {
        'generatedAt': now.isoformat(),
        'windowHours': window,
        'widened': window != AGENDA_WINDOW_HOURS,
        'centerCount': center_count,
        'left': {'voicesClassified': classified['left'], 'topics': left},
        'right': {'voicesClassified': classified['right'], 'topics': right},
        'shared': shared,
    }


def sanitize_query(q):
    """Sanitize search input."""
    if not q or not isinstance(q, str):
        return None
    q = q.strip()
    if len(q) < MIN_QUERY_LENGTH or len(q) > MAX_QUERY_LENGTH:
        return None
    # Remove control characters and excessive whitespace
    q = re.sub(r'[\x00-\x1f\x7f]', '', q)
    q = re.sub(r'\s+', ' ', q)
    return q


# ─── IN-PROCESS SEARCH (replaces subprocess) ────────────────────────────────

# Import lookup module directly instead of spawning subprocess
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import search_helpers  # noqa: E402  (pure helpers: story-join + recency sort)
from perspective_profiles import build_voice_profile  # noqa: E402  (pure tag scorer)
_lookup_module = None


def get_lookup():
    global _lookup_module
    if _lookup_module is None:
        try:
            import lookup as _lm
            _lookup_module = _lm
        except ImportError:
            log.error("Failed to import lookup module")
    return _lookup_module


def _cached_result_fallback(query):
    """Return a prebuilt data/results/<slug>.json for this query if one exists.

    The daily pipeline pre-renders results for known stories, so a live-search
    failure (cold start, Claude hiccup) can still serve a good answer instead
    of erroring."""
    slug = search_helpers.result_slug(query)
    result_path = os.path.join(ROOT, 'data', 'results', f'{slug}.json')
    if os.path.exists(result_path):
        return load_json_file(result_path)
    return None


def attach_matched_stories(result):
    """Pin curated homepage stories that match the search's topics.

    Adds ``result['matchedStories']`` (possibly empty) by joining the latest
    stories feed on topic slugs, so search can show "the Newsreel take" above
    the voice feed. Never raises — search must not fail over this."""
    try:
        topics = result.get('matchedTopics') if isinstance(result, dict) else None
        if not topics:
            return result
        stories_path = get_latest_file(os.path.join(ROOT, 'data', 'posts'), 'stories-')
        stories = load_json_file(stories_path) if stories_path else None
        if isinstance(stories, list):
            result['matchedStories'] = search_helpers.stories_for_topics(stories, topics, limit=2)
    except Exception as e:
        log.warning(f"Could not attach matched stories: {e}")
    return result


def do_search(query, days=None, fast=False):
    """Run search in-process with caching.

    fast=True skips the argument-clustering Claude call (the slowest step) so
    voices paint in ~1s; the client then re-requests without fast to get the
    clustered result. A cached FULL result always wins — even for fast requests —
    so the second phase is free whenever the query was searched recently.
    """
    cache_key = f"search:{hashlib.md5(f'{query}:{days}'.encode()).hexdigest()}"
    cached = cache_get(cache_key)
    if cached:
        log.info(f"Search cache hit: {query[:50]}")
        return cached

    lookup = get_lookup()
    if not lookup:
        fallback = _cached_result_fallback(query)
        if fallback:
            return attach_matched_stories(fallback)
        return {'error': 'Search temporarily unavailable. Try again in a moment.', 'code': 'unavailable'}

    if fast:
        fast_key = cache_key + ':fast'
        cached_fast = cache_get(fast_key)
        if cached_fast:
            return cached_fast
        try:
            result = lookup.lookup_story(query, days=int(days) if days else None, skip_clusters=True)
            if result:
                attach_matched_stories(result)
                # Short TTL: this only needs to live long enough for the client
                # to fetch the full result behind it.
                cache_set(fast_key, result, 120)
                return result
            return {'error': 'No results found for this topic yet. Try a broader term.', 'code': 'empty'}
        except Exception as e:
            log.error(f"Fast search error: {e}")
            fallback = _cached_result_fallback(query)
            if fallback:
                return attach_matched_stories(fallback)
            return {'error': 'Search failed. Please try again.', 'code': 'failed'}

    try:
        result = lookup.lookup_story(query, days=int(days) if days else None)
        if result:
            attach_matched_stories(result)
            cache_set(cache_key, result, CACHE_TTL_SEARCH)
            return result
        return {'error': 'No results found for this topic yet. Try a broader term.', 'code': 'empty'}
    except Exception as e:
        log.error(f"Search error: {e}")
        fallback = _cached_result_fallback(query)
        if fallback:
            return attach_matched_stories(fallback)
        return {'error': 'Search failed. Please try again.', 'code': 'failed'}


# ─── HANDLER ─────────────────────────────────────────────────────────────────

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, format, *args):
        # Use structured logging instead of default stderr
        log.info(f"{self.client_address[0]} {format % args}")

    def send_json(self, data, status=200, cache_ttl=0):
        """Send JSON response with proper headers and optional compression."""
        body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data.encode()

        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if cache_ttl > 0:
            self.send_header('Cache-Control', f'public, max-age={cache_ttl}')
        else:
            self.send_header('Cache-Control', 'no-cache')

        # Gzip if client supports it and body is large enough
        accept_encoding = self.headers.get('Accept-Encoding', '')
        if 'gzip' in accept_encoding and len(body) > 1024:
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode='wb') as gz:
                gz.write(body)
            body = buf.getvalue()
            self.send_header('Content-Encoding', 'gzip')

        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, filepath, cache_ttl=0):
        """Send HTML file with security headers."""
        try:
            with open(filepath) as f:
                body = f.read().encode()
        except FileNotFoundError:
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        if cache_ttl > 0:
            self.send_header('Cache-Control', f'public, max-age={cache_ttl}')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def get_client_ip(self):
        """Get client IP, respecting X-Forwarded-For behind proxies."""
        forwarded = self.headers.get('X-Forwarded-For')
        if forwarded:
            return forwarded.split(',')[0].strip()
        return self.client_address[0]

    def do_GET(self):
        ip = self.get_client_ip()
        path = self.path.split('?')[0].rstrip('/')

        # ── Health endpoint ──
        if path == '/health' or path == '/api/health':
            posts_dir = os.path.join(ROOT, 'data', 'posts')
            latest_stories = get_latest_file(posts_dir, 'stories-')
            latest_index = get_latest_file(posts_dir, 'topic-index-')

            health = {
                'status': 'ok',
                'timestamp': datetime.utcnow().isoformat() + 'Z',
                'latest_stories': os.path.basename(latest_stories) if latest_stories else None,
                'latest_index': os.path.basename(latest_index) if latest_index else None,
                'voices_count': 0,
                'cache_entries': len(_cache),
            }
            voices_path = os.path.join(ROOT, 'data', 'voices.json')
            if os.path.exists(voices_path):
                try:
                    health['voices_count'] = len(json.loads(open(voices_path).read()))
                except Exception:
                    pass

            # Check data freshness
            if latest_stories:
                mtime = os.path.getmtime(latest_stories)
                age_hours = (time.time() - mtime) / 3600
                health['stories_age_hours'] = round(age_hours, 1)
                if age_hours > 36:
                    health['status'] = 'degraded'
                    health['warning'] = 'Stories data is stale (>36 hours old)'

            self.send_json(health)
            return

        # ── Rate limits ──
        # Search gets its own tighter bucket; we skip the general bucket for lookup
        # so a search request doesn't burn two slots. Photos get a roomier bucket
        # of their own — a homepage load fetches dozens of images, and readers
        # behind one NAT (libraries/schools) would otherwise starve the API.
        if self.path.startswith('/api/lookup'):
            if is_rate_limited(ip, RATE_LIMIT_SEARCH, bucket='search'):
                self.send_json({'error': 'Search rate limited. Max 10 per minute.'}, status=429)
                return
        elif self.path.startswith('/photos/'):
            if is_rate_limited(ip, RATE_LIMIT_PHOTOS, bucket='photos'):
                self.send_json({'error': 'Rate limited. Try again in a minute.'}, status=429)
                return
        else:
            if is_rate_limited(ip, RATE_LIMIT_GENERAL, bucket='general'):
                self.send_json({'error': 'Rate limited. Try again in a minute.'}, status=429)
                return

        # ── API: Search/Lookup ──
        if self.path.startswith('/api/lookup'):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            raw_query = params.get('q', [''])[0]
            days = params.get('days', [None])[0]
            fast = params.get('fast', ['0'])[0] == '1'

            query = sanitize_query(raw_query)
            if not query:
                self.send_json({'error': f'Invalid query. Must be {MIN_QUERY_LENGTH}-{MAX_QUERY_LENGTH} characters.'}, status=400)
                return

            log.info(f"Search{' (fast)' if fast else ''}: '{query}' from {ip}")
            result = do_search(query, days, fast=fast)
            # Fast-phase responses are transitional — don't let browsers/CDNs
            # cache them long, or a reload could show cluster-less results.
            self.send_json(result, cache_ttl=(60 if fast else CACHE_TTL_SEARCH))
            return

        # ── API: Single story by slug (resolves rotated-out stories) ──
        if path == '/api/story':
            qs = parse_qs(urlparse(self.path).query)
            slug = (qs.get('slug', [''])[0] or '').strip().lower()[:60]
            if not slug:
                self.send_json({'error': 'missing_slug'}, status=400, cache_ttl=0)
                return
            story = find_story_by_slug(slug)
            if story:
                self.send_json(story, cache_ttl=CACHE_TTL_STORIES)
            else:
                self.send_json({'error': 'not_found', 'slug': slug}, status=404, cache_ttl=60)
            return

        # ── API: Stories ──
        if path == '/api/stories':
            cached = cache_get('stories')
            if cached:
                self.send_json(cached, cache_ttl=CACHE_TTL_STORIES)
                return

            posts_dir = os.path.join(ROOT, 'data', 'posts')
            filepath = get_latest_file(posts_dir, 'stories-')
            if filepath:
                data = load_json_file(filepath)
                if data:
                    cache_set('stories', data, CACHE_TTL_STORIES)
                    self.send_json(data, cache_ttl=CACHE_TTL_STORIES)
                    return
            self.send_json([], cache_ttl=60)
            return

        # ── API: Fractures (backward compat) ──
        if path == '/api/fractures':
            posts_dir = os.path.join(ROOT, 'data', 'posts')
            filepath = get_latest_file(posts_dir, 'fractures-')
            if filepath:
                data = load_json_file(filepath)
                if data:
                    self.send_json(data, cache_ttl=CACHE_TTL_STORIES)
                    return
            self.send_json([], cache_ttl=60)
            return

        # ── API: Topics ──
        if path == '/api/topics':
            cached = cache_get('topics')
            if cached:
                self.send_json(cached, cache_ttl=CACHE_TTL_TOPICS)
                return

            posts_dir = os.path.join(ROOT, 'data', 'posts')
            filepath = get_latest_file(posts_dir, 'topic-index-')
            if filepath:
                data = load_json_file(filepath)
                if data:
                    cache_set('topics', data, CACHE_TTL_TOPICS)
                    self.send_json(data, cache_ttl=CACHE_TTL_TOPICS)
                    return
            self.send_json({}, cache_ttl=60)
            return

        # ── API: Wire ──
        if path == '/api/wire':
            cached = cache_get('wire')
            if cached is None:
                # Single-flight: only one thread rebuilds on a cache miss; the
                # rest wait and then read the fresh cache. Double-check inside
                # the lock so a queued thread doesn't rebuild redundantly.
                with _wire_build_lock:
                    cached = cache_get('wire')
                    if cached is None:
                        cached = build_wire()
                        cache_set('wire', cached, CACHE_TTL_WIRE)
            self.send_json(cached, cache_ttl=CACHE_TTL_WIRE)
            return

        # ── API: Agenda (the Split Screen) ──
        if path == '/api/agenda':
            cached = cache_get('agenda')
            if cached is None:
                # Single-flight, same as /api/wire: one thread rebuilds on a
                # cache miss; queued threads re-check inside the lock.
                with _agenda_build_lock:
                    cached = cache_get('agenda')
                    if cached is None:
                        cached = build_agenda()
                        cache_set('agenda', cached, CACHE_TTL_AGENDA)
            self.send_json(cached, cache_ttl=CACHE_TTL_AGENDA)
            return

        # ── Photos (with caching headers) ──
        if self.path.startswith('/photos/'):
            filename = self.path.split('/photos/')[1].split('?')[0]
            # Strict filename sanitization: only allow safe image filenames
            ALLOWED_PHOTO_EXTS = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                                   '.png': 'image/png', '.webp': 'image/webp', '.gif': 'image/gif'}
            if '/' in filename or '\\' in filename or '..' in filename:
                self.send_error(400)
                return
            ext = os.path.splitext(filename)[1].lower()
            if ext not in ALLOWED_PHOTO_EXTS:
                self.send_error(400)
                return
            photo_path = os.path.join(ROOT, 'data', 'photos', filename)
            if os.path.exists(photo_path):
                self.send_response(200)
                self.send_header('Content-Type', ALLOWED_PHOTO_EXTS[ext])
                self.send_header('Cache-Control', 'public, max-age=604800')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                with open(photo_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404)
            return

        # ── HTML Pages ──
        PAGE_MAP = {
            '': 'search.html',
            '/search': 'search.html',
            '/voices': 'voices.html',
            '/methodology': 'methodology.html',
            '/review': 'review.html',
        }

        if path in PAGE_MAP:
            self.send_html(os.path.join(ROOT, PAGE_MAP[path]), cache_ttl=CACHE_TTL_STATIC)
            return

        if path.startswith('/voice/'):
            self.send_html(os.path.join(ROOT, 'voice.html'))
            return

        if path.startswith('/story/'):
            self.send_html(os.path.join(ROOT, 'story.html'))
            return

        if path.startswith('/profile/'):
            user_id = path.split('/profile/')[1].split('?')[0].strip('/')
            if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', user_id):
                profile_path = os.path.join(ROOT, 'data', 'profiles', f'{user_id}.html')
                if os.path.exists(profile_path):
                    self.send_html(profile_path)
                else:
                    self.send_error(404)
            else:
                self.send_error(400)
            return

        # ── Static files (CSS, JS, data) ──
        return super().do_GET()

    def do_POST(self):
        ip = self.get_client_ip()

        if is_rate_limited(ip, RATE_LIMIT_GENERAL, bucket='general'):
            self.send_json({'error': 'Rate limited'}, status=429)
            return

        if self.path == '/api/review':
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 50000:  # 50KB max
                self.send_json({'error': 'Request too large'}, status=413)
                return

            body = self.rfile.read(content_length).decode()
            try:
                review = json.loads(body)
                if not isinstance(review, dict):
                    self.send_json({'error': 'Review must be a JSON object'}, status=400)
                    return

                reviews_path = os.path.join(ROOT, 'data', 'editorial-reviews.json')
                existing = load_json_file(reviews_path) or []
                # Cap stored reviews to prevent unbounded disk growth
                existing = existing[-999:]
                existing.append(review)
                with open(reviews_path, 'w') as f:
                    f.write(json.dumps(existing, indent=2))

                if review.get('overrides'):
                    overrides_path = os.path.join(ROOT, 'data', 'editorial-overrides.json')
                    overrides = load_json_file(overrides_path) or {}
                    headline = review.get('headline', '')
                    # Validate headline is a non-empty string within reasonable length
                    if headline and isinstance(headline, str) and len(headline) <= 500:
                        overrides[headline] = review['overrides']
                        with open(overrides_path, 'w') as f:
                            f.write(json.dumps(overrides, indent=2))

                log.info(f"Editorial review saved: {review.get('headline', '')[:50]}")
                self.send_json({'ok': True})
            except json.JSONDecodeError:
                self.send_json({'error': 'Invalid JSON'}, status=400)
            except Exception as e:
                log.error(f"Review save error: {e}")
                self.send_json({'error': 'Internal error'}, status=500)
            return

        self.send_json({'error': 'Not found'}, status=404)


# ─── SERVER ──────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    log.info(f"Perspectives server starting on port {PORT}")
    log.info(f"Root: {ROOT}")

    # Pre-warm cache
    posts_dir = os.path.join(ROOT, 'data', 'posts')
    stories_file = get_latest_file(posts_dir, 'stories-')
    if stories_file:
        data = load_json_file(stories_file)
        if data:
            cache_set('stories', data, CACHE_TTL_STORIES)
            log.info(f"Pre-warmed stories cache: {len(data)} stories")

    topics_file = get_latest_file(posts_dir, 'topic-index-')
    if topics_file:
        data = load_json_file(topics_file)
        if data:
            cache_set('topics', data, CACHE_TTL_TOPICS)
            log.info(f"Pre-warmed topics cache: {len(data)} topics")

    # Pre-load lookup module
    get_lookup()

    server = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    log.info(f"Server ready at http://0.0.0.0:{PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()
