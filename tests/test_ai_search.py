#!/usr/bin/env python3
"""Tests for lookup.ai_search — the natural-language ("Ask AI") search.

    python -m unittest discover -s tests -v

Covers the orchestration (parse -> retrieve -> filter -> summarize) with the
Claude calls and retrieval mocked, plus the index-based matching and the
graceful fallbacks when the model output can't be parsed.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import lookup  # noqa: E402


def _voices(n):
    return [{'voiceId': f'v{i}', 'voiceName': f'Voice {i}', 'lens': 'bio',
             'tags': ['t'], 'quotes': [{'quote': f'take {i}'}]} for i in range(n)]


class FilterAndSummarizeTests(unittest.TestCase):
    def test_index_matching_maps_to_voice_ids(self):
        with mock.patch.object(lookup, '_claude_message',
                               return_value='{"matched": [0, 2], "summary": "A vs C."}'):
            out = lookup._filter_and_summarize('q', 'Republicans', _voices(4))
        self.assertEqual(out['matchedVoiceIds'], ['v0', 'v2'])
        self.assertEqual(out['summary'], 'A vs C.')

    def test_out_of_range_indices_are_dropped(self):
        with mock.patch.object(lookup, '_claude_message',
                               return_value='{"matched": [1, 99, -1], "summary": "s"}'):
            out = lookup._filter_and_summarize('q', None, _voices(3))
        self.assertEqual(out['matchedVoiceIds'], ['v1'])

    def test_unparseable_with_audience_falls_back_to_empty(self):
        # An audience was requested but the model output is junk: better to show
        # nothing than a mislabeled dump of everyone.
        with mock.patch.object(lookup, '_claude_message', return_value='not json'):
            out = lookup._filter_and_summarize('q', 'doctors', _voices(5))
        self.assertEqual(out['matchedVoiceIds'], [])

    def test_unparseable_without_audience_falls_back_to_top_voices(self):
        with mock.patch.object(lookup, '_claude_message', return_value='garbage'):
            out = lookup._filter_and_summarize('q', None, _voices(20))
        self.assertEqual(out['matchedVoiceIds'], [f'v{i}' for i in range(8)])


class AiSearchTests(unittest.TestCase):
    def test_audience_filters_voices_and_keeps_summary(self):
        with mock.patch.object(lookup, '_parse_ai_question',
                               return_value={'searchQuery': 'iran', 'audience': 'Republicans'}), \
             mock.patch.object(lookup, 'lookup_story',
                               return_value={'voices': _voices(4), 'matchedTopics': ['iran']}), \
             mock.patch.object(lookup, '_filter_and_summarize',
                               return_value={'matchedVoiceIds': ['v1', 'v3'],
                                             'summary': 'Hawks want escalation.',
                                             'audienceLabel': 'Republicans'}):
            out = lookup.ai_search('what do Republicans think of Iran?')
        self.assertEqual([v['voiceId'] for v in out['voices']], ['v1', 'v3'])
        self.assertEqual(out['summary'], 'Hawks want escalation.')
        self.assertEqual(out['audience'], 'Republicans')

    def test_no_voices_returns_no_results(self):
        with mock.patch.object(lookup, '_parse_ai_question',
                               return_value={'searchQuery': 'x', 'audience': None}), \
             mock.patch.object(lookup, 'lookup_story', return_value={'voices': []}):
            out = lookup.ai_search('anything?')
        self.assertTrue(out['noResults'])
        self.assertEqual(out['voices'], [])

    def test_empty_summary_gets_safety_net(self):
        with mock.patch.object(lookup, '_parse_ai_question',
                               return_value={'searchQuery': 'x', 'audience': 'doctors'}), \
             mock.patch.object(lookup, 'lookup_story',
                               return_value={'voices': _voices(3), 'matchedTopics': []}), \
             mock.patch.object(lookup, '_filter_and_summarize',
                               return_value={'matchedVoiceIds': [], 'summary': '',
                                             'audienceLabel': 'doctors'}):
            out = lookup.ai_search('what do doctors say?')
        self.assertEqual(out['voices'], [])
        self.assertIn('doctors', out['summary'])   # non-empty fallback


if __name__ == '__main__':
    unittest.main()
