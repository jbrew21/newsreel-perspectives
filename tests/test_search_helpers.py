#!/usr/bin/env python3
"""Tests for scripts/search_helpers.py — search story-join and recency sort.

    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import search_helpers as sh  # noqa: E402


def _voice(vid, *timestamps):
    return {"voiceId": vid, "quotes": [{"timestamp": t} for t in timestamps]}


class ParseTimestampTests(unittest.TestCase):
    def test_handles_z_suffix(self):
        self.assertEqual(sh.parse_timestamp("2026-07-08T08:38:24Z").year, 2026)

    def test_handles_offset_and_fractional(self):
        a = sh.parse_timestamp("2026-07-08T08:38:24+00:00")
        b = sh.parse_timestamp("2026-07-07T17:56:07.198Z")
        self.assertGreater(a, b)

    def test_date_only(self):
        self.assertEqual(sh.parse_timestamp("2026-07-08").day, 8)

    def test_empty_and_garbage_return_floor_not_none(self):
        self.assertEqual(sh.parse_timestamp(""), sh._EPOCH)
        self.assertEqual(sh.parse_timestamp("not-a-date"), sh._EPOCH)
        self.assertEqual(sh.parse_timestamp(None), sh._EPOCH)

    def test_non_string_input_returns_floor(self):
        self.assertEqual(sh.parse_timestamp(1720000000), sh._EPOCH)
        self.assertEqual(sh.parse_timestamp({"t": 1}), sh._EPOCH)

    def test_all_results_are_comparable(self):
        # The whole point: mixed/missing formats must never raise when sorted.
        vals = ["2026-07-08T08:38:24Z", "", None, "2026-07-07", "garbage"]
        parsed = sorted(sh.parse_timestamp(v) for v in vals)
        self.assertEqual(len(parsed), 5)


class RecencyTests(unittest.TestCase):
    def test_voice_latest_timestamp_picks_newest_quote(self):
        v = _voice("x", "2026-07-01T00:00:00Z", "2026-07-08T00:00:00Z", "2026-07-05T00:00:00Z")
        self.assertEqual(sh.voice_latest_timestamp(v).day, 8)

    def test_no_quotes_is_floor(self):
        self.assertEqual(sh.voice_latest_timestamp({"quotes": []}), sh._EPOCH)

    def test_sort_voices_newest_first(self):
        old = _voice("old", "2026-07-01T00:00:00Z")
        new = _voice("new", "2026-07-08T00:00:00Z")
        mid = _voice("mid", "2026-07-05T00:00:00Z")
        order = [v["voiceId"] for v in sh.sort_voices_by_recency([old, new, mid])]
        self.assertEqual(order, ["new", "mid", "old"])

    def test_sort_does_not_mutate_input(self):
        voices = [_voice("a", "2026-07-01T00:00:00Z"), _voice("b", "2026-07-08T00:00:00Z")]
        before = [v["voiceId"] for v in voices]
        sh.sort_voices_by_recency(voices)
        self.assertEqual([v["voiceId"] for v in voices], before)

    def test_sort_quotes_by_recency(self):
        quotes = [{"timestamp": "2026-07-01T00:00:00Z"}, {"timestamp": "2026-07-08T00:00:00Z"}]
        self.assertEqual(sh.sort_quotes_by_recency(quotes)[0]["timestamp"][:10], "2026-07-08")


class StoriesForTopicsTests(unittest.TestCase):
    def _story(self, headline, slugs, voice_count=0):
        return {"headline": headline, "topicSlugs": slugs, "voiceCount": voice_count}

    def test_matches_on_topic_overlap(self):
        stories = [
            self._story("Iran", ["iran-conflict", "military-defense"], 40),
            self._story("Economy", ["economy", "inflation"], 30),
        ]
        got = sh.stories_for_topics(stories, ["iran-conflict"])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["headline"], "Iran")

    def test_ranks_by_overlap_then_voice_count(self):
        stories = [
            self._story("A", ["iran-conflict"], 100),                       # overlap 1
            self._story("B", ["iran-conflict", "foreign-policy"], 10),      # overlap 2 -> wins
        ]
        got = sh.stories_for_topics(stories, ["iran-conflict", "foreign-policy"])
        self.assertEqual(got[0]["headline"], "B")

    def test_voice_count_breaks_ties(self):
        stories = [
            self._story("Small", ["iran-conflict"], 5),
            self._story("Big", ["iran-conflict"], 90),
        ]
        got = sh.stories_for_topics(stories, ["iran-conflict"])
        self.assertEqual(got[0]["headline"], "Big")

    def test_limit_is_respected(self):
        stories = [self._story(f"S{i}", ["iran-conflict"], i) for i in range(5)]
        self.assertEqual(len(sh.stories_for_topics(stories, ["iran-conflict"], limit=2)), 2)

    def test_returns_original_story_objects(self):
        # renderStoryCard needs the full story dict (clusters, voiceCount),
        # so the join must return the same objects, not copies.
        story = self._story("Iran", ["iran-conflict"], 40)
        got = sh.stories_for_topics([story], ["iran-conflict"])
        self.assertIs(got[0], story)

    def test_no_match_returns_empty(self):
        stories = [self._story("Iran", ["iran-conflict"])]
        self.assertEqual(sh.stories_for_topics(stories, ["sports"]), [])

    def test_empty_inputs_are_safe(self):
        self.assertEqual(sh.stories_for_topics([], ["x"]), [])
        self.assertEqual(sh.stories_for_topics([{"topicSlugs": ["x"]}], []), [])

    def test_case_insensitive_and_topics_alias(self):
        stories = [{"headline": "Iran", "topics": ["Iran-Conflict"], "voiceCount": 1}]
        self.assertEqual(len(sh.stories_for_topics(stories, ["iran-conflict"])), 1)

    def test_skips_non_dict_stories(self):
        stories = ["garbage", {"headline": "Iran", "topicSlugs": ["iran-conflict"]}]
        self.assertEqual(len(sh.stories_for_topics(stories, ["iran-conflict"])), 1)


class ResultSlugTests(unittest.TestCase):
    """The save-side (lookup.py) and fallback-side (serve.py) must agree."""

    def test_basic_slug(self):
        self.assertEqual(sh.result_slug("US Iran Strikes"), "us-iran-strikes")

    def test_lowercases_and_collapses_punctuation(self):
        self.assertEqual(sh.result_slug("Trump's NATO Summit!!"), "trump-s-nato-summit-")

    def test_truncates_to_50(self):
        self.assertLessEqual(len(sh.result_slug("word " * 40)), 50)

    def test_empty_and_none_safe(self):
        self.assertEqual(sh.result_slug(""), "")
        self.assertEqual(sh.result_slug(None), "")

    def test_matches_the_two_call_site_formulas(self):
        # Lock the exact formula both serve.py and lookup.py inline-replicated
        # before this helper existed; a drift here silently breaks the fallback.
        import re
        for q in ["US Iran strikes", "Graham Platner!!", "AI & the economy", ""]:
            legacy = re.sub(r'[^a-z0-9]+', '-', q.lower())[:50]
            self.assertEqual(sh.result_slug(q), legacy)


if __name__ == "__main__":
    unittest.main(verbosity=2)
