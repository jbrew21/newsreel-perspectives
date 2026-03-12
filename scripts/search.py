#!/usr/bin/env python3
"""
Newsreel Perspectives — Social Media Search Pipeline

Searches X, Bluesky, TikTok, Instagram, and YouTube for recent posts from
tracked voices on a given topic, then uses Claude to match quotes and cluster arguments.

Platforms:
  - X/Twitter: Playwright browser scraping (bypasses Cloudflare)
  - Bluesky: Free public API (no auth needed)
  - TikTok: Playwright browser scraping
  - Instagram: Playwright browser scraping
  - YouTube: RSS feeds (free, fast)
  - Web fallback: DuckDuckGo search for coverage/quotes

Usage:
  python scripts/search.py "Iran war Trump very soon"
  python scripts/search.py "Iran war" --headline "Trump Says Iran War Could End Very Soon" --summary "President Trump said..."
"""

import asyncio
import json
import os
import sys
import re
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).parent.parent
VOICES_PATH = ROOT / "data" / "voices.json"
FEEDS_DIR = ROOT / "data" / "feeds"
STORIES_DIR = ROOT / "data" / "stories"
TRANSCRIPT_CACHE = ROOT / "data" / "transcript_cache.json"
SEARCH_CACHE = ROOT / "data" / "search_cache.json"
ENV_PATH = ROOT.parent / "newsletter" / ".env"

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

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'


def load_voices():
    return json.loads(VOICES_PATH.read_text())


def topic_match(text, topic_words, min_matches=None):
    """Check if text is relevant to the topic."""
    if not text:
        return False
    text_lower = text.lower()
    matches = sum(1 for w in topic_words if w in text_lower)
    threshold = min_matches or min(2, len(topic_words))
    return matches >= threshold


# ─── X/TWITTER (Playwright) ───────────────────────────────────────────────────

async def search_x(topic, voices, browser_context=None):
    """Scrape X profiles via Playwright to find topic-relevant tweets."""
    print(f"\n  🐦 Searching X for '{topic}'...")

    handle_to_voice = {}
    for v in voices:
        h = v.get('handles', {}).get('x')
        if h:
            handle_to_voice[h.lower()] = v['id']

    results = []
    topic_words = set(topic.lower().split())

    if not browser_context:
        print("  ⚠ No browser context — skipping X")
        return results

    page = await browser_context.new_page()

    for handle, voice_id in list(handle_to_voice.items()):
        try:
            resp = await page.goto(f'https://x.com/{handle}', wait_until='domcontentloaded', timeout=12000)
            if not resp or resp.status != 200:
                continue
            await asyncio.sleep(2)

            tweet_els = await page.query_selector_all('[data-testid="tweetText"]')
            for el in tweet_els[:10]:
                text = await el.inner_text()
                if topic_match(text, topic_words):
                    # Try to get tweet URL from parent article
                    article = await el.evaluate_handle('el => el.closest("article")')
                    link_els = await article.query_selector_all('a[href*="/status/"]') if article else []
                    tweet_url = f'https://x.com/{handle}'
                    for link_el in link_els:
                        href = await link_el.get_attribute('href')
                        if href and '/status/' in href:
                            tweet_url = f'https://x.com{href}' if href.startswith('/') else href
                            break

                    results.append({
                        'voiceId': voice_id,
                        'platform': 'x',
                        'text': text,
                        'sourceUrl': tweet_url,
                        'timestamp': datetime.now().isoformat(),
                        'username': handle,
                    })
                    print(f"    ✓ @{handle}: \"{text[:60]}...\"")

            await asyncio.sleep(0.5)
        except Exception as e:
            pass

    await page.close()
    print(f"  ✓ X: {len(results)} relevant posts found")
    return results


# ─── BLUESKY (Public API) ─────────────────────────────────────────────────────

async def search_bluesky(topic, voices):
    """Search Bluesky via its free public API."""
    print(f"\n  🦋 Searching Bluesky for '{topic}'...")

    import urllib.request

    results = []
    topic_words = set(topic.lower().split())

    # Map voices to Bluesky handles (we'll discover them)
    bsky_handles = {}
    for v in voices:
        h = v.get('handles', {}).get('bluesky')
        if h:
            bsky_handles[h] = v['id']

    # Only use verified handles from voices.json — no auto-discovery (causes misattribution)

    # Fetch recent posts for each handle
    for handle, voice_id in list(bsky_handles.items()):
        try:
            url = f'https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?actor={handle}&limit=20'
            req = urllib.request.Request(url, headers={'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())

            for item in data.get('feed', []):
                post = item.get('post', {})
                record = post.get('record', {})
                text = record.get('text', '')

                if topic_match(text, topic_words):
                    uri = post.get('uri', '')
                    # Convert AT URI to web URL
                    # at://did:plc:xxx/app.bsky.feed.post/yyy -> https://bsky.app/profile/handle/post/yyy
                    post_id = uri.split('/')[-1] if '/' in uri else ''
                    web_url = f'https://bsky.app/profile/{handle}/post/{post_id}'

                    results.append({
                        'voiceId': voice_id,
                        'platform': 'bluesky',
                        'text': text,
                        'sourceUrl': web_url,
                        'timestamp': record.get('createdAt', datetime.now().isoformat()),
                        'username': handle,
                    })
                    voice_name = next((v['name'] for v in voices if v['id'] == voice_id), '?')
                    print(f"    ✓ {voice_name}: \"{text[:60]}...\"")
        except:
            pass

    print(f"  ✓ Bluesky: {len(results)} relevant posts found")
    return results


# ─── TIKTOK (Playwright) ──────────────────────────────────────────────────────

async def search_tiktok(topic, voices, browser_context=None):
    """Scrape TikTok profiles via Playwright."""
    print(f"\n  🎵 Searching TikTok for '{topic}'...")

    handle_to_voice = {}
    for v in voices:
        h = v.get('handles', {}).get('tiktok')
        if h:
            handle_to_voice[h.lower()] = v['id']

    results = []
    topic_words = set(topic.lower().split())

    if not browser_context:
        print("  ⚠ No browser context — skipping TikTok")
        return results

    page = await browser_context.new_page()

    for handle, voice_id in list(handle_to_voice.items()):
        try:
            resp = await page.goto(f'https://www.tiktok.com/@{handle}', wait_until='domcontentloaded', timeout=12000)
            if not resp or resp.status != 200:
                continue
            await asyncio.sleep(2)

            # TikTok renders video descriptions in various selectors
            desc_els = await page.query_selector_all('[data-e2e="user-post-item-desc"]')
            if not desc_els:
                desc_els = await page.query_selector_all('[class*="DivDesContainer"] a[title]')

            for el in desc_els[:10]:
                text = await el.get_attribute('title') or await el.inner_text()
                if topic_match(text, topic_words):
                    href = await el.get_attribute('href') or ''
                    video_url = href if href.startswith('http') else f'https://www.tiktok.com/@{handle}'

                    results.append({
                        'voiceId': voice_id,
                        'platform': 'tiktok',
                        'text': text,
                        'sourceUrl': video_url,
                        'timestamp': datetime.now().isoformat(),
                        'username': handle,
                    })
                    voice_name = next((v['name'] for v in voices if v['id'] == voice_id), '?')
                    print(f"    ✓ @{handle}: \"{text[:60]}...\"")

            await asyncio.sleep(1)
        except:
            pass

    await page.close()
    print(f"  ✓ TikTok: {len(results)} relevant posts found")
    return results


# ─── INSTAGRAM (Playwright) ───────────────────────────────────────────────────

async def search_instagram(topic, voices, browser_context=None):
    """Scrape Instagram profiles via Playwright."""
    print(f"\n  📸 Searching Instagram for '{topic}'...")

    handle_to_voice = {}
    for v in voices:
        h = v.get('handles', {}).get('instagram')
        if h:
            handle_to_voice[h.lower()] = v['id']

    results = []
    topic_words = set(topic.lower().split())

    if not browser_context:
        print("  ⚠ No browser context — skipping Instagram")
        return results

    page = await browser_context.new_page()

    for handle, voice_id in list(handle_to_voice.items()):
        try:
            resp = await page.goto(f'https://www.instagram.com/{handle}/', wait_until='domcontentloaded', timeout=12000)
            if not resp or resp.status != 200:
                continue
            await asyncio.sleep(2)

            # Instagram shows post captions in alt text of images and in meta tags
            # Try extracting from the page's embedded JSON or meta descriptions
            content = await page.content()

            # Look for post descriptions in the page source
            # Instagram embeds post data in script tags
            captions = re.findall(r'"caption":\s*\{[^}]*"text":\s*"([^"]{10,500})"', content)
            alt_texts = re.findall(r'alt="([^"]{20,500})"', content)

            all_texts = captions + alt_texts
            for text in all_texts[:10]:
                text = text.replace('\\n', ' ').replace('\\u0026', '&')
                if topic_match(text, topic_words):
                    results.append({
                        'voiceId': voice_id,
                        'platform': 'instagram',
                        'text': text[:500],
                        'sourceUrl': f'https://www.instagram.com/{handle}/',
                        'timestamp': datetime.now().isoformat(),
                        'username': handle,
                    })
                    voice_name = next((v['name'] for v in voices if v['id'] == voice_id), '?')
                    print(f"    ✓ @{handle}: \"{text[:60]}...\"")
                    break  # One per user

            await asyncio.sleep(1)
        except:
            pass

    await page.close()
    print(f"  ✓ Instagram: {len(results)} relevant posts found")
    return results


# ─── YOUTUBE (RSS) ─────────────────────────────────────────────────────────────

async def search_youtube(topic, voices):
    """Search YouTube RSS feeds for recent videos mentioning the topic."""
    print(f"\n  📺 Checking YouTube feeds for '{topic}'...")

    results = []
    topic_words = set(topic.lower().split())

    for voice in voices:
        yt_feed = voice.get('feeds', {}).get('youtube')
        if not yt_feed:
            continue

        try:
            import urllib.request
            req = urllib.request.Request(yt_feed, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                xml = resp.read().decode('utf-8')

            entries = re.findall(r'<entry>(.*?)</entry>', xml, re.DOTALL)
            for entry in entries[:5]:
                title_match = re.search(r'<title>(.*?)</title>', entry)
                link_match = re.search(r'<link rel="alternate" href="(.*?)"', entry)
                published_match = re.search(r'<published>(.*?)</published>', entry)

                if title_match:
                    title = title_match.group(1)
                    if topic_match(title, topic_words):
                        results.append({
                            'voiceId': voice['id'],
                            'platform': 'youtube',
                            'text': title,
                            'sourceUrl': link_match.group(1) if link_match else '',
                            'timestamp': published_match.group(1) if published_match else '',
                            'username': voice['name'],
                        })
                        print(f"    ✓ {voice['name']}: \"{title[:60]}...\"")
        except:
            pass

    print(f"  ✓ YouTube: {len(results)} relevant videos found")

    # Now try to get transcripts for matched videos (huge coverage boost)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        for r in results:
            url = r.get('sourceUrl', '')
            vid_match = re.search(r'(?:watch\?v=|youtu\.be/)([\w-]+)', url)
            if not vid_match:
                continue
            video_id = vid_match.group(1)
            try:
                ytt_api = YouTubeTranscriptApi()
                transcript = ytt_api.fetch(video_id, languages=['en'])
                # Combine first ~2 minutes of transcript
                text_parts = []
                for snippet in transcript.snippets:
                    if snippet.start > 180:  # first 3 min
                        break
                    text_parts.append(snippet.text)
                if text_parts:
                    transcript_text = ' '.join(text_parts)
                    # Replace title-only text with transcript excerpt
                    r['text'] = f"[VIDEO: {r['text']}] {transcript_text[:800]}"
                    r['has_transcript'] = True
                    voice_name = r.get('username', '?')
                    print(f"    📝 Got transcript for {voice_name}: {len(transcript_text)} chars")
            except Exception as e:
                pass  # No transcript available (auto-generated disabled, etc.)
    except ImportError:
        print("  ℹ youtube-transcript-api not installed — using titles only")

    return results


# ─── TWIKIT (X/Twitter guest mode) ───────────────────────────────────────────

async def search_x_twikit(topic, voices):
    """Search X/Twitter using twikit guest mode — no auth, no Playwright needed."""
    print(f"\n  🐦 Searching X via twikit guest mode for '{topic}'...")

    try:
        from twikit import Client as TwikitClient
    except ImportError:
        print("  ⚠ twikit not installed — skipping")
        return []

    results = []
    topic_words = set(topic.lower().split())

    handle_to_voice = {}
    for v in voices:
        h = v.get('handles', {}).get('x')
        if h:
            handle_to_voice[h.lower()] = v['id']

    try:
        client = TwikitClient('en-US')
        await client._get_guest_token()

        # Strategy 1: Broad topic search, then match to our voices
        try:
            tweets = await client.search_tweet(topic, product='Top', count=40)
            for tweet in tweets:
                text = tweet.text or ''
                username = (tweet.user.screen_name or '').lower() if tweet.user else ''
                if username in handle_to_voice:
                    tweet_url = f'https://x.com/{username}/status/{tweet.id}'
                    results.append({
                        'voiceId': handle_to_voice[username],
                        'platform': 'x',
                        'text': text,
                        'sourceUrl': tweet_url,
                        'timestamp': str(tweet.created_at) if tweet.created_at else datetime.now().isoformat(),
                        'username': username,
                    })
                    voice_name = next((v['name'] for v in voices if v['id'] == handle_to_voice[username]), '?')
                    print(f"    ✓ @{username} (broad search): \"{text[:60]}...\"")
            await asyncio.sleep(1)
        except Exception as e:
            print(f"    ⚠ Broad search failed: {e}")

        # Strategy 2: Search per-handle for high-value voices (top 30 by followers)
        sorted_handles = sorted(handle_to_voice.items(),
                                key=lambda x: next((v.get('followers', 0) for v in voices if v['id'] == x[1]), 0),
                                reverse=True)
        found_voices = {r['voiceId'] for r in results}
        searched = 0
        for handle, voice_id in sorted_handles:
            if voice_id in found_voices or searched >= 30:
                continue
            try:
                query = f"from:{handle} {topic}"
                tweets = await client.search_tweet(query, product='Latest', count=5)
                for tweet in tweets:
                    text = tweet.text or ''
                    if topic_match(text, topic_words, min_matches=1):
                        tweet_url = f'https://x.com/{handle}/status/{tweet.id}'
                        results.append({
                            'voiceId': voice_id,
                            'platform': 'x',
                            'text': text,
                            'sourceUrl': tweet_url,
                            'timestamp': str(tweet.created_at) if tweet.created_at else datetime.now().isoformat(),
                            'username': handle,
                        })
                        voice_name = next((v['name'] for v in voices if v['id'] == voice_id), '?')
                        print(f"    ✓ @{handle}: \"{text[:60]}...\"")
                        found_voices.add(voice_id)
                        break
                searched += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                pass  # Rate limit, user not found, etc.

    except Exception as e:
        print(f"  ⚠ twikit activation failed: {e}")

    print(f"  ✓ X (twikit): {len(results)} relevant tweets found")
    return results


# ─── INSTALOADER (public Instagram) ──────────────────────────────────────────

async def search_instagram_instaloader(topic, voices):
    """Scrape public Instagram posts using instaloader — no login needed for public profiles."""
    print(f"\n  📸 Searching Instagram via instaloader for '{topic}'...")

    try:
        import instaloader
    except ImportError:
        print("  ⚠ instaloader not installed — skipping")
        return []

    results = []
    topic_words = set(topic.lower().split())

    handle_to_voice = {}
    for v in voices:
        h = v.get('handles', {}).get('instagram')
        if h:
            handle_to_voice[h.lower()] = v['id']

    if not handle_to_voice:
        print("  ℹ No Instagram handles configured")
        return results

    L = instaloader.Instaloader(
        download_pictures=False, download_videos=False,
        download_video_thumbnails=False, download_geotags=False,
        download_comments=False, save_metadata=False,
        compress_json=False, quiet=True,
        max_connection_attempts=1,
    )

    for handle, voice_id in list(handle_to_voice.items()):
        try:
            profile = await asyncio.to_thread(instaloader.Profile.from_username, L.context, handle)
            posts = profile.get_posts()
            count = 0
            for post in posts:
                if count >= 10:
                    break
                count += 1
                caption = post.caption or ''
                if topic_match(caption, topic_words):
                    results.append({
                        'voiceId': voice_id,
                        'platform': 'instagram',
                        'text': caption[:500],
                        'sourceUrl': f'https://www.instagram.com/p/{post.shortcode}/',
                        'timestamp': post.date_utc.isoformat() if post.date_utc else datetime.now().isoformat(),
                        'username': handle,
                    })
                    voice_name = next((v['name'] for v in voices if v['id'] == voice_id), '?')
                    print(f"    ✓ @{handle}: \"{caption[:60]}...\"")
                    break  # One per user

            await asyncio.sleep(0.5)
        except Exception as e:
            pass  # Private profile, rate limit, etc.

    print(f"  ✓ Instagram (instaloader): {len(results)} relevant posts found")
    return results


# ─── WEB FALLBACK (DuckDuckGo) ────────────────────────────────────────────────

async def search_web(topic, voices):
    """DuckDuckGo web search — ONLY keeps results from actual social media platforms.

    Skips generic news articles to prevent misattribution of quotes.
    """
    print(f"\n  🌐 Searching web for direct posts on '{topic}'...")

    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            print("  ✗ ddgs not installed. Run: pip install ddgs")
            return []

    results = []
    found_urls = set()

    # Check cache first (1 hour TTL)
    import hashlib
    cache_key = hashlib.md5(topic.encode()).hexdigest()
    search_cache = {}
    if SEARCH_CACHE.exists():
        try:
            search_cache = json.loads(SEARCH_CACHE.read_text())
        except:
            search_cache = {}

    cached = search_cache.get(cache_key)
    if cached:
        cache_age = (datetime.now() - datetime.fromisoformat(cached['timestamp'])).total_seconds()
        if cache_age < 3600:  # 1 hour
            print(f"  ✓ Using cached web results ({len(cached['results'])} posts, {int(cache_age)}s old)")
            return cached['results']

    # Only keep results from actual social/video platforms (direct posts)
    PLATFORM_DOMAINS = {
        'x.com': 'x', 'twitter.com': 'x',
        'tiktok.com': 'tiktok',
        'instagram.com': 'instagram',
        'youtube.com': 'youtube', 'youtu.be': 'youtube',
        'bsky.app': 'bluesky',
        'truthsocial.com': 'truthsocial',
        'rumble.com': 'rumble',
    }

    # URL patterns that indicate a specific post (not a generic profile page)
    POST_URL_PATTERNS = [
        r'x\.com/\w+/status/\d+',           # x.com/user/status/123
        r'twitter\.com/\w+/status/\d+',      # twitter.com/user/status/123
        r'youtube\.com/watch\?v=',            # youtube.com/watch?v=xxx
        r'youtu\.be/',                        # youtu.be/xxx
        r'tiktok\.com/@[\w.]+/video/\d+',    # tiktok.com/@user/video/123
        r'instagram\.com/p/',                 # instagram.com/p/xxx
        r'instagram\.com/reel/',              # instagram.com/reel/xxx
        r'bsky\.app/profile/[\w.:]+/post/',   # bsky.app/profile/user/post/xxx
        r'truthsocial\.com/.+/posts/',        # truthsocial.com/user/posts/xxx
    ]

    topic_words = set(topic.lower().split())

    ddgs = DDGS()
    for v in voices:
        name = v['name']
        try:
            # Single focused query
            query = f'"{name}" {topic} site:x.com OR site:youtube.com OR site:tiktok.com OR site:truthsocial.com'
            search_results = ddgs.text(query, max_results=5)
            for r in search_results:
                url = r.get('href', '')
                if url in found_urls:
                    continue

                # Only keep if URL is from a social platform
                platform = None
                for domain, plat in PLATFORM_DOMAINS.items():
                    if domain in url:
                        platform = plat
                        break

                if not platform:
                    continue  # Skip news articles, blogs, etc.

                title = r.get('title', '')
                body = r.get('body', '')
                text = f"{title} — {body}" if body else title

                # FILTER 1: Text must mention the topic (not just a generic profile page)
                if not topic_match(text, topic_words, min_matches=1):
                    continue

                # FILTER 2: Prefer URLs that point to specific posts, not profile pages
                is_specific_post = any(re.search(pat, url) for pat in POST_URL_PATTERNS)

                # For generic profile URLs (youtube.com/c/Channel, x.com/user),
                # require stronger topic match in the text
                if not is_specific_post and not topic_match(text, topic_words, min_matches=2):
                    continue

                found_urls.add(url)

                if text and len(text) > 20:
                    results.append({
                        'voiceId': v['id'],
                        'platform': platform,
                        'text': text[:500],
                        'sourceUrl': url,
                        'timestamp': datetime.now().isoformat(),
                        'username': name,
                        'source': 'web_search',  # Flag that this came from search, not direct scrape
                    })
                    print(f"    ✓ {name} ({platform}): \"{text[:60]}...\"")

            await asyncio.sleep(0.3)
        except:
            pass

    print(f"  ✓ Web: {len(results)} direct platform posts found")

    # Save to cache
    search_cache[cache_key] = {
        'timestamp': datetime.now().isoformat(),
        'results': results,
    }
    SEARCH_CACHE.write_text(json.dumps(search_cache))

    return results


# ─── CLAUDE MATCHING ──────────────────────────────────────────────────────────

async def match_and_cluster(headline, summary, all_posts, voices):
    """Use Claude to match posts to the story and cluster by argument."""
    if not ANTHROPIC_API_KEY:
        print("\n  ⚠ No ANTHROPIC_API_KEY — skipping AI matching")
        return None

    if not all_posts:
        print("\n  ⚠ No posts to match")
        return None

    voice_map = {v['id']: v for v in voices}

    # Prioritize direct scrapes over web search results, cap at 150 posts for Claude
    direct = [p for p in all_posts if p.get('source') != 'web_search']
    web = [p for p in all_posts if p.get('source') == 'web_search']
    max_posts = 150
    if len(direct) >= max_posts:
        capped_posts = direct[:max_posts]
    else:
        capped_posts = direct + web[:max_posts - len(direct)]

    if len(capped_posts) < len(all_posts):
        print(f"  ℹ Capped from {len(all_posts)} to {len(capped_posts)} posts (prioritizing direct scrapes)")

    posts_text = ""
    for i, p in enumerate(capped_posts):
        voice = voice_map.get(p['voiceId'], {})
        source = p.get('source', '')
        if source == 'web_search':
            source_tag = " [FROM WEB SEARCH — snippet only, be skeptical]"
        elif source == 'enriched_tweet':
            source_tag = " [VERIFIED TWEET TEXT]"
        else:
            source_tag = " [DIRECT SCRAPE]"
        posts_text += f"\n[{i}] @{p.get('username', 'unknown')} ({voice.get('name', '?')}) on {p['platform']}{source_tag}:\n\"{p['text'][:300]}\"\n"

    prompt = f"""I have a news story and a list of social media posts/content found from major voices.

STORY HEADLINE: "{headline}"
STORY SUMMARY: {summary}

POSTS:
{posts_text}

CRITICAL RULES FOR MATCHING:
1. ONLY include posts where the person ACTUALLY said/wrote the quoted words themselves.
2. If the text is a NEWS ARTICLE about someone (e.g. "Ben Shapiro says..." from a news site), do NOT include it — that's secondhand reporting, not a direct quote.
3. Posts tagged [FROM WEB SEARCH] are search result snippets — be EXTRA skeptical. Only include if the text clearly contains a direct quote from the person (in quotation marks or clearly first-person speech).
4. Posts tagged [DIRECT SCRAPE] were scraped from the person's actual social media profile — these are trustworthy direct quotes.
5. Posts tagged [VERIFIED TWEET TEXT] were fetched directly from the tweet — these ARE the person's own words. Trust them.
6. Posts with [VIDEO: title] followed by transcript text — the transcript IS what the person said in their video. Trust it.
7. Never attribute a quote to someone just because their name appears near the topic. The text must be THEIR OWN WORDS.
8. When in doubt, EXCLUDE the post. False attributions are worse than missing data.

For each post that passes these rules AND is relevant to this story:
1. The post index number
2. The actual quote (their words, not a journalist's summary)
3. What argument cluster it belongs to (group by the ARGUMENT being made, NOT political lean)

Then list 3-6 argument clusters you identified.

Format your response as JSON:
{{
  "matches": [
    {{"index": 0, "quote": "...", "cluster": "Cluster name"}},
    ...
  ],
  "clusters": [
    {{"id": "slug-name", "label": "Human readable cluster name", "count": N}},
    ...
  ]
}}

Be very strict. It is far better to return fewer matches than to misattribute quotes."""

    print(f"\n  🤖 Matching {len(capped_posts)} posts to story with Claude...")

    try:
        import urllib.request
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
        json_match = re.search(r'\{[\s\S]*\}', result_text)
        if json_match:
            ai_result = json.loads(json_match.group())
            reactions = []
            for match in ai_result.get('matches', []):
                idx = match['index']
                if 0 <= idx < len(capped_posts):
                    post = capped_posts[idx]
                    reactions.append({
                        'voiceId': post['voiceId'],
                        'platform': post['platform'],
                        'quote': match['quote'],
                        'sourceUrl': post['sourceUrl'],
                        'timestamp': post['timestamp'],
                        'argumentCluster': match['cluster'],
                    })

            clusters = ai_result.get('clusters', [])
            print(f"  ✓ Matched {len(reactions)} reactions across {len(clusters)} argument clusters")
            return {'reactions': reactions, 'clusters': clusters}

    except Exception as e:
        print(f"  ⚠ Claude API error: {e}")

    return None


# ─── MAIN ─────────────────────────────────────────────────────────────────────

async def main():
    args = sys.argv[1:]
    if not args:
        print("Usage: python scripts/search.py \"topic keywords\"")
        print("       python scripts/search.py \"topic\" --headline \"...\" --summary \"...\"")
        sys.exit(1)

    topic = args[0]

    headline = topic
    summary = ""
    for i, arg in enumerate(args):
        if arg == '--headline' and i + 1 < len(args):
            headline = args[i + 1]
        if arg == '--summary' and i + 1 < len(args):
            summary = args[i + 1]

    print(f"\n  ╔══════════════════════════════════════════════╗")
    print(f"  ║   NEWSREEL PERSPECTIVES — Multi-Platform Search  ║")
    print(f"  ╚══════════════════════════════════════════════╝")
    print(f"\n  Topic: {topic}")
    print(f"  Headline: {headline}")

    voices = load_voices()
    print(f"  Tracking {len(voices)} voices")

    # Launch Playwright browser with Scrapling's Cloudflare bypass cookies
    browser_context = None
    browser = None
    pw = None
    try:
        from playwright.async_api import async_playwright

        # Use Scrapling to get Cloudflare bypass cookies
        cf_cookies = []
        try:
            from scrapling import StealthyFetcher
            fetcher = StealthyFetcher()
            cf_page = fetcher.fetch('https://x.com/')
            if cf_page.status == 200:
                cf_cookies = list(cf_page.cookies)
                print(f"  ✓ Scrapling bypassed Cloudflare ({len(cf_cookies)} cookies)")
        except Exception as e:
            print(f"  ⚠ Scrapling not available, using plain Playwright: {e}")

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        browser_context = await browser.new_context(user_agent=UA)
        if cf_cookies:
            await browser_context.add_cookies(cf_cookies)
        print(f"  ✓ Browser launched for X/TikTok/Instagram scraping")
    except Exception as e:
        print(f"  ⚠ Playwright not available: {e}")
        print(f"    Install: pip install playwright && playwright install chromium")

    # Run all searches in parallel — API-based + browser-based + new scrapers
    # Browser tasks run sequentially (shared browser context)
    async def browser_searches():
        x = await search_x(topic, voices, browser_context)
        tt = await search_tiktok(topic, voices, browser_context)
        ig = await search_instagram(topic, voices, browser_context)
        return x, tt, ig

    (x_results, tiktok_results, insta_results), bsky_results, yt_results, web_results, twikit_results, insta_loader_results = await asyncio.gather(
        browser_searches(),
        search_bluesky(topic, voices),
        search_youtube(topic, voices),
        search_web(topic, voices),
        search_x_twikit(topic, voices),
        search_instagram_instaloader(topic, voices),
    )

    # Close browser
    if browser:
        await browser.close()
    if pw:
        await pw.stop()

    # Merge X results: twikit (no auth) + Playwright (browser), dedupe by URL
    combined_x = twikit_results + x_results
    # Merge Instagram: instaloader + Playwright
    combined_ig = insta_loader_results + insta_results

    all_posts = combined_x + bsky_results + tiktok_results + combined_ig + yt_results + web_results

    # Deduplicate by sourceUrl
    seen = set()
    unique_posts = []
    for p in all_posts:
        if p['sourceUrl'] not in seen:
            seen.add(p['sourceUrl'])
            unique_posts.append(p)

    print(f"\n  📊 Total unique posts found: {len(unique_posts)}")

    if not unique_posts:
        print("  No posts found. Try different search terms.")
        sys.exit(0)

    # ── TWEET ENRICHMENT ──
    # For X URLs found via web search, fetch actual tweet text via embed API
    x_web_posts = [p for p in unique_posts
                   if p.get('source') == 'web_search'
                   and p.get('platform') == 'x'
                   and re.search(r'/status/\d+', p.get('sourceUrl', ''))]
    if x_web_posts:
        print(f"\n  🐦 Fetching actual tweet text for {len(x_web_posts)} X posts...")
        enriched_tweets = 0
        for p in x_web_posts:
            try:
                # Use Twitter's publish API (no auth needed) to get tweet text
                tweet_url = p['sourceUrl']
                api_url = f'https://publish.twitter.com/oembed?url={tweet_url}&omit_script=true'
                req = urllib.request.Request(api_url, headers={'User-Agent': UA})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode())
                html = data.get('html', '')
                # Extract text from the HTML blockquote
                text_match = re.search(r'<p[^>]*>(.*?)</p>', html, re.DOTALL)
                if text_match:
                    tweet_text = re.sub(r'<[^>]+>', '', text_match.group(1)).strip()
                    tweet_text = tweet_text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
                    if len(tweet_text) > 20:
                        p['text'] = tweet_text
                        p['source'] = 'enriched_tweet'  # Upgrade from web_search
                        enriched_tweets += 1
                await asyncio.sleep(0.3)
            except:
                pass
        print(f"  ✓ Enriched {enriched_tweets}/{len(x_web_posts)} tweets with actual text")

    # ── TRANSCRIPT ENRICHMENT (with cache) ──
    # Fetch YouTube transcripts for YouTube videos — transforms titles into quotes
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        # Load cache
        cache = {}
        if TRANSCRIPT_CACHE.exists():
            try:
                cache = json.loads(TRANSCRIPT_CACHE.read_text())
            except:
                cache = {}

        ytt_api = YouTubeTranscriptApi()
        yt_posts = [p for p in unique_posts
                    if 'youtube' in p.get('sourceUrl', '')
                    and not p.get('has_transcript')]

        if yt_posts:
            print(f"\n  📝 Fetching transcripts for {len(yt_posts)} YouTube videos...")
            enriched = 0
            fetched_new = 0
            for p in yt_posts[:60]:
                vid_match = re.search(r'(?:watch\?v=|youtu\.be/)([\w-]+)', p['sourceUrl'])
                if not vid_match:
                    continue
                video_id = vid_match.group(1)

                # Check cache first
                if video_id in cache:
                    if cache[video_id]:  # non-empty cached transcript
                        original_title = p['text'].split('] ')[-1] if '] ' in p['text'] else p['text']
                        p['text'] = f"[VIDEO: {original_title[:100]}] {cache[video_id]}"
                        p['has_transcript'] = True
                        enriched += 1
                    continue

                # Fetch fresh, with rate limiting
                try:
                    if fetched_new >= 50:  # max 50 new fetches per run
                        break
                    import time
                    time.sleep(1)  # pace requests
                    transcript = ytt_api.fetch(video_id, languages=['en'])
                    text_parts = []
                    for snippet in transcript.snippets:
                        if snippet.start > 180:
                            break
                        text_parts.append(snippet.text)
                    if text_parts:
                        transcript_text = ' '.join(text_parts)
                        cache[video_id] = transcript_text[:800]
                        original_title = p['text'].split('] ')[-1] if '] ' in p['text'] else p['text']
                        p['text'] = f"[VIDEO: {original_title[:100]}] {transcript_text[:800]}"
                        p['has_transcript'] = True
                        enriched += 1
                        fetched_new += 1
                    else:
                        cache[video_id] = ''
                except:
                    cache[video_id] = ''  # cache failures too

            # Save cache
            TRANSCRIPT_CACHE.write_text(json.dumps(cache))
            print(f"  ✓ Enriched {enriched}/{len(yt_posts)} videos ({fetched_new} new, {len(cache)} cached)")

            # Fallback: for videos without transcripts, try to get description via oembed
            unenriched = [p for p in yt_posts if not p.get('has_transcript')]
            if unenriched:
                print(f"  📄 Fetching descriptions for {len(unenriched)} videos without transcripts...")
                desc_enriched = 0
                for p in unenriched[:30]:
                    try:
                        oembed_url = f'https://www.youtube.com/oembed?url={p["sourceUrl"]}&format=json'
                        req = urllib.request.Request(oembed_url, headers={'User-Agent': UA})
                        with urllib.request.urlopen(req, timeout=5) as resp:
                            data = json.loads(resp.read().decode())
                        title = data.get('title', '')
                        author = data.get('author_name', '')
                        if title and len(title) > 20:
                            p['text'] = f"[VIDEO by {author}] {title}"
                            p['source'] = 'oembed_title'
                            desc_enriched += 1
                    except:
                        pass
                print(f"  ✓ Got {desc_enriched} video titles via oembed")
    except ImportError:
        pass

    # Match and cluster with Claude
    result = await match_and_cluster(headline, summary, unique_posts, voices)

    if result:
        slug = re.sub(r'[^a-z0-9]+', '-', headline.lower())[:50].strip('-')
        date = datetime.now().strftime('%Y-%m-%d')

        story = {
            'storyId': f'{slug}-{date}',
            'headline': headline,
            'date': date,
            'summary': summary,
            'reactions': result['reactions'],
            'argumentClusters': result['clusters'],
        }

        STORIES_DIR.mkdir(parents=True, exist_ok=True)
        out_path = STORIES_DIR / f'{slug}-{date}.json'
        out_path.write_text(json.dumps(story, indent=2))

        print(f"\n  ✓ Saved to {out_path}")
        print(f"  ✓ Open: http://localhost:8888?story={slug}-{date}")

        voice_map = {v['id']: v for v in voices}
        print(f"\n  Reactions:")
        for r in result['reactions']:
            name = voice_map.get(r['voiceId'], {}).get('name', '?')
            print(f"    {name} ({r['platform']}): \"{r['quote'][:80]}...\"")

        print(f"\n  Clusters:")
        for c in result['clusters']:
            print(f"    [{c['count']}] {c['label']}")

    print(f"\n  Done!\n")


if __name__ == '__main__':
    asyncio.run(main())
