#!/usr/bin/env python3
"""Tests for scripts/check_collection_health.py.

This guard exists because of a real incident: on 2026-08-19 the public Nitter
ecosystem shut down, X collection went from 1,614 posts/day to zero, and
nothing failed — the only check asked whether the topic index was non-empty,
and Bluesky/YouTube/Substack kept it comfortably non-empty. A third of the
roster stopped updating for eleven days without a single red build.

    python -m unittest discover -s tests -v
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_collection_health as health  # noqa: E402


class HealthCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.posts = Path(self.tmp.name) / "posts"
        self.posts.mkdir(parents=True)
        self.baseline = Path(self.tmp.name) / "collection-baseline.json"
        self.baseline.write_text(json.dumps({"expected": {}, "degraded": {}}))
        for p, target in (("POSTS_DIR", self.posts), ("BASELINE_PATH", self.baseline)):
            patcher = mock.patch.object(health, p, target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write(self, day_offset, per_platform, voices=6):
        """Write `voices` voice-files for a day, split across platforms."""
        day = (datetime.now() - timedelta(days=day_offset)).strftime("%Y-%m-%d")
        items = [(plat, n) for plat, n in per_platform.items() for _ in [0]]
        for vi in range(voices):
            posts = []
            for plat, n in items:
                share = n // voices + (1 if vi < n % voices else 0)
                posts += [{"platform": plat, "text": f"{plat} post {i}"} for i in range(share)]
            vdir = self.posts / f"voice-{vi}"
            vdir.mkdir(exist_ok=True)
            (vdir / f"{day}.json").write_text(json.dumps({"posts": posts}))
        return day

    def _run(self, day, argv=None):
        args = ["--date", day] + (argv or [])
        with mock.patch.object(sys, "argv", ["check"] + args):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = health.main()
        return code, buf.getvalue()

    # --- the incident ---

    def test_platform_collapse_fails_the_run(self):
        for d in range(1, 8):
            self._write(d, {"x": 300, "bluesky": 200})
        today = self._write(0, {"x": 0, "bluesky": 200})
        code, out = self._run(today)
        self.assertEqual(code, 1)
        self.assertIn("COLLAPSED", out)
        self.assertIn("x:", out)

    def test_steady_collection_passes(self):
        for d in range(1, 8):
            self._write(d, {"x": 300, "bluesky": 200})
        today = self._write(0, {"x": 290, "bluesky": 205})
        code, out = self._run(today)
        self.assertEqual(code, 0)
        self.assertIn("Healthy", out)

    def test_normal_daily_variance_does_not_fail(self):
        for d in range(1, 8):
            self._write(d, {"x": 300, "bluesky": 200})
        today = self._write(0, {"x": 240, "bluesky": 170})   # a quiet news day
        code, _ = self._run(today)
        self.assertEqual(code, 0)

    # --- the reason it hid for 11 days ---

    def test_declared_floor_catches_what_a_drifted_baseline_misses(self):
        # After a week of outage the trailing baseline normalizes zero, so the
        # relative check goes quiet. The committed floor must not.
        self.baseline.write_text(json.dumps({
            "expected": {"x": 100, "bluesky": 100}, "degraded": {}}))
        for d in range(1, 8):
            self._write(d, {"x": 0, "bluesky": 200})   # already-degraded baseline
        today = self._write(0, {"x": 0, "bluesky": 200})
        code, out = self._run(today)
        self.assertEqual(code, 1)
        self.assertIn("BELOW FLOOR", out)
        self.assertIn("declared floor", out)

    def test_degraded_sources_are_printed_even_when_passing(self):
        self.baseline.write_text(json.dumps({
            "expected": {"bluesky": 100},
            "degraded": {"x": "DEAD since 2026-08-19 — Nitter shut down"}}))
        for d in range(1, 8):
            self._write(d, {"bluesky": 200})
        today = self._write(0, {"bluesky": 200})
        code, out = self._run(today)
        self.assertEqual(code, 0)
        self.assertIn("Known-degraded", out)
        self.assertIn("Nitter shut down", out)
        self.assertIn("1 source(s) still degraded", out)

    # --- edges ---

    def test_total_volume_cliff_fails(self):
        for d in range(1, 8):
            self._write(d, {"bluesky": 500})
        today = self._write(0, {"bluesky": 100})
        code, out = self._run(today)
        self.assertEqual(code, 1)
        self.assertIn("total posts", out)

    def test_voice_coverage_cliff_fails(self):
        for d in range(1, 8):
            self._write(d, {"bluesky": 300}, voices=10)
        today = self._write(0, {"bluesky": 290}, voices=4)
        code, out = self._run(today)
        self.assertEqual(code, 1)
        self.assertIn("voices produced posts", out)

    def test_warn_only_never_fails(self):
        for d in range(1, 8):
            self._write(d, {"x": 300, "bluesky": 200})
        today = self._write(0, {"x": 0, "bluesky": 200})
        code, out = self._run(today, ["--warn-only"])
        self.assertEqual(code, 0)
        self.assertIn("COLLAPSED", out)   # still reported, just not fatal

    def test_no_baseline_passes(self):
        today = self._write(0, {"bluesky": 100})
        code, out = self._run(today)
        self.assertEqual(code, 0)
        self.assertIn("No baseline", out)

    def test_small_platform_is_not_treated_as_collapsed(self):
        # Below MIN_ESTABLISHED, normal variance looks like an outage.
        for d in range(1, 8):
            self._write(d, {"bluesky": 300, "blog": 6})
        today = self._write(0, {"bluesky": 300, "blog": 0})
        code, out = self._run(today)
        self.assertEqual(code, 0)
        self.assertNotIn("COLLAPSED", out)


if __name__ == "__main__":
    unittest.main()
