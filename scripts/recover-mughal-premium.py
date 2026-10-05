#!/usr/bin/env python3
"""Recover only unresolved Mughal assets, then resume the existing web film."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infofilm.core import Store, configuration, digest, read, write
from infofilm.image_recovery import generate_message_images
from infofilm.llm import Models
from infofilm.queue import Queue

root = Path('artifacts/runs/the-mughal-empire-in-india-1526-to-1857-ce-8301f52282').resolve()
job_id = '8301f52282e64979bbef84149a4499f2'
queue = Queue('artifacts/interface', 'config.json')
job = queue.get(job_id)
if job['status'] not in {'failed', 'interrupted'} or queue._alive(job):
    raise RuntimeError('The parent must be stopped before premium recovery')
policy = configuration()['image_terminal_recovery']
recovery = root / 'premium-recovery' / 'expert-review-v1'
old = {}
for chapter in ['c01', 'c04']:
    project = root/'chapters'/f'{root.name}-{chapter}'
    delivery = read(project/'delivery.json')
    old[chapter] = {o['video']: digest(Path(o['video']).read_bytes()) for o in delivery['outputs']}
write(recovery/'preserved-videos.json', old)
for chapter in ['c02', 'c03']:
    project = root/'chapters'/f'{root.name}-{chapter}'
    config = configuration(project/'config-snapshot.json')
    write(recovery/chapter/'previous-config.json', config)
    manifest = read(project/'image_manifest.json')
    write(recovery/chapter/'previous-manifest.json', manifest)
    write(recovery/chapter/'previous-image-report.json', read(project/'image-recovery-report.json'))
    extras = {}
    pilot = Path('artifacts/openai-image-comparison')/('mughal-'+chapter)
    for row in read(pilot/'cases.json'):
        state = read(row['state_file'])
        image = (pilot/(row['asset']+'-cloud.png')).resolve()
        extras[row['asset']] = [{'provider': 'codex_imagegen', 'candidate': str(image),
            'sha256': digest(image.read_bytes()), 'settings': {'prompt': row['prompt']},
            'visual_message': state['visual_message']}]
    config.update(image_terminal_recovery=policy, image_terminal_extra_candidates=extras)
    write(project/'config-snapshot.json', config)
    store = Store(project)
    try:
        result = generate_message_images(config, store, read(project/'scene-image-prompts.json'), Models(config, store))
        for key, value in manifest.items():
            if result[key] != value:
                raise ValueError('A previously selected image changed: ' + key)
        write(recovery/chapter/'result.json', result)
        print(chapter, 'complete', len(result), 'images, expert review:', [k for k,v in result.items() if v.get('needs_review')], flush=True)
    finally:
        store.db.close()
for images in old.values():
    for path, sha in images.items():
        if digest(Path(path).read_bytes()) != sha:
            raise ValueError('An unselected chapter video changed')
payload = {**job['request'], 'resume_from': 'images', 'image_chapters': ['C02', 'C03'],
           'image_recovery_settings': {**job['request'].get('image_recovery_settings', {}), 'image_terminal_recovery': policy}}
with queue.connection() as db:
    db.execute('BEGIN IMMEDIATE')
    current = db.execute('SELECT status,updated FROM jobs WHERE id=?', (job_id,)).fetchone()
    if current['status'] != job['status'] or current['updated'] != job['updated']:
        raise RuntimeError('Parent changed during premium recovery; do not requeue twice')
    write(recovery/'previous-request.json', job['request'])
    write(root/'web-request.json', payload)
    import json
    db.execute("UPDATE jobs SET request=?,status='queued',error=NULL,updated=? WHERE id=?",
               (json.dumps(payload), time.time(), job_id))
write(recovery/'resumed.json', {'job_id': job_id, 'chapters': ['C02', 'C03'], 'policy': policy})
print('Parent queued to finish C02/C03 and assemble the film.', flush=True)
