#!/usr/bin/env python3
"""
Newsreel Perspectives — Per-Voice Stance Accumulator

Every collected post is already a tracked perspective (it survived the
collect.py perspective filter: relevant + the voice is taking a stand). This
script accumulates those perspectives, per voice, into a persistent store so a
reader can open any voice's profile and see every stance they've taken,
grouped by topic.

Input:  data/posts/{voiceId}/{date}.json   (today's, merged into existing store)
Output: data/stances/{voiceId}.json        (accumulates over time)

The store grows daily: new stances are merged in (deduped by source URL),
grouped by topic, sorted newest-first, capped per topic and aged out so the
file stays bounded.

Usage:
  python scripts/build_stances.py                 # today
  python scripts/build_stances.py --date 2026-06-29
  python scripts/build_stances.py --rebuild       # rebuild from ALL post files
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
STANCES_DIR = ROOT / "data" / "stances"
TAXONOMY_PATH = ROOT / "data" / "taxonomy.json"

# Keep the store bounded.
MAX_STANCES_PER_TOPIC = 12      # most recent N per topic
MAX_AGE_DAYS = 120              # drop stances older than this


def load_topic_labels():
    """Map topic slug -> human display label from the taxonomy."""
    labels = {}
    try:
        data = json.loads(TAXONOMY_PATH.read_text())
        for t in data.get('topics', []):
            slug = t.get('slug')
            if slug:
                labels[slug] = t.get('display') or slug.replace('-', ' ').title()
    except Exception:
        pass
    return labels


def display_label(slug, labels):
    return labels.get(slug) or slug.replace('-', ' ').title()


# Promo / self-marketing markers. These get mis-categorized as stances
# (e.g. podcast episode blurbs) but aren't positions on anything -- they make
# profiles read like ad copy, so keep them out of the stance store.
# Use SPECIFIC phrases, not bare words: "subscribe"/"tune in"/"available now"
# appear in real political statements ("subscribe to my policy newsletter",
# "tune in to monitor coverage"), so match the promo phrasing instead.
PROMO_MARKERS = (
    'open.spotify.com/episode', 'spotify.com/episode',
    'new episode', 'full episode', 'latest episode', 'episode #',
    'new podcast', 'out now on', 'link in bio',
    'watch the full episode', 'subscribe to my', 'subscribe to our',
    'tune in to', 'catch the latest episode', 'available wherever you',
)


def looks_like_promo(quote):
    q = quote.lower()
    return any(m in q for m in PROMO_MARKERS)


def stance_from_post(post):
    """Build a stance entry from a collected post, or None if unusable."""
    url = post.get('sourceUrl', '')
    quote = (post.get('quote') or post.get('text') or '').strip()
    topic = post.get('topic', '')
    if not url or not quote or not topic or topic in ('uncategorized', 'other'):
        return None
    if looks_like_promo(quote):
        return None
    return {
        'date': post.get('date') or (post.get('timestamp', '')[:10]),
        'topic': topic,
        'stance': post.get('stance', 'lean'),     # strong | lean
        'summary': (post.get('summary', '') or '').strip()[:120],
        'quote': quote[:280],
        'sourceUrl': url,
        'platform': post.get('platform', ''),
    }


def collect_posts_for_date(date):
    """Yield (voiceId, voiceName, [posts]) for every voice with posts on `date`."""
    for voice_dir in sorted(POSTS_DIR.iterdir()):
        if not voice_dir.is_dir():
            continue
        post_file = voice_dir / f'{date}.json'
        if not post_file.exists():
            continue
        try:
            data = json.loads(post_file.read_text())
        except Exception:
            continue
        yield voice_dir.name, data.get('voiceName', voice_dir.name), data.get('posts', [])


def all_dates():
    """All available post dates (from topic-index files), newest first."""
    files = sorted(POSTS_DIR.glob('topic-index-*.json'), reverse=True)
    return [f.stem.replace('topic-index-', '') for f in files]


def load_store(voice_id):
    path = STANCES_DIR / f'{voice_id}.json'
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None


def flatten_store(store):
    """Return a flat list of stance entries from an existing store."""
    if not store:
        return []
    flat = []
    for topic_group in store.get('topics', []):
        for s in topic_group.get('stances', []):
            flat.append(s)
    return flat


def _norm_words(text):
    """Lowercased word set for near-duplicate comparison."""
    import re
    return set(re.findall(r'[a-z0-9]+', (text or '').lower()))


def dedupe_quotes(stances):
    """Drop near-identical quotes (>70% word overlap), keeping the newest.
    Different posts often repeat the same line nearly verbatim; without this
    a topic shows the same stance two or three times."""
    MIN_OVERLAP_WORDS = 6  # below this, high overlap is just shared policy terms
    kept = []
    kept_words = []
    for s in stances:  # assumed newest-first
        w = _norm_words(s.get('quote', ''))
        if not w:
            continue
        dup = False
        for prev in kept_words:
            # Only treat as a near-duplicate when both quotes are long enough
            # that high word-overlap is meaningful. Short quotes share common
            # policy terms by chance ("Backs Iran sanctions" vs "Iran sanctions"
            # = 100% overlap but distinct), so never dedupe them on overlap.
            if len(w) < MIN_OVERLAP_WORDS or len(prev) < MIN_OVERLAP_WORDS:
                continue
            overlap = len(w & prev) / min(len(w), len(prev))
            if overlap > 0.7:
                dup = True
                break
        if not dup:
            kept.append(s)
            kept_words.append(w)
    return kept


def build_store(voice_id, voice_name, new_stances, existing_store, labels, cutoff_date, updated_at):
    """Merge new stances into the existing store, dedup, group, cap, age out."""
    merged = {}  # sourceUrl -> stance (dedup, prefer newest seen)
    for s in flatten_store(existing_store) + new_stances:
        if not s or not s.get('sourceUrl'):
            continue
        if s.get('date', '') and s['date'] < cutoff_date:
            continue  # too old
        merged[s['sourceUrl']] = s

    # Group by topic (skip any entry missing a topic — defensive against a
    # corrupted store; stance_from_post already requires one).
    by_topic = {}
    for s in merged.values():
        topic = s.get('topic')
        if not topic:
            continue
        by_topic.setdefault(topic, []).append(s)

    topics = []
    total = 0
    for slug, stances in by_topic.items():
        stances.sort(key=lambda x: x.get('date', ''), reverse=True)
        stances = dedupe_quotes(stances)
        stances = stances[:MAX_STANCES_PER_TOPIC]
        if not stances:
            continue  # everything in this topic was a dupe/empty — no header
        total += len(stances)
        topics.append({
            'topic': slug,
            'label': display_label(slug, labels),
            'count': len(stances),
            'lastActive': stances[0].get('date', '') if stances else '',
            'stances': stances,
        })

    # Most recently active topics first
    topics.sort(key=lambda t: t['lastActive'], reverse=True)

    return {
        'voiceId': voice_id,
        'voiceName': voice_name,
        'updatedAt': updated_at,
        'topicCount': len(topics),
        'stanceCount': total,
        'topics': topics,
    }


def main():
    args = sys.argv[1:]
    rebuild = '--rebuild' in args

    date = None
    if '--date' in args:
        i = args.index('--date')
        if i + 1 < len(args):
            date = args[i + 1]

    dates = all_dates()
    if not dates:
        print("  No post data found.")
        return

    if not date:
        date = dates[0]

    # Cutoff for aging out old stances
    try:
        cutoff_date = (datetime.strptime(date, '%Y-%m-%d') - timedelta(days=MAX_AGE_DAYS)).strftime('%Y-%m-%d')
    except ValueError:
        cutoff_date = '0000-00-00'

    labels = load_topic_labels()
    STANCES_DIR.mkdir(parents=True, exist_ok=True)

    # Which dates to ingest: just `date` normally, or everything on --rebuild.
    ingest_dates = dates if rebuild else [date]

    # Gather new stances per voice across the ingest window.
    new_by_voice = {}   # voiceId -> {'name':..., 'stances':[...]}
    for d in ingest_dates:
        for vid, vname, posts in collect_posts_for_date(d):
            for p in posts:
                p.setdefault('date', d)
                entry = stance_from_post(p)
                if entry:
                    rec = new_by_voice.setdefault(vid, {'name': vname, 'stances': []})
                    rec['stances'].append(entry)
                    rec['name'] = vname

    if not new_by_voice:
        print(f"  No stances found for {date}.")
        return

    written = 0
    total_stances = 0
    for vid, rec in new_by_voice.items():
        existing = None if rebuild else load_store(vid)
        store = build_store(vid, rec['name'], rec['stances'], existing, labels, cutoff_date, date)
        if store['stanceCount'] == 0:
            continue
        (STANCES_DIR / f'{vid}.json').write_text(json.dumps(store, indent=2))
        written += 1
        total_stances += store['stanceCount']

    print(f"  ✓ Stances built for {written} voices ({total_stances} total stances) -> data/stances/")


if __name__ == '__main__':
    main()
