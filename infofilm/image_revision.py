"""Archive selected visual revisions and restart the existing web job safely."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .core import Store, configuration, digest, read, write
from .image_recovery import SETTINGS_KEYS
from .longform import chapter_alive, checked_delivery
from .resume import images_checkpoint


def visual_paths(project):
    paths = [project / name for name in ["images", "videos", "scene-image-prompts.json", "visual-library.json",
        "image_manifest.json", "image-provider-policy.json", "image-escalation-report.json",
        "image-recovery-policy.json", "image-recovery-report.json", "delivery.json"]]
    paths.extend(p for p in (project / 'planning').glob('*')
                 if p.name.startswith(('visual-beats-', 'scene-image-prompts', 'image-prompt-repair-', 'visual-library')))
    for film in project.glob('film-*'):
        paths.extend(film / name for name in ['storyboard.json', 'visual-metrics.json'])
    return [p for p in paths if p.exists()]


def revise_images(queue, job_id, chapters=None):
    job = queue.get(job_id)
    if job['status'] not in {'failed', 'interrupted', 'succeeded'} or queue._alive(job):
        raise ValueError('Only stopped runs may reset their image stage')
    project = Path(job['project'])
    long = job['request'].get('generation_mode') == 'long'
    status = read(project / 'longform-status.json') if long else None
    rows = status['chapters'] if long else [{'chapter': {'id': 'film'}, 'project': str(project),
                                            'duration': None}]
    selected = set(chapters or [r['chapter']['id'] for r in rows])
    if not selected or selected - {r['chapter']['id'] for r in rows}:
        raise ValueError('Unknown image revision chapter selection')
    preserved = {}
    for row in rows:
        child = Path(row['project'])
        if long and chapter_alive(row):
            raise ValueError('A chapter worker is still active; stop it before revision')
        if row['chapter']['id'] in selected:
            _, _, _, _, films = images_checkpoint(child, [row['duration']] if long else None)
            preserved[row['chapter']['id']] = {str(path): digest(path.read_bytes())
                for film in films for path in [Path(film['audio']),
                    child / f"film-{film['requested_seconds']}/audio-selection.json",
                    child / f"film-{film['requested_seconds']}/timing.json"]}
        elif long:
            orientations = ['landscape', 'portrait'] if job['request']['orientation'] == 'both' else [job['request']['orientation']]
            delivery = checked_delivery(child, orientations)
            if not delivery:
                raise ValueError('An unselected chapter needs a completed delivery')
            preserved[row['chapter']['id']] = {str(Path(o['video'])): digest(Path(o['video']).read_bytes()) for o in delivery['outputs']}
    config = configuration(queue.config_path)
    overrides = {key: config[key] for key in SETTINGS_KEYS if key in config}
    revision = project / 'image-revisions' / str(time.time_ns())
    moves = []
    payload = {**job['request'], 'resume_from': 'images', 'image_recovery_settings': overrides}
    if long:
        payload['image_chapters'] = sorted(selected)
    with queue.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT status,updated FROM jobs WHERE id=?', (job_id,)).fetchone()
        if current['status'] != job['status'] or current['updated'] != job['updated']:
            raise ValueError('Job changed while preparing the image revision')
        try:
            write(revision / 'previous-request.json', job['request'])
            if status:
                write(revision / 'previous-status.json', status)
            for row in rows:
                if row['chapter']['id'] not in selected:
                    continue
                child = Path(row['project'])
                archive = revision / row['chapter']['id']
                write(archive / 'config-snapshot.json', read(child / 'config-snapshot.json'))
                for path in visual_paths(child):
                    destination = archive / path.relative_to(child)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    path.replace(destination)
                    moves.append((path, destination))
                if not long:
                    write(child / 'config-snapshot.json', {**configuration(child / 'config-snapshot.json'), **overrides})
                else:
                    row.update(status='pending', error=None, child_pid=None, reused=False)
            for hashes in preserved.values():
                for path, sha in hashes.items():
                    if digest(Path(path).read_bytes()) != sha:
                        raise ValueError('A preserved narration/MFA/video checkpoint changed during revision')
            write(revision / 'revision.json', {'resume_from': 'images', 'chapters': sorted(selected),
                'settings': overrides, 'preserved': preserved,
                'archived': [{'original': str(a), 'archive': str(b)} for a,b in moves]})
            if status:
                status.update(completed=sum(r['status']=='succeeded' for r in rows), failed=0, updated=time.time())
                write(project / 'longform-status.json', status)
            write(project / 'web-request.json', payload)
            db.execute("UPDATE jobs SET request=?,status='queued',error=NULL,updated=? WHERE id=?",
                       (json.dumps(payload), time.time(), job_id))
        except Exception:
            for original, archived in reversed(moves):
                original.parent.mkdir(parents=True, exist_ok=True)
                archived.replace(original)
            if status:
                write(project / 'longform-status.json', read(revision / 'previous-status.json'))
            write(project / 'web-request.json', job['request'])
            raise
    queue.wake.set()
    return {'job': queue.get(job_id), 'revision': str(revision), 'chapters': sorted(selected)}
