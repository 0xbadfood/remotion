#!/usr/bin/env python3
"""Temporary two-image vLLM test using saved reviews and visual controls.

Run only on an idle local GPU. Restores the temporary server to stopped;
does not change production endpoints, model files, or the serving .env.
"""
from __future__ import annotations

import fcntl
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import httpx
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from infofilm.core import Store, configuration, read, write
from infofilm.educational_review import inventory_errors, normalize_inventory
from infofilm.llm import Models

SERVING = Path('/home/nitin/qwen38-27b-rtx3090')
OUT = ROOT / 'artifacts/experiments/service-switch/vision-two-images'


def gpu_memory():
    return int(subprocess.check_output([
        'nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'
    ], text=True).strip())


def main():
    global OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUT)
    parser.add_argument('--unconstrained-json', action='store_true', help='Request JSON in the prompt without native grammar enforcement')
    parser.add_argument('--spec', choices=['dflash2', 'mtp'], default='dflash2')
    parser.add_argument('--kv-mib', type=int, help='Explicit smaller KV cache for a memory-headroom test')
    parser.add_argument('--max-model-len', type=int, default=32768)
    parser.add_argument('--kind', choices=['control', 'person', 'anatomy'], help='Run only this class of saved tests')
    args = parser.parse_args()
    OUT = args.output.resolve()
    OUT.mkdir(parents=True, exist_ok=True)
    state = {'complete': False, 'rows': [], 'gpu_samples_mib': [],
             'temporary_image_limit': 2, 'production_routing_changed': False,
             'native_json_enforcement': not args.unconstrained_json, 'spec': args.spec}
    if args.kv_mib is not None:
        if args.kv_mib < 1024 or args.max_model_len < 4096:
            parser.error('Use at least 1024 MiB KV and 4096 context tokens')
        state.update(kv_cache_mib=args.kv_mib, max_model_len=args.max_model_len)
    original_client = httpx.Client
    class TestClient(original_client):
        def post(self, url, **kwargs):
            if str(url).endswith('/chat/completions'):
                kwargs['timeout'] = 120
                if args.unconstrained_json:
                    kwargs['json'] = {k:v for k,v in kwargs['json'].items() if k != 'response_format'}
            return super().post(url, **kwargs)
    httpx.Client = TestClient
    def save():
        write(OUT / 'summary.json', state)

    with sqlite3.connect(f'file:{ROOT}/artifacts/interface/queue.sqlite3?mode=ro', uri=True) as db:
        if db.execute("SELECT 1 FROM jobs WHERE status IN ('running','queued')").fetchone():
            raise RuntimeError('A webapp job is active; do not start GPU tests')
    if subprocess.run(['systemctl', '--user', 'is-active', '--quiet', 'qwen-vllm.service']).returncode == 0:
        raise RuntimeError('Reviewer service is already active; leave it undisturbed')
    if gpu_memory() >= 1000:
        raise RuntimeError('Local GPU is occupied; leave it undisturbed')
    for line in (SERVING / '.env').read_text().splitlines():
        if line.startswith('VLLM_API_KEY='):
            os.environ['VLLM_API_KEY'] = line.split('=', 1)[1].strip().strip('\"').strip("'")
        if line.startswith('EXTRA_ARGS=') and line.split('=', 1)[1].strip():
            raise RuntimeError('Existing EXTRA_ARGS needs inspection before override')

    # Compose override changes only this temporary launch; credentials remain
    # in the existing private env file, never copied into artifacts.
    # Snap Docker has a private /tmp mount, so keep its input in the workspace.
    override = OUT / 'compose-override.json'
    environment = {
        'VISION': '1', 'VISION_OFFLOAD': '1', 'SPEC': args.spec,
        'EXTRA_ARGS': '--limit-mm-per-prompt {"image":{"count":2}}'
    }
    if args.kv_mib is not None:
        environment.update(KV_MEM=str(args.kv_mib * 1024 * 1024),
                           MAX_LEN=str(args.max_model_len), DFLASH_MAX_LEN=str(args.max_model_len))
    write(override, {'services': {'single': {'environment': environment}}})
    compose = ['sudo', '-n', '/snap/bin/docker', 'compose', '--project-directory', str(SERVING),
               '-f', str(SERVING / 'docker-compose.yml'),
               '-f', str(SERVING / 'docker-compose.override.yml'),
               '-f', str(override), '--profile', 'single']
    config = configuration(); config['reviewer_url'] = 'http://127.0.0.1:7001/v1'
    store = Store(OUT); models = Models(config, store)
    samples_stop = threading.Event()
    def sample():
        while not samples_stop.wait(2):
            try:
                state['gpu_samples_mib'].append(gpu_memory())
            except (ValueError, subprocess.SubprocessError):
                pass
    sampler = threading.Thread(target=sample, daemon=True)
    lock = Path(config['gpu_lock']).open('a+')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    started_service = False
    try:
        started_service = True
        t = time.monotonic()
        subprocess.run(compose + ['up', '--detach'], check=True)
        sampler.start()
        last = 0
        while True:
            try:
                r = httpx.get('http://127.0.0.1:7001/health', timeout=3)
                if r.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            elapsed = time.monotonic() - t
            if elapsed > 600:
                raise TimeoutError('Temporary two-image reviewer did not become ready')
            if elapsed - last >= 30:
                print('Reviewer starting:', round(elapsed), 'seconds', flush=True); last = elapsed
            time.sleep(2)
        state['startup_seconds'] = round(time.monotonic() - t, 3); save()

        controls = OUT / 'controls'; controls.mkdir(exist_ok=True)
        for shape, color in [('triangle', 'red'), ('circle', 'blue'), ('rectangle', 'green')]:
            im = Image.new('RGB', (512, 512), 'white'); draw = ImageDraw.Draw(im)
            if shape == 'triangle': draw.polygon([(256, 80), (80, 400), (432, 400)], fill=color)
            elif shape == 'circle': draw.ellipse((90, 90, 422, 422), fill=color)
            else: draw.rectangle((80, 155, 432, 357), fill=color)
            im.save(controls / f'{shape}.png')
        cases = []
        colors = {'triangle': 'red', 'circle': 'blue', 'rectangle': 'green'}
        for i, (first, second) in enumerate([('triangle', 'circle'), ('triangle', 'rectangle'), ('rectangle', 'triangle')]):
            cases.append({'name': f'control-{i}', 'kind': 'control',
                'image': str(controls / f'{first}.png'), 'reference_images': [str(controls / f'{second}.png')],
                'prompt': 'Inspect both attached images in their attachment order. Return JSON '
                          '{"image_1":{"color":string,"shape":string},"image_2":{"color":string,"shape":string}}. '
                          'Describe the single colored shape in each image. Use lowercase basic color and shape names.',
                'max_tokens': 300, 'expected': {'image_1': {'color': colors[first], 'shape': first},
                                              'image_2': {'color': colors[second], 'shape': second}}})
        anatomy = ROOT / 'artifacts/experiments/anatomy-digestive-component-review-v3'
        for provider in ['qwen_local', 'flux_dev_local']:
            for case in ['D03', 'D05', 'D06']:
                p = sorted((anatomy / 'requests').glob(f'anatomy-strict-{provider}-{case}-0-*.request.json'))[0]
                d = read(p); baseline = read(anatomy / 'results' / provider / case / 'result.json')
                cases.append({'name': f'anatomy-{provider}-{case}', 'kind': 'anatomy', 'source_request': str(p),
                              'baseline_review': baseline['review'], **d, 'max_tokens': d['settings']['max_tokens']})
        persons = next((ROOT / 'artifacts/runs').glob('partition-of-india*'))
        for take in [0, 2]:
            p = sorted((persons / 'requests').glob(f'message-review-A005-*-{take}-json-0-*.request.json'))[0]
            d = read(p); baseline = read(p.with_name(p.name.replace('.request.json', '.result.json')))
            cases.append({'name': f'person-card-take-{take}', 'kind': 'person', 'source_request': str(p),
                          'baseline_review': baseline, **d, 'max_tokens': d['settings']['max_tokens']})
        cases.sort(key=lambda case: {'control': 0, 'person': 1, 'anatomy': 2}[case['kind']])
        if args.kind:
            cases = [case for case in cases if case['kind'] == args.kind]
        write(OUT / 'cases.json', cases)
        for index, case in enumerate(cases):
            print(f'Vision test {index+1}/{len(cases)}: {case["name"]}', flush=True)
            row = {'name': case['name'], 'kind': case['kind'], 'image': case['image'],
                   'reference_images': case['reference_images'], 'image_count': 1 + len(case['reference_images'])}
            t = time.monotonic()
            try:
                value = models.call('reviewer', case['name'], case['prompt'], image_path=Path(case['image']),
                                    reference_image_paths=[Path(p) for p in case['reference_images']],
                                    max_tokens=case['max_tokens'])
                row.update(status='complete', review=value)
                if case['kind'] == 'control': row['control_pass'] = value == case['expected']
                if case['kind'] == 'anatomy':
                    row['inventory_errors'] = inventory_errors(normalize_inventory(value), required=True,
                                                               nonempty=True, reference_available=True)
                if 'baseline_review' in case: row['baseline_review'] = case['baseline_review']
            except Exception as exc:
                row.update(status='error', error=f'{type(exc).__name__}: {exc}')
            row['seconds'] = round(time.monotonic() - t, 3)
            state['rows'].append(row); save()
            if row['status'] == 'error' and row['error'].startswith(('ReadTimeout:', 'ConnectError:', 'RemoteProtocolError:')):
                state['halted'] = 'Stop after a transport stall; do not queue further reviews on a stalled engine'
                break
        state['complete'] = len(state['rows']) == len(cases)
    except Exception as exc:
        state['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        if started_service:
            with (OUT / 'container.log').open('w') as log:
                subprocess.run(compose + ['logs', '--no-color', '--tail', '150', 'single'], stdout=log, stderr=subprocess.STDOUT)
            subprocess.run(compose + ['down'], check=True)
        samples_stop.set()
        if sampler.is_alive(): sampler.join(timeout=5)
        state['peak_gpu_used_mib'] = max(state['gpu_samples_mib'], default=None)
        state['restored_gpu_used_mib'] = gpu_memory(); save()
        store.db.close(); lock.close()


if __name__ == '__main__':
    main()
