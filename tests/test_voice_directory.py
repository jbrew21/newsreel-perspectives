#!/usr/bin/env python3
"""Tests for the voice directory work (Aug 29 2026):

  - serve.build_voice_activity: the compact summary that replaced a ~760KB
    topic-index download on the /voices browse page.
  - build_stances topic ordering: profiles used to sort topics by "touched
    most recently", so a one-off post outranked a voice's real throughline
    and the page read as a random pile.

    python -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import serve  # noqa: E402
import build_stances  # noqa: E402


def _entry(vid, name=None):
    return {
        "voiceId": vid,
        "voiceName": name or vid.replace("-", " ").title(),
        "quote": "a quote that costs real bytes " * 8,
        "sourceUrl": f"https://x.com/{vid}/status/1",
        "platform": "x",
        "timestamp": "2026-08-28T12:00:00+00:00",
    }


class VoiceActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.posts = self.root / "data" / "posts"
        self.posts.mkdir(parents=True)

    def _index(self, day_offset, mapping):
        day = (datetime.now() - timedelta(days=day_offset)).strftime("%Y-%m-%d")
        (self.posts / f"topic-index-{day}.json").write_text(json.dumps(mapping))
        return day

    def test_summarizes_topics_posts_and_recency(self):
        today = self._index(0, {
            "immigration": [_entry("jane-doe"), _entry("john-roe")],
            "economy-trade": [_entry("jane-doe")],
        })
        out = serve.build_voice_activity(root=str(self.root))
        self.assertEqual(out["jane-doe"]["posts"], 2)
        self.assertEqual(sorted(out["jane-doe"]["topics"]),
                         ["economy-trade", "immigration"])
        self.assertEqual(out["jane-doe"]["lastActive"], today)
        self.assertTrue(out["jane-doe"]["active"])

    def test_stale_voice_marked_inactive_but_still_listed(self):
        # A voice must never silently vanish — the card says "Last posted X".
        old = self._index(30, {"immigration": [_entry("rip-van")]})
        out = serve.build_voice_activity(root=str(self.root))
        self.assertIn("rip-van", out)
        self.assertFalse(out["rip-van"]["active"])
        self.assertEqual(out["rip-van"]["lastActive"], old)

    def test_last_active_is_the_newest_day_across_files(self):
        self._index(5, {"immigration": [_entry("jane-doe")]})
        newest = self._index(1, {"economy-trade": [_entry("jane-doe")]})
        out = serve.build_voice_activity(root=str(self.root))
        self.assertEqual(out["jane-doe"]["lastActive"], newest)
        self.assertEqual(out["jane-doe"]["posts"], 2)

    def test_topics_capped_so_payload_stays_small(self):
        self._index(0, {f"topic-{i}": [_entry("chatty")] for i in range(15)})
        out = serve.build_voice_activity(root=str(self.root))
        self.assertEqual(len(out["chatty"]["topics"]), 8)
        self.assertEqual(out["chatty"]["posts"], 15)  # count is NOT capped

    def test_payload_is_far_smaller_than_the_raw_index(self):
        raw = {f"topic-{i}": [_entry(f"voice-{v}") for v in range(20)]
               for i in range(10)}
        self._index(0, raw)
        out = serve.build_voice_activity(root=str(self.root))
        self.assertLess(len(json.dumps(out)), len(json.dumps(raw)) / 5)

    def test_missing_posts_dir_returns_empty(self):
        self.assertEqual(serve.build_voice_activity(root="/nonexistent"), {})

    def test_malformed_entries_do_not_crash(self):
        self._index(0, {
            "immigration": [_entry("jane-doe"), {"noVoiceId": True}],
            "broken": "not-a-list",
        })
        out = serve.build_voice_activity(root=str(self.root))
        self.assertEqual(out["jane-doe"]["posts"], 1)


class StanceOrderingTests(unittest.TestCase):
    """Topics lead with what a voice actually covers, not what they touched last."""

    # Deliberately unalike wording per index: build_store drops near-duplicate
    # quotes (>70% word overlap), so templated fixtures would collapse into one
    # stance and quietly invalidate every count assertion below.
    _BODIES = [
        "Sanctions belong nowhere near this negotiation and never have",
        "Courts keep ignoring what voters plainly asked them to protect",
        "Funding these programs rewards exactly the wrong institutions",
        "Nobody in leadership will say the quiet part about costs",
        "Journalists covering it missed the entire underlying question",
        "State legislatures moved faster than anyone predicted last spring",
        "Enforcement priorities shifted without a single public hearing",
        "Polling shows an electorate far less divided than pundits claim",
        "Regulators had years of warning and chose delay every time",
        "The economic argument here collapses under basic arithmetic",
    ]

    def _post(self, topic, day_offset, i=0):
        day = (datetime.now() - timedelta(days=day_offset)).strftime("%Y-%m-%d")
        body = self._BODIES[i % len(self._BODIES)]
        return {
            "sourceUrl": f"https://x.com/v/status/{topic}-{day}-{i}",
            "quote": body,
            "text": body,
            "topic": topic,
            "stance": "strong",
            "relevance": "high",
            "summary": f"Position on {topic} {i}",
            "platform": "x",
            "timestamp": f"{day}T12:00:00+00:00",
            "date": day,
        }

    def _build(self, posts):
        stances = [build_stances.stance_from_post(p) for p in posts]
        stances = [s for s in stances if s]
        cutoff = (datetime.now() - timedelta(days=build_stances.MAX_AGE_DAYS)
                  ).strftime('%Y-%m-%d')
        today = datetime.now().strftime('%Y-%m-%d')
        return build_stances.build_store(
            "v", "V", stances, None, build_stances.load_topic_labels(),
            cutoff, today)

    def test_volume_outranks_a_single_newer_post(self):
        posts = ([self._post("culture-war", 3, i) for i in range(6)] +
                 [self._post("religion-faith", 1)])
        topics = self._build(posts)["topics"]
        self.assertEqual(topics[0]["topic"], "culture-war")
        self.assertEqual(topics[0]["count"], 6)

    def test_live_topic_edges_out_a_slightly_bigger_dormant_one(self):
        # Freshness is a weight, not a partition: 4 live beats 5 dormant...
        posts = ([self._post("old-beat", 90, i) for i in range(5)] +
                 [self._post("live-beat", 1, i) for i in range(4)])
        topics = self._build(posts)["topics"]
        self.assertEqual(topics[0]["topic"], "live-beat")
        self.assertTrue(topics[0]["active"])

    def test_a_much_bigger_dormant_beat_still_leads(self):
        # ...but a real body of work is not displaced by one recent flurry.
        # This is the AOC case: her 7-stance beat must outrank a 1-stance
        # live topic, which the earlier active/dormant split got backwards.
        posts = ([self._post("old-beat", 40, i) for i in range(7)] +
                 [self._post("live-beat", 1)])
        topics = self._build(posts)["topics"]
        self.assertEqual(topics[0]["topic"], "old-beat")
        self.assertEqual(topics[0]["count"], 7)

    def test_ordering_is_monotonic_by_weighted_score(self):
        posts = ([self._post("a", 1, i) for i in range(4)] +
                 [self._post("b", 60, i) for i in range(5)] +
                 [self._post("c", 2, i) for i in range(2)])
        topics = self._build(posts)["topics"]
        scores = [t["count"] * (1.5 if t["active"] else 1.0) for t in topics]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_counts_descend_within_the_live_group(self):
        posts = []
        for topic, n in (("a", 2), ("b", 7), ("c", 4)):
            posts += [self._post(topic, 2, i) for i in range(n)]
        counts = [t["count"] for t in self._build(posts)["topics"]]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_recency_breaks_ties_at_equal_volume(self):
        posts = ([self._post("older", 6, i) for i in range(3)] +
                 [self._post("newer", 2, i) for i in range(3)])
        topics = self._build(posts)["topics"]
        self.assertEqual(topics[0]["topic"], "newer")


if __name__ == "__main__":
    unittest.main()
