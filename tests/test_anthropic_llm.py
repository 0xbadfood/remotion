import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import anthropic
import httpx

from infofilm.anthropic_llm import LLMBillingError, agent_turn, _messages
from infofilm.core import Store
from infofilm.llm import Models
from infofilm.llm_settings import Registry

CONFIG = {'planner_url': 'http://10.8.0.5:8001/v1', 'reviewer_url': 'http://10.8.0.4:8001/v1'}
CLAUDE = {'type': 'anthropic', 'label': 'Claude Haiku 5.5', 'url': 'https://api.anthropic.com', 'model': 'claude-haiku-5-5',
          'vision': True, 'api_key': 'sk-ant-secret'}


def message(content, stop='end_turn'):
    return anthropic.types.Message.model_validate({
        'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': 'claude-haiku-5-5', 'content': content,
        'stop_reason': stop, 'stop_sequence': None, 'usage': {'input_tokens': 12, 'output_tokens': 3}})


def fake_client(*replies):
    cli = MagicMock()
    streams = []
    for r in replies:
        s = MagicMock()
        if isinstance(r, Exception):
            s.__enter__.side_effect = r
        else:
            s.__enter__.return_value.get_final_message.return_value = r
        streams.append(s)
    cli.messages.stream.side_effect = streams
    return cli


class ModelsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name); registry = Registry(root / 'interface', dict(CONFIG))
        registry.assign({'planner': registry.upsert(CLAUDE)})
        self.config = registry.freeze(dict(CONFIG))
        self.store = Store(root / 'project'); self.addCleanup(self.store.db.close)

    def run_with(self, *replies):
        cli = fake_client(*replies)
        with patch('infofilm.anthropic_llm.client', return_value=cli) as made:
            value = Models(self.config, self.store).call('planner', 'draft', 'Prompt')
        return value, cli, made

    def test_planner_calls_claude_natively_with_effort_and_thinking_room(self):
        value, cli, made = self.run_with(message([{'type': 'text', 'text': '```json\n{"value": 1}\n```'}]))
        self.assertEqual(value, {'value': 1})
        self.assertEqual(made.call_args.args[0], 'sk-ant-secret')
        params = cli.messages.stream.call_args.kwargs
        self.assertEqual(params['model'], 'claude-haiku-5-5'); self.assertEqual(params['max_tokens'], 7000 + 8000)
        self.assertEqual(params['output_config'], {'effort': 'low'})
        for banned in ('temperature', 'thinking', 'response_format'):
            self.assertNotIn(banned, params)

    def test_output_limit_is_escalated_like_other_connections(self):
        value, cli, _ = self.run_with(message([{'type': 'text', 'text': '{"va'}], stop='max_tokens'),
                                      message([{'type': 'text', 'text': '{"value": 2}'}]))
        self.assertEqual(value, {'value': 2})
        self.assertEqual(cli.messages.stream.call_args.kwargs['max_tokens'], round(7000 * 1.3) + 8000)

    def test_low_credit_balance_stops_the_job(self):
        error = anthropic.BadRequestError('Your credit balance is too low to access the Anthropic API.',
                                          response=httpx.Response(400, request=httpx.Request('POST', 'https://api.anthropic.com')),
                                          body=None)
        with self.assertRaises(LLMBillingError):
            self.run_with(error)

    def test_refusal_is_an_error_not_an_answer(self):
        with self.assertRaisesRegex(RuntimeError, 'declined'):
            self.run_with(message([], stop='refusal'))


class AgentTurnTests(unittest.TestCase):
    def test_chat_shaped_tool_loop_round_trips(self):
        history = [{'role': 'system', 'content': 'Research.'}, {'role': 'user', 'content': 'Find X'},
                   {'role': 'assistant', 'content': '', 'tool_calls': [
                       {'id': 't1', 'type': 'function', 'function': {'name': 'web_search', 'arguments': '{"query": "x"}'}},
                       {'id': 't2', 'type': 'function', 'function': {'name': 'fetch_url', 'arguments': '{"url": "u"}'}}]},
                   {'role': 'tool', 'tool_call_id': 't1', 'content': 'results'},
                   {'role': 'tool', 'tool_call_id': 't2', 'content': 'page'}]
        system, converted = _messages(history)
        self.assertEqual(system, 'Research.')
        self.assertEqual([m['role'] for m in converted], ['user', 'assistant', 'user'])
        self.assertEqual([b['tool_use_id'] for b in converted[2]['content']], ['t1', 't2'])   # one message, both results
        self.assertEqual(converted[1]['content'][0]['input'], {'query': 'x'})
        cli = fake_client(message([{'type': 'text', 'text': 'Searching.'},
                                   {'type': 'tool_use', 'id': 't3', 'name': 'web_search', 'input': {'query': 'y'}}], stop='tool_use'))
        tools = [{'type': 'function', 'function': {'name': 'web_search', 'description': 'Search',
                                                   'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}}}}]
        reply, finish, usage = agent_turn(cli, 'claude-haiku-5-5', history, tools, max_tokens=4000)
        self.assertEqual(finish, 'tool_calls'); self.assertEqual(usage['completion_tokens'], 3)
        self.assertEqual(reply['tool_calls'][0]['function'], {'name': 'web_search', 'arguments': '{"query": "y"}'})
        params = cli.messages.stream.call_args.kwargs
        self.assertEqual(params['tools'][0]['input_schema']['properties'], {'query': {'type': 'string'}})
        self.assertEqual(params['tool_choice'], {'type': 'auto'}); self.assertEqual(params['thinking'], {'type': 'disabled'})


if __name__ == '__main__':
    unittest.main()


class DroppedStreamTests(unittest.TestCase):
    def test_a_stream_cut_off_midway_starts_again(self):
        # 2026-10-29, Stopping Distance: "peer closed connection without sending complete message body" stopped a film
        from infofilm.anthropic_llm import STREAM_TRIES, _final
        message = MagicMock(stop_reason="end_turn")
        good = MagicMock(); good.__enter__.return_value.get_final_message.return_value = message
        cut = MagicMock(); cut.__enter__.return_value.get_final_message.side_effect = httpx.RemoteProtocolError("peer closed")
        cli, waits = MagicMock(), []
        cli.messages.stream.side_effect = [cut, cut, good]
        self.assertIs(_final(cli, "beats", sleep=waits.append, model="m"), message)
        self.assertEqual(waits, [10, 20])
        cli.messages.stream.side_effect = [cut] * STREAM_TRIES
        with self.assertRaises(httpx.RemoteProtocolError):
            _final(cli, "beats", sleep=waits.append, model="m")

    def test_the_tries_sit_out_a_half_hour_outage(self):
        # 2026-10-31, Carnot Engine: a network outage stopped a film after four minutes of tries
        from infofilm.anthropic_llm import LONGEST_WAIT, STREAM_TRIES, _final
        cut = MagicMock(); cut.__enter__.return_value.get_final_message.side_effect = anthropic.APIConnectionError(request=MagicMock())
        cli, waits = MagicMock(), []
        cli.messages.stream.side_effect = [cut] * STREAM_TRIES
        with self.assertRaises(anthropic.APIConnectionError):
            _final(cli, "beats", sleep=waits.append, model="m")
        self.assertGreaterEqual(sum(waits), 1800)
        self.assertEqual(max(waits), LONGEST_WAIT)
