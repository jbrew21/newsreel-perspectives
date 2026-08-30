#!/usr/bin/env python3
"""
Newsreel Perspectives — Daily Voice Collector

Pulls ALL recent content from every tracked voice, categorizes by topic,
and stores organized posts. Runs once daily, everything is free:
  - YouTube RSS feeds (unlimited)
  - YouTube transcripts (free)
  - Bluesky public API (free, no auth)
  - Twitter oembed (free, no auth)

Then when a user searches a story, we just look up matching topics
instead of scraping in real-time.

Usage:
  python scripts/collect.py              # collect all voices
  python scripts/collect.py --voice elon-musk  # collect one voice
  python scripts/collect.py --categorize  # just re-categorize with Claude
"""

import asyncio
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).parent.parent
VOICES_PATH = ROOT / "data" / "voices.json"
POSTS_DIR = ROOT / "data" / "posts"
TRANSCRIPT_CACHE = ROOT / "data" / "transcript_cache.json"
TAXONOMY_PATH = ROOT / "data" / "taxonomy.json"
USAGE_LOG_PATH = ROOT / "data" / "usage-log.json"
ENV_PATH = ROOT.parent / "newsletter" / ".env"

# Cost tracking globals (accumulated during categorization)
_usage_stats = {
    'claude_calls': 0,
    'total_input_chars': 0,
    'total_output_tokens_est': 0,
    'posts_reused': 0,
}

# Categorization model — Perspectives is intentionally on Haiku 4.5.
CLAUDE_MODEL = 'claude-haiku-4-5-20251001'

# Message Batches polling (Phase 3). Batches usually finish well within the
# GitHub Action's 90-minute job budget, but cap the poll so a slow batch
# falls back to the sequential path instead of blowing the job timeout.
BATCH_POLL_INTERVAL = 30       # seconds between status checks
BATCH_POLL_TIMEOUT = 40 * 60   # hard deadline before sequential fallback

# Full, realistic Chrome UA. The previous value was truncated at
# "AppleWebKit/537.36" (no KHTML/Chrome/Safari tail), which Substack's
# Cloudflare bot protection flagged as non-browser and 403'd.
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

# Browser-like header set for feeds behind bot protection (Substack et al.)
BROWSER_HEADERS = {
    'User-Agent': UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

# Load env
def load_env():
    for env_path in [ROOT / ".env", ENV_PATH]:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    key, _, val = line.partition('=')
                    if key.strip() not in os.environ:
                        os.environ[key.strip()] = val.strip()

load_env()
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')


sys.path.append(str(Path(__file__).parent))
from voices_lib import load_voices as _canonical_load_voices  # noqa: E402


def load_voices():
    return _canonical_load_voices(VOICES_PATH)


# ─── X/TWITTER VIA NITTER RSS ────────────────────────────────────────────────

# The public Nitter ecosystem shut down. Verified 2026-08-30: nitter.net
# returns 410 Gone, and privacydev / poast / esmailelbob / kavin / 1d4 /
# moomoo / lucabased are dead, refused or NXDOMAIN; tiekoetter and xcancel
# answer but 429/400 every request. syndication.twitter.com 429s from any
# datacenter IP and twikit's guest-token flow no longer completes.
#
# Left as an empty list rather than deleted so the fallback chain stays intact
# for whenever a working instance exists again. X collection now depends on a
# per-voice rss.app feed URL in voices.json (feeds.x), which is checked first
# and still works; without one, a voice's X posts are simply not collected and
# fetch_x_posts says so rather than failing silently.
NITTER_INSTANCES = []


def strip_quoted_tweet(text):
    """Remove an embedded quote-tweet so we keep only the AUTHOR's own words.

    Nitter/rss embed a quoted tweet after the author's text as:
        <own text>\\n\\n\\n<Display Name> (@handle)\\n\\n<quoted text> ... — <url>
    Pulling the quoted tweet pollutes both the displayed quote and the argument
    classification (a bare "Thoughts?" requote was being clustered on the quoted
    account's content). Keep everything before the quoted tweet's attribution line.
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
    # Drop a trailing quoted permalink and media/thread artifacts.
    t = re.sub(r'\s*—\s*https?://\S+\s*$', '', t)
    t = re.sub(r'(?:\n+\s*(?:Video|GIF|Link|Show this thread|Watch)\s*)+$', '', t, flags=re.I)
    return t.strip()


def _parse_rssapp_json(voice, data):
    """Parse rss.app JSON format into standard post objects."""
    x_handle = voice.get('handles', {}).get('x', '').lstrip('@')
    posts = []
    for item in data.get('items', [])[:20]:
        text = strip_quoted_tweet(item.get('title', ''))
        if not text or len(text) < 15:
            continue

        source_url = item.get('url', '')

        # Skip retweets
        if text.startswith('RT by @') or text.startswith('RT @'):
            continue

        # Skip reposts
        if source_url and x_handle and x_handle.lower() not in source_url.lower():
            continue

        # Parse date
        timestamp = ''
        date_str = item.get('date_published', '')
        if date_str:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(date_str)
                timestamp = dt.isoformat()
            except Exception:
                timestamp = date_str

        posts.append({
            'voiceId': voice['id'],
            'voiceName': voice['name'],
            'platform': 'x',
            'text': text[:500],
            'sourceUrl': source_url,
            'timestamp': timestamp,
            'type': 'tweet',
        })
    return posts


def _parse_nitter_rss(voice, rss, nitter_host):
    """Parse Nitter RSS XML into standard post objects."""
    x_handle = voice.get('handles', {}).get('x', '').lstrip('@')
    posts = []

    items = re.findall(r'<item>(.*?)</item>', rss, re.DOTALL)
    for item in items[:20]:  # last 20 tweets
        # Get tweet text from description (cleaner than title)
        desc_match = re.search(r'<description><!\[CDATA\[(.*?)\]\]></description>', item, re.DOTALL)
        title_match = re.search(r'<title>(.*?)</title>', item)
        link_match = re.search(r'<link>(.*?)</link>', item)
        pub_match = re.search(r'<pubDate>(.*?)</pubDate>', item)

        text = ''
        if desc_match:
            # Strip HTML tags from description
            text = re.sub(r'<[^>]+>', '', desc_match.group(1)).strip()
        elif title_match:
            text = title_match.group(1)

        text = strip_quoted_tweet(text)
        if not text or len(text) < 15:
            continue

        # Convert nitter URL to x.com URL
        source_url = ''
        if link_match:
            source_url = link_match.group(1).replace(nitter_host, 'x.com')
            # Remove #m anchor
            source_url = re.sub(r'#m$', '', source_url)

        # Skip retweets (they start with "RT by @handle:")
        if text.startswith('RT by @'):
            continue

        # Skip reposts: if the URL doesn't contain this user's handle, it's someone else's tweet
        if source_url and x_handle.lower() not in source_url.lower():
            continue

        # Parse pubDate to ISO format
        timestamp = ''
        if pub_match:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(pub_match.group(1))
                timestamp = dt.isoformat()
            except Exception:
                timestamp = pub_match.group(1)

        posts.append({
            'voiceId': voice['id'],
            'voiceName': voice['name'],
            'platform': 'x',
            'text': text[:500],
            'sourceUrl': source_url,
            'timestamp': timestamp,
            'type': 'tweet',
        })

    return posts


# Track X/Twitter collection failures for monitoring
_x_failures = {'rssapp': 0, 'nitter': 0, 'total_attempts': 0, 'successes': 0, 'failed_voices': []}


def fetch_x_posts(voice):
    """Pull recent tweets from X/Twitter via rss.app (if configured) or Nitter RSS (free, no auth)."""
    x_handle = voice.get('handles', {}).get('x')
    if not x_handle:
        return []

    # Strip @ if present
    x_handle = x_handle.lstrip('@')
    _x_failures['total_attempts'] += 1

    # Try rss.app feed first if configured
    rssapp_url = voice.get('feeds', {}).get('x', '')
    if rssapp_url and 'rss.app' in rssapp_url:
        try:
            req = urllib.request.Request(rssapp_url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            posts = _parse_rssapp_json(voice, data)
            if posts:
                _x_failures['successes'] += 1
                return posts
        except Exception:
            _x_failures['rssapp'] += 1

    # Try each Nitter instance in order. NOTE: the public Nitter ecosystem has
    # degraded to ~1 reliable instance (nitter.net), and the GitHub-runner IP
    # gets rate-limited by it during a full run -- the real cause of zero-post
    # voices. A retry sweep was tried and reverted (it only added load to the
    # one instance and reduced total yield). Durable fix is an infra change:
    # rotating/residential proxy or a paid X source.
    for instance in NITTER_INSTANCES:
        try:
            url = f'{instance}/{x_handle}/rss'
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                rss = resp.read().decode('utf-8')

            # Extract the hostname for URL replacement (e.g. "nitter.net")
            nitter_host = instance.replace('https://', '').replace('http://', '')
            posts = _parse_nitter_rss(voice, rss, nitter_host)
            if posts:
                _x_failures['successes'] += 1
                return posts
        except Exception as e:
            if '404' in str(e):
                break  # user doesn't exist, no point trying other instances
            continue  # instance down, try next

    # All methods failed for this voice
    _x_failures['nitter'] += 1
    _x_failures['failed_voices'].append(voice['name'])
    return []


# ─── YOUTUBE RSS ──────────────────────────────────────────────────────────────

def fetch_youtube_posts(voice):
    """Pull recent videos from YouTube RSS feed (free, unlimited)."""
    yt_feed = voice.get('feeds', {}).get('youtube')
    if not yt_feed:
        return []

    posts = []
    try:
        req = urllib.request.Request(yt_feed, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml = resp.read().decode('utf-8')

        entries = re.findall(r'<entry>(.*?)</entry>', xml, re.DOTALL)
        for entry in entries[:10]:  # last 10 videos
            title_match = re.search(r'<title>(.*?)</title>', entry)
            link_match = re.search(r'<link rel="alternate" href="(.*?)"', entry)
            published_match = re.search(r'<published>(.*?)</published>', entry)

            if title_match and link_match:
                posts.append({
                    'voiceId': voice['id'],
                    'voiceName': voice['name'],
                    'platform': 'youtube',
                    'text': title_match.group(1),
                    'sourceUrl': link_match.group(1),
                    'timestamp': published_match.group(1) if published_match else '',
                    'type': 'video_title',
                })
    except Exception as e:
        # A 404 here almost always means a stale/renamed channel_id, which
        # otherwise silently yields zero posts forever. Surface it like the
        # Bluesky/Substack/Instagram fetchers do so dead feeds get noticed.
        print(f"    ⚠ YouTube fetch failed for {voice['id']} ({yt_feed}): {e}")

    return posts


# ─── YOUTUBE TRANSCRIPTS ─────────────────────────────────────────────────────

def _ytdlp_transcript(video_id):
    """Fallback: fetch YouTube transcript via yt-dlp when API is IP blocked.
    Tries with Chrome cookies first (higher rate limit), then without."""
    import subprocess
    import tempfile

    # Skip cookies — can hang on macOS Keychain prompts in automated runs
    for use_cookies in [False]:
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                cmd = [
                    'yt-dlp',
                    f'https://www.youtube.com/watch?v={video_id}',
                    '--write-auto-sub', '--sub-lang', 'en',
                    '--skip-download', '--no-warnings',
                    '-o', f'{tmpdir}/%(id)s.%(ext)s',
                ]
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=20,
                )
                # Look for .vtt file
                import glob
                vtt_files = glob.glob(f'{tmpdir}/*.vtt')
                if not vtt_files:
                    continue  # try next cookie option
                vtt_text = Path(vtt_files[0]).read_text()
                # Parse VTT: strip timestamps and metadata, keep text
                lines = []
                for line in vtt_text.split('\n'):
                    line = line.strip()
                    if not line or line.startswith('WEBVTT') or line.startswith('Kind:') or line.startswith('Language:'):
                        continue
                    if re.match(r'^\d{2}:\d{2}', line) or re.match(r'^\d+$', line):
                        continue
                    # Strip VTT tags like <c> </c>
                    line = re.sub(r'<[^>]+>', '', line)
                    if line and line not in lines[-1:]:  # deduplicate consecutive
                        lines.append(line)
                return ' '.join(lines[:200])  # ~first 5 min worth
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ''
        except Exception:
            continue  # try without cookies
    return ''


def enrich_transcripts(posts):
    """Add transcript text to YouTube posts (free)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import IpBlocked
    except ImportError:
        print("    ⚠ youtube-transcript-api not installed, skipping transcripts")
        return posts

    # Load cache
    cache = {}
    if TRANSCRIPT_CACHE.exists():
        try:
            cache = json.loads(TRANSCRIPT_CACHE.read_text())
        except Exception:
            cache = {}

    ytt_api = YouTubeTranscriptApi()
    fetched_new = 0
    ip_blocked = False

    for p in posts:
        if p['platform'] != 'youtube':
            continue

        vid_match = re.search(r'(?:watch\?v=|youtu\.be/)([\w-]+)', p['sourceUrl'])
        if not vid_match:
            continue
        video_id = vid_match.group(1)

        # Check cache
        if video_id in cache:
            if cache[video_id]:
                p['text'] = f"[VIDEO: {p['text'][:100]}] {cache[video_id]}"
                p['type'] = 'video_transcript'
            continue

        # Stop fetching if IP blocked or hit cap
        if ip_blocked or fetched_new >= 100:
            continue
        try:
            import time
            time.sleep(0.5)
            transcript = ytt_api.fetch(video_id, languages=['en'])
            text_parts = []
            for snippet in transcript.snippets:
                if snippet.start > 300:  # first 5 min
                    break
                text_parts.append(snippet.text)
            if text_parts:
                transcript_text = ' '.join(text_parts)
                cache[video_id] = transcript_text[:800]
                p['text'] = f"[VIDEO: {p['text'][:100]}] {transcript_text[:800]}"
                p['type'] = 'video_transcript'
                fetched_new += 1
            else:
                cache[video_id] = ''
        except IpBlocked:
            print("    ⚠ YouTube IP rate limited — falling back to yt-dlp")
            ip_blocked = True
        except Exception:
            cache[video_id] = ''

    # Fallback: use yt-dlp for a small batch of uncached videos when IP blocked
    # Cap at 10 videos and 3 minutes total to prevent pipeline hangs
    if ip_blocked:
        import time as _time
        ytdlp_fetched = 0
        ytdlp_start = _time.time()
        for p in posts:
            if p['platform'] != 'youtube':
                continue
            vid_match = re.search(r'(?:watch\?v=|youtu\.be/)([\w-]+)', p['sourceUrl'])
            if not vid_match:
                continue
            video_id = vid_match.group(1)
            if video_id in cache:
                continue
            if ytdlp_fetched >= 10 or (_time.time() - ytdlp_start) > 180:
                break
            transcript_text = _ytdlp_transcript(video_id)
            if transcript_text:
                cache[video_id] = transcript_text[:800]
                p['text'] = f"[VIDEO: {p['text'][:100]}] {transcript_text[:800]}"
                p['type'] = 'video_transcript'
                ytdlp_fetched += 1
            else:
                cache[video_id] = ''
        if ytdlp_fetched:
            print(f"    yt-dlp fallback: {ytdlp_fetched} transcripts recovered")

    # Save cache
    TRANSCRIPT_CACHE.write_text(json.dumps(cache))
    cached_total = sum(1 for v in cache.values() if v)
    print(f"    Transcripts: {fetched_new} new, {cached_total} cached total")
    return posts


# ─── BLUESKY ─────────────────────────────────────────────────────────────────

def fetch_bluesky_posts(voice):
    """Pull recent posts from Bluesky (free, no auth)."""
    handle = voice.get('handles', {}).get('bluesky')
    if not handle:
        return []

    posts = []
    try:
        url = f'https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?actor={handle}&limit=20'
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())

        for item in data.get('feed', []):
            # Skip reposts: getAuthorFeed mixes in posts the voice reposted,
            # whose text belongs to a different author. Attributing them to this
            # voice would misquote them and build a sourceUrl under the wrong
            # profile (the reposter's handle + the original's rkey → dead link).
            if item.get('reason', {}).get('$type') == 'app.bsky.feed.defs#reasonRepost':
                continue

            post = item.get('post', {})
            record = post.get('record', {})
            text = record.get('text', '')
            if not text or len(text) < 20:
                continue

            uri = post.get('uri', '')
            post_id = uri.split('/')[-1] if '/' in uri else ''
            web_url = f'https://bsky.app/profile/{handle}/post/{post_id}'

            posts.append({
                'voiceId': voice['id'],
                'voiceName': voice['name'],
                'platform': 'bluesky',
                'text': text,
                'sourceUrl': web_url,
                'timestamp': record.get('createdAt', ''),
                'type': 'post',
            })
    except Exception as e:
        print(f"    ⚠ Bluesky failed for @{handle}: {e}")

    return posts


# ─── SUBSTACK / NEWSLETTER RSS ───────────────────────────────────────────────

def fetch_substack_posts(voice):
    """Pull recent articles from a Substack/newsletter/blog RSS feed (free, no auth).

    Also handles the 'blog' feed key (standard RSS 2.0, e.g. Election Law Blog).
    Before this, voices configured with only a 'blog' feed were silently never
    collected — collect_voice() had no blog handler.
    """
    feeds = voice.get('feeds', {})
    feed_url = feeds.get('substack') or feeds.get('blog')
    platform = 'substack' if feeds.get('substack') else 'blog'
    if not feed_url:
        return []

    posts = []
    try:
        # Substack sits behind Cloudflare; full browser headers help on most
        # IPs. NOTE: the GitHub-runner IP still gets 403'd regardless (~26/run);
        # retrying from the same blocked IP doesn't help, so we don't. Durable
        # fix is an infra change (residential/rotating proxy).
        req = urllib.request.Request(feed_url, headers=BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            rss = resp.read().decode('utf-8')

        items = re.findall(r'<item>(.*?)</item>', rss, re.DOTALL)
        for item in items[:15]:  # last 15 articles
            title_match = re.search(r'<title><!\[CDATA\[(.*?)\]\]></title>', item)
            if not title_match:
                title_match = re.search(r'<title>(.*?)</title>', item)
            link_match = re.search(r'<link>(.*?)</link>', item)
            pub_match = re.search(r'<pubDate>(.*?)</pubDate>', item)

            # Get article preview text from description
            desc_match = re.search(r'<description><!\[CDATA\[(.*?)\]\]></description>', item, re.DOTALL)
            desc_text = ''
            if desc_match:
                # Strip HTML tags, get first ~500 chars as preview
                desc_text = re.sub(r'<[^>]+>', '', desc_match.group(1)).strip()
                # Unescape HTML entities
                import html
                desc_text = html.unescape(desc_text)
                desc_text = desc_text[:500]

            title = title_match.group(1) if title_match else ''
            if not title:
                continue

            # Check for actual author (multi-author publications like Free Press)
            author_match = re.search(r'<dc:creator><!\[CDATA\[(.*?)\]\]></dc:creator>', item)
            if not author_match:
                author_match = re.search(r'<dc:creator>(.*?)</dc:creator>', item)
            if not author_match:
                author_match = re.search(r'<author>(.*?)</author>', item)
            actual_author = author_match.group(1).strip() if author_match else None

            # If a different person wrote it, prefix the title
            if actual_author and actual_author.lower() != voice['name'].lower():
                title = f"{title} (by {actual_author})"

            # Combine title + preview for richer text
            text = title
            if desc_text and len(desc_text) > 50:
                text = f"{title}. {desc_text}"

            source_url = link_match.group(1) if link_match else ''

            # Parse pubDate to ISO format
            timestamp = ''
            if pub_match:
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(pub_match.group(1))
                    timestamp = dt.isoformat()
                except Exception:
                    timestamp = pub_match.group(1)

            posts.append({
                'voiceId': voice['id'],
                'voiceName': voice['name'],
                'platform': platform,
                'text': text[:500],
                'sourceUrl': source_url,
                'timestamp': timestamp,
                'type': 'article',
            })
    except Exception as e:
        if '404' not in str(e):
            print(f"    ⚠ {platform.capitalize()} fetch failed: {e}")

    return posts


# ─── INSTAGRAM ───────────────────────────────────────────────────────────────

def _get_ig_cookies():
    """Load Instagram session cookies from Chrome Profile 1 (burner account)."""
    try:
        import browser_cookie3
        cj = browser_cookie3.chrome(
            domain_name='.instagram.com',
            cookie_file=os.path.expanduser('~/Library/Application Support/Google/Chrome/Profile 1/Cookies'),
        )
        cookies = {c.name: c.value for c in cj}
        if 'sessionid' not in cookies:
            return None
        return cookies
    except Exception:
        return None


def fetch_instagram_posts(voice):
    """Pull recent post captions from Instagram via API with session cookies."""
    handle = voice.get('handles', {}).get('instagram')
    if not handle:
        return []

    cookies = _get_ig_cookies()
    if not cookies:
        return []

    posts = []
    try:
        cookie_str = '; '.join(f'{k}={v}' for k, v in cookies.items())
        headers = {
            'User-Agent': UA,
            'X-IG-App-ID': '936619743392459',
            'X-CSRFToken': cookies.get('csrftoken', ''),
            'Cookie': cookie_str,
        }

        # Get user ID
        url = f'https://www.instagram.com/api/v1/users/web_profile_info/?username={handle}'
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        user = data['data']['user']
        user_id = user['id']

        # Get feed
        url2 = f'https://www.instagram.com/api/v1/feed/user/{user_id}/?count=12'
        req2 = urllib.request.Request(url2, headers=headers)
        with urllib.request.urlopen(req2, timeout=15) as resp2:
            feed = json.loads(resp2.read().decode())

        for item in feed.get('items', []):
            cap = item.get('caption')
            text = cap.get('text', '') if cap else ''
            if len(text) < 15:
                continue

            ts = item.get('taken_at', 0)
            timestamp = ''
            if ts:
                timestamp = datetime.utcfromtimestamp(ts).strftime('%Y-%m-%dT%H:%M:%S+00:00')

            posts.append({
                'voiceId': voice['id'],
                'voiceName': voice['name'],
                'platform': 'instagram',
                'text': text[:500],
                'sourceUrl': f'https://www.instagram.com/p/{item.get("code", "")}/',
                'timestamp': timestamp,
                'type': 'post',
            })

    except Exception as e:
        if '404' not in str(e) and 'not found' not in str(e).lower():
            print(f"    ⚠ Instagram failed for @{handle}: {e}")

    return posts


# ─── TIKTOK ──────────────────────────────────────────────────────────────────

def fetch_tiktok_posts(voice):
    """Pull recent video captions from TikTok via yt-dlp + oEmbed (free, no auth)."""
    handle = voice.get('handles', {}).get('tiktok')
    if not handle:
        return []

    import subprocess

    posts = []
    try:
        # Step 1: yt-dlp to enumerate recent video URLs
        result = subprocess.run(
            ['yt-dlp', f'https://www.tiktok.com/@{handle}',
             '--flat-playlist', '--print', '%(url)s\t%(timestamp)s',
             '--playlist-items', '1:15'],
            capture_output=True, text=True, timeout=30,
        )
        lines = [l for l in result.stdout.strip().split('\n') if l.startswith('http')]

        # Step 2: oEmbed API for captions (free, no auth, official)
        for line in lines[:10]:
            parts = line.split('\t')
            video_url = parts[0]
            ts_raw = parts[1] if len(parts) > 1 else ''

            try:
                oembed_url = f'https://www.tiktok.com/oembed?url={video_url}'
                req = urllib.request.Request(oembed_url, headers={'User-Agent': UA})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())

                title = data.get('title', '')
                if len(title) < 10:
                    continue

                # Convert unix timestamp to ISO
                timestamp = ''
                if ts_raw and ts_raw != 'NA':
                    try:
                        timestamp = datetime.utcfromtimestamp(int(ts_raw)).strftime('%Y-%m-%dT%H:%M:%S+00:00')
                    except (ValueError, OSError):
                        pass

                posts.append({
                    'voiceId': voice['id'],
                    'voiceName': voice['name'],
                    'platform': 'tiktok',
                    'text': title[:500],
                    'sourceUrl': video_url,
                    'timestamp': timestamp,
                    'type': 'video_caption',
                })
            except Exception:
                continue

    except FileNotFoundError:
        pass  # yt-dlp not installed
    except Exception as e:
        if '404' not in str(e):
            print(f"    ⚠ TikTok failed for @{handle}: {e}")

    return posts


# ─── PODCAST RSS ─────────────────────────────────────────────────────────────

def fetch_podcast_posts(voice):
    """Pull recent episodes from podcast RSS feed (free, no auth)."""
    feed_url = voice.get('feeds', {}).get('podcast')
    if not feed_url:
        return []

    posts = []
    try:
        req = urllib.request.Request(feed_url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            rss = resp.read().decode('utf-8')

        items = re.findall(r'<item>(.*?)</item>', rss, re.DOTALL)
        for item in items[:10]:  # last 10 episodes
            title_match = re.search(r'<title><!\[CDATA\[(.*?)\]\]></title>', item)
            if not title_match:
                title_match = re.search(r'<title>(.*?)</title>', item)
            link_match = re.search(r'<link>(.*?)</link>', item)
            pub_match = re.search(r'<pubDate>(.*?)</pubDate>', item)

            # Try itunes:summary first, then description
            desc_match = re.search(r'<itunes:summary><!\[CDATA\[(.*?)\]\]></itunes:summary>', item, re.DOTALL)
            if not desc_match:
                desc_match = re.search(r'<itunes:summary>(.*?)</itunes:summary>', item, re.DOTALL)
            if not desc_match:
                desc_match = re.search(r'<description><!\[CDATA\[(.*?)\]\]></description>', item, re.DOTALL)
            if not desc_match:
                desc_match = re.search(r'<description>(.*?)</description>', item, re.DOTALL)

            title = title_match.group(1) if title_match else ''
            if not title:
                continue

            # Build text from title + description preview
            desc_text = ''
            if desc_match:
                desc_text = re.sub(r'<[^>]+>', '', desc_match.group(1)).strip()
                import html
                desc_text = html.unescape(desc_text)
                desc_text = desc_text[:300]

            text = title
            if desc_text and len(desc_text) > 30:
                text = f"{title}. {desc_text}"

            source_url = link_match.group(1) if link_match else ''

            # Parse pubDate to ISO format
            timestamp = ''
            if pub_match:
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(pub_match.group(1))
                    timestamp = dt.isoformat()
                except Exception:
                    timestamp = pub_match.group(1)

            posts.append({
                'voiceId': voice['id'],
                'voiceName': voice['name'],
                'platform': 'podcast',
                'text': text[:500],
                'sourceUrl': source_url,
                'timestamp': timestamp,
                'type': 'episode',
            })
    except Exception as e:
        if '404' not in str(e):
            print(f"    ⚠ Podcast fetch failed: {e}")

    return posts


# ─── CATEGORIZE WITH CLAUDE ─────────────────────────────────────────────────

def load_taxonomy():
    """Load the fixed topic taxonomy."""
    if TAXONOMY_PATH.exists():
        taxonomy = json.loads(TAXONOMY_PATH.read_text())
        return taxonomy.get('topics', [])
    return []


def get_taxonomy_slug_list():
    """Return a formatted string of valid taxonomy slugs for the Claude prompt."""
    topics = load_taxonomy()
    if not topics:
        return ""
    lines = []
    for t in topics:
        lines.append(f'  - "{t["slug"]}" — {t["description"]}')
    return "\n".join(lines)


def enforce_taxonomy(topic_slug):
    """Map any topic slug to a canonical taxonomy slug. Fixes Claude inventing slugs."""
    topics = load_taxonomy()
    if not topics:
        return topic_slug

    # Build lookup: all valid slugs and aliases -> canonical slug
    canonical = {}
    for t in topics:
        canonical[t['slug']] = t['slug']
        for alias in t.get('aliases', []):
            canonical[alias] = t['slug']

    # Direct match (slug or alias)
    if topic_slug in canonical:
        return canonical[topic_slug]

    # Fuzzy: match on distinctive words (skip generic ones)
    GENERIC = {'politics', 'policy', 'news', 'general', 'trump', 'biden', 'war', 'media', 'culture', 'social', 'political'}
    slug_parts = set(topic_slug.split('-'))
    distinctive_parts = slug_parts - GENERIC

    # First try: match on distinctive words only
    if distinctive_parts:
        best_match = None
        best_overlap = 0
        for alias, canon in canonical.items():
            if canon == 'other':
                continue
            alias_parts = set(alias.split('-'))
            overlap = len(distinctive_parts & alias_parts)
            if overlap > best_overlap:
                best_overlap = overlap
                best_match = canon
        if best_overlap >= 1:
            return best_match

    # Second try: match on all words but require 2+ overlap
    best_match = None
    best_overlap = 0
    for alias, canon in canonical.items():
        if canon == 'other':
            continue
        alias_parts = set(alias.split('-'))
        overlap = len(slug_parts & alias_parts)
        if overlap > best_overlap:
            best_overlap = overlap
            best_match = canon
    if best_overlap >= 2:
        return best_match

    # No local match — ask Claude to map it (one cheap call)
    topics = load_taxonomy()
    slug_list = [t['slug'] for t in topics if t['slug'] != 'other']
    descriptions = {t['slug']: t['description'] for t in topics if t['slug'] != 'other'}
    desc_block = '\n'.join(f'  - "{s}": {descriptions[s]}' for s in slug_list)

    try:
        prompt = f"""Map this topic slug to the single best canonical slug from the list below.

Unknown slug: "{topic_slug}"

Canonical slugs:
{desc_block}

If none fit, respond with "other".
Respond with ONLY the canonical slug, nothing else."""

        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json.dumps({
                'model': CLAUDE_MODEL,
                'max_tokens': 32,
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
        mapped = data.get('content', [{}])[0].get('text', '').strip().strip('"').lower()
        if mapped in {t['slug'] for t in topics}:
            return mapped
    except Exception:
        pass

    return 'other'


def _parse_categorization_json(text):
    """Robustly extract a list of {index,topic,relevance,stance} dicts from a
    Claude response that may be wrapped in code fences, have trailing prose,
    or be truncated mid-array. Returns a list (possibly empty). This replaces a
    bare re.search + json.loads that failed (~15% of voices) with 'Extra data'
    errors and dumped those voices to 'uncategorized'."""
    if not text:
        return []
    t = text.strip()
    # Strip markdown code fences (```json ... ```)
    if t.startswith('```'):
        t = re.sub(r'^```[a-zA-Z]*\s*', '', t)
        t = re.sub(r'\s*```$', '', t).strip()
    # 1) Decode the first complete JSON array; raw_decode ignores trailing prose
    #    (the exact cause of the "Extra data" failures).
    start = t.find('[')
    if start != -1:
        try:
            arr, _ = json.JSONDecoder().raw_decode(t[start:])
            if isinstance(arr, list):
                return arr
        except Exception:
            pass
    # 2) Salvage path (handles truncated arrays / multi-block output): pull each
    #    individual {...} object and parse it independently.
    items = []
    for m in re.finditer(r'\{[^{}]*\}', t):
        try:
            obj = json.loads(m.group())
            if 'index' in obj:
                items.append(obj)
        except Exception:
            continue
    return items


def _quote_from_text(text):
    """Derive the display quote from a post's REAL text (never AI-generated)."""
    if text.startswith('[VIDEO: ') and '] ' in text:
        # Has transcript — pull the transcript part as the quote
        return text.split('] ', 1)[1][:300]
    # Use the actual post/title text
    return text[:300]


def load_categorized_cache(voice_id):
    """Build {sourceUrl: categorization} from the voice's recent day files
    (yesterday + today if present, today wins). Only trusts entries that were
    fully categorized AND survived the perspective filter — a day file written
    after a total Claude failure contains posts with no 'topic' key, and those
    must be re-sent to the model."""
    cache = {}
    now = datetime.now()
    for delta in (1, 0):
        day = (now - timedelta(days=delta)).strftime('%Y-%m-%d')
        path = POSTS_DIR / voice_id / f'{day}.json'
        if not path.exists():
            continue
        try:
            day_data = json.loads(path.read_text())
        except Exception:
            continue
        for p in day_data.get('posts', []):
            url = p.get('sourceUrl')
            if not url or 'topic' not in p:
                continue
            # Entries without a 'topics' list predate multi-tag (Aug 28 2026).
            # Treat them as misses so they get re-tagged once — a one-day cost
            # bump, then the cache is fully multi-tagged.
            if not p.get('topics'):
                continue
            if p.get('relevance') not in ('high', 'medium') or p.get('stance') not in ('strong', 'lean'):
                continue
            cache[url] = {
                'topic': p['topic'],
                'topics': p['topics'],
                'relevance': p['relevance'],
                'stance': p['stance'],
                'summary': p.get('summary', ''),
            }
    return cache


def split_cached_posts(posts, cache):
    """Split posts into (reused, new). Cache hits get the stored categorization
    applied verbatim and skip Claude entirely; posts without a sourceUrl or
    without a hit are returned as new and go to the model."""
    reused, new_posts = [], []
    for p in posts:
        entry = cache.get(p.get('sourceUrl')) if cache else None
        if entry:
            # Re-canonicalize against the CURRENT taxonomy — a cached slug may
            # predate a rename/removal (fresh categorizations get the same
            # treatment in _apply_categorization; idempotent otherwise).
            p['topic'] = enforce_taxonomy(entry['topic'])
            slugs = []
            for rt in entry.get('topics', [entry['topic']]):
                ct = enforce_taxonomy(rt)
                if ct and ct not in slugs:
                    slugs.append(ct)
            p['topics'] = slugs or [p['topic']]
            p['relevance'] = entry['relevance']
            p['stance'] = entry['stance']
            p['summary'] = entry['summary']
            p['quote'] = _quote_from_text(p['text'])
            reused.append(p)
        else:
            new_posts.append(p)
    _usage_stats['posts_reused'] += len(reused)
    return reused, new_posts


def _build_categorization_prompt(voice_name, posts):
    """Build the per-voice categorization prompt (shared by the sequential
    and batch paths)."""
    posts_text = ""
    for i, p in enumerate(posts):
        posts_text += f"\n[{i}] ({p['platform']}) {p['text'][:300]}\n"

    taxonomy_list = get_taxonomy_slug_list()

    return f"""Here are recent posts/videos from {voice_name}. For each one:
1. Assign 1-3 topic slugs from the FIXED TAXONOMY below ("topics", best match FIRST). You MUST use these exact slugs — do NOT invent new ones. Most posts get 1-2 slugs; add a 2nd or 3rd only when the post genuinely spans topics (an ICE-raid post is ["immigration", "trump-administration"], not just one).
2. Rate relevance to current news: "high" (clearly about a news story), "medium" (tangentially related), "low" (personal, promo, entertainment only)
3. Rate stance: Does this person EXPRESS or IMPLY a clear position, reaction, or argument?
   - "strong" = clear opinion, argument, criticism, praise, or call to action
   - "lean" = position is implied or can be inferred from framing/tone, even if not stated outright
   - "neutral" = purely informational summary, both-sides reporting, or no discernible position
4. Summarize their POSITION in 4-8 words ("summary"): a neutral, third-person label of the stance they're taking, e.g. "Backs sanctions, opposes unfreezing Iran's assets" or "Calls the strikes an illegal war". This is a LABEL, not a quote — paraphrasing is fine here. Leave "" if stance is neutral.

FIXED TAXONOMY (use ONLY these slugs):
{taxonomy_list}

CRITICAL RULES:
- You MUST pick the single best-matching slug from the list above. Never create a new slug.
- If nothing fits well, use "other".
- Do NOT make up or paraphrase quotes. Use the EXACT text from the post. (The "summary" field is the ONLY place paraphrasing is allowed — and it must stay faithful to their actual position.)
- If it's a video title, just use the title. If it includes a transcript, pull a real sentence from the transcript. Never invent words they didn't say.
- For stance: we want voices who are REACTING, not just reporting. A newsletter summarizing "here's what happened" with no opinion = "neutral". A tweet saying "this is insane" = "strong". An article that frames an issue in a way that clearly favors one side = "lean".

POSTS:
{posts_text}

Return JSON array:
[
  {{"index": 0, "topics": ["iran-conflict", "foreign-policy-diplomacy"], "relevance": "high", "stance": "strong", "summary": "Backs sanctions, opposes unfreezing Iran's assets"}},
  ...
]

Include ALL posts with "high" or "medium" relevance. Skip pure promo, personal stuff, and entertainment-only content. When in doubt, include it — we want coverage."""


def _apply_categorization(posts, categorized):
    """Apply parsed categorization items to posts in place, then run the
    perspective filter. Returns the filtered list, or posts unchanged when
    categorized is empty (mirrors the original inline behavior)."""
    if not categorized:
        return posts
    for item in categorized:
        # The model occasionally emits a malformed array element — a bare int,
        # a string, or an object whose "index" isn't a number. One bad element
        # must not crash the whole nightly collect (it did: a stray int here
        # aborted the entire run mid-way through the voice list). Skip anything
        # that isn't a well-formed {index: <number>, ...} object.
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get('index', -1))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(posts):
            # Multi-tag (Aug 28 2026): the model returns "topics" (1-3 slugs,
            # best first). Accept legacy single-"topic" responses too. The
            # primary slug stays in 'topic' so every downstream consumer
            # (stances, stories, day files) keeps working unchanged.
            raw_topics = item.get('topics') or [item.get('topic', 'uncategorized')]
            if isinstance(raw_topics, str):
                raw_topics = [raw_topics]
            slugs = []
            for rt in raw_topics[:3]:
                if not isinstance(rt, str):
                    continue
                ct = enforce_taxonomy(rt)
                if ct and ct not in slugs:
                    slugs.append(ct)
            posts[idx]['topic'] = slugs[0] if slugs else 'uncategorized'
            posts[idx]['topics'] = slugs or ['uncategorized']
            posts[idx]['relevance'] = item.get('relevance', 'low')
            posts[idx]['stance'] = item.get('stance', 'neutral')
            posts[idx]['summary'] = (item.get('summary', '') or '').strip()[:120]
            # Use REAL text, never AI-generated quotes
            posts[idx]['quote'] = _quote_from_text(posts[idx]['text'])

    # Perspective filter: keep only posts that are relevant AND where
    # the voice is actually taking a stand (strong/lean). Pure reporting,
    # promo and personal posts (neutral / low relevance) are dropped so
    # downstream only ever sees genuine perspectives.
    kept = [p for p in posts
            if p.get('relevance') in ('high', 'medium')
            and p.get('stance') in ('strong', 'lean')]
    dropped = len(posts) - len(kept)
    if dropped:
        print(f"    Perspective filter: kept {len(kept)}, dropped {dropped} (no stance / not news)")
    return kept


def categorize_posts(voice_name, posts):
    """Use Claude to categorize posts by news topic and filter garbage.

    Sequential per-voice call. Also the mandatory fallback path when the
    Message Batches path (categorize_posts_batch) is unavailable or a batch
    result is errored/expired/missing."""
    if not ANTHROPIC_API_KEY or not posts:
        return posts

    prompt = _build_categorization_prompt(voice_name, posts)

    # Track usage for cost estimation
    _usage_stats['claude_calls'] += 1
    _usage_stats['total_input_chars'] += len(prompt)

    # Retry logic: 3 attempts with exponential backoff
    last_error = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                'https://api.anthropic.com/v1/messages',
                data=json.dumps({
                    'model': CLAUDE_MODEL,
                    'max_tokens': 2048,
                    'messages': [{'role': 'user', 'content': prompt}],
                }).encode(),
                headers={
                    'x-api-key': ANTHROPIC_API_KEY,
                    'anthropic-version': '2023-06-01',
                    'content-type': 'application/json',
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
            break  # success
        except Exception as e:
            last_error = e
            if attempt < 2:
                wait = (attempt + 1) * 2
                print(f"    Retry {attempt + 1}/3 after {wait}s: {e}")
                import time as _t
                _t.sleep(wait)
            continue
    else:
        print(f"    Claude categorization failed after 3 attempts: {last_error}")
        return posts

    try:

        result_text = data.get('content', [{}])[0].get('text', '')
        # Track output tokens from API response if available
        usage = data.get('usage', {})
        if usage.get('output_tokens'):
            _usage_stats['total_output_tokens_est'] += usage['output_tokens']
        else:
            _usage_stats['total_output_tokens_est'] += 500  # rough estimate per call
        categorized = _parse_categorization_json(result_text)
        if categorized:
            return _apply_categorization(posts, categorized)

    except Exception as e:
        print(f"    ⚠ Claude categorization failed: {e}")

    return posts


def categorize_posts_batch(voice_entries):
    """Categorize many voices' posts in one Message Batches call (50% price).

    voice_entries: list of (voice_id, voice_name, posts) with non-empty posts.
    Returns {voice_id: parsed_items} for every request that SUCCEEDED (parsed
    items may be an empty list — that mirrors the sequential unparseable-response
    behavior and must NOT trigger a re-send). Voices whose result was errored,
    expired, canceled, or missing are absent from the dict — the caller must
    fall back to the sequential categorize_posts() path for those.
    Returns None when the batch could not be created, the SDK is unavailable,
    or polling timed out — the caller falls back to sequential for ALL voices.
    """
    if not ANTHROPIC_API_KEY or not voice_entries:
        return None

    try:
        import anthropic
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request
    except Exception as e:
        print(f"    anthropic SDK unavailable ({e}); using sequential categorization")
        return None

    # Build one request per voice, keyed by a synthetic custom_id (voice ids
    # are slugs, but custom_id has charset/length limits — index is safe).
    custom_ids = {}
    requests = []
    for i, (vid, voice_name, posts) in enumerate(voice_entries):
        prompt = _build_categorization_prompt(voice_name, posts)
        _usage_stats['claude_calls'] += 1
        _usage_stats['total_input_chars'] += len(prompt)
        custom_id = f'voice-{i}'
        custom_ids[custom_id] = vid
        requests.append(Request(
            custom_id=custom_id,
            params=MessageCreateParamsNonStreaming(
                model=CLAUDE_MODEL,
                max_tokens=2048,
                messages=[{'role': 'user', 'content': prompt}],
            ),
        ))

    import time as _t
    batch = None
    client = None

    def _cancel_batch():
        # Never leave a submitted batch running while we re-pay sequentially.
        if batch is not None and client is not None:
            try:
                client.messages.batches.cancel(batch.id)
            except Exception:
                pass

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        batch = client.messages.batches.create(requests=requests)
        print(f"    Batch {batch.id}: {len(requests)} voices submitted")

        # Poll until ended, with a hard deadline (the GH Action job has a
        # 90-minute budget shared with the rest of the pipeline). A transient
        # poll error must NOT abandon the batch — retry until the deadline
        # (same discipline as stories.py's poll loop).
        deadline = _t.monotonic() + BATCH_POLL_TIMEOUT
        while True:
            try:
                batch = client.messages.batches.retrieve(batch.id)
                if batch.processing_status == 'ended':
                    break
            except Exception as poll_err:
                print(f"    Batch poll error ({poll_err}); retrying")
            if _t.monotonic() >= deadline:
                print(f"    Batch {batch.id} timed out after {BATCH_POLL_TIMEOUT // 60}min; falling back to sequential")
                _cancel_batch()
                return None
            _t.sleep(BATCH_POLL_INTERVAL)

        # Results arrive in ANY order — key strictly by custom_id.
        results = {}
        for result in client.messages.batches.results(batch.id):
            vid = custom_ids.get(result.custom_id)
            if vid is None:
                continue
            if result.result.type == 'succeeded':
                msg = result.result.message
                text = next((b.text for b in msg.content if b.type == 'text'), '')
                usage = getattr(msg, 'usage', None)
                if usage is not None and getattr(usage, 'output_tokens', None):
                    _usage_stats['total_output_tokens_est'] += usage.output_tokens
                else:
                    _usage_stats['total_output_tokens_est'] += 500  # rough estimate per call
                results[vid] = _parse_categorization_json(text)
            else:
                print(f"    Batch result for {vid}: {result.result.type}; will retry sequentially")
        return results
    except Exception as e:
        print(f"    ⚠ Batch categorization failed ({e}); falling back to sequential")
        _cancel_batch()
        return None


# ─── MAIN ────────────────────────────────────────────────────────────────────

def collect_voice(voice):
    """Collect and categorize all recent posts from a single voice."""
    print(f"\n  📥 {voice['name']}...")

    all_posts = []

    # X/Twitter (via Nitter RSS)
    x_posts = fetch_x_posts(voice)
    if x_posts:
        print(f"    X/Twitter: {len(x_posts)} tweets")
    all_posts.extend(x_posts)

    # YouTube
    yt_posts = fetch_youtube_posts(voice)
    if yt_posts:
        print(f"    YouTube: {len(yt_posts)} videos")
    all_posts.extend(yt_posts)

    # Bluesky
    bsky_posts = fetch_bluesky_posts(voice)
    if bsky_posts:
        print(f"    Bluesky: {len(bsky_posts)} posts")
    all_posts.extend(bsky_posts)

    # Substack / Newsletter
    sub_posts = fetch_substack_posts(voice)
    if sub_posts:
        print(f"    Substack: {len(sub_posts)} articles")
    all_posts.extend(sub_posts)

    # Instagram
    ig_posts = fetch_instagram_posts(voice)
    if ig_posts:
        print(f"    Instagram: {len(ig_posts)} posts")
    all_posts.extend(ig_posts)

    # TikTok
    tt_posts = fetch_tiktok_posts(voice)
    if tt_posts:
        print(f"    TikTok: {len(tt_posts)} videos")
    all_posts.extend(tt_posts)

    # Podcast
    pod_posts = fetch_podcast_posts(voice)
    if pod_posts:
        print(f"    Podcast: {len(pod_posts)} episodes")
    all_posts.extend(pod_posts)

    if not all_posts:
        print(f"    No posts found")
        return []

    return all_posts


def log_usage(voices_collected, posts_collected):
    """Append today's usage stats to data/usage-log.json for cost monitoring."""
    date = datetime.now().strftime('%Y-%m-%d')

    # Estimate tokens: ~4 chars per token for input
    est_input_tokens = _usage_stats['total_input_chars'] // 4
    est_output_tokens = _usage_stats['total_output_tokens_est']

    # Sonnet pricing: $3/M input, $15/M output
    est_cost = (est_input_tokens / 1_000_000 * 0.80) + (est_output_tokens / 1_000_000 * 4.0)

    entry = {
        'date': date,
        'voices_collected': voices_collected,
        'posts_collected': posts_collected,
        'claude_calls': _usage_stats['claude_calls'],
        'posts_reused': _usage_stats['posts_reused'],
        'estimated_input_tokens': est_input_tokens,
        'estimated_output_tokens': est_output_tokens,
        'estimated_cost_usd': round(est_cost, 2),
        'x_health': {
            'attempts': _x_failures['total_attempts'],
            'successes': _x_failures['successes'],
            'success_rate': round(_x_failures['successes'] / max(_x_failures['total_attempts'], 1) * 100),
            'failed_voices': _x_failures['failed_voices'][:20],
        },
    }

    # Load existing log or start fresh
    log = []
    if USAGE_LOG_PATH.exists():
        try:
            log = json.loads(USAGE_LOG_PATH.read_text())
        except Exception:
            log = []

    log.append(entry)
    USAGE_LOG_PATH.write_text(json.dumps(log, indent=2))
    print(f"\n  💰 Usage: {_usage_stats['claude_calls']} Claude calls, {_usage_stats['posts_reused']} posts reused, ~{est_input_tokens:,} input tokens, ~{est_output_tokens:,} output tokens, ~${est_cost:.2f}")


def main():
    args = sys.argv[1:]

    single_voice = None
    skip_categorize = False
    for i, arg in enumerate(args):
        if arg == '--voice' and i + 1 < len(args):
            single_voice = args[i + 1]
        if arg == '--no-categorize':
            skip_categorize = True

    voices = load_voices()

    print(f"\n  ╔══════════════════════════════════════════════╗")
    print(f"  ║   NEWSREEL PERSPECTIVES — Daily Collector    ║")
    print(f"  ╚══════════════════════════════════════════════╝")
    print(f"\n  Tracking {len(voices)} voices")
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d')}")

    if single_voice:
        voices = [v for v in voices if v['id'] == single_voice]
        if not voices:
            print(f"  ⚠ Voice '{single_voice}' not found")
            return

    POSTS_DIR.mkdir(parents=True, exist_ok=True)

    # Phase 1: Collect raw posts from all voices
    all_voice_posts = {}
    total_posts = 0
    for i, voice in enumerate(voices):
        posts = collect_voice(voice)
        if i > 0 and i % 5 == 0:
            import time
            time.sleep(1)  # rate limit nitter
        if posts:
            all_voice_posts[voice['id']] = {
                'voice': voice,
                'posts': posts,
            }
            total_posts += len(posts)

    print(f"\n  📊 Collected {total_posts} posts from {len(all_voice_posts)} voices")

    # Phase 2: Enrich YouTube transcripts
    all_posts_flat = []
    for vid, data in all_voice_posts.items():
        all_posts_flat.extend(data['posts'])

    print(f"\n  📝 Enriching transcripts...")
    all_posts_flat = enrich_transcripts(all_posts_flat)

    # Phase 3: Categorize with Claude (per voice)
    if not skip_categorize:
        print(f"\n  🤖 Categorizing posts with Claude...")
        import time

        # 3a: Reuse categorizations from recent day files — feeds re-surface
        # the same posts night after night, so only genuinely new posts go
        # to the model.
        pending = []       # (voice_id, voice_name, uncached_posts)
        reused_posts = {}  # voice_id -> posts categorized from cache
        for vid, data in all_voice_posts.items():
            voice_posts = [p for p in all_posts_flat if p['voiceId'] == vid]
            cache = load_categorized_cache(vid)
            reused, new_posts = split_cached_posts(voice_posts, cache)
            reused_posts[vid] = reused
            data['posts'] = reused
            if new_posts:
                pending.append((vid, data['voice']['name'], new_posts))
        if _usage_stats['posts_reused']:
            print(f"    ♻ Reused categorization for {_usage_stats['posts_reused']} posts from recent day files")

        # 3b: One Message Batch for all new posts (50% price). Any voice the
        # batch can't cover falls back to the sequential per-voice call.
        batch_results = categorize_posts_batch(pending)
        for vid, voice_name, new_posts in pending:
            items = None if batch_results is None else batch_results.get(vid)
            if items is not None:
                categorized = _apply_categorization(new_posts, items)
            else:
                categorized = categorize_posts(voice_name, new_posts)
                time.sleep(0.5)  # rate limit Claude calls (Haiku handles this)
            data = all_voice_posts[vid]
            data['posts'] = reused_posts[vid] + categorized
            topics = set(p.get('topic', '?') for p in data['posts'])
            if data['posts']:
                print(f"    {voice_name}: {len(data['posts'])} relevant posts — {', '.join(topics)}")

        # Log usage after categorization
        log_usage(len(all_voice_posts), total_posts)

    # Phase 4: Save organized posts
    date = datetime.now().strftime('%Y-%m-%d')
    for vid, data in all_voice_posts.items():
        voice_dir = POSTS_DIR / vid
        voice_dir.mkdir(parents=True, exist_ok=True)

        output = {
            'voiceId': vid,
            'voiceName': data['voice']['name'],
            'collectedAt': datetime.now().isoformat(),
            'date': date,
            'posts': data['posts'],
            'topicSummary': {},
        }

        # Build topic summary
        for p in data['posts']:
            topic = p.get('topic', 'uncategorized')
            if topic not in output['topicSummary']:
                output['topicSummary'][topic] = []
            output['topicSummary'][topic].append({
                'quote': p.get('quote', p['text'][:200]),
                'sourceUrl': p['sourceUrl'],
                'platform': p['platform'],
                'timestamp': p['timestamp'],
            })

        out_path = voice_dir / f'{date}.json'
        out_path.write_text(json.dumps(output, indent=2))

    # Phase 5: Build topic index (all voices, all topics)
    # Load transcript cache so YouTube quotes use real excerpts, not titles
    yt_cache = {}
    if TRANSCRIPT_CACHE.exists():
        try:
            yt_cache = json.loads(TRANSCRIPT_CACHE.read_text())
        except Exception:
            pass

    topic_index = {}
    uncategorized_fixed = 0
    for vid, data in all_voice_posts.items():
        for p in data['posts']:
            # Multi-tag (Aug 28 2026): index the post under EVERY tag so an
            # ICE-raid post tagged ["immigration", "trump-administration"] is
            # findable from both searches. lookup dedupes by sourceUrl, so the
            # same post never renders twice.
            tags = p.get('topics') or [p.get('topic', 'uncategorized')]

            # For YouTube posts with only a title, try transcript cache
            quote = p.get('quote', p['text'][:200])
            if p['platform'] == 'youtube' and p.get('type') == 'video_title':
                vid_match = re.search(r'(?:watch\?v=|youtu\.be/)([\w-]+)', p.get('sourceUrl', ''))
                if vid_match and vid_match.group(1) in yt_cache and yt_cache[vid_match.group(1)]:
                    quote = yt_cache[vid_match.group(1)][:300]

            for topic in tags:
                # Safety net: enforce taxonomy on every topic slug
                if topic and topic != 'uncategorized':
                    topic = enforce_taxonomy(topic)
                else:
                    # Post was never categorized — skip it from the index
                    # (it adds noise and dilutes story matching)
                    continue

                if topic == 'other':
                    continue  # skip catch-all bucket

                if topic not in topic_index:
                    topic_index[topic] = []

                topic_index[topic].append({
                    'voiceId': vid,
                    'voiceName': data['voice']['name'],
                    'quote': quote,
                    'sourceUrl': p['sourceUrl'],
                    'platform': p['platform'],
                    'timestamp': p['timestamp'],
                })

    index_path = POSTS_DIR / f'topic-index-{date}.json'
    index_path.write_text(json.dumps(topic_index, indent=2))

    print(f"\n  ✓ Saved posts for {len(all_voice_posts)} voices")
    print(f"  ✓ Topic index: {len(topic_index)} topics")
    for topic, posts in sorted(topic_index.items(), key=lambda x: -len(x[1])):
        names = list(set(p['voiceName'] for p in posts))[:5]
        print(f"    [{len(posts)}] {topic}: {', '.join(names)}")

    # X/Twitter health report
    if _x_failures['total_attempts'] > 0:
        success_rate = _x_failures['successes'] / _x_failures['total_attempts'] * 100
        print(f"\n  X/Twitter Health: {_x_failures['successes']}/{_x_failures['total_attempts']} voices collected ({success_rate:.0f}%)")
        if _x_failures['failed_voices']:
            print(f"  ⚠ Failed voices ({len(_x_failures['failed_voices'])}): {', '.join(_x_failures['failed_voices'][:10])}")
            if len(_x_failures['failed_voices']) > 10:
                print(f"    ... and {len(_x_failures['failed_voices']) - 10} more")
        if success_rate < 50:
            configured = sum(1 for v in load_voices()
                             if ((v.get('feeds') or {}).get('x') or ''))
            print(f"  🚨 X collection is below 50%. Public Nitter is gone (see NITTER_INSTANCES),")
            print(f"     so a voice only collects from X if it has a WORKING feeds.x URL:")
            print(f"     {configured} of {_x_failures['total_attempts']} attempted have one.")
            print(f"     Restore options: add rss.app feeds to voices.json (feeds.x), or cover")
            print(f"     these voices from Bluesky (scripts/discover_bluesky.py).")

    print(f"\n  Done!\n")


if __name__ == '__main__':
    main()
