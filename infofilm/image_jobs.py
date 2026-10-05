"""Recover confirmed missing Qwen job IDs without repeating ambiguous submissions."""
from __future__ import annotations

import time
import httpx

from .core import read, write


def wait_qwen_job(config, store, provider, label, *, request_fn=None, wait_fn=None):
    from .media import request, wait_job
    request_fn = request_fn or request
    wait_fn = wait_fn or wait_job
    maximum = config.get('image_missing_job_retries', 2)
    if type(maximum) is not int or not 0 <= maximum <= 5:
        raise ValueError('image_missing_job_retries must be an integer between 0 and 5')
    while True:
        record = read(provider)
        job_id = record.get('job_id')
        if not job_id:
            raise RuntimeError(f'Unknown Qwen submission outcome: {provider}; reconcile before resubmitting')
        try:
            return wait_fn(config, 'image', '/api/jobs/' + job_id, {'complete'}, {'error'}, store, label)
        except httpx.HTTPStatusError as exc:
            # Only a confirmed GET of this job may authorize replacement. A
            # failed submit, missing asset download or generic 5xx cannot.
            if (exc.response.status_code != 404 or exc.request.method != 'GET'
                    or exc.request.url.path != '/api/jobs/' + job_id):
                raise
            history = list(record.get('missing_job_history', []))
            store.event(label, 'job_missing', {'job_id': job_id, 'resubmissions': len(history),
                                              'max_resubmissions': maximum})
            if len(history) >= maximum:
                raise RuntimeError(f'Qwen job remains missing after {maximum} resubmissions; saved at {provider}') from exc
            # Confirm the API is back and restore its model if the restarted
            # process lost it. This runs inside the caller's GPU lease.
            status = request_fn(config, 'image', 'GET', '/api/status')
            if status.get('state') == 'unloaded':
                request_fn(config, 'image', 'POST', '/api/load')
            deadline = time.monotonic() + 900
            last_notice = -30
            while status.get('state') != 'loaded':
                if status.get('state') not in {'unloaded', 'loading'}:
                    raise RuntimeError(f'Qwen is not ready to recover the missing job: {status}')
                if time.monotonic() >= deadline:
                    raise TimeoutError('Qwen model did not become ready for missing-job recovery')
                if time.monotonic() - last_notice >= 25:
                    store.event(label, 'recovery_waiting', {'job_id': job_id, 'state': status.get('state')})
                    last_notice = time.monotonic()
                time.sleep(2)
                status = request_fn(config, 'image', 'GET', '/api/status')
            history.append({'job_id': job_id, 'missing_at': time.time(), 'status': 404})
            # Persist intent before POST. A crash here remains ambiguous and
            # cannot authorize another POST, even though the old ID was missing.
            pending = {**record, 'state': 'resubmission_started', 'missing_job_history': history}
            pending.pop('job_id')
            write(provider, pending)
            from .person_references import submit_qwen_job
            result = submit_qwen_job(config, record['params'], request_fn)
            new_id = result.get('id')
            if not isinstance(new_id, str) or not new_id or new_id == job_id:
                raise RuntimeError('Replacement Qwen submission has no new job ID; reconcile its saved intent')
            write(provider, {**pending, 'state': 'submitted', 'job_id': new_id})
            store.event(label, 'job_resubmitted', {'missing_job_id': job_id, 'job_id': new_id,
                    'resubmission': len(history), 'max_resubmissions': maximum,
                    'prompt_changed': False, 'seed': record['params'].get('seed')})
