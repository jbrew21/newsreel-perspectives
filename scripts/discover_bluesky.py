#!/usr/bin/env python3
"""
Find Bluesky accounts for voices we can no longer collect.

Why this exists: X collection died on 2026-08-19 when the public Nitter
ecosystem went dark (nitter.net now returns 410; every other instance is dead,
rate-limited, or 400s). X went from 1,614 posts/day to zero, which stranded
109 voices — 78 of them configured with no platform except X. Bluesky has a
free public API that works, and most of these people are on it.

Attribution safety is the whole design. Putting someone else's words under a
real person's name is the worst failure this product can have, and Bluesky is
full of parody, fan and "unofficial" accounts (searching "Mitch McConnell"
returns "Is Mitch McConnell Dead Yet?" first). So a candidate is only ever
auto-accepted on overwhelming evidence; everything else is written to a review
file for a human. The bar is deliberately set so that the cost of a miss is a
manual lookup, and the cost of a false accept is never paid.

Usage:
  python scripts/discover_bluesky.py                  # dry run, all stale voices
  python scripts/discover_bluesky.py --stale-days 7   # what counts as stale
  python scripts/discover_bluesky.py --apply          # write accepted handles
  python scripts/discover_bluesky.py --voice aoc      # single voice
  python scripts/discover_bluesky.py --validate       # check configured handles still resolve
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
VOICES_PATH = ROOT / "data" / "voices.json"
POSTS_DIR = ROOT / "data" / "posts"
REVIEW_PATH = ROOT / "data" / "bluesky-candidates.json"

API = "https://public.api.bsky.app/xrpc"
CLAUDE_MODEL = 'claude-haiku-4-5-20251001'   # same model the pipeline uses
UA = "Mozilla/5.0 (compatible; NewsreelPerspectives/1.0)"

# Accounts that impersonate, satirize or aggregate someone. If any of these
# appear in the handle, display name or description, the candidate is rejected
# outright regardless of how well the name matches.
IMPOSTOR_MARKERS = (
    'parody', 'fan account', 'fan page', 'unofficial', 'not the real',
    'satire', 'bot', 'archive', 'quotes', 'tracker', 'dead yet',
    'commentary on', 'daily', 'updates', 'news about', 'stan',
)

# A real person's account says "I am"; these say "about". Weaker than the
# markers above, so they cost points rather than disqualifying.
SUSPICIOUS_MARKERS = ('unaffiliated', 'not affiliated', 'tribute', 'appreciation')

MIN_FOLLOWERS = 500          # public figures we track clear this easily
AUTO_ACCEPT_SCORE = 100      # only an exact-name, high-follower match reaches this

# A Bluesky handle that IS a domain is verified by DNS: you cannot hold
# @schumer.senate.gov without controlling senate.gov. For an official domain
# that is stronger evidence of identity than anything else available here.
OFFICIAL_TLDS = ('.senate.gov', '.house.gov', '.gov', '.mil')

# Bridge services mirror another platform into Bluesky. The content may be
# genuine, but the account is not the person and the mirror can lag or break,
# so these never auto-accept.
BRIDGE_MARKERS = ('.brid.gy', '.bridgy', 'bsky.brid')


def load_voices():
    data = json.loads(VOICES_PATH.read_text())
    return data if isinstance(data, list) else data.get('voices', [])


def normalize(name):
    """Lowercase, strip punctuation/titles, collapse whitespace."""
    n = (name or '').lower()
    n = re.sub(r'\b(sen|senator|rep|representative|gov|governor|dr|prof|professor|the|hon)\b\.?', ' ', n)
    n = re.sub(r'[^a-z0-9\s]', ' ', n)
    return re.sub(r'\s+', ' ', n).strip()


def api_get(path, params, retries=3):
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if attempt == retries - 1:
                print(f"    api error ({path}): {str(e)[:70]}")
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def get_profile(handle):
    """searchActors omits followersCount, so fetch the full profile for any
    candidate we are actually going to score on it."""
    return api_get('app.bsky.actor.getProfile', {'actor': handle}) or {}


def recent_post_count(handle, days=30):
    """How many posts in the window — an account that never posts is useless
    to us even if the identity is right."""
    feed = api_get('app.bsky.feed.getAuthorFeed', {'actor': handle, 'limit': 50})
    if not feed:
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    n = 0
    for item in feed.get('feed', []):
        ts = (item.get('post', {}).get('record', {}) or {}).get('createdAt', '')
        try:
            when = datetime.fromisoformat(ts.replace('Z', '+00:00')).replace(tzinfo=None)
        except Exception:
            continue
        if when >= cutoff:
            n += 1
    return n


def score_candidate(voice, actor):
    """Return (score, reasons, disqualified). Higher is better."""
    handle = (actor.get('handle') or '').lower()
    display = actor.get('displayName') or ''
    desc = actor.get('description') or ''
    blob = f"{handle} {display} {desc}".lower()

    for marker in IMPOSTOR_MARKERS:
        if marker in blob:
            return 0, [f"rejected: '{marker}' in profile"], True

    want = normalize(voice['name'])
    got_display = normalize(display)
    score, reasons = 0, []

    if got_display == want:
        score += 60
        reasons.append('display name exact')
    elif want and want in got_display:
        score += 35
        reasons.append('display name contains')
    elif got_display and got_display in want:
        score += 25
        reasons.append('display name partial')
    else:
        # Require at least the surname to appear somewhere.
        parts = want.split()
        if parts and parts[-1] in blob:
            score += 10
            reasons.append('surname only')
        else:
            return 0, ['rejected: name does not match'], True

    # Handle echoes the name (jaketapper.bsky.social)
    handle_stem = handle.split('.')[0]
    if normalize(handle_stem).replace(' ', '') == want.replace(' ', ''):
        score += 25
        reasons.append('handle matches name')

    # DNS-verified official domain — the strongest identity signal available.
    if any(handle.endswith(t) for t in OFFICIAL_TLDS):
        score += 45
        reasons.append('official .gov domain (DNS-verified)')

    if any(b in handle for b in BRIDGE_MARKERS):
        score -= 30
        reasons.append('bridge mirror, not their own account')

    followers = actor.get('followersCount')
    if followers is None:
        followers = 0
    if followers >= 50000:
        score += 30
        reasons.append(f'{followers:,} followers')
    elif followers >= 5000:
        score += 20
        reasons.append(f'{followers:,} followers')
    elif followers >= MIN_FOLLOWERS:
        score += 8
        reasons.append(f'{followers:,} followers')
    else:
        score -= 25
        reasons.append(f'only {followers:,} followers')

    # Their known lens/tags should echo somewhere in the bio.
    lens_words = set(normalize(voice.get('lens', '')).split())
    bio_words = set(normalize(desc).split())
    overlap = len(lens_words & bio_words - {'and', 'of', 'for', 'a', 'in', 'on'})
    if overlap >= 3:
        score += 15
        reasons.append(f'bio matches lens ({overlap} terms)')

    for marker in SUSPICIOUS_MARKERS:
        if marker in blob:
            score -= 20
            reasons.append(f"suspicious: '{marker}'")

    return score, reasons, False


def _load_api_key():
    key = os.environ.get('ANTHROPIC_API_KEY', '')
    if key:
        return key
    for env_path in (ROOT / '.env', ROOT.parent / 'newsletter' / '.env'):
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith('ANTHROPIC_API_KEY='):
                    return line.partition('=')[2].strip()
    return ''


def verify_with_model(voice, cand, api_key):
    """Ask the model whether this account IS this person.

    Scoring alone cannot separate the cases that matter. Searching "Elon Musk"
    surfaces @elonjet.net — a jet-tracking account with a huge following and a
    perfect name match — while Heather Cox Richardson's real account looks
    identical on paper to a fan page. A model reading the bio tells these apart
    trivially. Returns (verdict, reason) where verdict is yes/no/unsure; any
    failure returns 'unsure' so the candidate stays in human review.
    """
    prompt = f"""A news product tracks this person and needs their real Bluesky account.

PERSON WE ARE LOOKING FOR
  Name: {voice['name']}
  Who they are: {voice.get('lens', '(no description on file)')}

CANDIDATE BLUESKY ACCOUNT
  Handle: @{cand['handle']}
  Display name: {cand['displayName']}
  Bio: {cand['description'] or '(empty)'}
  Followers: {cand['followers']:,}

Is this account operated BY that person (or their official office/organization)?

Answer "no" if it is any of: a fan or tribute account, a parody, a bot, a
tracker or aggregator ABOUT them, a news account that covers them, a different
person with a similar name, or an account you cannot positively identify.

Misattributing a quote to a real person is the worst possible error here, so
answer "unsure" whenever the evidence is not clear.

Reply with JSON only: {{"verdict": "yes"|"no"|"unsure", "reason": "<12 words max>"}}"""

    try:
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json.dumps({
                'model': CLAUDE_MODEL,
                'max_tokens': 150,
                'messages': [{'role': 'user', 'content': prompt}],
            }).encode(),
            headers={
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
        )
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode())
        text = data.get('content', [{}])[0].get('text', '')
        m = re.search(r'\{[\s\S]*?\}', text)
        if not m:
            return 'unsure', 'unparseable model reply'
        out = json.loads(m.group())
        v = str(out.get('verdict', 'unsure')).lower()
        if v not in ('yes', 'no', 'unsure'):
            v = 'unsure'
        return v, str(out.get('reason', ''))[:60]
    except Exception as e:
        return 'unsure', f'model error: {str(e)[:40]}'


def find_for_voice(voice, verbose=True):
    """Best candidate for one voice, or None."""
    actors = (api_get('app.bsky.actor.searchActors',
                      {'q': voice['name'], 'limit': 8}) or {}).get('actors', [])
    if not actors:
        return None

    # Cheap pass first (name/impostor checks only) to shortlist, then pay for
    # a profile fetch on the few that survive.
    shortlist = []
    for a in actors:
        _, _, dq = score_candidate(voice, a)
        if not dq:
            shortlist.append(a)
    if not shortlist:
        return None

    ranked = []
    for a in shortlist[:4]:
        full = get_profile(a['handle'])
        merged = dict(a)
        if full:
            merged['followersCount'] = full.get('followersCount', 0)
            merged['description'] = full.get('description', a.get('description', ''))
            merged['displayName'] = full.get('displayName', a.get('displayName', ''))
        score, reasons, dq = score_candidate(voice, merged)
        if dq:
            continue
        ranked.append((score, merged, reasons))
        time.sleep(0.25)
    if not ranked:
        return None

    ranked.sort(key=lambda x: -x[0])
    score, actor, reasons = ranked[0]

    # An account that does not post is not worth wiring up.
    posts30 = recent_post_count(actor['handle'])
    if posts30 == 0:
        reasons.append('no posts in 30d')
        score -= 40
    else:
        reasons.append(f'{posts30} posts/30d')
        if posts30 >= 5:
            score += 10

    # A close runner-up usually means the name is ambiguous. But when the top
    # candidate is independently verified — exact display name plus a large
    # real following, or a DNS-verified .gov domain — a nearby impostor score
    # says nothing: parody accounts of famous people also match the name.
    runner_up = ranked[1][0] if len(ranked) > 1 else 0
    handle = actor['handle'].lower()
    strongly_verified = (
        any(handle.endswith(t) for t in OFFICIAL_TLDS)
        or (normalize(actor.get('displayName', '')) == normalize(voice['name'])
            and (actor.get('followersCount') or 0) >= 25000)
    )
    ambiguous = (runner_up >= score - 25) and not strongly_verified

    is_bridge = any(b in handle for b in BRIDGE_MARKERS)

    return {
        'voiceId': voice['id'],
        'voiceName': voice['name'],
        'handle': actor['handle'],
        'displayName': actor.get('displayName', ''),
        'description': (actor.get('description') or '')[:200],
        'followers': actor.get('followersCount', 0),
        'postsLast30d': posts30,
        'score': score,
        'reasons': reasons,
        'ambiguous': ambiguous,
        'bridge': is_bridge,
        'autoAccept': (score >= AUTO_ACCEPT_SCORE and not ambiguous
                       and posts30 > 0 and not is_bridge),
    }


def validate_existing(voices):
    """Check every configured Bluesky handle still resolves.

    A handle that stops resolving fails silently inside collect.py — the voice
    just quietly returns no posts, which is exactly the failure mode that hid
    the X outage for eleven days. stacey-abrams carried a dead
    @staceyabrams.bsky.social this way. Returns [(voiceId, handle, code)].
    """
    bad = []
    configured = [(v['id'], (v.get('handles') or {}).get('bluesky'))
                  for v in voices if (v.get('handles') or {}).get('bluesky')]
    print(f"  Validating {len(configured)} configured Bluesky handles...")
    for vid, h in configured:
        if not get_profile(h):
            bad.append((vid, h))
            print(f"    DEAD  {vid:<26} @{h}")
        time.sleep(0.12)
    print(f"  {len(bad)} dead handle(s)\n")
    return bad


def stale_voices(voices, days):
    cutoff = {(datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(days)}
    out = []
    for v in voices:
        vdir = POSTS_DIR / v['id']
        have = set()
        if vdir.is_dir():
            have = {f.stem for f in vdir.glob('*.json')}
        if not (have & cutoff):
            out.append(v)
    return out


def main():
    args = sys.argv[1:]
    apply = '--apply' in args
    use_ai = '--verify-ai' in args
    days = 7
    only = None
    for i, a in enumerate(args):
        if a == '--stale-days' and i + 1 < len(args):
            days = int(args[i + 1])
        if a == '--voice' and i + 1 < len(args):
            only = args[i + 1]

    api_key = _load_api_key() if use_ai else ''
    if use_ai and not api_key:
        print("  --verify-ai requested but no ANTHROPIC_API_KEY found; "
              "falling back to scoring only.")
        use_ai = False

    voices = load_voices()

    if '--validate' in args:
        bad = validate_existing(voices)
        if bad:
            print("  Re-run discovery for these voices to replace them, e.g.:")
            for vid, _ in bad[:5]:
                print(f"    python scripts/discover_bluesky.py --voice {vid} --verify-ai")
            print()
        return

    if only:
        targets = [v for v in voices if v['id'] == only]
    else:
        targets = [v for v in stale_voices(voices, days)
                   if not (v.get('handles') or {}).get('bluesky')]

    print(f"\n  Searching Bluesky for {len(targets)} voices we cannot currently collect")
    print(f"  ({'APPLY — accepted handles will be written' if apply else 'dry run'})\n")

    accepted, review, missing = [], [], []
    for i, v in enumerate(targets, 1):
        res = find_for_voice(v)

        # Scoring alone cannot rule on the middle band: a jet-tracker and a
        # real senator can score identically. Ask the model, but only ever let
        # it CONFIRM a plausible candidate — never let it rescue one the
        # deterministic checks disqualified, and never let it override a
        # bridge/inactive rejection.
        if (use_ai and res and not res['autoAccept'] and res['score'] >= 60
                and res['postsLast30d'] > 0 and not res.get('bridge')):
            verdict, why = verify_with_model(v, res, api_key)
            res['aiVerdict'] = verdict
            res['aiReason'] = why
            if verdict == 'yes':
                res['autoAccept'] = True
                res['reasons'].append(f'model: {why}')
            elif verdict == 'no':
                res['reasons'].append(f'model rejected: {why}')
                res['rejected'] = True
            time.sleep(0.3)

        if not res:
            missing.append(v['name'])
            print(f"  [{i}/{len(targets)}] {v['name']:<30} no candidate")
        elif res.get('rejected'):
            review.append(res)
            print(f"  [{i}/{len(targets)}] {v['name']:<30} REJECTED @{res['handle']} "
                  f"({res.get('aiReason','')})")
        elif res['autoAccept']:
            accepted.append(res)
            tag = ' [model]' if res.get('aiVerdict') == 'yes' else ''
            print(f"  [{i}/{len(targets)}] {v['name']:<30} ACCEPT{tag} @{res['handle']} "
                  f"({res['score']}, {res['followers']:,} followers, {res['postsLast30d']} posts/30d)")
        else:
            review.append(res)
            why = 'ambiguous' if res['ambiguous'] else f"score {res['score']}"
            print(f"  [{i}/{len(targets)}] {v['name']:<30} review @{res['handle']} ({why})")
        time.sleep(0.4)  # be polite to a free public API

    print(f"\n  Auto-accepted: {len(accepted)}   Needs review: {len(review)}   No candidate: {len(missing)}")

    REVIEW_PATH.write_text(json.dumps({
        'generatedAt': datetime.now().isoformat(),
        'accepted': accepted,
        'needsReview': review,
        'noCandidate': missing,
    }, indent=2))
    print(f"  Wrote {REVIEW_PATH.relative_to(ROOT)}")

    if apply and accepted:
        by_id = {a['voiceId']: a for a in accepted}
        data = json.loads(VOICES_PATH.read_text())
        rows = data if isinstance(data, list) else data.get('voices', [])
        n = 0
        for v in rows:
            hit = by_id.get(v['id'])
            if hit:
                v.setdefault('handles', {})['bluesky'] = hit['handle']
                n += 1
        VOICES_PATH.write_text(json.dumps(data, indent=2))
        print(f"  Wrote {n} bluesky handles into data/voices.json")
    elif accepted:
        print("  Re-run with --apply to write these handles.")
    print()


if __name__ == '__main__':
    main()
