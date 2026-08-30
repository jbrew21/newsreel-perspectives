#!/usr/bin/env python3
"""
Fail the daily pipeline when collection quietly collapses.

On 2026-08-19 the public Nitter ecosystem went dark and X collection went from
1,614 posts/day to zero. Nothing failed. The workflow's only guard asked
whether the topic index was non-empty, and it stayed comfortably non-empty
because Bluesky, YouTube and Substack kept producing — so a third of the
roster silently stopped updating and it went unnoticed for eleven days, until
someone looked at the site and asked why the voices pages were stale.

This compares today against a trailing baseline and fails on the shape of that
incident: a platform that was reliably producing drops to (near) nothing, or
total coverage falls off a cliff. Thresholds are deliberately loose — this is
a smoke alarm for "a source died", not a quality bar.

Usage:
  python scripts/check_collection_health.py                 # today vs prior 7d
  python scripts/check_collection_health.py --date 2026-08-19
  python scripts/check_collection_health.py --warn-only     # never exit non-zero
"""

import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
BASELINE_PATH = ROOT / "data" / "collection-baseline.json"

BASELINE_DAYS = 7
# A platform must have averaged at least this many posts/day to be considered
# "established" — below it, normal variance looks like a collapse.
MIN_ESTABLISHED = 20
# An established platform dropping below this fraction of its baseline is an
# outage, not a slow news day.
PLATFORM_FLOOR = 0.25
# Total posts below this fraction of baseline means something broad broke.
TOTAL_FLOOR = 0.60
# Same, for how many distinct voices we heard from.
VOICE_FLOOR = 0.70


def day_stats(day):
    """(platform counter, distinct voice ids, total posts) for one date."""
    platforms = Counter()
    voices = set()
    total = 0
    if not POSTS_DIR.is_dir():
        return platforms, voices, total
    for vdir in POSTS_DIR.iterdir():
        if not vdir.is_dir():
            continue
        f = vdir / f"{day}.json"
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        posts = data.get('posts', [])
        if posts:
            voices.add(vdir.name)
        for p in posts:
            platforms[p.get('platform', 'unknown')] += 1
            total += 1
    return platforms, voices, total


def main():
    args = sys.argv[1:]
    warn_only = '--warn-only' in args
    day = datetime.now().strftime('%Y-%m-%d')
    for i, a in enumerate(args):
        if a == '--date' and i + 1 < len(args):
            day = args[i + 1]

    today_plat, today_voices, today_total = day_stats(day)

    base_plat = Counter()
    base_voices = []
    base_totals = []
    days_used = 0
    ref = datetime.strptime(day, '%Y-%m-%d')
    for i in range(1, BASELINE_DAYS + 1):
        d = (ref - timedelta(days=i)).strftime('%Y-%m-%d')
        p, v, t = day_stats(d)
        if t == 0:
            continue
        days_used += 1
        base_plat.update(p)
        base_voices.append(len(v))
        base_totals.append(t)

    print(f"\n  Collection health for {day}")
    print(f"  Baseline: {days_used} prior day(s) with data\n")

    if days_used == 0:
        print("  No baseline yet — nothing to compare. Passing.\n")
        return 0

    avg_total = sum(base_totals) / days_used
    avg_voices = sum(base_voices) / days_used

    print(f"  posts:  {today_total:>6}   baseline avg {avg_total:>8.0f}")
    print(f"  voices: {len(today_voices):>6}   baseline avg {avg_voices:>8.0f}\n")

    problems = []

    if avg_total and today_total < avg_total * TOTAL_FLOOR:
        problems.append(
            f"total posts {today_total} is {today_total / avg_total:.0%} of the "
            f"{avg_total:.0f}/day baseline")
    if avg_voices and len(today_voices) < avg_voices * VOICE_FLOOR:
        problems.append(
            f"only {len(today_voices)} voices produced posts, {len(today_voices) / avg_voices:.0%} "
            f"of the {avg_voices:.0f} baseline")

    # --- Absolute floors, independent of the trailing baseline ---
    # A trailing average absorbs an outage: by the time X had been dead a week,
    # its 7-day baseline was zero and "no X at all" scored as healthy. These
    # floors are committed to the repo and only change when someone decides to
    # expect less.
    try:
        declared = json.loads(BASELINE_PATH.read_text())
    except Exception:
        declared = {}
    expected = declared.get('expected', {})
    if expected:
        print("  Declared floors (data/collection-baseline.json):")
        for plat, floor in sorted(expected.items(), key=lambda x: -x[1]):
            now = today_plat.get(plat, 0)
            bad = now < floor
            print(f"    {plat:<12} {now:>6}   floor {floor:>7}{'  <-- BELOW FLOOR' if bad else ''}")
            if bad:
                problems.append(
                    f"{plat}: {now} posts is below the declared floor of {floor}/day")
        print()

    print("  Per platform (vs trailing baseline):")
    for plat, base_count in sorted(base_plat.items(), key=lambda x: -x[1]):
        base_avg = base_count / days_used
        now = today_plat.get(plat, 0)
        established = base_avg >= MIN_ESTABLISHED
        share = (now / base_avg) if base_avg else 1.0
        flag = ''
        if established and share < PLATFORM_FLOOR:
            flag = '  <-- COLLAPSED'
            problems.append(
                f"{plat}: {now} posts vs a {base_avg:.0f}/day baseline ({share:.0%})")
        print(f"    {plat:<12} {now:>6}   baseline {base_avg:>7.0f}/day{flag}")

    # A platform that vanished entirely won't appear in today's counter at all,
    # which the loop above already covers since it iterates the baseline.
    new_plats = set(today_plat) - set(base_plat)
    if new_plats:
        print(f"\n  New platforms today: {', '.join(sorted(new_plats))}")

    # Keep known-broken sources in view. A platform parked in "degraded" is
    # not a passing grade — it is an outage someone decided to live with, and
    # it should stay uncomfortable to read.
    degraded = declared.get('degraded', {})
    if degraded:
        print("  Known-degraded sources (not collected, or unreliable):")
        for plat, why in degraded.items():
            print(f"    {plat:<12} {why[:96]}")
        print()

    if not problems:
        print("  Healthy against current expectations"
              + (f" ({len(degraded)} source(s) still degraded).\n" if degraded else ".\n"))
        return 0

    print("\n  " + "!" * 62)
    for p in problems:
        print(f"  ! {p}")
    print("  " + "!" * 62)
    print("\n  A source has probably died. Check the collector for that platform")
    print("  before trusting today's data.\n")

    if warn_only:
        return 0
    return 1


if __name__ == '__main__':
    sys.exit(main())
