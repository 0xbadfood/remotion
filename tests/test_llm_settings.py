import fcntl
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from infofilm.core import Store, digest
from infofilm.llm import Models, llm_slot
from infofilm.llm_settings import Registry, endpoint, llm_config

CONFIG = {'planner_url': 'http://10.8.0.5:8001/v1', 'reviewer_url': 'http://10.8.0.4:8001/v1'}
HAIKU = {'type': 'openrouter', 'label': 'Claude Haiku 5.5 (OpenRouter)', 'url': 'https://openrouter.ai/api/v1',
         'model': 'anthropic/claude-haiku-5.5', 'vision': True, 'api_key': 'sk-or-secret'}
LUNA = {'type': 'openai_responses', 'label': 'Luna (OpenAI)', 'url': 'https://api.openai.com/v1', 'model': 'gpt-6-luna',
        'vision': True, 'api_key': 'sk-openai-secret'}


def reply(content, status=200):
    return httpx.Response(status, json={'choices': [{'finish_reason': 'stop', 'message': {'content': content}}],
                                        'usage': {'prompt_tokens': 10, 'completion_tokens': 2}},
                          request=httpx.Request('POST', 'https://example.org'))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name); self.registry = Registry(self.root, dict(CONFIG))

    def test_first_run_offers_the_configured_local_endpoints(self):
        data = self.registry.public()
        self.assertEqual(sorted(c['url'] for c in data['connections'].values()), sorted(CONFIG.values()))
        self.assertEqual(set(data['assignments']), {'planner', 'reviewer'})

    def test_keys_are_stored_privately_and_never_returned(self):
        cid = self.registry.upsert(HAIKU)
        public = json.dumps(self.registry.public())
        self.assertNotIn('sk-or-secret', public); self.assertTrue(self.registry.public()['connections'][cid]['key_set'])
        self.assertNotIn('sk-or-secret', (self.root / 'llm-settings.json').read_text())
        self.assertEqual(stat.S_IMODE(os.stat(self.root / 'llm-secrets.json').st_mode), 0o600)
        self.registry.upsert({**HAIKU, 'id': cid, 'api_key': None, 'label': 'Haiku'})
        self.assertTrue(self.registry.public()['connections'][cid]['key_set'])     # an empty key keeps the saved one

    def test_reviewer_must_accept_images_and_in_use_connections_cannot_be_deleted(self):
        text_only = self.registry.upsert({**HAIKU, 'label': 'Text only', 'vision': False})
        planner = self.registry.upsert(HAIKU)
        with self.assertRaisesRegex(ValueError, 'images'):
            self.registry.assign({'planner': planner, 'reviewer': text_only})
        self.registry.assign({'planner': text_only})
        with self.assertRaisesRegex(ValueError, 'In use'):
            self.registry.delete(text_only)

    def test_configurations_save_apply_and_freeze_a_run_without_secrets(self):
        haiku, luna = self.registry.upsert(HAIKU), self.registry.upsert(LUNA)
        self.registry.assign({'planner': haiku, 'reviewer': luna})
        pid = self.registry.save_configuration('Haiku + Luna')
        self.registry.assign({'reviewer': haiku})
        self.assertIsNone(self.registry.public()['active_configuration'])
        config = self.registry.freeze(dict(CONFIG), pid)
        self.assertEqual(config['planner_url'], 'https://openrouter.ai/api/v1#' + haiku)
        self.assertEqual(endpoint(config, 'reviewer')['model'], 'gpt-6-luna')
        self.assertEqual(config['llm_configuration']['label'], 'Haiku + Luna')
        self.assertNotIn('sk-or-secret', json.dumps(config)); self.assertNotIn('sk-openai-secret', json.dumps(config))
        self.assertEqual(set(llm_config(config)), {'planner_url', 'reviewer_url', 'llm_endpoints', 'llm_secrets_root'})
        self.registry.apply_configuration(pid)
        self.assertEqual(self.registry.public()['active_configuration'], pid)

    def test_saved_configuration_without_premium_image_keeps_the_current_choice(self):
        haiku, luna = self.registry.upsert(HAIKU), self.registry.upsert(LUNA)
        gemini = self.registry.upsert({**HAIKU, 'label': 'Gemini image', 'model': 'google/gemini-3.1-flash-image'})
        self.registry.assign({'planner': haiku, 'reviewer': luna})
        pid = self.registry.save_configuration('Haiku + Luna')               # saved before a premium model was chosen
        self.registry.assign({'premium_image': gemini})
        config = self.registry.freeze(dict(CONFIG), pid)
        self.assertEqual(config['premium_image_endpoint']['model'], 'google/gemini-3.1-flash-image')
        self.assertEqual(config['llm_configuration']['id'], pid)

    def test_same_base_url_for_both_roles_keeps_distinct_identities(self):
        haiku = self.registry.upsert(HAIKU); other = self.registry.upsert({**HAIKU, 'label': 'Gemma', 'model': 'google/gemma'})
        self.registry.assign({'planner': haiku, 'reviewer': other})
        config = self.registry.freeze(dict(CONFIG))
        self.assertNotEqual(config['planner_url'], config['reviewer_url'])


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name); registry = Registry(self.root / 'interface', dict(CONFIG))
        haiku, luna = registry.upsert(HAIKU), registry.upsert(LUNA)
        registry.assign({'planner': haiku, 'reviewer': luna})
        self.config = registry.freeze(dict(CONFIG))
        self.store = Store(self.root / 'project'); self.addCleanup(self.store.db.close)

    def client(self, *responses):
        client = Mock(); client.post.side_effect = list(responses)
        constructor = patch('infofilm.llm.httpx.Client'); mocked = constructor.start(); self.addCleanup(constructor.stop)
        mocked.return_value.__enter__.return_value = client
        return mocked, client

    def test_openrouter_uses_saved_key_and_model_without_local_reasoning_switch(self):
        constructor, client = self.client(reply('{"value": 1}'))
        self.assertEqual(Models(self.config, self.store).call('planner', 'draft', 'Prompt'), {'value': 1})
        self.assertEqual(constructor.call_args.kwargs['headers'], {'Authorization': 'Bearer sk-or-secret'})
        url, body = client.post.call_args.args[0], client.post.call_args.kwargs['json']
        self.assertEqual(url, 'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(body['model'], 'anthropic/claude-haiku-5.5'); self.assertNotIn('chat_template_kwargs', body)
        self.assertEqual(body['reasoning'], {'effort': 'low'}); self.assertEqual(body['max_tokens'], 7000 + 8000)
        client.get.assert_not_called()

    def test_responses_api_reviewer_parses_output_text_and_normalises_usage(self):
        raw = {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"pass": true}'}]}],
               'usage': {'input_tokens': 50, 'output_tokens': 5}}
        _, client = self.client(httpx.Response(200, json=raw, request=httpx.Request('POST', 'https://api.openai.com')))
        self.assertEqual(Models(self.config, self.store).call('reviewer', 'review', 'Prompt'), {'pass': True})
        self.assertEqual(client.post.call_args.args[0], 'https://api.openai.com/v1/responses')
        self.assertIn('JSON', client.post.call_args.kwargs['json']['input'][0]['content'][0]['text'])
        saved = json.loads(next((self.root / 'project' / 'requests').glob('review-*.response.json')).read_text())
        self.assertEqual(saved['usage']['prompt_tokens'], 50)

    def test_outages_and_rate_limits_wait_and_retry_without_counting_as_attempts(self):
        busy = httpx.Response(429, headers={'retry-after': '2'}, request=httpx.Request('POST', 'https://openrouter.ai'))
        _, client = self.client(httpx.ConnectError('refused'), busy, reply('{"value": 2}'))
        with patch('infofilm.llm.time.sleep') as sleep:
            self.assertEqual(Models(self.config, self.store).call('planner', 'draft', 'Prompt'), {'value': 2})
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 2.0])
        retries = self.store.db.execute("SELECT COUNT(*) FROM events WHERE state='transient_retry'").fetchone()[0]
        self.assertEqual(retries, 2)

    def test_cloudflare_edge_error_on_responses_api_is_retried(self):
        # The first History Studio film died on a single OpenAI 520 (Cloudflare) during image-prompt review.
        edge = httpx.Response(520, request=httpx.Request('POST', 'https://api.openai.com'))
        raw = {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"pass": true}'}]}],
               'usage': {'input_tokens': 5, 'output_tokens': 1}}
        _, client = self.client(edge, httpx.Response(200, json=raw, request=httpx.Request('POST', 'https://api.openai.com')))
        with patch('infofilm.llm.time.sleep'):
            self.assertEqual(Models(self.config, self.store).call('reviewer', 'review', 'Prompt'), {'pass': True})
        self.assertEqual(client.post.call_count, 2)

    def test_missing_cloud_key_fails_clearly(self):
        config = dict(self.config); config['llm_secrets_root'] = str(self.root / 'empty')
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(RuntimeError, 'needs an API key'):
            Models(config, self.store).call('planner', 'draft', 'Prompt')

    def test_concurrency_slots_are_shared_across_processes(self):
        connection = {'url': 'https://example.org/v1', 'model': 'm-' + self.folder.name, 'max_concurrency': 1}
        with llm_slot(connection):
            folder = Path(tempfile.gettempdir()) / 'infofilm-llm-slots' / digest({'url': connection['url'], 'model': connection['model']})[:16]
            handle = (folder / 'slot-0').open('a')
            with self.assertRaises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.close()


if __name__ == '__main__':
    unittest.main()
