/**
 * Newsreel Perspectives — Daily Refresh
 *
 * The "morning run" script that:
 * 1. Fetches all RSS feeds (fetch-feeds.js)
 * 2. Matches posts to stories (match-stories.js)
 *
 * Usage:
 *   node scripts/refresh.js --headline "Iran Update..." --summary "..."
 *   node scripts/refresh.js --story data/stories/iran-war-2026-03-10.json
 *   node scripts/refresh.js --config data/story-config.json
 *
 * The --config option reads a JSON file with { headline, summary, date }.
 * All other flags are passed through to match-stories.js.
 */

import { execFileSync } from 'child_process';
import { existsSync, readFileSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');

function run(script, args = []) {
  const scriptPath = join(__dirname, script);
  console.log(`\n${'='.repeat(60)}`);
  console.log(`Running: node ${script} ${args.join(' ')}`);
  console.log('='.repeat(60) + '\n');

  execFileSync('node', [scriptPath, ...args], {
    stdio: 'inherit',
    cwd: ROOT,
  });
}

function main() {
  const args = process.argv.slice(2);
  const startTime = Date.now();

  console.log('Newsreel Perspectives — Daily Refresh');
  console.log(`Started at ${new Date().toISOString()}\n`);

  // Handle --config flag: read story info from a JSON config file
  let matchArgs = args;
  const configIdx = args.indexOf('--config');
  if (configIdx !== -1 && args[configIdx + 1]) {
    const configPath = args[configIdx + 1];
    if (existsSync(configPath)) {
      const config = JSON.parse(readFileSync(configPath, 'utf-8'));
      matchArgs = [];
      if (config.headline) matchArgs.push('--headline', config.headline);
      if (config.summary) matchArgs.push('--summary', config.summary);
      if (config.date) matchArgs.push('--date', config.date);
      if (config.output) matchArgs.push('--output', config.output);
      console.log(`Loaded config from ${configPath}`);
    } else {
      console.error(`Config file not found: ${configPath}`);
      process.exit(1);
    }
  }

  // Step 1: Fetch all feeds
  try {
    run('fetch-feeds.js');
  } catch (err) {
    console.error('\nFeed fetching failed, but continuing to match with existing data...');
  }

  // Step 2: Match stories (only if we have headline/story args)
  const hasStoryArgs = matchArgs.some(a => ['--headline', '--story', '--config'].includes(a));
  // After config expansion, check again
  const hasMatchInput = matchArgs.includes('--headline') || matchArgs.includes('--story');

  if (hasMatchInput) {
    try {
      run('match-stories.js', matchArgs.filter(a => a !== '--config'));
    } catch (err) {
      console.error('\nStory matching failed:', err.message);
      process.exit(1);
    }
  } else {
    console.log('\nNo story specified — skipping match step.');
    console.log('To match stories, add: --headline "..." --summary "..."');
    console.log('Or: --story data/stories/some-story.json');
  }

  const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
  console.log(`\n${'='.repeat(60)}`);
  console.log(`Refresh complete in ${elapsed}s`);
  console.log('='.repeat(60));
}

main();
