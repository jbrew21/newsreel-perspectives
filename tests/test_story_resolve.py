#!/usr/bin/env python3
"""Tests for serve.find_story_by_slug, including the per-slug archive fallback.

    python -m unittest discover -s tests -v

A story's only id is a slug derived from its AI-generated headline, which every
rebuild regenerates (and the same-day stories-*.json is overwritten). Links to
the old slug used to 404 as "may have rotated out". Build-time snapshots under
data/posts/story-archive/<slug>.json keep those slugs resolvable; these tests
pin the fast path, the archive fallback, and the genuine-miss behavior.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import serve  # noqa: E402


class FindStoryBySlugTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.posts = root / "data" / "posts"
        (self.posts / "story-archive").mkdir(parents=True)
        # A current story lives in a dated aggregate file (the fast path).
        self.dated = [{"headline": "Fresh Story Today", "clusters": [], "voiceCount": 3}]
        (self.posts / "stories-2026-07-15.json").write_text(json.dumps(self.dated))
        # An older story exists only as an archived snapshot (rotated off / its
        # headline was regenerated on a later rebuild).
        (self.posts / "story-archive" / "old-headline-that-rotated.json").write_text(
            json.dumps({"headline": "Old Headline That Rotated", "clusters": [], "voiceCount": 9})
        )
        patcher = mock.patch.object(serve, "ROOT", str(root))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fast_path_resolves_from_dated_file(self):
        got = serve.find_story_by_slug("fresh-story-today")
        self.assertIsNotNone(got)
        self.assertEqual(got["headline"], "Fresh Story Today")

    def test_archive_fallback_resolves_rotated_slug(self):
        got = serve.find_story_by_slug("old-headline-that-rotated")
        self.assertIsNotNone(got)
        self.assertEqual(got["headline"], "Old Headline That Rotated")

    def test_unknown_slug_returns_none(self):
        self.assertIsNone(serve.find_story_by_slug("never-existed-anywhere"))


if __name__ == "__main__":
    unittest.main()
