#!/usr/bin/env python3
"""Tests for the /api/agenda (Split Screen) builder in serve.py.

    python -m unittest discover -s tests -v

Covers voice_lean bucketing boundaries, the build_agenda fixture behavior
(empty index, unknown voices, story-slug join, window fallback), and a smoke
test against repo data.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import serve  # noqa: E402

NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def _voice(vid, tags):
    return {"id": vid, "name": vid.title(), "photo": f"/photos/{vid}.jpg", "tags": tags}


def _post(vid, hours_ago):
    return {"voiceId": vid, "voiceName": vid, "quote": "q", "sourceUrl": "",
            "platform": "x", "timestamp": _iso(hours_ago)}


class VoiceLeanTests(unittest.TestCase):
    def test_progressive_tag_is_left(self):
        self.assertEqual(serve.voice_lean(_voice("a", ["progressive"])), "left")

    def test_conservative_tag_is_right(self):
        self.assertEqual(serve.voice_lean(_voice("a", ["conservative"])), "right")

    def test_centrist_tag_is_center(self):
        self.assertEqual(serve.voice_lean(_voice("a", ["centrist"])), "center")

    def test_no_tags_is_center(self):
        self.assertEqual(serve.voice_lean(_voice("a", [])), "center")

    def test_unscorable_tags_are_center(self):
        # classified=False when nothing matches TAG_SCORES
        self.assertEqual(serve.voice_lean(_voice("a", ["basketball fan"])), "center")

    def test_missing_voice_dict(self):
        self.assertEqual(serve.voice_lean(None), "center")


class BuildAgendaFixtureTests(unittest.TestCase):
    def _make_root(self, voices, topic_index, stories=None, taxonomy=None):
        tmp = tempfile.mkdtemp()
        posts = os.path.join(tmp, "data", "posts")
        os.makedirs(posts)
        with open(os.path.join(tmp, "data", "voices.json"), "w") as f:
            json.dump(voices, f)
        with open(os.path.join(posts, "topic-index-2026-07-13.json"), "w") as f:
            json.dump(topic_index, f)
        if stories is not None:
            with open(os.path.join(posts, "stories-2026-07-13.json"), "w") as f:
                json.dump(stories, f)
        with open(os.path.join(tmp, "data", "taxonomy.json"), "w") as f:
            json.dump(taxonomy or {"topics": []}, f)
        return tmp

    def test_empty_topic_index_gives_empty_columns(self):
        root = self._make_root([_voice("a", ["progressive"])], {})
        agenda = serve.build_agenda(root=root, now=NOW)
        self.assertEqual(agenda["left"]["topics"], [])
        self.assertEqual(agenda["right"]["topics"], [])

    def test_unknown_voice_excluded(self):
        root = self._make_root(
            [_voice("lefty", ["progressive"])],
            {"elections": [_post("lefty", 1), _post("ghost-voice", 1)]},
        )
        agenda = serve.build_agenda(root=root, now=NOW)
        self.assertEqual(agenda["left"]["topics"][0]["voices"], 1)
        self.assertEqual(agenda["left"]["topics"][0]["posts"], 1)

    def test_center_voices_excluded_and_counted(self):
        root = self._make_root(
            [_voice("lefty", ["progressive"]), _voice("mid", ["centrist"])],
            {"elections": [_post("lefty", 1), _post("mid", 1)]},
        )
        agenda = serve.build_agenda(root=root, now=NOW)
        self.assertEqual(agenda["centerCount"], 1)
        self.assertEqual(agenda["left"]["topics"][0]["voices"], 1)

    def test_story_slug_join(self):
        root = self._make_root(
            [_voice("lefty", ["progressive"])],
            {"iran-conflict": [_post("lefty", 1)]},
            stories=[{"headline": "Iran Strikes Resume", "topicSlugs": ["iran-conflict"]}],
        )
        agenda = serve.build_agenda(root=root, now=NOW)
        self.assertEqual(agenda["left"]["topics"][0]["storySlug"], "iran-strikes-resume")

    def test_taxonomy_display_name_used(self):
        root = self._make_root(
            [_voice("lefty", ["progressive"])],
            {"iran-conflict": [_post("lefty", 1)]},
            taxonomy={"topics": [{"slug": "iran-conflict", "display": "Iran Conflict"}]},
        )
        agenda = serve.build_agenda(root=root, now=NOW)
        self.assertEqual(agenda["left"]["topics"][0]["display"], "Iran Conflict")

    def test_window_fallback_widens_when_thin(self):
        # All posts are 100h old — outside 48h, inside 168h. Both sides need
        # >=3 topics with >=2 voices to satisfy the healthy() check at 48h.
        lefties = [_voice(f"l{i}", ["progressive"]) for i in range(4)]
        righties = [_voice(f"r{i}", ["conservative"]) for i in range(4)]
        index = {}
        for t in ("elections", "immigration", "culture-war"):
            index[t] = [_post(f"l{i}", 100) for i in range(4)] + \
                       [_post(f"r{i}", 100) for i in range(4)]
        root = self._make_root(lefties + righties, index)
        agenda = serve.build_agenda(root=root, now=NOW)
        self.assertEqual(agenda["windowHours"], serve.AGENDA_FALLBACK_HOURS)
        self.assertTrue(agenda["widened"])
        self.assertEqual(len(agenda["left"]["topics"]), 3)

    def test_old_posts_filtered_inside_window(self):
        root = self._make_root(
            [_voice("lefty", ["progressive"]), _voice("l2", ["progressive"])],
            {"elections": [_post("lefty", 1), _post("l2", 500)]},
        )
        agenda = serve.build_agenda(root=root, now=NOW)
        # l2's post is outside even the fallback window
        self.assertEqual(agenda["left"]["topics"][0]["voices"], 1)

    def test_shared_attention_requires_both_sides(self):
        lefties = [_voice(f"l{i}", ["progressive"]) for i in range(4)]
        righties = [_voice(f"r{i}", ["conservative"]) for i in range(4)]
        index = {"elections": [_post(f"l{i}", 1) for i in range(4)] +
                              [_post(f"r{i}", 1) for i in range(4)],
                 "culture-war": [_post(f"r{i}", 1) for i in range(4)]}
        root = self._make_root(lefties + righties, index)
        agenda = serve.build_agenda(root=root, now=NOW)
        shared_slugs = [s["slug"] for s in agenda["shared"]]
        self.assertIn("elections", shared_slugs)
        self.assertNotIn("culture-war", shared_slugs)

    def test_non_string_timestamp_does_not_crash_endpoint(self):
        # A numeric epoch from a scraper change must degrade to oldest (and
        # fall out of the window), not raise and 500 /api/agenda.
        bad = {"voiceId": "lefty", "quote": "q", "sourceUrl": "",
               "platform": "x", "timestamp": 1783300000}
        root = self._make_root(
            [_voice("lefty", ["progressive"])],
            {"elections": [_post("lefty", 1), bad]},
        )
        agenda = serve.build_agenda(root=root, now=NOW)
        self.assertEqual(agenda["left"]["topics"][0]["posts"], 1)

    def test_voice_meta_cache_invalidates_on_mtime_change(self):
        root = self._make_root([_voice("lefty", ["progressive"])], {})
        meta1, leans1 = serve.load_voice_meta(root)
        self.assertEqual(leans1, {"lefty": "left"})
        # Rewrite voices.json with a new voice and a bumped mtime
        path = os.path.join(root, "data", "voices.json")
        with open(path, "w") as f:
            json.dump([_voice("righty", ["conservative"])], f)
        os.utime(path, (os.path.getmtime(path) + 10,) * 2)
        meta2, leans2 = serve.load_voice_meta(root)
        self.assertEqual(leans2, {"righty": "right"})

    def test_uncategorized_skipped(self):
        root = self._make_root(
            [_voice("lefty", ["progressive"])],
            {"uncategorized": [_post("lefty", 1)]},
        )
        agenda = serve.build_agenda(root=root, now=NOW)
        self.assertEqual(agenda["left"]["topics"], [])


class RepoDataSmokeTests(unittest.TestCase):
    def test_build_agenda_runs_against_repo_data(self):
        agenda = serve.build_agenda()
        self.assertIn("left", agenda)
        self.assertIn("right", agenda)
        self.assertLessEqual(len(agenda["left"]["topics"]), serve.AGENDA_TOP_N)
        self.assertLessEqual(len(agenda["right"]["topics"]), serve.AGENDA_TOP_N)
        for t in agenda["left"]["topics"] + agenda["right"]["topics"]:
            self.assertGreaterEqual(t["voices"], 1)
            self.assertGreaterEqual(t["posts"], t["voices"])
            self.assertLessEqual(len(t["topVoices"]), 3)

    def test_wire_entries_carry_lean(self):
        wire = serve.build_wire()
        if not wire:
            self.skipTest("no wire data today")
        self.assertTrue(all(e.get("lean") in ("left", "right", "center") for e in wire))


if __name__ == "__main__":
    unittest.main()
