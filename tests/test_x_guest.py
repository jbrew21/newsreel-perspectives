#!/usr/bin/env python3
"""Tests for scripts/x_guest.py — the guest-token X reader that replaced Nitter.

No network: every HTTP call is mocked. The behaviours pinned here are the ones
that cost real time to work out.

    python -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import x_guest  # noqa: E402


def _resp(payload):
    """A urlopen context manager yielding JSON bytes."""
    cm = mock.MagicMock()
    cm.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return cm


def _tweet(rest_id, uid, text, created="Sat Aug 30 18:30:00 +0000 2026", **legacy):
    lg = {"full_text": text, "user_id_str": uid, "created_at": created, "id_str": rest_id}
    lg.update(legacy)
    return {"__typename": "Tweet", "rest_id": rest_id, "legacy": lg}


def _timeline(tweets):
    """Bury tweets in nested junk — the real payload is deeply nested, which is
    why the parser walks the tree instead of indexing a fixed path."""
    return {"data": {"user": {"result": {"timeline_v2": {"timeline": {"instructions": [
        {"type": "TimelineAddEntries", "entries": [
            {"content": {"itemContent": {"tweet_results": {"result": t}}}} for t in tweets
        ]}
    ]}}}}}}


class UserIdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(x_guest, 'ID_CACHE_PATH',
                                    Path(self.tmp.name) / 'ids.json')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = x_guest.XGuestClient()
        self.client._token = 'tok-1'          # skip activation in tests

    def test_resolves_a_normal_user(self):
        with mock.patch('urllib.request.urlopen', return_value=_resp(
                {"data": {"user": {"result": {"__typename": "User", "rest_id": "44196397"}}}})):
            self.assertEqual(self.client.user_id('elonmusk'), '44196397')

    def test_suspended_account_is_a_real_answer_not_a_failure(self):
        # The whole X outage was misdiagnosed because every probe used
        # SenJohnThune, a suspended account, and its UserUnavailable looked
        # like "guest access is blocked".
        with mock.patch('urllib.request.urlopen', return_value=_resp(
                {"data": {"user": {"result": {"__typename": "UserUnavailable",
                                              "message": "User is suspended"}}}})):
            self.assertIsNone(self.client.user_id('SenJohnThune'))
        self.assertEqual(self.client.stats['suspended'], 1)
        self.assertEqual(self.client.stats['not_found'], 0)

    def test_handle_is_case_insensitive_and_strips_at(self):
        with mock.patch('urllib.request.urlopen', return_value=_resp(
                {"data": {"user": {"result": {"__typename": "User", "rest_id": "1"}}}})) as u:
            self.client.user_id('@ElonMusk')
            self.client.user_id('elonmusk')
            self.assertEqual(u.call_count, 1)   # second call served from cache

    def test_negative_result_is_cached_so_we_stop_asking(self):
        with mock.patch('urllib.request.urlopen', return_value=_resp(
                {"data": {"user": {"result": {"__typename": "UserUnavailable",
                                              "message": "User is suspended"}}}})) as u:
            self.client.user_id('gone')
            self.client.user_id('gone')
            self.assertEqual(u.call_count, 1)

    def test_cache_round_trips_to_disk(self):
        with mock.patch('urllib.request.urlopen', return_value=_resp(
                {"data": {"user": {"result": {"__typename": "User", "rest_id": "77"}}}})):
            self.client.user_id('someone')
        self.client.save_cache()
        fresh = x_guest.XGuestClient()
        with mock.patch('urllib.request.urlopen', side_effect=AssertionError('should not call')):
            self.assertEqual(fresh.user_id('someone'), '77')


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(x_guest, 'ID_CACHE_PATH',
                                    Path(self.tmp.name) / 'ids.json')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = x_guest.XGuestClient()
        self.client._token = 'tok-1'
        self.client._ids['someone'] = 'UID'

    def _posts(self, tweets, **kw):
        with mock.patch('urllib.request.urlopen', return_value=_resp(_timeline(tweets))):
            return self.client.recent_posts('someone', **kw)

    def test_extracts_tweets_from_a_deeply_nested_payload(self):
        posts = self._posts([_tweet('1', 'UID', 'hello world')])
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]['text'], 'hello world')
        self.assertEqual(posts[0]['url'], 'https://x.com/someone/status/1')

    def test_other_peoples_tweets_are_dropped(self):
        # A profile timeline threads in replies by other accounts; attributing
        # those to this voice would be a misattribution.
        posts = self._posts([_tweet('1', 'UID', 'mine'),
                             _tweet('2', 'OTHER', 'someone else entirely')])
        self.assertEqual([p['text'] for p in posts], ['mine'])

    def test_retweets_are_dropped(self):
        posts = self._posts([_tweet('1', 'UID', 'mine'),
                             _tweet('2', 'UID', 'RT text',
                                    retweeted_status_result={'result': {}})])
        self.assertEqual([p['text'] for p in posts], ['mine'])

    def test_long_posts_prefer_note_tweet_over_truncated_full_text(self):
        t = _tweet('1', 'UID', 'truncated beginning…')
        t['note_tweet'] = {'note_tweet_results': {'result': {'text': 'the complete long post'}}}
        posts = self._posts([t])
        self.assertEqual(posts[0]['text'], 'the complete long post')

    def test_results_are_newest_first(self):
        posts = self._posts([
            _tweet('1', 'UID', 'older', created="Fri Aug 28 10:00:00 +0000 2026"),
            _tweet('2', 'UID', 'newer', created="Sat Aug 30 10:00:00 +0000 2026"),
        ])
        self.assertEqual([p['text'] for p in posts], ['newer', 'older'])

    def test_timestamps_parse_to_aware_datetimes(self):
        posts = self._posts([_tweet('1', 'UID', 'x')])
        self.assertIsNotNone(posts[0]['created_at'].tzinfo)

    def test_empty_text_is_skipped(self):
        posts = self._posts([_tweet('1', 'UID', '')])
        self.assertEqual(posts, [])

    def test_suspended_user_yields_no_posts_without_calling_timeline(self):
        self.client._ids['gone'] = None
        with mock.patch('urllib.request.urlopen', side_effect=AssertionError('should not call')):
            self.assertEqual(self.client.recent_posts('gone'), [])


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(x_guest, 'ID_CACHE_PATH',
                                    Path(self.tmp.name) / 'ids.json')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_429_rotates_the_token_and_retries_once(self):
        client = x_guest.XGuestClient()
        client._token = 'stale'
        err = urllib.error.HTTPError('u', 429, 'Too Many Requests', {}, None)
        good = _resp({"data": {"user": {"result": {"__typename": "User", "rest_id": "9"}}}})
        with mock.patch('urllib.request.urlopen', side_effect=[err, _resp({'guest_token': 'fresh'}), good]):
            with mock.patch('time.sleep'):
                self.assertEqual(client.user_id('someone'), '9')
        self.assertEqual(client.stats['rate_limited'], 1)
        self.assertGreaterEqual(client.stats['tokens'], 1)

    def test_a_second_429_gives_up_rather_than_looping(self):
        client = x_guest.XGuestClient()
        client._token = 'stale'
        err = urllib.error.HTTPError('u', 429, 'Too Many Requests', {}, None)
        with mock.patch('urllib.request.urlopen',
                        side_effect=[err, _resp({'guest_token': 'fresh'}), err]):
            with mock.patch('time.sleep'):
                self.assertIsNone(client.user_id('someone'))
        self.assertEqual(client.stats['errors'], 1)


if __name__ == '__main__':
    unittest.main()
