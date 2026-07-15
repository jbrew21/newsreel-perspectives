#!/usr/bin/env python3
"""Tests for scripts/stories.py Claude plumbing — model config, call_claude
retry/parse behavior, and the Message Batches analyze pass with sequential
fallback. All network traffic is mocked; no real API calls are made.

    python -m unittest discover -s tests -v
"""

import io
import json
import sys
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import stories  # noqa: E402


def _fake_urlopen_response(payload):
    """Context manager mimicking urllib.request.urlopen for a JSON payload."""
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    cm = mock.MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


def _message_with_text(text):
    """A /v1/messages response body whose first content block is `text`."""
    return {'content': [{'type': 'text', 'text': text}]}


class RequestBodyTests(unittest.TestCase):
    def test_model_is_sonnet_5(self):
        self.assertEqual(stories.CLAUDE_MODEL, 'claude-sonnet-5')
        body = stories._claude_request_body('hi', 512)
        self.assertEqual(body['model'], 'claude-sonnet-5')

    def test_thinking_explicitly_disabled(self):
        # Sonnet 5 defaults to adaptive thinking when 'thinking' is omitted;
        # thinking tokens would eat the tight max_tokens budgets.
        body = stories._claude_request_body('hi', 512)
        self.assertEqual(body['thinking'], {'type': 'disabled'})

    def test_no_sampling_params(self):
        # Non-default temperature/top_p/top_k are rejected on Sonnet 5.
        body = stories._claude_request_body('hi', 512)
        for key in ('temperature', 'top_p', 'top_k'):
            self.assertNotIn(key, body)

    def test_max_tokens_and_messages(self):
        body = stories._claude_request_body('the prompt', 1536)
        self.assertEqual(body['max_tokens'], 1536)
        self.assertEqual(body['messages'],
                         [{'role': 'user', 'content': 'the prompt'}])


class ParseClaudeMessageTests(unittest.TestCase):
    def test_extracts_json_blob(self):
        msg = _message_with_text('Sure! {"clusters": {"A": ["V"]}} done')
        self.assertEqual(stories._parse_claude_message(msg),
                         {'clusters': {'A': ['V']}})

    def test_no_json_returns_none(self):
        self.assertIsNone(
            stories._parse_claude_message(_message_with_text('no json here')))

    def test_empty_content_returns_none(self):
        self.assertIsNone(stories._parse_claude_message({'content': [{}]}))

    def test_malformed_json_raises(self):
        # call_claude treats this as transient (retried); batch path skips it.
        with self.assertRaises(json.JSONDecodeError):
            stories._parse_claude_message(_message_with_text('{"broken": ,}'))


class CallClaudeTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(stories, 'ANTHROPIC_API_KEY', 'test-key')
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep_patcher = mock.patch.object(stories.time, 'sleep')
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def test_returns_none_without_api_key(self):
        with mock.patch.object(stories, 'ANTHROPIC_API_KEY', ''):
            self.assertIsNone(stories.call_claude('hi'))

    def test_success_parses_json(self):
        payload = _message_with_text('{"matches": {"h": ["slug"]}}')
        with mock.patch.object(stories.urllib.request, 'urlopen',
                               return_value=_fake_urlopen_response(payload)) as m:
            result = stories.call_claude('prompt', max_tokens=999)
        self.assertEqual(result, {'matches': {'h': ['slug']}})
        req = m.call_args[0][0]
        sent = json.loads(req.data.decode())
        self.assertEqual(sent['model'], 'claude-sonnet-5')
        self.assertEqual(sent['thinking'], {'type': 'disabled'})
        self.assertEqual(sent['max_tokens'], 999)

    def test_no_json_in_response_returns_none_without_retry(self):
        payload = _message_with_text('plain prose, no braces')
        with mock.patch.object(stories.urllib.request, 'urlopen',
                               return_value=_fake_urlopen_response(payload)) as m:
            with redirect_stdout(io.StringIO()):
                self.assertIsNone(stories.call_claude('prompt'))
        self.assertEqual(m.call_count, 1)

    def test_retries_transient_http_error_then_succeeds(self):
        err = urllib.error.HTTPError('url', 529, 'overloaded', {}, None)
        payload = _message_with_text('{"ok": 1}')
        with mock.patch.object(
                stories.urllib.request, 'urlopen',
                side_effect=[err, _fake_urlopen_response(payload)]) as m:
            with redirect_stdout(io.StringIO()):
                result = stories.call_claude('prompt')
        self.assertEqual(result, {'ok': 1})
        self.assertEqual(m.call_count, 2)

    def test_non_transient_http_error_returns_none_immediately(self):
        err = urllib.error.HTTPError('url', 400, 'bad request', {}, None)
        with mock.patch.object(stories.urllib.request, 'urlopen',
                               side_effect=err) as m:
            with redirect_stdout(io.StringIO()):
                self.assertIsNone(stories.call_claude('prompt'))
        self.assertEqual(m.call_count, 1)

    def test_exhausts_retries_and_returns_none(self):
        err = urllib.error.HTTPError('url', 500, 'server error', {}, None)
        with mock.patch.object(stories.urllib.request, 'urlopen',
                               side_effect=err) as m:
            with redirect_stdout(io.StringIO()):
                self.assertIsNone(stories.call_claude('prompt'))
        self.assertEqual(m.call_count, stories.MAX_API_RETRIES)


def _candidates(n=3):
    return [
        {
            'headline': f'Headline {i}',
            'voices': {f'voice-{i}': {'voiceName': f'Voice {i}', 'quote': 'q'}},
        }
        for i in range(n)
    ]


def _batch_result_line(idx, result_type='succeeded', text='{"ok": true}'):
    entry = {'custom_id': f'analyze-{idx}', 'result': {'type': result_type}}
    if result_type == 'succeeded':
        entry['result']['message'] = _message_with_text(text)
    return json.dumps(entry)


class BatchAnalyzeVoicesTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(stories, 'ANTHROPIC_API_KEY', 'test-key')
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep_patcher = mock.patch.object(stories.time, 'sleep')
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)
        poll_patcher = mock.patch.object(stories, 'BATCH_POLL_SECONDS', 0)
        poll_patcher.start()
        self.addCleanup(poll_patcher.stop)

    def test_no_api_key_returns_empty(self):
        with mock.patch.object(stories, 'ANTHROPIC_API_KEY', ''):
            self.assertEqual(stories.batch_analyze_voices(_candidates(), {}), {})

    def test_single_candidate_skips_batch(self):
        with mock.patch.object(stories, '_batch_api') as m:
            self.assertEqual(stories.batch_analyze_voices(_candidates(1), {}), {})
        m.assert_not_called()

    def test_happy_path_keys_unordered_results_by_custom_id(self):
        create = json.dumps({'id': 'batch_1', 'processing_status': 'in_progress'})
        ended = json.dumps({'processing_status': 'ended',
                            'results_url': 'https://api.anthropic.com/results/1'})
        # Deliberately out of order — must key by custom_id, not position.
        results_jsonl = '\n'.join([
            _batch_result_line(2, text='{"headline": "two"}'),
            _batch_result_line(0, text='{"headline": "zero"}'),
            _batch_result_line(1, text='{"headline": "one"}'),
        ])
        with mock.patch.object(stories, '_batch_api',
                               side_effect=[create, ended, results_jsonl]) as m:
            with redirect_stdout(io.StringIO()):
                out = stories.batch_analyze_voices(_candidates(3), {})
        self.assertEqual(out, {0: {'headline': 'zero'},
                               1: {'headline': 'one'},
                               2: {'headline': 'two'}})
        # Create call sent one request per candidate with the right shape.
        create_payload = m.call_args_list[0].kwargs['payload']
        self.assertEqual(len(create_payload['requests']), 3)
        first = create_payload['requests'][0]
        self.assertEqual(first['custom_id'], 'analyze-0')
        self.assertEqual(first['params']['model'], 'claude-sonnet-5')
        self.assertEqual(first['params']['thinking'], {'type': 'disabled'})
        self.assertEqual(first['params']['max_tokens'], stories.ANALYZE_MAX_TOKENS)

    def test_errored_and_malformed_entries_are_omitted(self):
        create = json.dumps({'id': 'batch_1'})
        ended = json.dumps({'processing_status': 'ended',
                            'results_url': 'https://api.anthropic.com/results/1'})
        results_jsonl = '\n'.join([
            _batch_result_line(0, text='{"headline": "zero"}'),
            _batch_result_line(1, result_type='errored'),
            _batch_result_line(2, text='not json at all'),  # no JSON blob
            'garbage-line',
        ])
        with mock.patch.object(stories, '_batch_api',
                               side_effect=[create, ended, results_jsonl]):
            with redirect_stdout(io.StringIO()):
                out = stories.batch_analyze_voices(_candidates(3), {})
        # Only index 0 succeeded; 1 and 2 fall back to sequential calls.
        self.assertEqual(out, {0: {'headline': 'zero'}})

    def test_create_failure_returns_empty(self):
        with mock.patch.object(stories, '_batch_api',
                               side_effect=Exception('boom')):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(stories.batch_analyze_voices(_candidates(), {}), {})

    def test_timeout_cancels_and_returns_empty(self):
        create = json.dumps({'id': 'batch_1'})
        canceling = json.dumps({'processing_status': 'canceling'})
        # Deadline already passed -> poll loop never runs -> cancel + {}.
        with mock.patch.object(stories, 'BATCH_TIMEOUT_SECONDS', -1):
            with mock.patch.object(stories, '_batch_api',
                                   side_effect=[create, canceling]) as m:
                with redirect_stdout(io.StringIO()):
                    out = stories.batch_analyze_voices(_candidates(3), {})
        self.assertEqual(out, {})
        cancel_call = m.call_args_list[-1]
        self.assertTrue(cancel_call.args[0].endswith('/batch_1/cancel'))
        self.assertEqual(cancel_call.kwargs.get('method'), 'POST')

    def test_poll_error_retries_until_ended(self):
        create = json.dumps({'id': 'batch_1'})
        ended = json.dumps({'processing_status': 'ended',
                            'results_url': 'https://api.anthropic.com/results/1'})
        results_jsonl = _batch_result_line(0, text='{"a": 1}')
        with mock.patch.object(
                stories, '_batch_api',
                side_effect=[create, Exception('transient'), ended, results_jsonl]):
            with redirect_stdout(io.StringIO()):
                out = stories.batch_analyze_voices(_candidates(2), {})
        self.assertEqual(out, {0: {'a': 1}})

    def test_ended_without_results_url_returns_empty(self):
        create = json.dumps({'id': 'batch_1'})
        ended = json.dumps({'processing_status': 'ended'})
        with mock.patch.object(stories, '_batch_api', side_effect=[create, ended]):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(stories.batch_analyze_voices(_candidates(2), {}), {})


class AnalyzeVoicesTests(unittest.TestCase):
    def test_sequential_call_uses_shared_prompt_and_budget(self):
        voices = {'v-1': {'voiceName': 'V One', 'quote': 'a take', 'platform': 'x'}}
        with mock.patch.object(stories, 'call_claude',
                               return_value={'clusters': {}}) as m:
            out = stories.analyze_voices('Headline', voices, {})
        self.assertEqual(out, {'clusters': {}})
        prompt = m.call_args.args[0]
        self.assertEqual(prompt,
                         stories.build_analysis_prompt('Headline', voices, {}))
        self.assertIn('Headline', prompt)
        self.assertEqual(m.call_args.kwargs['max_tokens'],
                         stories.ANALYZE_MAX_TOKENS)


class TangentialClusterTests(unittest.TestCase):
    def test_catch_all_bucket_names_are_dropped(self):
        for name in ['Unrelated Commentary', 'Tangential', 'Off-Topic Reactions',
                     'Off Topic', 'No Clear Position', 'No Position',
                     'Miscellaneous', 'Not Related']:
            self.assertTrue(stories.is_tangential_cluster(name),
                            f"{name!r} should be treated as off-topic")

    def test_real_positions_are_kept(self):
        for name in ['Structural/Policy Solutions', 'Affordability Crisis Persists',
                     'Celebrating the Good Numbers', 'Warning Relief Is Temporary',
                     'Media Criticism', 'Deterrence Advocates', 'Pro-Intervention Hawk',
                     'Position of Strength', 'Common Position', 'Opposition Voices',
                     'Now Positioned']:
            self.assertFalse(stories.is_tangential_cluster(name),
                             f"{name!r} is a real stance and must be kept")

    def test_empty_and_none_are_not_tangential(self):
        self.assertFalse(stories.is_tangential_cluster(''))
        self.assertFalse(stories.is_tangential_cluster(None))


if __name__ == '__main__':
    unittest.main()
