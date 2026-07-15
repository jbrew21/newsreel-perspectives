#!/usr/bin/env python3
"""Tests for scripts/collect.py categorization: the cross-day reuse cache and
the Message Batches path with its mandatory sequential fallback.

Run from the repo root with the stdlib test runner:

    python -m unittest discover -s tests -v

No real Claude API calls are made — the anthropic client and urllib are
mocked throughout.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Make scripts/ importable regardless of the current working directory.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import collect  # noqa: E402


def _post(url="https://x.com/a/status/1", text="This policy is insane", **over):
    base = {
        "voiceId": "jane-doe",
        "voiceName": "Jane Doe",
        "platform": "x",
        "text": text,
        "sourceUrl": url,
        "timestamp": "2026-07-13T12:00:00+00:00",
        "type": "tweet",
    }
    base.update(over)
    return base


def _cached_day_post(url, topic="immigration", stance="strong", relevance="high", **over):
    p = _post(url)
    p.update({"topic": topic, "relevance": relevance, "stance": stance,
              "summary": "Opposes the policy", "quote": p["text"][:300]})
    p.update(over)
    return p


def _reset_usage():
    collect._usage_stats.update({
        "claude_calls": 0,
        "total_input_chars": 0,
        "total_output_tokens_est": 0,
        "posts_reused": 0,
    })


class ReuseCacheTests(unittest.TestCase):
    """Change 1: {sourceUrl: categorization} map from recent day files."""

    def setUp(self):
        _reset_usage()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.posts_dir = Path(self.tmp.name)
        patcher = mock.patch.object(collect, "POSTS_DIR", self.posts_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_day_file(self, voice_id, day, posts):
        voice_dir = self.posts_dir / voice_id
        voice_dir.mkdir(parents=True, exist_ok=True)
        (voice_dir / f"{day}.json").write_text(json.dumps({
            "voiceId": voice_id, "voiceName": "Jane Doe",
            "date": day, "posts": posts, "topicSummary": {},
        }))

    def _yesterday(self):
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    def test_hit_reuses_stored_categorization_verbatim(self):
        self._write_day_file("jane-doe", self._yesterday(),
                             [_cached_day_post("https://x.com/a/status/1")])
        cache = collect.load_categorized_cache("jane-doe")
        self.assertIn("https://x.com/a/status/1", cache)

        posts = [_post("https://x.com/a/status/1")]
        reused, new = collect.split_cached_posts(posts, cache)
        self.assertEqual(len(reused), 1)
        self.assertEqual(new, [])
        self.assertEqual(reused[0]["topic"], "immigration")
        self.assertEqual(reused[0]["stance"], "strong")
        self.assertEqual(reused[0]["relevance"], "high")
        self.assertEqual(reused[0]["summary"], "Opposes the policy")
        self.assertEqual(reused[0]["quote"], "This policy is insane")
        self.assertEqual(collect._usage_stats["posts_reused"], 1)

    def test_miss_goes_to_new(self):
        self._write_day_file("jane-doe", self._yesterday(),
                             [_cached_day_post("https://x.com/a/status/1")])
        cache = collect.load_categorized_cache("jane-doe")
        posts = [_post("https://x.com/a/status/999")]
        reused, new = collect.split_cached_posts(posts, cache)
        self.assertEqual(reused, [])
        self.assertEqual(len(new), 1)
        self.assertNotIn("topic", new[0])

    def test_post_without_source_url_always_sent(self):
        cache = {"https://x.com/a/status/1": {"topic": "immigration",
                                              "relevance": "high",
                                              "stance": "strong", "summary": ""}}
        posts = [_post(url="")]
        reused, new = collect.split_cached_posts(posts, cache)
        self.assertEqual(reused, [])
        self.assertEqual(len(new), 1)

    def test_uncategorized_day_file_entries_not_trusted(self):
        # A total-Claude-failure day writes posts with no 'topic' key — those
        # must not populate the cache.
        self._write_day_file("jane-doe", self._yesterday(),
                             [_post("https://x.com/a/status/1")])
        cache = collect.load_categorized_cache("jane-doe")
        self.assertEqual(cache, {})

    def test_neutral_or_low_entries_not_trusted(self):
        self._write_day_file("jane-doe", self._yesterday(), [
            _cached_day_post("https://x.com/a/status/1", stance="neutral"),
            _cached_day_post("https://x.com/a/status/2", relevance="low"),
        ])
        cache = collect.load_categorized_cache("jane-doe")
        self.assertEqual(cache, {})

    def test_today_file_wins_over_yesterday(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self._write_day_file("jane-doe", self._yesterday(),
                             [_cached_day_post("https://x.com/a/status/1", topic="immigration")])
        # today's re-run recategorized the same URL
        voice_dir = self.posts_dir / "jane-doe"
        (voice_dir / f"{today}.json").write_text(json.dumps({
            "posts": [_cached_day_post("https://x.com/a/status/1", topic="economy")],
        }))
        cache = collect.load_categorized_cache("jane-doe")
        self.assertEqual(cache["https://x.com/a/status/1"]["topic"], "economy")

    def test_missing_day_files_yield_empty_cache(self):
        self.assertEqual(collect.load_categorized_cache("nobody"), {})


def _fake_client(create=None, retrieve=None, results=None, cancel=None):
    """Build a mock anthropic client with a messages.batches namespace."""
    batches = SimpleNamespace(
        create=create or mock.Mock(return_value=SimpleNamespace(id="msgbatch_test", processing_status="in_progress")),
        retrieve=retrieve or mock.Mock(return_value=SimpleNamespace(id="msgbatch_test", processing_status="ended")),
        results=results or mock.Mock(return_value=iter([])),
        cancel=cancel or mock.Mock(),
    )
    return SimpleNamespace(messages=SimpleNamespace(batches=batches))


def _succeeded(custom_id, items, output_tokens=100):
    """A fake batch result of type 'succeeded' whose message text is JSON."""
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(
            type="succeeded",
            message=SimpleNamespace(
                content=[SimpleNamespace(type="text", text=json.dumps(items))],
                usage=SimpleNamespace(output_tokens=output_tokens),
            ),
        ),
    )


def _errored(custom_id, kind="errored"):
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type=kind))


class BatchCategorizationTests(unittest.TestCase):
    """Change 2: Message Batches path keyed by custom_id, with fallback."""

    def setUp(self):
        _reset_usage()
        # Make sure the code path thinks an API key exists (no real calls are
        # possible — the anthropic client is mocked in every test).
        patcher = mock.patch.object(collect, "ANTHROPIC_API_KEY", "test-key")
        patcher.start()
        self.addCleanup(patcher.stop)
        # Keep enforce_taxonomy from reading taxonomy.json / calling Claude.
        tax = mock.patch.object(collect, "enforce_taxonomy", side_effect=lambda s: s)
        tax.start()
        self.addCleanup(tax.stop)

    def _entries(self):
        return [
            ("jane-doe", "Jane Doe", [_post("https://x.com/a/status/1")]),
            ("john-roe", "John Roe", [_post("https://x.com/b/status/2", voiceId="john-roe")]),
        ]

    def _run_batch(self, client, entries=None):
        with mock.patch("anthropic.Anthropic", return_value=client):
            with redirect_stdout(io.StringIO()):
                return collect.categorize_posts_batch(entries or self._entries())

    def test_results_keyed_by_custom_id_out_of_order(self):
        items0 = [{"index": 0, "topic": "immigration", "relevance": "high",
                   "stance": "strong", "summary": "Opposes the policy"}]
        items1 = [{"index": 0, "topic": "economy", "relevance": "high",
                   "stance": "lean", "summary": "Backs the tariffs"}]
        # Results arrive REVERSED relative to submission order.
        client = _fake_client(results=mock.Mock(return_value=iter([
            _succeeded("voice-1", items1),
            _succeeded("voice-0", items0),
        ])))
        results = self._run_batch(client)
        self.assertEqual(results["jane-doe"][0]["topic"], "immigration")
        self.assertEqual(results["john-roe"][0]["topic"], "economy")
        # Submission counted as Claude calls; output tokens from batch usage.
        self.assertEqual(collect._usage_stats["claude_calls"], 2)
        self.assertEqual(collect._usage_stats["total_output_tokens_est"], 200)

    def test_errored_result_missing_from_dict_triggers_sequential_fallback(self):
        items0 = [{"index": 0, "topic": "immigration", "relevance": "high",
                   "stance": "strong", "summary": ""}]
        client = _fake_client(results=mock.Mock(return_value=iter([
            _succeeded("voice-0", items0),
            _errored("voice-1"),
        ])))
        entries = self._entries()
        results = self._run_batch(client, entries)
        self.assertIn("jane-doe", results)
        self.assertNotIn("john-roe", results)  # caller must go sequential

        # Simulate the caller's fallback: sequential categorize_posts for the
        # errored voice, with urllib mocked (no real API call).
        api_response = json.dumps({
            "content": [{"type": "text", "text": json.dumps([
                {"index": 0, "topic": "economy", "relevance": "high",
                 "stance": "strong", "summary": "Backs the tariffs"},
            ])}],
            "usage": {"output_tokens": 42},
        }).encode()
        fake_resp = mock.MagicMock()
        fake_resp.__enter__.return_value.read.return_value = api_response
        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            with redirect_stdout(io.StringIO()):
                kept = collect.categorize_posts("John Roe", entries[1][2])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["topic"], "economy")
        self.assertEqual(kept[0]["stance"], "strong")

    def test_expired_result_also_missing(self):
        client = _fake_client(results=mock.Mock(return_value=iter([
            _errored("voice-0", kind="expired"),
            _errored("voice-1", kind="canceled"),
        ])))
        results = self._run_batch(client)
        self.assertEqual(results, {})

    def test_poll_timeout_returns_none_and_cancels(self):
        cancel = mock.Mock()
        client = _fake_client(
            retrieve=mock.Mock(return_value=SimpleNamespace(
                id="msgbatch_test", processing_status="in_progress")),
            cancel=cancel,
        )
        with mock.patch.object(collect, "BATCH_POLL_TIMEOUT", 0):
            results = self._run_batch(client)
        self.assertIsNone(results)  # caller falls back to sequential for ALL voices
        cancel.assert_called_once_with("msgbatch_test")

    def test_batch_creation_failure_returns_none(self):
        client = _fake_client(create=mock.Mock(side_effect=RuntimeError("boom")))
        results = self._run_batch(client)
        self.assertIsNone(results)

    def test_no_api_key_returns_none(self):
        with mock.patch.object(collect, "ANTHROPIC_API_KEY", ""):
            self.assertIsNone(collect.categorize_posts_batch(self._entries()))

    def test_empty_entries_returns_none(self):
        self.assertIsNone(collect.categorize_posts_batch([]))

    def test_succeeded_but_unparseable_returns_empty_list_not_fallback(self):
        # Mirrors the sequential unparseable-response behavior: posts pass
        # through uncategorized; the voice must NOT be re-sent.
        result = SimpleNamespace(
            custom_id="voice-0",
            result=SimpleNamespace(
                type="succeeded",
                message=SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="no json here")],
                    usage=SimpleNamespace(output_tokens=5),
                ),
            ),
        )
        client = _fake_client(results=mock.Mock(return_value=iter([result])))
        results = self._run_batch(client, [self._entries()[0]])
        self.assertEqual(results, {"jane-doe": []})
        # And applying an empty categorization leaves posts unfiltered.
        posts = [_post()]
        self.assertEqual(collect._apply_categorization(posts, []), posts)


class ApplyCategorizationTests(unittest.TestCase):
    """The shared apply/filter helper matches the original inline behavior."""

    def setUp(self):
        _reset_usage()
        tax = mock.patch.object(collect, "enforce_taxonomy", side_effect=lambda s: s)
        tax.start()
        self.addCleanup(tax.stop)

    def test_filter_keeps_only_strong_lean_high_medium(self):
        posts = [
            _post("https://x.com/a/1", text="Strong take"),
            _post("https://x.com/a/2", text="Just the news"),
            _post("https://x.com/a/3", text="Promo"),
        ]
        items = [
            {"index": 0, "topic": "economy", "relevance": "high", "stance": "strong", "summary": "s"},
            {"index": 1, "topic": "economy", "relevance": "high", "stance": "neutral", "summary": ""},
            {"index": 2, "topic": "other", "relevance": "low", "stance": "strong", "summary": ""},
        ]
        with redirect_stdout(io.StringIO()):
            kept = collect._apply_categorization(posts, items)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["quote"], "Strong take")

    def test_video_transcript_quote_extraction(self):
        posts = [_post("https://youtube.com/watch?v=abc",
                       text="[VIDEO: Some title] Real transcript sentence here.",
                       platform="youtube", type="video_transcript")]
        items = [{"index": 0, "topic": "economy", "relevance": "high",
                  "stance": "strong", "summary": "s"}]
        kept = collect._apply_categorization(posts, items)
        self.assertEqual(kept[0]["quote"], "Real transcript sentence here.")

    def test_out_of_range_index_ignored(self):
        posts = [_post()]
        items = [{"index": 5, "topic": "economy", "relevance": "high",
                  "stance": "strong", "summary": ""}]
        with redirect_stdout(io.StringIO()):
            kept = collect._apply_categorization(posts, items)
        self.assertEqual(kept, [])  # post never categorized -> filtered out

    def test_malformed_items_do_not_crash(self):
        # A stray int / string / null / bad-index object in the array (real
        # crash from the 2026-07-15 nightly run) must be skipped, and the
        # well-formed items in the same array must still apply.
        posts = [
            _post("https://x.com/a/1", text="Strong take"),
            _post("https://x.com/a/2", text="Another take"),
        ]
        items = [
            0,                       # bare int — the exact crash trigger
            "nonsense",              # bare string
            None,                    # null element
            {"index": "x", "topic": "economy", "relevance": "high", "stance": "strong"},
            {"index": 0, "topic": "economy", "relevance": "high", "stance": "strong", "summary": "s"},
            {"index": 1, "topic": "economy", "relevance": "medium", "stance": "lean", "summary": ""},
        ]
        with redirect_stdout(io.StringIO()):
            kept = collect._apply_categorization(posts, items)
        self.assertEqual(len(kept), 2)
        self.assertEqual({p["quote"] for p in kept}, {"Strong take", "Another take"})


if __name__ == "__main__":
    unittest.main()
