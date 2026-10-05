#!/usr/bin/env python3
"""Review saved exact-prompt imagegen candidates without changing production."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infofilm.core import Store, configuration, digest, read, write
from infofilm.image_recovery import current_image_review
from infofilm.llm import Models

root = Path('artifacts/openai-image-comparison').resolve()
results = []
for folder in ['mughal-c02', 'mughal-c03']:
    store = Store(root / folder)
    try:
        for case in read(store.root / 'cases.json'):
            state_path = Path(case['state_file']).resolve()
            project = state_path.parents[2]
            models = Models(configuration(project / 'config-snapshot.json'), store)
            candidate = store.root / (case['asset'] + '-cloud.png')
            state = read(state_path)
            review = current_image_review(models, {'visual_message': state['visual_message']}, candidate,
                                          case['prompt'], 'cloud-comparison-' + case['asset'])
            row = {'chapter': folder, 'asset': case['asset'], 'visual_message': state['visual_message'],
                   'prompt_sha256': digest(case['prompt'].encode()), 'candidate': str(candidate),
                   'sha256': digest(candidate.read_bytes()), 'review': review,
                   'original_candidate': case['take']['candidate'], 'original_review': case['take']['review'],
                   'engine': 'Codex built-in imagegen', 'prompt_unchanged': True,
                   'production_manifest_updated': False}
            write(store.root / (case['asset'] + '-comparison.json'), row)
            results.append(row)
            write(root / 'results.json', results)
            print(case['asset'], 'PASS' if review['pass'] else 'FAIL', review['reason_keys'], flush=True)
    finally:
        store.db.close()
