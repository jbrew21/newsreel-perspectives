#!/usr/bin/env python3
"""
Newsreel Perspectives -- JSON to Supabase Migration

Migrates all data from JSON files into the Supabase Postgres database.
Idempotent: safe to re-run. Uses deterministic UUIDs and upserts.

Usage:
  python scripts/migrate_to_supabase.py              # full migration
  python scripts/migrate_to_supabase.py --dry-run     # count rows, don't insert

Requires: pip install supabase
Env vars: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
"""

import json
import os
import re
import sys
import uuid
import logging
from pathlib import Path
from datetime import date, datetime

# Setup
ROOT = Path(__file__).parent.parent
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('migrate')

# Load env
for env_path in [ROOT / '.env', ROOT.parent / 'newsletter' / '.env']:
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, val = line.partition('=')
                if key.strip() not in os.environ:
                    os.environ[key.strip()] = val.strip()

DRY_RUN = '--dry-run' in sys.argv

# Fixed UUID namespace for deterministic IDs
NS = uuid.UUID('b7e23ec2-9a5f-4c1a-8d3e-1f2a3b4c5d6e')


def det_uuid(*parts):
    """Deterministic UUID from parts. Same input = same UUID."""
    return str(uuid.uuid5(NS, '|'.join(str(p) for p in parts)))


def slugify(text):
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', text.lower())).strip('-')[:60]


def connect():
    """Connect to Supabase with service_role key (bypasses RLS)."""
    try:
        from supabase import create_client
    except ImportError:
        log.error('Install supabase: pip install supabase')
        sys.exit(1)

    url = os.environ.get('SUPABASE_URL', '')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not url or not key:
        log.error('Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env')
        sys.exit(1)

    return create_client(url, key)


def upsert_batch(client, table, rows, on_conflict, batch_size=500):
    """Upsert rows in batches. Returns (success, failed) counts."""
    if DRY_RUN:
        log.info(f'  [DRY RUN] {table}: {len(rows)} rows')
        return len(rows), 0

    total_ok, total_fail = 0, 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        try:
            client.table(table).upsert(batch, on_conflict=on_conflict).execute()
            total_ok += len(batch)
        except Exception as e:
            log.error(f'  Batch {i // batch_size} failed: {e}')
            # Fall back to row-by-row
            for row in batch:
                try:
                    client.table(table).upsert([row], on_conflict=on_conflict).execute()
                    total_ok += 1
                except Exception as row_err:
                    log.error(f'  Row failed: {row_err}')
                    total_fail += 1

    return total_ok, total_fail


def migrate_topics(client):
    """Phase 1: Topics from taxonomy.json"""
    log.info('Phase 1: Topics')
    tax_path = ROOT / 'data' / 'taxonomy.json'
    if not tax_path.exists():
        log.warning('  No taxonomy.json found, skipping')
        return set()

    raw = json.loads(tax_path.read_text())
    topics = raw.get('topics', raw) if isinstance(raw, dict) else raw

    rows = []
    valid_slugs = set()
    for t in topics:
        slug = t.get('slug', '')
        if not slug:
            continue
        valid_slugs.add(slug)
        rows.append({
            'slug': slug,
            'display_name': t.get('display', t.get('display_name', slug.replace('-', ' ').title())),
            'description': t.get('description', ''),
            'aliases': t.get('aliases', []),
        })

    ok, fail = upsert_batch(client, 'topics', rows, 'slug')
    log.info(f'  Topics: {ok} ok, {fail} failed')
    return valid_slugs


def migrate_voices(client):
    """Phase 2: Voices from voices.json"""
    log.info('Phase 2: Voices')
    voices = json.loads((ROOT / 'data' / 'voices.json').read_text())

    rows = []
    for v in voices:
        rows.append({
            'id': v['id'],
            'name': v['name'],
            'photo_url': v.get('photo', ''),
            'bio': v.get('lens', ''),
            'category': v.get('category', 'creator'),
            'approach': v.get('approach', ''),
            'tags': v.get('tags', []),
            'handles': v.get('handles', {}),
            'feeds': v.get('feeds', {}),
            'followers': v.get('followers', 0),
            'followers_display': v.get('followersDisplay', ''),
            'platforms': v.get('platforms', []),
            'is_active': True,
        })

    ok, fail = upsert_batch(client, 'voices', rows, 'id')
    log.info(f'  Voices: {ok} ok, {fail} failed')
    return {v['id'] for v in voices}


def migrate_posts(client, valid_slugs, valid_voices):
    """Phase 3: Posts from per-voice daily files"""
    log.info('Phase 3: Posts')

    # Load topic mapping for legacy slug normalization
    topic_map = {}
    mapping_path = ROOT / 'data' / 'topic-mapping.json'
    if mapping_path.exists():
        topic_map = json.loads(mapping_path.read_text())
        topic_map.pop('_description', None)

    def resolve_topic(raw):
        if not raw or raw == 'uncategorized' or raw == 'other':
            return None
        normalized = topic_map.get(raw, raw)
        return normalized if normalized in valid_slugs else None

    posts_dir = ROOT / 'data' / 'posts'
    all_rows = []
    seen = set()

    for voice_dir in sorted(posts_dir.iterdir()):
        if not voice_dir.is_dir():
            continue
        voice_id = voice_dir.name
        if voice_id not in valid_voices:
            continue

        for day_file in sorted(voice_dir.glob('*.json')):
            try:
                data = json.loads(day_file.read_text())
            except Exception:
                continue

            collected_date = data.get('date', day_file.stem)
            posts = data.get('posts', [])

            for p in posts:
                text = (p.get('text') or '').strip()
                if len(text) < 10:
                    continue

                # Dedup key
                source_url = p.get('sourceUrl', '')
                dedup_key = (voice_id, p.get('platform', ''), source_url)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                # Extract external ID from URL
                external_id = None
                platform = p.get('platform', '')
                if source_url:
                    patterns = {
                        'x': r'/status/(\d+)',
                        'youtube': r'(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})',
                        'tiktok': r'/video/(\d+)',
                        'bluesky': r'/post/([a-z0-9]+)',
                    }
                    pat = patterns.get(platform)
                    if pat:
                        m = re.search(pat, source_url)
                        if m:
                            external_id = m.group(1)

                # Parse timestamp
                published_at = None
                ts = p.get('timestamp', '')
                if ts:
                    try:
                        published_at = datetime.fromisoformat(ts.replace('Z', '+00:00')).isoformat()
                    except Exception:
                        pass

                row_id = det_uuid(voice_id, platform, source_url or text[:100], collected_date)

                all_rows.append({
                    'id': row_id,
                    'voice_id': voice_id,
                    'platform': platform,
                    'post_type': p.get('type', 'post'),
                    'text': text[:5000],
                    'quote': (p.get('quote') or '')[:1000] or None,
                    'source_url': source_url or None,
                    'external_id': external_id,
                    'topic_slug': resolve_topic(p.get('topic')),
                    'relevance': p.get('relevance', 'medium'),
                    'stance': p.get('stance'),
                    'confidence': None,
                    'published_at': published_at,
                    'collected_date': collected_date,
                })

    log.info(f'  Prepared {len(all_rows)} posts')
    ok, fail = upsert_batch(client, 'posts', all_rows, 'id,collected_date', batch_size=500)
    log.info(f'  Posts: {ok} ok, {fail} failed')


def migrate_stories(client, valid_voices):
    """Phase 4-6: Stories, clusters, cluster_voices from stories-{date}.json"""
    log.info('Phase 4-6: Stories, clusters, cluster_voices')

    posts_dir = ROOT / 'data' / 'posts'
    story_files = sorted(posts_dir.glob('stories-*.json'))

    story_rows = []
    cluster_rows = []
    cv_rows = []

    for sf in story_files:
        date_str = sf.stem.replace('stories-', '')
        try:
            stories = json.loads(sf.read_text())
        except Exception:
            continue

        for story in stories:
            headline = story.get('headline', '')
            if not headline:
                continue

            slug = slugify(headline)
            story_id = det_uuid('story', slug, date_str)

            story_rows.append({
                'id': story_id,
                'slug': slug,
                'headline': headline,
                'summary': story.get('summary', '') or None,
                'story_type': story.get('type', 'split') or 'split',
                'source': story.get('source', 'voices'),
                'heat_score': story.get('heatScore', 0) or 0,
                'voice_count': story.get('voiceCount', 0) or 0,
                'cluster_count': story.get('clusterCount', 0) or 0,
                'cover_url': story.get('coverUrl') or None,
                'topic_slugs': story.get('topicSlugs', []),
                'story_date': date_str,
                'is_published': True,
            })

            clusters = story.get('clusters', [])
            for ci, cluster in enumerate(clusters):
                cluster_name = cluster.get('name', '')
                if not cluster_name:
                    continue

                cluster_id = det_uuid('cluster', story_id, cluster_name)
                voices_list = cluster.get('voices', [])

                # Resolve best quote voice ID
                bq = cluster.get('bestQuote', {}) or {}
                bq_voice_id = None
                if bq.get('voiceName'):
                    for cv in voices_list:
                        if cv.get('voiceName') == bq['voiceName']:
                            bq_voice_id = cv.get('voiceId')
                            break

                cluster_rows.append({
                    'id': cluster_id,
                    'story_id': story_id,
                    'name': cluster_name,
                    'slug': slugify(cluster_name),
                    'voice_count': cluster.get('voiceCount', len(voices_list)),
                    'sort_order': ci,
                    'best_quote_voice_id': bq_voice_id if bq_voice_id in valid_voices else None,
                    'best_quote_text': bq.get('quote') or None,
                    'best_quote_platform': bq.get('platform') or None,
                })

                seen_voices = set()
                for cv in voices_list:
                    voice_id = cv.get('voiceId', '')
                    if not voice_id or voice_id not in valid_voices or voice_id in seen_voices:
                        continue
                    seen_voices.add(voice_id)

                    cv_id = det_uuid('cv', cluster_id, voice_id)
                    cv_rows.append({
                        'id': cv_id,
                        'cluster_id': cluster_id,
                        'story_id': story_id,
                        'voice_id': voice_id,
                        'quote': (cv.get('quote') or 'No quote available')[:2000],
                        'source_url': cv.get('sourceUrl') or None,
                        'platform': cv.get('platform') or None,
                        'quote_quality': cv.get('quoteQuality', 5),
                        'confidence': cv.get('fit', 0.7),
                    })

    # Insert in FK order
    log.info(f'  Stories: {len(story_rows)}')
    ok, fail = upsert_batch(client, 'stories', story_rows, 'slug,story_date')
    log.info(f'    {ok} ok, {fail} failed')

    log.info(f'  Clusters: {len(cluster_rows)}')
    ok, fail = upsert_batch(client, 'clusters', cluster_rows, 'id')
    log.info(f'    {ok} ok, {fail} failed')

    log.info(f'  Cluster voices: {len(cv_rows)}')
    ok, fail = upsert_batch(client, 'cluster_voices', cv_rows, 'cluster_id,voice_id')
    log.info(f'    {ok} ok, {fail} failed')


def refresh_views(client):
    """Phase 7: Refresh materialized views"""
    if DRY_RUN:
        log.info('Phase 7: [DRY RUN] Would refresh materialized views')
        return

    log.info('Phase 7: Refreshing materialized views')
    try:
        client.rpc('refresh_alignments').execute()
        log.info('  Alignments refreshed')
    except Exception as e:
        log.warning(f'  Alignment refresh failed (may be empty): {e}')


def main():
    log.info('=' * 50)
    log.info('Perspectives JSON -> Supabase Migration')
    log.info('=' * 50)

    if DRY_RUN:
        log.info('DRY RUN MODE -- no data will be written')

    client = connect()

    valid_slugs = migrate_topics(client)
    valid_voices = migrate_voices(client)
    migrate_posts(client, valid_slugs, valid_voices)
    migrate_stories(client, valid_voices)
    refresh_views(client)

    log.info('=' * 50)
    log.info('Migration complete')
    log.info('=' * 50)


if __name__ == '__main__':
    main()
