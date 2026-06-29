#!/usr/bin/env python3
"""
One-time backfill: add a short position "summary" to existing stance stores.

Going forward, collect.py emits a `summary` per post and build_stances carries
it through, so new stances already have one. This script fills the field for
stances collected before that change, so voice profiles read cleanly today.

One Claude (Haiku) call per voice summarizes all of that voice's quotes at
once; calls run concurrently. Idempotent — only fills empty summaries.

Usage:
  python scripts/backfill_summaries.py            # all voices
  python scripts/backfill_summaries.py --limit 5  # first N (smoke test)
"""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).parent.parent
STANCES_DIR = ROOT / "data" / "stances"
MODEL = "claude-haiku-4-5-20251001"
WORKERS = 8


def load_env():
    for env_path in [ROOT / ".env", ROOT.parent / "newsletter" / ".env"]:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    key, _, val = line.partition('=')
                    if key.strip() not in os.environ:
                        os.environ[key.strip()] = val.strip()


load_env()
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')


def call_haiku(prompt, max_tokens=1024):
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=json.dumps({
            'model': MODEL,
            'max_tokens': max_tokens,
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
    text = data.get('content', [{}])[0].get('text', '')
    m = re.search(r'\{[\s\S]*\}', text)
    return json.loads(m.group()) if m else {}


def summarize_voice(path):
    """Fill empty summaries for one voice store. Returns (voiceName, filled)."""
    try:
        store = json.loads(path.read_text())
    except Exception:
        return (path.stem, 0)

    # Collect stances needing a summary, with a stable id.
    needing = []
    for ti, topic in enumerate(store.get('topics', [])):
        for si, s in enumerate(topic.get('stances', [])):
            if not (s.get('summary') or '').strip() and (s.get('quote') or '').strip():
                needing.append({'id': f'{ti}.{si}', 'quote': s['quote'][:240], 'topic': topic.get('label', '')})

    if not needing:
        return (store.get('voiceName', path.stem), 0)

    name = store.get('voiceName', path.stem)
    topics = store.get('topics', [])

    # Chunk so each call's response comfortably fits in the token budget --
    # big voices (100+ stances) otherwise truncate and parse to nothing.
    CHUNK = 30
    result = {}
    for start in range(0, len(needing), CHUNK):
        batch = needing[start:start + CHUNK]
        items = '\n'.join(f'[{n["id"]}] ({n["topic"]}) "{n["quote"]}"' for n in batch)
        prompt = f"""For each quote from {name} below, write a 4-8 word neutral, third-person LABEL of the position they're taking (not a quote — paraphrasing is fine, stay faithful). Examples: "Backs sanctions, opposes unfreezing Iran's assets", "Calls the strikes an illegal war".

Quotes:
{items}

Return ONLY a JSON object mapping each id to its label:
{{"0.0": "Backs sanctions on Iran", "0.1": "Calls strikes an illegal war"}}"""
        try:
            batch_result = call_haiku(prompt, max_tokens=2048)
            if isinstance(batch_result, dict):
                result.update(batch_result)
        except Exception as e:
            print(f"  ! {name}: batch {start // CHUNK} API failed ({e})")

    filled = 0
    for n in needing:
        label = (result.get(n['id']) or '').strip()
        if not label:
            continue
        ti, si = (int(x) for x in n['id'].split('.'))
        try:
            topics[ti]['stances'][si]['summary'] = label[:120]
            filled += 1
        except (IndexError, KeyError):
            continue

    if filled:
        path.write_text(json.dumps(store, indent=2))
    return (name, filled)


def main():
    if not ANTHROPIC_API_KEY:
        print("  No ANTHROPIC_API_KEY — cannot backfill.")
        return

    limit = None
    if '--limit' in sys.argv:
        i = sys.argv.index('--limit')
        if i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    files = sorted(STANCES_DIR.glob('*.json'))
    if limit:
        files = files[:limit]

    print(f"  Backfilling summaries for {len(files)} voices ({WORKERS} workers)...")
    total_voices = 0
    total_filled = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(summarize_voice, f): f for f in files}
        for fut in as_completed(futures):
            name, filled = fut.result()
            if filled:
                total_voices += 1
                total_filled += filled
                print(f"  ✓ {name}: {filled} summaries")

    print(f"\n  Done: filled {total_filled} summaries across {total_voices} voices.")


if __name__ == '__main__':
    main()
