import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from infofilm.core import Store, configuration, read
from infofilm.image_direct import generate_unreviewed_images
from infofilm.image_recovery import render_take
from infofilm.media import generate_images


class DirectImageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(self.folder.name)
        self.addCleanup(self.store.db.close)
        self.config = {**configuration(), 'image_review': False}
        asset = {'id': 'A001', 'prompt': 'A clerk holds a closed ledger.',
                 'visual_message': 'A new administration collects revenue.',
                 'setting': 'Eighteenth-century Bengal.', 'visual_style': 'Cinematic illustration.'}
        self.library = {'assets': [asset, {**asset, 'id': 'A002'}], 'style': 'Film style',
                        'orientations': ['landscape']}

    def candidate(self, config, store, root, params, label):
        root.mkdir(parents=True, exist_ok=True)
        path = root / 'candidate-0.png'
        # Meaningful variation ensures technical checks are exercised separately.
        image = Image.new('RGB', (int(params['width']), int(params['height'])), 'navy')
        image.putpixel((0, 0), (255, 255, 255))
        image.save(path)
        return path

    def test_direct_mode_generates_once_without_model_calls_or_escalation(self):
        models = Mock()
        with patch('infofilm.image_direct.provider_session', return_value=contextlib.nullcontext()), \
             patch('infofilm.image_direct.render_take', side_effect=self.candidate) as render, \
             patch('infofilm.image_terminal.recover') as premium, \
             patch('infofilm.image_recovery.current_image_review') as review:
            output = generate_images(self.config, self.store, self.library, models)
        self.assertEqual(render.call_count, 2)
        models.call.assert_not_called()
        premium.assert_not_called()
        review.assert_not_called()
        for value in output.values():
            self.assertEqual(value['provider_id'], 'qwen_local')
            self.assertEqual(value['selection_status'], 'unreviewed')
            self.assertIs(value['review_performed'], False)
            self.assertIsNone(value['review']['pass'])
            self.assertNotIn('needs_review', value)
            self.assertEqual(value['total_attempts'], 1)
        self.assertEqual(read(self.store.root / 'image-direct-report.json')['reviewed_images'], 0)

    def test_resume_reuses_direct_images_without_gpu_and_detects_changed_files(self):
        with patch('infofilm.image_direct.provider_session', return_value=contextlib.nullcontext()), \
             patch('infofilm.image_direct.render_take', side_effect=self.candidate):
            output = generate_unreviewed_images(self.config, self.store, self.library)
        with patch('infofilm.image_direct.provider_session') as session, \
             patch('infofilm.image_direct.render_take') as render:
            self.assertEqual(generate_unreviewed_images(self.config, self.store, self.library), output)
            session.assert_not_called()
            render.assert_not_called()
            Path(output['A001']['file']).write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'checkpoint changed'):
                generate_unreviewed_images(self.config, self.store, self.library)
            session.assert_not_called()

    def test_real_renderer_still_rejects_wrong_dimensions_and_blank_files(self):
        params = {'width': '20', 'height': '10'}
        root = self.store.root / 'take'
        root.mkdir()
        image = Image.new('RGB', (10, 10), 'navy')
        image.putpixel((0, 0), (255, 255, 255))
        image.save(root / 'candidate-0.png')
        with self.assertRaisesRegex(ValueError, 'expected requested'):
            render_take({}, self.store, root, params, 'A001')
        Image.new('RGB', (20, 10), 'navy').save(root / 'candidate-0.png')
        with self.assertRaisesRegex(RuntimeError, 'unusable output'):
            render_take({}, self.store, root, params, 'A001')

    def test_invalid_review_setting_is_rejected(self):
        for value in ['off', 0, None]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'boolean'):
                generate_images({**self.config, 'image_review': value}, self.store, self.library, Mock())
