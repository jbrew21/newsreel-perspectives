#!/usr/bin/env python3
"""Tests for scripts/lookup.py search-precision guards (added Aug 28 2026 after
"immigration" returned podcast blurbs and Census posts via broad topic tags).

Run from the repo root:

    python -m unittest discover -s tests -v

No Claude API calls — these helpers are pure (topic_bonus_terms reads the
repo's real taxonomy.json, which is a committed fixture).
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import lookup  # noqa: E402


class BroadTopicGuardTests(unittest.TestCase):
    def test_unnamed_umbrellas_dropped(self):
        # The exact Aug 28 failure: "immigration" matched two umbrellas.
        topics = ["immigration", "refugees-humanitarian",
                  "foreign-policy-diplomacy", "congress-legislation"]
        kept = lookup.drop_unnamed_broad_topics(topics, "immigration")
        self.assertEqual(kept, ["immigration", "refugees-humanitarian"])

    def test_umbrella_kept_when_query_names_it(self):
        kept = lookup.drop_unnamed_broad_topics(
            ["congress-legislation", "economy-trade"], "congress budget fight")
        self.assertEqual(kept, ["congress-legislation"])

    def test_specific_topics_untouched(self):
        topics = ["iran-conflict", "military-defense"]
        self.assertEqual(
            lookup.drop_unnamed_broad_topics(topics, "iran strikes"), topics)

    def test_order_preserved(self):
        topics = ["gun-policy", "elections", "criminal-justice"]
        self.assertEqual(
            lookup.drop_unnamed_broad_topics(topics, "gun control debate"),
            ["gun-policy", "criminal-justice"])


class TopicBonusTermsTests(unittest.TestCase):
    def test_immigration_terms_include_signature_words(self):
        terms = lookup.topic_bonus_terms(["immigration"])
        # From the taxonomy description: "Immigration policy, ICE enforcement,
        # deportation, border security, DACA, asylum"
        for w in ("ice", "deportation", "border", "asylum"):
            self.assertIn(w, terms)

    def test_generic_words_excluded(self):
        terms = lookup.topic_bonus_terms(["immigration"])
        self.assertNotIn("policy", terms)

    def test_limit_two_topics(self):
        terms = lookup.topic_bonus_terms(
            ["immigration", "refugees-humanitarian", "sports"])
        # Third topic's words must not leak in.
        self.assertNotIn("sports", terms)

    def test_empty_topics_empty_terms(self):
        self.assertEqual(lookup.topic_bonus_terms([]), set())


if __name__ == "__main__":
    unittest.main()
