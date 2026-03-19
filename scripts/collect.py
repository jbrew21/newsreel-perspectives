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
}

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'

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


def load_voices():
    return json.loads(VOICES_PATH.read_text())


# ─── X/TWITTER VIA NITTER RSS ────────────────────────────────────────────────

NITTER_INSTANCES = [
    'https://nitter.net',
    'https://nitter.privacydev.net',
    'https://nitter.poast.org',
    'https://nitter.esmailelbob.xyz',
]


def _parse_rssapp_json(voice, data):
    """Parse rss.app JSON format into standard post objects."""
    x_handle = voice.get('handles', {}).get('x', '').lstrip('@')
    posts = []
    for item in data.get('items', [])[:20]:
        text = item.get('title', '')
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
            except:
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
            except:
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

    # Try each Nitter instance in order
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
    except:
        pass

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
        except:
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
        except:
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
    except:
        pass

    return posts


# ─── SUBSTACK / NEWSLETTER RSS ───────────────────────────────────────────────

def fetch_substack_posts(voice):
    """Pull recent articles from Substack/newsletter RSS feed (free, no auth)."""
    feed_url = voice.get('feeds', {}).get('substack')
    if not feed_url:
        return []

    posts = []
    try:
        req = urllib.request.Request(feed_url, headers={'User-Agent': UA})
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
                except:
                    timestamp = pub_match.group(1)

            posts.append({
                'voiceId': voice['id'],
                'voiceName': voice['name'],
                'platform': 'substack',
                'text': text[:500],
                'sourceUrl': source_url,
                'timestamp': timestamp,
                'type': 'article',
            })
    except Exception as e:
        if '404' not in str(e):
            print(f"    ⚠ Substack fetch failed: {e}")

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
                except:
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
                'model': 'claude-haiku-3-5-20241022',
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


def categorize_posts(voice_name, posts):
    """Use Claude to categorize posts by news topic and filter garbage."""
    if not ANTHROPIC_API_KEY or not posts:
        return posts

    posts_text = ""
    for i, p in enumerate(posts):
        posts_text += f"\n[{i}] ({p['platform']}) {p['text'][:300]}\n"

    taxonomy_list = get_taxonomy_slug_list()

    prompt = f"""Here are recent posts/videos from {voice_name}. For each one:
1. Assign a topic slug from the FIXED TAXONOMY below. You MUST use one of these exact slugs — do NOT invent new ones.
2. Rate relevance to current news: "high" (clearly about a news story), "medium" (tangentially related), "low" (personal, promo, entertainment only)
3. Rate stance: Does this person EXPRESS or IMPLY a clear position, reaction, or argument?
   - "strong" = clear opinion, argument, criticism, praise, or call to action
   - "lean" = position is implied or can be inferred from framing/tone, even if not stated outright
   - "neutral" = purely informational summary, both-sides reporting, or no discernible position

FIXED TAXONOMY (use ONLY these slugs):
{taxonomy_list}

CRITICAL RULES:
- You MUST pick the single best-matching slug from the list above. Never create a new slug.
- If nothing fits well, use "other".
- Do NOT make up or paraphrase quotes. Use the EXACT text from the post.
- If it's a video title, just use the title. If it includes a transcript, pull a real sentence from the transcript. Never invent words they didn't say.
- For stance: we want voices who are REACTING, not just reporting. A newsletter summarizing "here's what happened" with no opinion = "neutral". A tweet saying "this is insane" = "strong". An article that frames an issue in a way that clearly favors one side = "lean".

POSTS:
{posts_text}

Return JSON array:
[
  {{"index": 0, "topic": "iran-conflict", "relevance": "high", "stance": "strong"}},
  ...
]

Include ALL posts with "high" or "medium" relevance. Skip pure promo, personal stuff, and entertainment-only content. When in doubt, include it — we want coverage."""

    # Track usage for cost estimation
    _usage_stats['claude_calls'] += 1
    _usage_stats['total_input_chars'] += len(prompt)

    try:
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json.dumps({
                'model': 'claude-haiku-4-5-20251001',
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

        result_text = data.get('content', [{}])[0].get('text', '')
        # Track output tokens from API response if available
        usage = data.get('usage', {})
        if usage.get('output_tokens'):
            _usage_stats['total_output_tokens_est'] += usage['output_tokens']
        else:
            _usage_stats['total_output_tokens_est'] += 500  # rough estimate per call
        json_match = re.search(r'\[[\s\S]*\]', result_text)
        if json_match:
            categorized = json.loads(json_match.group())
            for item in categorized:
                idx = item.get('index', -1)
                if 0 <= idx < len(posts):
                    raw_topic = item.get('topic', 'uncategorized')
                    posts[idx]['topic'] = enforce_taxonomy(raw_topic)
                    posts[idx]['relevance'] = item.get('relevance', 'low')
                    posts[idx]['stance'] = item.get('stance', 'neutral')
                    # Use REAL text, never AI-generated quotes
                    original = posts[idx]['text']
                    if original.startswith('[VIDEO: ') and '] ' in original:
                        # Has transcript — pull the transcript part as the quote
                        posts[idx]['quote'] = original.split('] ', 1)[1][:300]
                    else:
                        # Use the actual post/title text
                        posts[idx]['quote'] = original[:300]

            # Filter: must be relevant AND taking a position
            return [p for p in posts
                    if p.get('relevance') in ('high', 'medium')
                    and p.get('stance') in ('strong', 'lean')]

    except Exception as e:
        print(f"    ⚠ Claude categorization failed: {e}")

    return posts


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
    print(f"\n  💰 Usage: {_usage_stats['claude_calls']} Claude calls, ~{est_input_tokens:,} input tokens, ~{est_output_tokens:,} output tokens, ~${est_cost:.2f}")


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
        for vid, data in all_voice_posts.items():
            voice_posts = [p for p in all_posts_flat if p['voiceId'] == vid]
            categorized = categorize_posts(data['voice']['name'], voice_posts)
            data['posts'] = categorized
            topics = set(p.get('topic', '?') for p in categorized)
            if categorized:
                print(f"    {data['voice']['name']}: {len(categorized)} relevant posts — {', '.join(topics)}")

            import time
            time.sleep(1)  # rate limit Claude calls

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
        except:
            pass

    topic_index = {}
    uncategorized_fixed = 0
    for vid, data in all_voice_posts.items():
        for p in data['posts']:
            topic = p.get('topic', 'uncategorized')

            # Safety net: enforce taxonomy on every topic slug
            if topic and topic != 'uncategorized':
                topic = enforce_taxonomy(topic)
            elif topic == 'uncategorized' or not topic:
                # Post was never categorized — skip it from the index
                # (it adds noise and dilutes story matching)
                continue

            if topic == 'other':
                continue  # skip catch-all bucket

            if topic not in topic_index:
                topic_index[topic] = []

            # For YouTube posts with only a title, try transcript cache
            quote = p.get('quote', p['text'][:200])
            if p['platform'] == 'youtube' and p.get('type') == 'video_title':
                vid_match = re.search(r'(?:watch\?v=|youtu\.be/)([\w-]+)', p.get('sourceUrl', ''))
                if vid_match and vid_match.group(1) in yt_cache and yt_cache[vid_match.group(1)]:
                    quote = yt_cache[vid_match.group(1)][:300]

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
            print(f"  🚨 CRITICAL: X collection below 50%. Nitter may be down. Consider rss.app migration.")

    print(f"\n  Done!\n")


if __name__ == '__main__':
    main()
