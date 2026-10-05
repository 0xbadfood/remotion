"""Audio-first delivery and validated re-entry before any visual planning."""
from __future__ import annotations

import html
import shutil
import subprocess
from pathlib import Path

from .core import ROOT, digest, read, write
from .media import audio_info
from .music import delivery_levels, pcm_metrics, prepare_music
from .planning import script_text
from .resume import images_checkpoint


def checked_audio(project, durations=None):
    return images_checkpoint(project, durations)[-1]


def _proof(film):
    return {'audio_sha256': film['audio_info']['audio_sha256'],
            'script_sha256': digest(script_text(film['script'])),
            'timing_sha256': digest(film['timing'])}


def delivery_checkpoint(project, *, long=False):
    """Validate every saved chapter before accepting a video continuation."""
    project = Path(project)
    manifest = read(project / 'audio-delivery.json')
    if manifest.get('schema_version') != 'infofilm.audio-delivery.v1':
        raise ValueError('Audio delivery has no validated continuation checkpoint')
    if long:
        plan = read(project / 'chapter-plan.json')
        if digest(plan) != manifest.get('plan_sha256'):
            raise ValueError('The chapter plan changed after audio review')
        current = {}
        for chapter in plan['chapters']:
            child = project / 'chapters' / f"{project.name}-{chapter['id'].lower()}"
            films = checked_audio(child, [max(30, round(chapter['advisory_seconds']))])
            current[chapter['id']] = _proof(films[0])
    else:
        current = {str(f['requested_seconds']): _proof(f) for f in checked_audio(project)}
    if current != manifest.get('audio_checkpoints'):
        raise ValueError('Narration or word timing changed after audio review')
    for output in manifest['outputs']:
        for key, sha in [('master_audio', 'master_sha256'), ('mp3', 'mp3_sha256')]:
            path = Path(output[key]).resolve()
            if not path.is_relative_to((ROOT / 'output' / project.name / 'audio-review').resolve()):
                raise ValueError('Audio delivery points outside its review folder')
            if digest(path.read_bytes()) != output[sha]:
                raise ValueError('The listening delivery changed after checks')
    return manifest


def publish_audio(store, subject, outputs, checkpoints, *, plan=None):
    """Export WAV/MP3 and scripts, without a video or image dependency."""
    destination = ROOT / 'output' / store.root.name / 'audio-review'
    destination.mkdir(parents=True, exist_ok=True)
    published, cards = [], []
    for output in outputs:
        master = Path(output['master_audio'])
        info = audio_info(master)
        if abs(info['duration_seconds'] - output['actual_seconds']) > 2/48000:
            raise ValueError('Listening master duration differs from its chapter timeline')
        if pcm_metrics(master)['clipped_sample_ratio']:
            raise ValueError('Listening master contains clipping')
        stem = f"{output['requested_seconds']}s-{info['audio_sha256'][:10]}"
        wav, mp3, script = destination / (stem + '.wav'), destination / (stem + '.mp3'), destination / (stem + '.txt')
        if not wav.exists():
            shutil.copyfile(master, wav)
        if digest(wav.read_bytes()) != info['audio_sha256']:
            raise ValueError('Exported audio differs from its checked master')
        if not mp3.exists():
            subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-i', str(wav),
                '-c:a', 'libmp3lame', '-b:a', '192k', str(mp3)], check=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(mp3), '-f', 'null', '-'], check=True)
        script.write_text(output['script_text'] + '\n')
        value = {k: v for k, v in output.items() if k != 'script_text'}
        value.update(master_audio=str(wav), master_sha256=info['audio_sha256'], mp3=str(mp3),
                     mp3_sha256=digest(mp3.read_bytes()), script_file=str(script),
                     audio_qa_passed=True, master_audio_info=info)
        published.append(value)
        cards.append(f'<article><h2>{output["requested_seconds"]}s advisory · {info["duration_seconds"]:.1f}s actual</h2>'
                     f'<audio controls preload="metadata" src="{mp3.name}"></audio><p>'
                     f'<a href="{mp3.name}">Download MP3</a> · <a href="{wav.name}">Download WAV</a> · '
                     f'<a href="{script.name}">Read narration</a></p></article>')
    manifest = {'schema_version': 'infofilm.audio-delivery.v1', 'subject': subject,
                'generation_mode': 'long' if plan else 'short', 'outputs': published,
                'audio_checkpoints': checkpoints, 'human_listening_review': 'pending'}
    if plan:
        manifest['plan_sha256'] = digest(plan)
    write(store.root / 'audio-delivery.json', manifest)
    write(destination / 'manifest.json', manifest)
    (destination / 'index.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Audio review</title>'
        '<style>body{max-width:960px;margin:40px auto;padding:20px;background:#082d34;color:#f7f3e9;'
        'font:17px/1.6 system-ui}a{color:#f3b85e}audio{width:100%}article{margin:24px 0}</style>'
        '<h1>' + html.escape(subject) + '</h1><p>Listen to the complete narration before continuing to video. '
        'Scene planning and image generation have not started.</p>' + ''.join(cards) + '</html>')
    return destination


def finish_audio(args, config, store, films, brief):
    outputs = []
    store.event('pipeline:music', 'running', {'engine': args.music_engine, 'audio_first': True})
    for film in films:
        music = prepare_music(config, store, film, brief, args.music_engine)
        outputs.append({'requested_seconds': film['requested_seconds'], 'actual_seconds': film['actual_seconds'],
                        'point_count': film['point_count'], 'master_audio': music['master_audio'],
                        'music_engine': args.music_engine, 'script_text': script_text(film['script'])})
    destination = publish_audio(store, args.subject, outputs, {str(f['requested_seconds']): _proof(f) for f in films})
    store.event('pipeline:music', 'succeeded', {'output_directory': str(destination), 'audio_first': True})
    return destination


def assemble_audio(config, store, plan, records, target, music_engine):
    """Prepare chapter scores only after all narration and continuity pass."""
    from .longform_video import audio_stems
    films, proofs, scripts, intervals = [], {}, [], []
    for row in records:
        film = checked_audio(row['project'], [row['duration']])[0]
        films.append(film)
        proofs[row['chapter']['id']] = _proof(film)
        scripts.append(row['chapter']['id'] + ' · ' + row['chapter']['title'] + '\n\n' + script_text(film['script']))
    identity = digest({'plan': plan, 'audio_checkpoints': proofs, 'engine': music_engine,
                       'delivery_levels': delivery_levels(config), 'policy': 'audio-first-v1'})
    directory = store.root / 'audio-assembly' / identity[:20]
    # Native score caches are identical to those later used for chapter video.
    for row, film in zip(records, films):
        child_store = type(store)(row['project'])
        try:
            brief = read(child_store.root / 'creative-brief.json')
            store.event('pipeline:assembly', 'chapter_music', {'chapter': row['chapter']['id'], 'engine': music_engine})
            prepare_music(config, child_store, film, brief, music_engine)
        finally:
            child_store.db.close()
    gap = plan['assembly']['narration_pause_seconds']
    cursor = 0
    for index, (row, film) in enumerate(zip(records, films)):
        if index:
            cursor += gap
        intervals.append({'chapter_id': row['chapter']['id'], 'start_seconds': cursor, 'seconds': film['actual_seconds']})
        cursor += film['actual_seconds']
    music = audio_stems(records, intervals, directory, cursor, gap,
                        plan['assembly']['music_crossfade_seconds'], config=config)
    output = {'requested_seconds': target, 'actual_seconds': cursor,
              'point_count': sum(f['point_count'] for f in films), 'master_audio': music['master_audio'],
              'music_engine': music_engine, 'chapters': intervals, 'script_text': '\n\n'.join(scripts)}
    return publish_audio(store, plan['film']['subject'], [output], proofs, plan=plan)
