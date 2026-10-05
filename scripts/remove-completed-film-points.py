#!/usr/bin/env python3
"""Apply an explicit editorial cut to a checked film; retain speech and image assets.

Requires a reviewed choice of point IDs. No automatic content deletion or fresh
TTS, image or music generation. Originals remain in delivery-history. Only
checked rebuilt pictures/audio are published.
"""
import argparse
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infofilm.core import Store, configuration, digest, read, write
from infofilm.creative import beat_errors, visual_metrics
from infofilm.delivery import preview
from infofilm.media import audio_info, audit_audio
from infofilm.music import delivery_levels, level_master, mix_master, pcm_metrics
from infofilm.planning import script_text, word_count, script_errors
from infofilm.point_edit import plan_point_removal
from infofilm.resume import music_checkpoint
from infofilm.silence import cut_pcm, retime, verify_pcm_edit
from infofilm.video import compile_timeline, render


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--drop-points', required=True, nargs='+')
    parser.add_argument('--reason', required=True)
    args = parser.parse_args()
    project = args.project.resolve()
    delivery, brief, films = music_checkpoint(project)
    if len(films) != 1:
        raise ValueError('Edit one chapter/version at a time; point IDs differ across versions')
    target, original = next(iter(films.items()))
    timing = read(project / f'film-{target}/timing.json')
    storyboard = read(project / f'film-{target}/storyboard.json')
    script, retained_timing, report = plan_point_removal(original['script'], timing, args.drop_points)
    storyboard['scenes'] = [s for s in storyboard['scenes'] if s['point_id'] in report['retained_point_ids']]
    evidence, sources = read(project / 'evidence-final.json'), read(project / 'sources.json')
    errors = script_errors(script, evidence)
    if errors:
        raise ValueError('; '.join(errors))
    report.update(reason=args.reason, original_script_sha256=digest(original['script']),
                  edited_script_sha256=digest(script), original_delivery_sha256=digest(delivery))
    revision = project / 'delivery-history' / ('point-cut-' + digest(report)[:20])
    revision.mkdir(parents=True, exist_ok=True)
    originals = {name: read(project / name) for name in ['delivery.json',
        *[f'film-{target}/{name}.json' for name in ['audio-selection', 'script', 'timing', 'storyboard', 'music-selection', 'visual-metrics']]]}
    original_text = (project / f'film-{target}/script.txt').read_text()
    for name, value in originals.items():
        write(revision / name, value)
    (revision / 'script.txt').write_text(original_text)
    write(revision / 'edit-plan.json', report)
    config = configuration(project / 'config-snapshot.json')
    store = Store(project)
    published = False
    try:
        store.event('point-cut', 'running', {'points': report['removed_point_ids'], 'reason': args.reason,
                                           'removed_seconds': report['removed_seconds']})
        audio = revision / 'narration.wav'
        cut_pcm(original['audio'], audio, report)
        proof = verify_pcm_edit(original['audio'], audio, report)
        timing = retime(retained_timing, report, audio)
        timing['provenance']['point_removal'] = report
        if beat_errors(storyboard, script, timing):
            raise ValueError('; '.join(beat_errors(storyboard, script, timing)))
        fresh_audit = audit_audio(config, store, audio, script_text(script), points=script['points'])
        if fresh_audit['status'] == 'passed':
            audit = fresh_audit
        elif original['audit']['status'] == 'passed' and proof['passed']:
            # Keep the disagreement; retained words and samples come from the
            # approved source. This is a preservation decision, not an ASR pass.
            audit = {'status': 'passed', 'audit_method': 'retained-approved-points-pcm-proof-v1',
                'source_audio_sha256': report['source_sha256'], 'source_audit_sha256': digest(original['audit']),
                'retained_point_ids': report['retained_point_ids'], 'preservation_proof': proof,
                'post_edit_whisper': fresh_audit,
                'decision': 'All remaining speech is unchanged; only explicitly removed full narration points were cut.'}
        else:
            raise RuntimeError('Edited narration needs review; originals retained')
        info = {**original['audio_info'], **audio_info(audio)}
        for key in ['point_clips', 'silence_cleanup', 'point_timing']:
            info.pop(key, None)
        info.update(observed_words_per_minute=round(word_count(script_text(script)) / info['duration_seconds'] * 60, 3),
                    point_removal=report, source_audio=str(original['audio']))
        film = {**original, 'script': script, 'audio': str(audio), 'audio_info': info, 'audit': audit,
                'point_count': len(script['points']), 'actual_seconds': info['duration_seconds'],
                'duration_deviation_seconds': info['duration_seconds']-target,
                'editorial_revision': report, 'human_listening_review': 'pending'}
        music = copy.deepcopy(originals[f'film-{target}/music-selection.json'])
        levels = music.get('delivery_levels', delivery_levels(config))
        master = revision / 'master.wav'
        if music.get('engine') != 'none':
            score = Path(music['score']['audio'])
            if digest(score.read_bytes()) != music['score']['audio_sha256']:
                raise ValueError('Approved music cue changed')
            master_info = mix_master(audio, score, master, film['actual_seconds'], gain=music['mix']['music_gain'],
                                     delivery_gain_db=levels['gain_db'])
            music.update(master_audio=str(master), master_audio_info=master_info, narration_sha256=info['audio_sha256'],
                score_reused=True, mixed_speech_audit={'status': 'not_rerun', 'clean_narration_audit': audit,
                    'decision': 'Existing approved instrumental cue remixed under audited narration; waveform delivery QA follows.'})
            music['mix'].update(levels=pcm_metrics(master), edit_policy='continuous existing cue remixed under shortened narration')
        else:
            master_info = level_master(audio, master, film['actual_seconds'], levels['gain_db'])
            music.update(master_audio=str(master), master_audio_info=master_info, narration_sha256=info['audio_sha256'])
        music['delivery_levels'] = levels
        if music.get('mix', {}).get('levels', {}).get('clipped_sample_ratio', 0) != 0:
            raise ValueError('Edited master is clipped')
        outputs = []
        metrics = visual_metrics(storyboard, script, timing)
        for old in delivery['outputs']:
            portrait = old['orientation'] == 'portrait'
            render_config = {**config, 'width': 1080 if portrait else 1920, 'height': 1920 if portrait else 1080}
            root, timeline = compile_timeline(render_config, store, script, timing, storyboard,
                read(project / 'image_manifest.json'), evidence, sources, target, creative_brief=brief, music=music)
            video = render(render_config, store, root, timeline)
            if not read(root / 'qa.json')['passed']:
                raise RuntimeError('Edited video failed delivery checks; originals retained')
            outputs.append({**old, 'video': str(video), 'timeline': str(timeline), 'qa': str(root / 'qa.json'),
                'actual_seconds': film['actual_seconds'], 'point_count': len(script['points']), 'visual_metrics': metrics,
                'previous_video': old['video'], 'editorial_revision': report,
                'needs_expert_review_assets': read(root / 'compile-report.json')['needs_expert_review_assets']})
        updates = {f'film-{target}/{name}.json': value for name, value in [
            ('audio-selection', film), ('script', script), ('timing', timing), ('storyboard', storyboard),
            ('music-selection', music), ('visual-metrics', metrics)]}
        updates['delivery.json'] = {**delivery, 'outputs': outputs, 'revision': args.reason, 'human_acceptance': 'pending'}
        published = True
        for name, value in updates.items():
            write(project / name, value)
        (project / f'film-{target}/script.txt').write_text(script_text(script))
        destination = preview(store)
        write(revision / 'result.json', {'passed': True, 'outputs': outputs, 'preservation_proof': proof,
                                       'portable_review': str(destination)})
        store.event('point-cut', 'succeeded', {'points': report['removed_point_ids'], 'outputs': outputs})
    except BaseException:
        if published:
            for name, value in originals.items():
                write(project / name, value)
            (project / f'film-{target}/script.txt').write_text(original_text)
        raise
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
