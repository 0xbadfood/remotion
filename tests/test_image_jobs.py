import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from infofilm.core import read, write
from infofilm.image_jobs import wait_qwen_job


def failure(code=404, path='/api/jobs/lost', method='GET'):
    request = httpx.Request(method, 'http://images' + path)
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError('Provider error', request=request, response=response)


class MissingImageJobTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.provider = Path(self.folder.name) / 'provider-0.json'
        self.params = {'prompt': 'Original approved prompt', 'seed': '123', 'width': '1536', 'steps': '25'}
        write(self.provider, {'job_id': 'lost', 'state': 'submitted', 'params': self.params})
        self.store = Mock()
        self.request = Mock(side_effect=[{'state': 'loaded'}, {'id': 'replacement'}])

    def run_wait(self, wait, config=None):
        return wait_qwen_job(config or {}, self.store, self.provider, 'A001',
                             request_fn=self.request, wait_fn=wait)

    def test_missing_job_resubmits_same_settings_and_records_new_id(self):
        wait = Mock(side_effect=[failure(), {'state': 'complete', 'url': '/image.png'}])
        result = self.run_wait(wait)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(wait.call_args_list[1].args[2], '/api/jobs/replacement')
        self.assertEqual(self.request.call_args.args[2:], ('POST', '/api/generate'))
        self.assertEqual(self.request.call_args.kwargs['data'], self.params)
        record = read(self.provider)
        self.assertEqual(record['job_id'], 'replacement')
        self.assertEqual(record['missing_job_history'][0]['job_id'], 'lost')
        self.assertEqual(record['params'], self.params)

    def test_unloaded_service_reloads_before_resubmission(self):
        self.request.side_effect = [{'state': 'unloaded'}, {'state': 'loading'},
                                    {'state': 'loading'}, {'state': 'loaded'}, {'id': 'replacement'}]
        with patch('infofilm.image_jobs.time.sleep'):
            self.run_wait(Mock(side_effect=[failure(), {'state': 'complete'}]))
        paths = [c.args[2:4] for c in self.request.call_args_list]
        self.assertEqual(paths, [('GET', '/api/status'), ('POST', '/api/load'),
                                 ('GET', '/api/status'), ('GET', '/api/status'), ('POST', '/api/generate')])

    def test_saved_budget_survives_resume(self):
        self.run_wait(Mock(side_effect=[failure(), {'state': 'complete'}]), {'image_missing_job_retries': 1})
        self.request.reset_mock()
        with self.assertRaisesRegex(RuntimeError, 'after 1 resubmissions'):
            self.run_wait(Mock(side_effect=failure(path='/api/jobs/replacement')), {'image_missing_job_retries': 1})
        self.request.assert_not_called()

    def test_other_errors_do_not_resubmit(self):
        for error in [failure(500), failure(401), failure(path='/assets/lost.png'),
                      failure(method='POST'), httpx.ReadTimeout('Timeout')]:
            with self.subTest(error=error):
                self.request.reset_mock()
                with self.assertRaises(type(error)):
                    self.run_wait(Mock(side_effect=error))
                self.request.assert_not_called()
                self.assertEqual(read(self.provider)['job_id'], 'lost')

    def test_ambiguous_resubmit_cannot_repeat_post(self):
        self.request.side_effect = [{'state': 'loaded'}, httpx.ReadTimeout('Submission timeout')]
        with self.assertRaises(httpx.ReadTimeout):
            self.run_wait(Mock(side_effect=failure()))
        record = read(self.provider)
        self.assertEqual(record['state'], 'resubmission_started')
        self.assertNotIn('job_id', record)
        self.request.reset_mock()
        with self.assertRaisesRegex(RuntimeError, 'Unknown Qwen submission'):
            self.run_wait(Mock())
        self.request.assert_not_called()

    def test_unhealthy_model_blocks_resubmission(self):
        self.request.side_effect = [{'state': 'error'}]
        with self.assertRaisesRegex(RuntimeError, 'not ready'):
            self.run_wait(Mock(side_effect=failure()))
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(read(self.provider)['job_id'], 'lost')

    def test_invalid_budget_and_zero_are_bounded(self):
        for maximum in [True, -1, 6, '2']:
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                self.run_wait(Mock(), {'image_missing_job_retries': maximum})
        with self.assertRaisesRegex(RuntimeError, 'after 0 resubmissions'):
            self.run_wait(Mock(side_effect=failure()), {'image_missing_job_retries': 0})
        self.request.assert_not_called()
