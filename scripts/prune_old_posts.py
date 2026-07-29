#!/usr/bin/env python3
"""Prune per-voice post JSON older than KEEP_DAYS from data/posts/<voice>/.

The site only reads a rolling 48h-168h window (see serve.py AGENDA_* + the
2-day window in the wire/agenda builders), so months of committed post files
are dead weight that bloats every free-tier Render deploy (re-clones the whole
repo). This keeps a generous buffer and deletes the rest from the WORKING TREE
only -- git history still retains everything, so it is reversible.

Dry-run by default. Pass --apply to actually delete. Pass --keep-days N to
override the window (default 45, matching the daily-pipeline prune step; the
site's widest lookback is lookup.py's 30-day auto-expand, so 45 keeps a buffer).

Does NOT touch: topic-index-*.json, stories-*.json, story-archive/, results/,
voices.json, or any non-dated file.
"""
import os
import re
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTS = os.path.join(ROOT, 'data', 'posts')
DATED = re.compile(r'^(\d{4}-\d{2}-\d{2})\.json$')


def main():
    apply = '--apply' in sys.argv
    keep_days = 45
    for i, a in enumerate(sys.argv):
        if a == '--keep-days' and i + 1 < len(sys.argv):
            keep_days = int(sys.argv[i + 1])
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()

    to_delete, kept, bytes_freed = [], 0, 0
    for voice in os.listdir(POSTS):
        vdir = os.path.join(POSTS, voice)
        if not os.path.isdir(vdir):
            continue
        for fn in os.listdir(vdir):
            m = DATED.match(fn)
            if not m:
                kept += 1
                continue
            if m.group(1) < cutoff:
                fp = os.path.join(vdir, fn)
                to_delete.append(fp)
                bytes_freed += os.path.getsize(fp)
            else:
                kept += 1

    print(f"cutoff (keep >= {keep_days}d): {cutoff}")
    print(f"dated files to delete: {len(to_delete)}")
    print(f"space freed: {bytes_freed/1e6:.1f} MB")
    print(f"kept files: {kept}")
    if not apply:
        print("\nDRY RUN -- pass --apply to delete. Sample:")
        for fp in to_delete[:8]:
            print("  ", os.path.relpath(fp, ROOT))
        return
    for fp in to_delete:
        os.remove(fp)
    print(f"\nDELETED {len(to_delete)} files from working tree (git history intact).")


if __name__ == '__main__':
    main()
