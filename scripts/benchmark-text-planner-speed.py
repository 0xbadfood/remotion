#!/usr/bin/env python3
"""Small sequential, paired text-only latency probes; no production work."""
import argparse
import json
import sys
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from infofilm.core import Store, read, write
from infofilm.llm import Models

TASKS = [
    ('outline', 'Plan a general-audience explanation of how a bicycle stays moving. '
     'Return JSON with central_question and exactly four chapters, each containing title, '
     'objective, scope and handoff. Give each chapter a distinct teaching role. '
     'Use 150 to 220 words total. This is an outline, not narration.'),
    ('repair', 'Repair this film outline. Intake, compression, power and exhaust are the four '
     'strokes of an ideal spark-ignition engine. The draft incorrectly assigns spark to intake, '
     'opens both valves throughout compression, and calls one cycle one crankshaft revolution. '
     'Return JSON {corrections:[{error,repair}], corrected_sequence:[{stroke,valves,motion,event}],'
     'cycle_revolutions:number}. Use 150 to 220 words total; no extra fields.'),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--new-url', required=True)
    parser.add_argument('--baseline-url', default='http://10.8.0.5:8001/v1')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    store = Store(args.output)
    models = Models({'planner_url': args.new_url.rstrip('/'),
                     'reviewer_url': args.baseline_url.rstrip('/')}, store)
    rows = []
    try:
        for index, (task, prompt) in enumerate(TASKS):
            # Alternate endpoint order; serial requests avoid mutual queueing.
            roles = ['planner', 'reviewer'] if index == 0 else ['reviewer', 'planner']
            for role in roles:
                name = f'speed-{task}-{role}'
                value = models.call(role, name, prompt, max_tokens=1200)
                response_path = next((store.root/'requests').glob(name+'-*.response.json'))
                raw = read(response_path)
                usage = raw.get('usage', {})
                seconds = raw['measured_latency_seconds']
                characters = len(raw['choices'][0]['message'].get('content') or '')
                rows.append({'task': task, 'endpoint_label': 'new' if role=='planner' else 'baseline',
                             'model': raw['model'], 'latency_seconds': seconds, 'usage': usage,
                             'completion_tokens_per_wall_second': round(usage.get('completion_tokens',0)/seconds,2),
                             'output_characters': characters,
                             'output_characters_per_wall_second': round(characters/seconds,2),
                             'valid_json': isinstance(value,dict),
                             'finish_reason': raw['choices'][0]['finish_reason']})
                write(store.root/'summary.json', {'calls': rows, 'limitations': [
                    'Two short tasks are latency probes, not a quality or concurrency benchmark.',
                    'Completion tokens per wall second includes prefill and request overhead; not isolated decode speed.',
                    'Server load and tokenizer differences can affect paired comparisons.']})
        print(json.dumps(rows), flush=True)
    except httpx.HTTPError as exc:
        write(store.root/'failure.json', {
            'error_type': type(exc).__name__, 'error': str(exc),
            'completed_calls': rows,
            **({'response_body': exc.response.text[:12000]}
               if isinstance(exc, httpx.HTTPStatusError) else {}),
        })
        raise
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
