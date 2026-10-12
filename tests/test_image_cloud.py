import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from PIL import Image

from infofilm.core import Store, read
from infofilm.image_cloud import aspect_ratio, render_cloud
from infofilm.image_providers import provider_config
from infofilm.llm_settings import Registry

ENDPOINT = {'id': 'gemini', 'type': 'openrouter', 'label': 'Gemini', 'url': 'https://openrouter.ai/api/v1',
            'model': 'google/gemini-3.1-flash-image', 'key_env': 'OPENROUTER_API_KEY'}


def png(size=(1376, 768)):
    buffer = io.BytesIO(); Image.new('RGB', size, '#336699').save(buffer, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()


class CloudImageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name); self.store = Store(self.root / 'store'); self.addCleanup(self.store.db.close)
        self.config = {'premium_image_endpoint': ENDPOINT, 'llm_secrets_root': str(self.root)}
        self.params = {'prompt': 'A night scene, no text.', 'width': 1920, 'height': 1080, 'steps': 1, 'seed': 1}

    def client(self, *responses):
        constructor = patch('infofilm.image_cloud.httpx.Client'); mocked = constructor.start(); self.addCleanup(constructor.stop)
        client = Mock(); client.post.side_effect = list(responses); mocked.return_value.__enter__.return_value = client
        return client

    def reply(self, status=200, url=None):
        body = {'choices': [{'message': {'images': [{'image_url': {'url': url or png()}}]}}], 'usage': {'cost': 0.07}}
        return httpx.Response(status, json=body, request=httpx.Request('POST', 'https://openrouter.ai'))

    def test_renders_to_the_exact_frame_and_reuses_the_saved_result(self):
        client = self.client(httpx.Response(429, request=httpx.Request('POST', 'https://x')), self.reply())
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'sk-test'}), patch('infofilm.image_cloud.time.sleep'):
            result = render_cloud(self.config, self.store, self.root / 'take', 0, self.params)
            again = render_cloud(self.config, self.store, self.root / 'take', 0, self.params)
        body = client.post.call_args.kwargs['json']
        self.assertEqual((body['modalities'], body['image_config']), (['image', 'text'], {'aspect_ratio': '16:9'}))
        with Image.open(self.root / 'take/candidate-0.png') as image:
            self.assertEqual(image.size, (1920, 1080))
        self.assertEqual(result, again); self.assertEqual(client.post.call_count, 2)    # one 429 retry, no second render
        self.assertEqual(result['usage']['cost'], 0.07)

    def test_unconfirmed_submission_is_not_blindly_repeated(self):
        take = self.root / 'take'; take.mkdir()
        record = {'model': ENDPOINT['model'], 'connection': 'gemini', 'prompt': self.params['prompt'], 'aspect_ratio': '16:9',
                  'reference_sha256': []}
        (take / 'provider-0.json').write_text(json.dumps({'state': 'submission_started', 'request': record}))
        with self.assertRaisesRegex(RuntimeError, 'may have been billed'):
            render_cloud(self.config, self.store, take, 0, self.params)

    def test_missing_model_and_ratio_choice(self):
        with self.assertRaisesRegex(RuntimeError, 'Premium images'):
            render_cloud({}, self.store, self.root / 'x', 0, self.params)
        self.assertEqual((aspect_ratio(1080, 1920), aspect_ratio(1000, 1000)), ('9:16', '1:1'))

    def test_registry_tier_and_settings_role(self):
        self.assertEqual(provider_config({'image_url': 'http://q'}, 'cloud_premium',
            {'adapter': 'openrouter_image', 'url': 'https://openrouter.ai/api/v1', 'settings': {'steps': 1}})['image_engine'], 'openrouter')
        registry = Registry(self.root / 'interface', {'planner_url': 'http://p/v1', 'reviewer_url': 'http://r/v1'})
        local = registry.load()['roles']['planner']
        with self.assertRaisesRegex(ValueError, 'openrouter'):
            registry.assign({'premium_image': local})
        cid = registry.upsert({**ENDPOINT, 'api_key': 'sk-x'})
        registry.assign({'premium_image': cid})
        config = registry.freeze({'planner_url': 'http://p/v1', 'reviewer_url': 'http://r/v1'})
        self.assertEqual(config['premium_image_endpoint']['model'], ENDPOINT['model'])
        self.assertNotIn('premium_image_url', config)
        registry.assign({'planner': local})                       # premium role kept when not sent
        self.assertEqual(registry.load()['roles']['premium_image'], cid)
        registry.assign({'premium_image': ''})                    # cleared when sent empty
        self.assertNotIn('premium_image', registry.load()['roles'])


if __name__ == '__main__':
    unittest.main()
