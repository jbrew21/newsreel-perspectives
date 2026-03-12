/**
 * Newsreel Perspectives — Story Matcher
 *
 * Reads raw feed data from data/feeds/, uses Claude API to:
 * 1. Match which posts are relevant to a given story
 * 2. Extract the key quote from each matched post
 * 3. Cluster reactions by argument (not political lean)
 *
 * Usage:
 *   node scripts/match-stories.js --headline "Iran Update..." --summary "President Trump said..."
 *   node scripts/match-stories.js --story data/stories/iran-war-2026-03-10.json
 *
 * Outputs a story JSON file matching the web app schema.
 */

import { readFileSync, writeFileSync, readdirSync, mkdirSync, existsSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');
const FEEDS_DIR = join(ROOT, 'data', 'feeds');
const STORIES_DIR = join(ROOT, 'data', 'stories');
const VOICES_PATH = join(ROOT, 'data', 'voices.json');

// Load .env from newsletter directory for ANTHROPIC_API_KEY
const ENV_PATH = join(ROOT, '..', 'newsletter', '.env');
if (existsSync(ENV_PATH)) {
  const envContent = readFileSync(ENV_PATH, 'utf-8');
  for (const line of envContent.split('\n')) {
    const trimmed = line.trim();
    if (trimmed && !trimmed.startsWith('#')) {
      const eqIdx = trimmed.indexOf('=');
      if (eqIdx > 0) {
        const key = trimmed.slice(0, eqIdx).trim();
        const val = trimmed.slice(eqIdx + 1).trim();
        if (!process.env[key]) {
          process.env[key] = val;
        }
      }
    }
  }
}

// ── CLI Argument Parsing ────────────────────────────────────────────────────

function parseArgs() {
  const args = process.argv.slice(2);
  const parsed = {};

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--headline' && args[i + 1]) {
      parsed.headline = args[++i];
    } else if (args[i] === '--summary' && args[i + 1]) {
      parsed.summary = args[++i];
    } else if (args[i] === '--story' && args[i + 1]) {
      parsed.storyFile = args[++i];
    } else if (args[i] === '--date' && args[i + 1]) {
      parsed.date = args[++i];
    } else if (args[i] === '--output' && args[i + 1]) {
      parsed.output = args[++i];
    }
  }

  return parsed;
}

// ── Rate-Limited Claude API ─────────────────────────────────────────────────

let lastCallTime = 0;
const MIN_DELAY_MS = 13000; // ~4.6 req/min, same as newsletter

async function rateLimitWait() {
  const elapsed = Date.now() - lastCallTime;
  if (lastCallTime > 0 && elapsed < MIN_DELAY_MS) {
    const wait = MIN_DELAY_MS - elapsed;
    console.log(`    Waiting ${Math.ceil(wait / 1000)}s for rate limit...`);
    await new Promise(r => setTimeout(r, wait));
  }
  lastCallTime = Date.now();
}

async function callClaude(prompt, systemPrompt) {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    console.error('No ANTHROPIC_API_KEY found. Set it in environment or in newsletter/.env');
    return null;
  }

  await rateLimitWait();

  const res = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      model: 'claude-sonnet-4-20250514',
      max_tokens: 4096,
      system: systemPrompt,
      messages: [{ role: 'user', content: prompt }],
    }),
  });

  if (!res.ok) {
    const errText = await res.text();
    if (res.status === 429) {
      console.log('    Rate limited — waiting 60s and retrying...');
      await new Promise(r => setTimeout(r, 60000));
      lastCallTime = Date.now();
      const retry = await fetch('https://api.anthropic.com/v1/messages', {
        method: 'POST',
        headers: {
          'x-api-key': apiKey,
          'anthropic-version': '2023-06-01',
          'content-type': 'application/json',
        },
        body: JSON.stringify({
          model: 'claude-sonnet-4-20250514',
          max_tokens: 4096,
          system: systemPrompt,
          messages: [{ role: 'user', content: prompt }],
        }),
      });
      if (retry.ok) {
        const data = await retry.json();
        return data.content?.[0]?.text || null;
      }
    }
    console.error('  Claude API error:', errText);
    return null;
  }

  const data = await res.json();
  return data.content?.[0]?.text || null;
}

// ── Feed Loading ────────────────────────────────────────────────────────────

function loadAllFeeds() {
  const feeds = {};
  if (!existsSync(FEEDS_DIR)) {
    console.error(`No feeds directory found at ${FEEDS_DIR}. Run fetch-feeds.js first.`);
    return feeds;
  }

  const files = readdirSync(FEEDS_DIR).filter(f => f.endsWith('.json'));
  for (const file of files) {
    try {
      const data = JSON.parse(readFileSync(join(FEEDS_DIR, file), 'utf-8'));
      feeds[data.voiceId] = data;
    } catch (err) {
      console.warn(`  Warning: Could not read ${file}: ${err.message}`);
    }
  }
  return feeds;
}

/**
 * Flatten all feed entries across all voices into a single array
 * with voice metadata attached.
 */
function flattenFeedEntries(feeds) {
  const all = [];
  for (const [voiceId, feedData] of Object.entries(feeds)) {
    for (const [platform, platformData] of Object.entries(feedData.platforms || {})) {
      if (platformData.status !== 'ok') continue;
      for (const entry of platformData.entries || []) {
        all.push({
          voiceId,
          voiceName: feedData.name,
          platform,
          title: entry.title || '',
          text: entry.text || '',
          url: entry.url || '',
          timestamp: entry.timestamp || '',
        });
      }
    }
  }
  return all;
}

// ── Matching Logic ──────────────────────────────────────────────────────────

const SYSTEM_PROMPT = `You are a news analyst for Newsreel, a news app for young Americans. Your job is to analyze social media posts and video titles from political commentators and match them to a specific news story.

You must be STRICT about relevance: only match posts that are clearly about the given story. A vague reference is not enough. The post must specifically address the topic.

You cluster reactions by ARGUMENT, not by political lean. Two people on opposite sides of the aisle can share the same argument cluster (e.g., both a libertarian and a progressive might be "skeptical of optimistic timelines").

Always respond with valid JSON. No markdown, no explanation outside the JSON.`;

/**
 * Step 1: Filter which posts are relevant to the story.
 * We batch posts to avoid massive prompts.
 */
async function matchPostsToStory(entries, headline, summary) {
  const BATCH_SIZE = 30;
  const allMatches = [];

  for (let i = 0; i < entries.length; i += BATCH_SIZE) {
    const batch = entries.slice(i, i + BATCH_SIZE);
    const batchNum = Math.floor(i / BATCH_SIZE) + 1;
    const totalBatches = Math.ceil(entries.length / BATCH_SIZE);

    console.log(`  Matching batch ${batchNum}/${totalBatches} (${batch.length} posts)...`);

    const postList = batch.map((entry, idx) => {
      const preview = (entry.text || entry.title).slice(0, 300);
      return `[${idx}] ${entry.voiceName} (${entry.platform}): "${preview}"`;
    }).join('\n');

    const prompt = `NEWS STORY:
Headline: ${headline}
Summary: ${summary}

POSTS TO ANALYZE:
${postList}

Which of these posts are SPECIFICALLY about this news story? For each match, extract the most quotable sentence or phrase.

Respond with JSON array. Each element:
{
  "index": <number>,
  "relevant": true,
  "quote": "<the most quotable 1-2 sentences from the post about this story>",
  "confidence": <0.0 to 1.0>
}

Only include posts with confidence >= 0.6. If NO posts match, return an empty array: []`;

    const result = await callClaude(prompt, SYSTEM_PROMPT);
    if (!result) continue;

    try {
      // Extract JSON from response (handle markdown code blocks)
      const jsonStr = result.replace(/```json?\n?/g, '').replace(/```/g, '').trim();
      const matches = JSON.parse(jsonStr);

      for (const match of matches) {
        if (match.relevant && match.confidence >= 0.6) {
          const entry = batch[match.index];
          if (entry) {
            allMatches.push({
              ...entry,
              quote: match.quote,
              confidence: match.confidence,
            });
          }
        }
      }
    } catch (err) {
      console.warn(`    Warning: Could not parse match response: ${err.message}`);
    }
  }

  return allMatches;
}

/**
 * Step 2: Cluster matched reactions by argument.
 */
async function clusterReactions(matches, headline, summary) {
  if (matches.length === 0) {
    return { reactions: [], argumentClusters: [] };
  }

  const reactionList = matches.map((m, i) => {
    return `[${i}] ${m.voiceName} (${m.platform}): "${m.quote}"`;
  }).join('\n');

  const prompt = `NEWS STORY:
Headline: ${headline}
Summary: ${summary}

MATCHED REACTIONS:
${reactionList}

Cluster these reactions by ARGUMENT (not political lean). Two people on opposite sides can share a cluster if they make the same argument.

Respond with JSON:
{
  "reactions": [
    {
      "index": <number>,
      "argumentCluster": "<short label for the argument, e.g. 'Skeptical of optimistic timelines'>"
    }
  ],
  "argumentClusters": [
    {
      "id": "<kebab-case-id>",
      "label": "<human readable label>",
      "count": <number of reactions in this cluster>
    }
  ]
}`;

  const result = await callClaude(prompt, SYSTEM_PROMPT);
  if (!result) {
    // Fallback: put everything in one cluster
    return {
      reactions: matches.map((m, i) => ({ index: i, argumentCluster: 'General reaction' })),
      argumentClusters: [{ id: 'general', label: 'General reaction', count: matches.length }],
    };
  }

  try {
    const jsonStr = result.replace(/```json?\n?/g, '').replace(/```/g, '').trim();
    return JSON.parse(jsonStr);
  } catch (err) {
    console.warn(`    Warning: Could not parse cluster response: ${err.message}`);
    return {
      reactions: matches.map((m, i) => ({ index: i, argumentCluster: 'General reaction' })),
      argumentClusters: [{ id: 'general', label: 'General reaction', count: matches.length }],
    };
  }
}

// ── Output ──────────────────────────────────────────────────────────────────

function buildStoryJson(headline, summary, date, matches, clustering) {
  const reactions = matches.map((match, i) => {
    const clusterInfo = clustering.reactions.find(r => r.index === i);
    return {
      voiceId: match.voiceId,
      platform: match.platform,
      quote: match.quote,
      sourceUrl: match.url,
      timestamp: match.timestamp || new Date().toISOString(),
      argumentCluster: clusterInfo?.argumentCluster || 'General reaction',
    };
  });

  // Generate a slug from the headline
  const slug = headline
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, '')
    .replace(/\s+/g, '-')
    .slice(0, 50)
    .replace(/-+$/, '');

  const storyId = `${slug}-${date}`;

  return {
    storyId,
    headline,
    date,
    summary,
    reactions,
    argumentClusters: clustering.argumentClusters,
  };
}

// ── Main ────────────────────────────────────────────────────────────────────

async function main() {
  const args = parseArgs();
  let headline, summary, date;

  // Get story info from args or story file
  if (args.storyFile) {
    const storyData = JSON.parse(readFileSync(args.storyFile, 'utf-8'));
    headline = storyData.headline;
    summary = storyData.summary;
    date = storyData.date || new Date().toISOString().slice(0, 10);
    console.log(`Loaded story from ${args.storyFile}`);
  } else if (args.headline) {
    headline = args.headline;
    summary = args.summary || headline;
    date = args.date || new Date().toISOString().slice(0, 10);
  } else {
    console.error('Usage:');
    console.error('  node scripts/match-stories.js --headline "..." --summary "..."');
    console.error('  node scripts/match-stories.js --story data/stories/example.json');
    console.error('');
    console.error('Options:');
    console.error('  --headline   Story headline (required unless --story)');
    console.error('  --summary    Story summary');
    console.error('  --story      Path to existing story JSON');
    console.error('  --date       Story date (YYYY-MM-DD, defaults to today)');
    console.error('  --output     Output file path (defaults to data/stories/<slug>.json)');
    process.exit(1);
  }

  console.log(`\nStory: "${headline}"`);
  console.log(`Date: ${date}\n`);

  // Load voices for reference
  const voices = JSON.parse(readFileSync(VOICES_PATH, 'utf-8'));
  const voiceMap = Object.fromEntries(voices.map(v => [v.id, v]));

  // Load all feed data
  console.log('Loading feed data...');
  const feeds = loadAllFeeds();
  const feedCount = Object.keys(feeds).length;

  if (feedCount === 0) {
    console.error('No feed data found. Run `node scripts/fetch-feeds.js` first.');
    process.exit(1);
  }
  console.log(`  Loaded feeds for ${feedCount} voices`);

  // Flatten all entries
  const entries = flattenFeedEntries(feeds);
  console.log(`  ${entries.length} total posts across all platforms\n`);

  if (entries.length === 0) {
    console.log('No feed entries to match. Check that fetch-feeds.js ran successfully.');
    process.exit(0);
  }

  // Step 1: Match posts to story
  console.log('Step 1: Matching posts to story...');
  const matches = await matchPostsToStory(entries, headline, summary);
  console.log(`  Found ${matches.length} matching posts\n`);

  if (matches.length === 0) {
    console.log('No posts matched this story. The story JSON will have empty reactions.');
  }

  // Step 2: Cluster by argument
  console.log('Step 2: Clustering reactions by argument...');
  const clustering = await clusterReactions(matches, headline, summary);
  console.log(`  Created ${clustering.argumentClusters.length} argument clusters\n`);

  // Build output
  const storyJson = buildStoryJson(headline, summary, date, matches, clustering);

  // Save
  mkdirSync(STORIES_DIR, { recursive: true });
  const outputPath = args.output || join(STORIES_DIR, `${storyJson.storyId}.json`);
  writeFileSync(outputPath, JSON.stringify(storyJson, null, 2));
  console.log(`Story saved to ${outputPath}`);
  console.log(`  ${storyJson.reactions.length} reactions across ${storyJson.argumentClusters.length} clusters`);

  // Summary
  if (storyJson.argumentClusters.length > 0) {
    console.log('\nArgument clusters:');
    for (const cluster of storyJson.argumentClusters) {
      console.log(`  - ${cluster.label} (${cluster.count} voices)`);
    }
  }

  return storyJson;
}

// Allow importing as module or running directly
const isMainModule = process.argv[1] && fileURLToPath(import.meta.url).includes(process.argv[1].replace(/\\/g, '/'));
if (isMainModule) {
  main().catch(err => {
    console.error('Fatal error:', err);
    process.exit(1);
  });
}

export { main as matchStories };
