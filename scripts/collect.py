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
ENV_PATH = ROOT.parent / "newsletter" / ".env"

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'

# Load env
def load_env():
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                key, _, val = line.partition('=')
                os.environ[key.strip()] = val.strip()

load_env()
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')


def load_voices():
    return json.loads(VOICES_PATH.read_text())


# ─── X/TWITTER VIA NITTER RSS ────────────────────────────────────────────────

NITTER_INSTANCE = 'https://nitter.net'

def fetch_x_posts(voice):
    """Pull recent tweets from X/Twitter via Nitter RSS (free, no auth)."""
    x_handle = voice.get('handles', {}).get('x')
    if not x_handle:
        return []

    # Strip @ if present
    x_handle = x_handle.lstrip('@')

    posts = []
    try:
        url = f'{NITTER_INSTANCE}/{x_handle}/rss'
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            rss = resp.read().decode('utf-8')

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
                source_url = link_match.group(1).replace('nitter.net', 'x.com')
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
    except Exception as e:
        if '404' not in str(e):
            print(f"    ⚠ X fetch failed for @{x_handle}: {e}")

    return posts


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

def enrich_transcripts(posts):
    """Add transcript text to YouTube posts (free)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
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

        # Fetch fresh
        if fetched_new >= 100:  # cap per run
            continue
        try:
            import time
            time.sleep(0.5)
            transcript = ytt_api.fetch(video_id, languages=['en'])
            text_parts = []
            for snippet in transcript.snippets:
                if snippet.start > 180:  # first 3 min
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
        except:
            cache[video_id] = ''

    # Save cache
    TRANSCRIPT_CACHE.write_text(json.dumps(cache))
    print(f"    Transcripts: {fetched_new} new, {sum(1 for v in cache.values() if v)} cached total")
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


# ─── CATEGORIZE WITH CLAUDE ─────────────────────────────────────────────────

def categorize_posts(voice_name, posts):
    """Use Claude to categorize posts by news topic and filter garbage."""
    if not ANTHROPIC_API_KEY or not posts:
        return posts

    posts_text = ""
    for i, p in enumerate(posts):
        posts_text += f"\n[{i}] ({p['platform']}) {p['text'][:300]}\n"

    prompt = f"""Here are recent posts/videos from {voice_name}. For each one:
1. Assign a topic tag (e.g. "iran-war", "ai-technology", "trump-tariffs", "campus-protests", "epstein", "personal", "promo")
2. Rate relevance to current news: "high" (clearly about a news story), "medium" (tangentially related), "low" (personal, promo, entertainment only)

CRITICAL: Do NOT make up or paraphrase quotes. Use the EXACT text from the post. If it's a video title, just use the title. If it includes a transcript, pull a real sentence from the transcript. Never invent words they didn't say.

POSTS:
{posts_text}

Return JSON array:
[
  {{"index": 0, "topic": "iran-war", "relevance": "high"}},
  ...
]

Include ALL posts with "high" or "medium" relevance. Skip pure promo, personal stuff, and entertainment-only content. When in doubt, include it — we want coverage."""

    try:
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json.dumps({
                'model': 'claude-sonnet-4-20250514',
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
        json_match = re.search(r'\[[\s\S]*\]', result_text)
        if json_match:
            categorized = json.loads(json_match.group())
            for item in categorized:
                idx = item.get('index', -1)
                if 0 <= idx < len(posts):
                    posts[idx]['topic'] = item.get('topic', 'uncategorized')
                    posts[idx]['relevance'] = item.get('relevance', 'low')
                    # Use REAL text, never AI-generated quotes
                    original = posts[idx]['text']
                    if original.startswith('[VIDEO: ') and '] ' in original:
                        # Has transcript — pull the transcript part as the quote
                        posts[idx]['quote'] = original.split('] ', 1)[1][:300]
                    else:
                        # Use the actual post/title text
                        posts[idx]['quote'] = original[:300]

            # Filter to only high/medium relevance
            return [p for p in posts if p.get('relevance') in ('high', 'medium')]

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

    if not all_posts:
        print(f"    No posts found")
        return []

    return all_posts


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
    topic_index = {}
    for vid, data in all_voice_posts.items():
        for p in data['posts']:
            topic = p.get('topic', 'uncategorized')
            if topic not in topic_index:
                topic_index[topic] = []
            topic_index[topic].append({
                'voiceId': vid,
                'voiceName': data['voice']['name'],
                'quote': p.get('quote', p['text'][:200]),
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

    print(f"\n  Done!\n")


if __name__ == '__main__':
    main()
