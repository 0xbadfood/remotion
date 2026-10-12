#!/usr/bin/env python3
"""Check long-context retrieval and two-image reading on Spark together."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from infofilm.core import Store, configuration, read, write
from infofilm.llm import Models


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://10.8.0.4:8001/v1')
    parser.add_argument('--text-tokens', type=int, default=60000)
    args = parser.parse_args()
    if not 8000 <= args.text_tokens <= 60000:
        parser.error('Use 8000..60000 text tokens, preserving room for images and output')
    with sqlite3.connect(f'file:{ROOT}/artifacts/interface/queue.sqlite3?mode=ro', uri=True) as db:
        if db.execute("SELECT 1 FROM jobs WHERE status IN ('running','queued')").fetchone():
            raise RuntimeError('A production job is active; defer the long request')
    out = ROOT / 'artifacts/experiments/service-switch/spark-context-vision'
    store = Store(out)
    state = {'complete': False, 'url': args.url, 'production_routing_changed': False,
             'output_token_allowance': 768, 'image_count': 2,
             'test_scope': 'Context capacity and attachment reading, not general review accuracy'}
    try:
        with httpx.Client(timeout=60) as client:
            base = args.url.rstrip('/').removesuffix('/v1')
            r = client.get(args.url.rstrip('/') + '/models'); r.raise_for_status()
            state['model_metadata'] = r.json()['data'][0]
            def token_count(text):
                r = client.post(base + '/tokenize', json={'content': text, 'add_special': False})
                r.raise_for_status()
                return len(r.json()['tokens'])
            block = ('Archive background record. This paragraph is unrelated to the tagged values and pictures. '
                     'Use the requested tags for retrieval and the attached pixels for visual observations.\n')
            expected = {'start_code': 'cobalt-4316', 'middle_code': 'maple-9274', 'end_code': 'quartz-6185',
                        'image_1': {'color': 'red', 'shape': 'triangle'},
                        'image_2': {'color': 'blue', 'shape': 'circle'}}
            task = ('Return JSON with start_code, middle_code and end_code, copied from the CONTEXT_START, '
                    'CONTEXT_MIDDLE and CONTEXT_END tags below. Also describe the colored shape in each '
                    'attached image in attachment order using image_1 and image_2, each an object with '
                    'color and shape in lowercase. Background archive paragraphs are irrelevant.\n')
            def prompt(n):
                return (task + '\n[CONTEXT_START] cobalt-4316\n' + block * (n // 2) +
                        '\n[CONTEXT_MIDDLE] maple-9274\n' + block * (n - n // 2) +
                        '\n[CONTEXT_END] quartz-6185\nReturn the requested JSON using the tags and both images.')
            lo, hi = 0, args.text_tokens // token_count(block) + 4
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if token_count(prompt(mid)) <= args.text_tokens: lo = mid
                else: hi = mid - 1
            text = prompt(lo)
            state['tokenizer_text_tokens'] = token_count(text)
            state['expected'] = expected
            state['input_image_paths'] = [str(ROOT / 'artifacts/experiments/service-switch/vision-two-images-clean/controls' / name)
                                          for name in ['triangle.png', 'circle.png']]
            write(out / 'summary.json', state)
        config = configuration(); config['reviewer_url'] = args.url.rstrip('/')
        models = Models(config, store)
        start = time.monotonic()
        value = models.call('reviewer', 'spark-long-context-two-images', text,
                            image_path=Path(state['input_image_paths'][0]),
                            reference_image_paths=[Path(state['input_image_paths'][1])], max_tokens=768)
        response_path = next((out / 'requests').glob('spark-long-context-two-images-*.response.json'))
        raw = read(response_path)
        state.update(complete=True, seconds=raw['measured_latency_seconds'],
                     resume_wall_seconds=round(time.monotonic()-start, 3),
                     usage=raw.get('usage'), observed=value, test_pass=value == expected)
        print(json.dumps({k:v for k,v in state.items() if k not in {'expected','input_image_paths'}}), flush=True)
    except Exception as exc:
        state['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        write(out / 'summary.json', state)
        store.db.close()


if __name__ == '__main__':
    main()
