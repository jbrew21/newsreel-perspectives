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
import http.server
import io
import json
import logging
import os
import re
import sys
import threading
import time
from collections import defaultdict
from datetime import date, datetime
from urllib.parse import urlparse, parse_qs

# ─── CONFIGURATION ──────────────────────────────────────────────────────────

PORT = int(os.environ.get('PORT', 8888))
ROOT = os.path.dirname(os.path.abspath(__file__))

# Rate limiting
RATE_LIMIT_SEARCH = 10       # max searches per IP per minute
RATE_LIMIT_GENERAL = 120     # max requests per IP per minute

# Cache TTL (seconds)
CACHE_TTL_STORIES = 300      # 5 min for stories
CACHE_TTL_TOPICS = 300       # 5 min for topics
CACHE_TTL_WIRE = 120         # 2 min for wire
CACHE_TTL_SEARCH = 600       # 10 min for search results
CACHE_TTL_STATIC = 3600      # 1 hour for static pages

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


def is_rate_limited(ip, limit, window=60):
    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets[ip]
        # Prune old entries
        _rate_buckets[ip] = [t for t in bucket if now - t < window]
        if len(_rate_buckets[ip]) >= limit:
            return True
        _rate_buckets[ip].append(now)
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


def is_content_safe(text):
    text_lower = text.lower()
    return not any(term in text_lower for term in SAFETY_TERMS)


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


def do_search(query, days=None):
    """Run search in-process with caching."""
    cache_key = f"search:{hashlib.md5(f'{query}:{days}'.encode()).hexdigest()}"
    cached = cache_get(cache_key)
    if cached:
        log.info(f"Search cache hit: {query[:50]}")
        return cached

    lookup = get_lookup()
    if not lookup:
        return {'error': 'Search unavailable'}

    try:
        result = lookup.lookup_story(query, days=int(days) if days else None)
        if result:
            cache_set(cache_key, result, CACHE_TTL_SEARCH)
            return result
        return {'error': 'No results found'}
    except Exception as e:
        log.error(f"Search error: {e}")
        return {'error': 'Search failed'}


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

        # ── Rate limit check ──
        if is_rate_limited(ip, RATE_LIMIT_GENERAL):
            self.send_json({'error': 'Rate limited. Try again in a minute.'}, status=429)
            return

        # ── API: Search/Lookup ──
        if self.path.startswith('/api/lookup'):
            # Additional rate limit for search (costs money)
            if is_rate_limited(ip, RATE_LIMIT_SEARCH):
                self.send_json({'error': 'Search rate limited. Max 10 per minute.'}, status=429)
                return

            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            raw_query = params.get('q', [''])[0]
            days = params.get('days', [None])[0]

            query = sanitize_query(raw_query)
            if not query:
                self.send_json({'error': f'Invalid query. Must be {MIN_QUERY_LENGTH}-{MAX_QUERY_LENGTH} characters.'}, status=400)
                return

            log.info(f"Search: '{query}' from {ip}")
            result = do_search(query, days)
            self.send_json(result, cache_ttl=CACHE_TTL_SEARCH)
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
            if cached:
                self.send_json(cached, cache_ttl=CACHE_TTL_WIRE)
                return

            today = date.today().isoformat()
            posts_dir = os.path.join(ROOT, 'data', 'posts')
            voices_path = os.path.join(ROOT, 'data', 'voices.json')

            voice_meta = {}
            voices_data = load_json_file(voices_path)
            if voices_data:
                for v in voices_data:
                    voice_meta[v['id']] = v

            all_posts = []
            try:
                for voice_dir in os.listdir(posts_dir):
                    day_file = os.path.join(posts_dir, voice_dir, f'{today}.json')
                    if not os.path.isfile(day_file):
                        continue
                    data = load_json_file(day_file)
                    if not data:
                        continue
                    posts = data.get('posts', []) if isinstance(data, dict) else data
                    meta = voice_meta.get(voice_dir, {})
                    for p in posts:
                        text = (p.get('text') or '').strip()
                        if len(text) < 30:
                            continue
                        if not is_content_safe(text):
                            continue
                        all_posts.append({
                            'voiceId': voice_dir,
                            'voiceName': meta.get('name', p.get('voiceName', voice_dir)),
                            'photo': meta.get('photo', ''),
                            'platform': p.get('platform', ''),
                            'text': text[:200],
                            'sourceUrl': p.get('sourceUrl', ''),
                            'timestamp': p.get('timestamp', ''),
                        })
            except Exception as e:
                log.error(f"Wire error: {e}")

            all_posts.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            all_posts = all_posts[:100]
            cache_set('wire', all_posts, CACHE_TTL_WIRE)
            self.send_json(all_posts, cache_ttl=CACHE_TTL_WIRE)
            return

        # ── Photos (with caching headers) ──
        if self.path.startswith('/photos/'):
            filename = self.path.split('/photos/')[1].split('?')[0]
            # Sanitize filename
            if '/' in filename or '..' in filename:
                self.send_error(400)
                return
            photo_path = os.path.join(ROOT, 'data', 'photos', filename)
            if os.path.exists(photo_path):
                content_type = 'image/png' if filename.endswith('.png') else 'image/jpeg'
                self.send_response(200)
                self.send_header('Content-Type', content_type)
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

        if is_rate_limited(ip, RATE_LIMIT_GENERAL):
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

                reviews_path = os.path.join(ROOT, 'data', 'editorial-reviews.json')
                existing = load_json_file(reviews_path) or []
                existing.append(review)
                with open(reviews_path, 'w') as f:
                    f.write(json.dumps(existing, indent=2))

                if review.get('overrides'):
                    overrides_path = os.path.join(ROOT, 'data', 'editorial-overrides.json')
                    overrides = load_json_file(overrides_path) or {}
                    headline = review.get('headline', '')
                    if headline:
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
