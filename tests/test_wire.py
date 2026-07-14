#!/usr/bin/env python3
"""Tests for the /api/wire de-clustering logic in serve.py.

    python -m unittest discover -s tests -v

Covers the burst-monopoly fix: a single voice's thread must not dominate the
top of the wire. Also pins the timestamp-parsing and edge-case behavior the
code review called out (ties, single voice, missing/odd timestamps, non-UTC
offsets).
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import serve  # noqa: E402


def _p(vid, ts, text=None):
    return {
        "voiceId": vid,
        "timestamp": ts,
        "text": text or f"{vid} said something at {ts}",
    }


def _max_run(posts):
    """Longest run of consecutive same-voice entries."""
    best = cur = 0
    last = object()
    for p in posts:
        if p["voiceId"] == last:
            cur += 1
        else:
            cur, last = 1, p["voiceId"]
        best = max(best, cur)
    return best


def _min_gap(posts, vid):
    """Smallest index distance between two entries from the same voice."""
    idxs = [i for i, p in enumerate(posts) if p["voiceId"] == vid]
    return min((b - a for a, b in zip(idxs, idxs[1:])), default=None)


class TsKeyTests(unittest.TestCase):
    def test_z_and_offset_compare_equal_instant(self):
        a = serve.wire_ts_key({"timestamp": "2026-07-09T11:00:00Z"})
        b = serve.wire_ts_key({"timestamp": "2026-07-09T11:00:00+00:00"})
        self.assertEqual(a, b)

    def test_non_utc_offset_orders_correctly(self):
        # 11:00 -05:00 == 16:00Z, which is LATER than 12:00Z.
        later = serve.wire_ts_key({"timestamp": "2026-07-09T11:00:00-05:00"})
        earlier = serve.wire_ts_key({"timestamp": "2026-07-09T12:00:00Z"})
        self.assertGreater(later, earlier)

    def test_missing_or_bad_timestamp_sorts_oldest(self):
        floor = serve.wire_ts_key({"timestamp": ""})
        real = serve.wire_ts_key({"timestamp": "2020-01-01T00:00:00Z"})
        self.assertLess(floor, real)
        self.assertEqual(floor, serve.wire_ts_key({"timestamp": "garbage"}))
        self.assertEqual(floor, serve.wire_ts_key({}))


class DeclusterTests(unittest.TestCase):
    def test_burst_does_not_monopolize(self):
        # One voice posts an 8-item thread; two others post once.
        posts = [_p("burst", f"2026-07-09T11:23:{s:02d}+00:00") for s in range(8)]
        posts += [_p("solo_a", "2026-07-09T11:22:00+00:00"),
                  _p("solo_b", "2026-07-09T11:21:00+00:00")]
        out = serve.decluster(posts)
        self.assertLessEqual(_max_run(out), 1)
        self.assertLessEqual(sum(1 for p in out if p["voiceId"] == "burst"),
                             serve.WIRE_MAX_PER_VOICE)

    def test_min_gap_enforced_when_possible(self):
        posts = [_p("burst", f"2026-07-09T11:00:{s:02d}+00:00") for s in range(4)]
        posts += [_p(f"v{i}", f"2026-07-09T10:{i:02d}:00+00:00") for i in range(6)]
        out = serve.decluster(posts, max_per_voice=2, min_gap=3)
        gap = _min_gap(out, "burst")
        if gap is not None:
            self.assertGreaterEqual(gap, 3)

    def test_chronological_newest_first(self):
        posts = [_p(f"v{i}", f"2026-07-09T{i:02d}:00:00Z") for i in range(10)]
        out = serve.decluster(posts)
        keys = [serve.wire_ts_key(p) for p in out]
        self.assertEqual(keys, sorted(keys, reverse=True))

    def test_single_voice_terminates_and_caps(self):
        posts = [_p("only", f"2026-07-09T11:00:{s:02d}Z") for s in range(5)]
        out = serve.decluster(posts, max_per_voice=2, min_gap=3)
        self.assertEqual(len(out), 2)  # capped, and no infinite loop

    def test_respects_limit(self):
        posts = [_p(f"v{i}", f"2026-07-09T10:00:{i % 60:02d}Z") for i in range(300)]
        out = serve.decluster(posts, limit=100)
        self.assertLessEqual(len(out), 100)

    def test_identical_timestamps_are_stable(self):
        posts = [_p(f"v{i}", "2026-07-09T11:00:00Z") for i in range(5)]
        out = serve.decluster(posts)
        self.assertEqual(len(out), 5)
        self.assertLessEqual(_max_run(out), 1)

    def test_empty_input(self):
        self.assertEqual(serve.decluster([]), [])

    def test_min_gap_constant_name_matches_behavior(self):
        # WIRE_MIN_POSTS_BETWEEN posts must sit between two from one voice,
        # i.e. an index gap of N+1. Guards the naming the review flagged.
        posts = [_p("burst", f"2026-07-09T11:00:{s:02d}Z") for s in range(2)]
        posts += [_p(f"v{i}", f"2026-07-09T10:{i:02d}:00Z") for i in range(6)]
        out = serve.decluster(posts, max_per_voice=2,
                              min_gap=serve.WIRE_MIN_POSTS_BETWEEN)
        gap = _min_gap(out, "burst")
        if gap is not None:
            self.assertGreater(gap, serve.WIRE_MIN_POSTS_BETWEEN)


class BuildWireTests(unittest.TestCase):
    def _make_root(self, files):
        """files: {(voice, iso_day): [post, ...]}"""
        import json as _json
        import os as _os
        import tempfile
        tmp = tempfile.mkdtemp()
        _os.makedirs(_os.path.join(tmp, "data", "posts"))
        with open(_os.path.join(tmp, "data", "voices.json"), "w") as f:
            _json.dump([{"id": v, "name": v.title(), "photo": "", "tags": []}
                        for v in {v for v, _ in files}], f)
        for (voice, day), posts in files.items():
            vdir = _os.path.join(tmp, "data", "posts", voice)
            _os.makedirs(vdir, exist_ok=True)
            with open(_os.path.join(vdir, f"{day}.json"), "w") as f:
                _json.dump({"posts": posts}, f)
        return tmp

    def test_wire_includes_yesterday_after_utc_midnight(self):
        # Regression: the wire went dark from 00:00 UTC until the morning
        # pipeline commit because only <today>.json was read.
        from datetime import date, timedelta
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        post = {"text": "a post long enough to clear the thirty char floor",
                "platform": "x", "sourceUrl": "https://x.com/a/1",
                "timestamp": f"{yesterday}T22:00:00Z"}
        root = self._make_root({("some-voice", yesterday): [post]})
        out = serve.build_wire(root=root)
        self.assertEqual(len(out), 1)

    def test_wire_dedupes_posts_across_day_files(self):
        from datetime import date, timedelta
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        post = {"text": "the same post captured by two consecutive scrapes",
                "platform": "x", "sourceUrl": "https://x.com/a/2",
                "timestamp": f"{yesterday}T23:00:00Z"}
        root = self._make_root({("some-voice", today): [post],
                                ("some-voice", yesterday): [post]})
        out = serve.build_wire(root=root)
        self.assertEqual(len(out), 1)

    def test_build_wire_runs_against_repo_data(self):
        # Smoke test: build_wire reads real data/ and returns a valid feed
        # obeying the cap and adjacency guarantees.
        out = serve.build_wire()
        self.assertIsInstance(out, list)
        self.assertLessEqual(len(out), serve.WIRE_MAX_ITEMS)
        self.assertLessEqual(_max_run(out), 1)
        # No raw HTML entities should survive the unescape-at-ingest step.
        for e in out:
            self.assertNotIn("&amp;", e.get("text", ""))


if __name__ == "__main__":
    unittest.main()
