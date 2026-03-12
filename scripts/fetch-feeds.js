/**
 * Newsreel Perspectives — RSS Feed Fetcher
 *
 * Reads voices.json, fetches RSS/Atom feeds for each voice,
 * parses entries, and saves raw feed data to data/feeds/.
 *
 * Usage: node scripts/fetch-feeds.js
 *
 * YouTube feeds are native Atom XML (free, no auth).
 * X/TikTok/Instagram feeds use rss.app placeholders (requires rss.app account).
 * Podcast feeds are standard RSS XML.
 */

import { readFileSync, writeFileSync, mkdirSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');
const VOICES_PATH = join(ROOT, 'data', 'voices.json');
const FEEDS_DIR = join(ROOT, 'data', 'feeds');

// ── XML Parsing Helpers ─────────────────────────────────────────────────────

/**
 * Extract all occurrences of a tag from XML. Returns array of inner text.
 * Handles both <tag>text</tag> and self-closing <tag/>.
 */
function extractTag(xml, tag) {
  const results = [];
  const regex = new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`, 'gi');
  let match;
  while ((match = regex.exec(xml)) !== null) {
    results.push(match[1].trim());
  }
  return results;
}

/**
 * Extract attribute value from a tag. E.g. extractAttr('<link href="..."/>', 'href')
 */
function extractAttr(tagStr, attr) {
  const regex = new RegExp(`${attr}=["']([^"']+)["']`);
  const match = tagStr.match(regex);
  return match ? match[1] : null;
}

/**
 * Parse a YouTube/Atom feed into normalized entries.
 */
function parseAtomFeed(xml) {
  const entries = [];
  const entryBlocks = xml.split(/<entry>/i).slice(1);

  for (const block of entryBlocks) {
    const entryXml = block.split(/<\/entry>/i)[0];
    const title = extractTag(entryXml, 'title')[0] || '';
    const published = extractTag(entryXml, 'published')[0] || extractTag(entryXml, 'updated')[0] || '';

    // Get the alternate link
    const linkMatches = entryXml.match(/<link[^>]*>/gi) || [];
    let url = '';
    for (const linkTag of linkMatches) {
      const rel = extractAttr(linkTag, 'rel');
      const href = extractAttr(linkTag, 'href');
      if (rel === 'alternate' && href) {
        url = href;
        break;
      }
      if (!rel && href) url = href;
    }

    // YouTube-specific: media:group > media:description
    const mediaDesc = extractTag(entryXml, 'media:description')[0] || '';
    const videoId = extractTag(entryXml, 'yt:videoId')[0] || '';

    if (title) {
      entries.push({
        title: decodeXmlEntities(title),
        text: decodeXmlEntities(mediaDesc || title),
        url: url || (videoId ? `https://www.youtube.com/watch?v=${videoId}` : ''),
        timestamp: published,
        videoId,
      });
    }
  }
  return entries;
}

/**
 * Parse a standard RSS 2.0 feed into normalized entries.
 */
function parseRssFeed(xml) {
  const entries = [];
  const itemBlocks = xml.split(/<item>/i).slice(1);

  for (const block of itemBlocks) {
    const itemXml = block.split(/<\/item>/i)[0];
    const title = extractTag(itemXml, 'title')[0] || '';
    const description = extractTag(itemXml, 'description')[0] || '';
    const link = extractTag(itemXml, 'link')[0] || '';
    const pubDate = extractTag(itemXml, 'pubDate')[0] || '';

    // Clean CDATA wrappers
    const cleanText = (description || title)
      .replace(/<!\[CDATA\[/g, '')
      .replace(/\]\]>/g, '')
      .replace(/<[^>]+>/g, '') // strip HTML tags
      .trim();

    if (title || cleanText) {
      entries.push({
        title: decodeXmlEntities(title.replace(/<!\[CDATA\[/g, '').replace(/\]\]>/g, '')),
        text: decodeXmlEntities(cleanText),
        url: link,
        timestamp: pubDate ? new Date(pubDate).toISOString() : '',
      });
    }
  }
  return entries;
}

/**
 * Parse JSON feeds (rss.app returns JSON for social media feeds).
 */
function parseJsonFeed(data) {
  const entries = [];
  const items = data.items || data.entries || [];

  for (const item of items) {
    entries.push({
      title: item.title || '',
      text: item.content_text || item.content_html?.replace(/<[^>]+>/g, '') || item.summary || item.title || '',
      url: item.url || item.id || '',
      timestamp: item.date_published || item.date_modified || '',
    });
  }
  return entries;
}

/**
 * Decode common XML entities.
 */
function decodeXmlEntities(str) {
  return str
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&#39;/g, "'");
}

// ── Main ────────────────────────────────────────────────────────────────────

async function fetchFeed(url, platform) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);

  try {
    const res = await fetch(url, {
      signal: controller.signal,
      headers: {
        'User-Agent': 'Newsreel-Perspectives/1.0',
        'Accept': 'application/xml, application/atom+xml, application/rss+xml, application/json, text/xml, */*',
      },
    });
    clearTimeout(timeout);

    if (!res.ok) {
      throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    }

    const contentType = res.headers.get('content-type') || '';
    const body = await res.text();

    // JSON feed (rss.app social feeds)
    if (contentType.includes('json') || url.endsWith('.json')) {
      try {
        const jsonData = JSON.parse(body);
        return parseJsonFeed(jsonData);
      } catch {
        // Fall through to XML parsing
      }
    }

    // Atom feed (YouTube)
    if (body.includes('<feed') || body.includes('<entry>')) {
      return parseAtomFeed(body);
    }

    // RSS 2.0 feed (podcasts, etc.)
    if (body.includes('<rss') || body.includes('<item>') || body.includes('<channel>')) {
      return parseRssFeed(body);
    }

    // Last resort: try JSON
    try {
      return parseJsonFeed(JSON.parse(body));
    } catch {
      throw new Error('Unrecognized feed format');
    }
  } catch (err) {
    clearTimeout(timeout);
    if (err.name === 'AbortError') {
      throw new Error('Request timed out (15s)');
    }
    throw err;
  }
}

async function main() {
  // Load voices
  const voices = JSON.parse(readFileSync(VOICES_PATH, 'utf-8'));
  mkdirSync(FEEDS_DIR, { recursive: true });

  console.log(`Fetching feeds for ${voices.length} voices...\n`);

  let successCount = 0;
  let failCount = 0;
  const results = {};

  for (const voice of voices) {
    const feeds = voice.feeds || {};
    const platformKeys = Object.keys(feeds);

    if (platformKeys.length === 0) {
      console.log(`  [skip] ${voice.name} — no feeds configured`);
      continue;
    }

    const voiceFeedData = {
      voiceId: voice.id,
      name: voice.name,
      fetchedAt: new Date().toISOString(),
      platforms: {},
    };

    for (const platform of platformKeys) {
      const url = feeds[platform];
      try {
        const entries = await fetchFeed(url, platform);
        voiceFeedData.platforms[platform] = {
          url,
          entries,
          entryCount: entries.length,
          status: 'ok',
        };
        successCount++;
        console.log(`  [ok]   ${voice.name} / ${platform} — ${entries.length} entries`);
      } catch (err) {
        voiceFeedData.platforms[platform] = {
          url,
          entries: [],
          entryCount: 0,
          status: 'error',
          error: err.message,
        };
        failCount++;
        console.log(`  [fail] ${voice.name} / ${platform} — ${err.message}`);
      }

      // Small delay between requests to be polite
      await new Promise(r => setTimeout(r, 500));
    }

    // Save per-voice feed file
    const outPath = join(FEEDS_DIR, `${voice.id}.json`);
    writeFileSync(outPath, JSON.stringify(voiceFeedData, null, 2));
    results[voice.id] = voiceFeedData;
  }

  console.log(`\nDone. ${successCount} feeds succeeded, ${failCount} failed.`);
  console.log(`Feed data saved to ${FEEDS_DIR}/`);

  return results;
}

main().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});
