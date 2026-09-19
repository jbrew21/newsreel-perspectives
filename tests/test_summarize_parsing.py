#!/usr/bin/env python3
"""Regression tests for the summary JSON extraction.

Production failure, Sep 19 2026: summarize_posts used a greedy bracket regex.
Haiku echoed the input listing, which starts "[0] (x) ...", so the match ran
from that bracket to the last one in the response and json.loads raised
"Extra data". The raise then hit a second bug (a missing local time import)
and took the whole nightly run down. Both are covered here.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import collect  # noqa: E402


class TestExtractJsonArray(unittest.TestCase):
    def test_clean_array(self):
        out = collect._extract_json_array('[{"index": 0, "summary": "Backs sanctions"}]')
        self.assertEqual(out[0]["summary"], "Backs sanctions")

    def test_the_production_failure_echoed_listing(self):
        """The exact shape that broke the Sep 19 run."""
        text = ('[0] (x) Some post text here\n'
                '[1] (x) Another post\n'
                '[{"index": 0, "summary": "Opposes tariffs"}, '
                '{"index": 1, "summary": "Backs aid"}]')
        out = collect._extract_json_array(text)
        self.assertIsNotNone(out, "must find the real array past the echoed listing")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["summary"], "Backs aid")

    def test_markdown_fence(self):
        out = collect._extract_json_array('```json\n[{"index": 0, "summary": "X"}]\n```')
        self.assertEqual(out[0]["index"], 0)

    def test_preamble_prose(self):
        out = collect._extract_json_array(
            'Sure, here are the summaries:\n[{"index": 0, "summary": "Y"}]')
        self.assertEqual(out[0]["summary"], "Y")

    def test_bracket_inside_a_string_value(self):
        out = collect._extract_json_array('[{"index": 0, "summary": "Says [sic] no"}]')
        self.assertEqual(out[0]["summary"], "Says [sic] no")

    def test_garbage_returns_none_and_does_not_raise(self):
        for junk in ("", "no json at all", "[0] (x) only a listing", "[1,2,3]", "[["):
            self.assertIsNone(collect._extract_json_array(junk), junk)


class TestSummarizeNeverRaises(unittest.TestCase):
    """The run must survive anything the summary step does."""

    def setUp(self):
        self.posts = [{"platform": "x", "text": "a"}, {"platform": "x", "text": "b"}]
        self._key = collect.ANTHROPIC_API_KEY

    def tearDown(self):
        collect.ANTHROPIC_API_KEY = self._key

    def test_no_api_key_returns_posts_untouched(self):
        collect.ANTHROPIC_API_KEY = ""
        out = collect.summarize_posts("V", self.posts)
        self.assertEqual(out, self.posts)

    def test_empty_posts(self):
        self.assertEqual(collect.summarize_posts("V", []), [])

    def test_network_failure_returns_posts_rather_than_raising(self):
        import urllib.request
        orig = urllib.request.urlopen

        def boom(*a, **k):
            raise OSError("network down")

        urllib.request.urlopen = boom
        orig_sleep = collect.time.sleep if hasattr(collect, "time") else None
        try:
            import time as _t
            real = _t.sleep
            _t.sleep = lambda s: None
            try:
                out = collect.summarize_posts("V", self.posts)
            finally:
                _t.sleep = real
            self.assertEqual(len(out), 2)
            self.assertNotIn("summary", out[0])
        finally:
            urllib.request.urlopen = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
