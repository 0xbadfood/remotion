#!/usr/bin/env python3
"""Repair long silence in a checked film, retime its words, and reuse pictures/BGM.

Archives original selections and publishes only after the rebuilt video passes
layout and waveform QA. No TTS, image, research or music generation is invoked.
"""
import argparse
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infofilm.core import Store, configuration, digest, read, write
from infofilm.creative import visual_metrics
from infofilm.delivery import preview
from infofilm.media import audio_info, audit_audio
from infofilm.music import pcm_metrics
from infofilm.planning import script_text, word_count
from infofilm.resume import music_checkpoint
from infofilm.silence import cleanup, cut_pcm, retime, verify_pcm_edit
from infofilm.video import compile_timeline, render


def preservation_audit(original_audit, fresh_audit, proof, original_audio):
    if fresh_audit['status'] == 'passed':
        return fresh_audit
    if original_audit['status'] != 'passed' or not proof['passed']:
        raise RuntimeError('No approved speech-preserving checkpoint for this edit')
    # ASR is stochastic with respect to segmentation. A silence-only edit outside
    # every MFA word retains the approved content exactly; never discard the new
    # ASR disagreement or pretend it passed independently.
    return {**original_audit, 'audit_policy': 'approved-speech-pcm-preservation-v1',
            'source_audio': str(original_audio), 'source_audio_sha256': digest(Path(original_audio).read_bytes()),
            'preservation_proof': proof, 'post_edit_whisper': fresh_audit,
            'decision': 'Inherited approved content: every retained PCM sample is identical; cuts lie outside all MFA words.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    args = parser.parse_args()
    project = args.project.resolve()
    delivery, brief, films = music_checkpoint(project)
    config = configuration(project / 'config-snapshot.json')
    store = Store(project)
    revision = project / 'delivery-history' / ('silence-' + digest(delivery)[:20])
    images = read(project / 'image_manifest.json')
    evidence, sources = read(project / 'evidence-final.json'), read(project / 'sources.json')
    new_films, outputs = {}, []
    try:
        write(revision / 'delivery.json', delivery)
        for duration, saved in films.items():
            film = copy.deepcopy(saved)
            timing = read(project / f'film-{duration}/timing.json')
            storyboard = read(project / f'film-{duration}/storyboard.json')
            music = read(project / f'film-{duration}/music-selection.json')
            for key, value in [('audio-selection', saved), ('timing', timing), ('storyboard', storyboard), ('music-selection', music)]:
                write(revision / f'{duration}/{key}.json', value)
            audio, report = cleanup(saved['audio'], config, store, 'completed-film-pause-repair')
            if not report['cuts_samples']:
                print('No excessive silence remains; no revision needed.', flush=True)
                return
            # Prove edits avoid every aligned word before publishing or rendering.
            timing = retime(timing, report, audio)
            proof = verify_pcm_edit(saved['audio'], audio, report)
            audit = audit_audio(config, store, audio, script_text(film['script']), points=film['script']['points'])
            audit = preservation_audit(saved['audit'], audit, proof, saved['audio'])
            info = {**film['audio_info'], **audio_info(audio), 'silence_cleanup': report,
                    'source_point_clips': film['audio_info'].get('point_clips')}
            info.pop('point_clips', None) # Source offsets are historical, not valid after editing the join.
            info['observed_words_per_minute'] = round(word_count(script_text(film['script'])) / info['duration_seconds'] * 60, 3)
            film.update(audio=str(audio), audio_info=info, actual_seconds=info['duration_seconds'],
                        duration_deviation_seconds=info['duration_seconds']-duration, audit=audit, silence_cleanup=report)
            film.update(timing=timing, storyboard=storyboard)
            if music.get('engine') != 'none':
                master = Path(audio).parent / 'master.wav'
                original_master = Path(music['master_audio'])
                if digest(original_master.read_bytes()) != music['master_audio_info']['audio_sha256']:
                    raise ValueError('Original scored master changed')
                cut_pcm(original_master, master, report)
                master_proof = verify_pcm_edit(original_master, master, report)
                master_audit = audit_audio(config, store, master, script_text(film['script']), points=film['script']['points'])
                master_audit = preservation_audit(music['mixed_speech_audit'], master_audit, master_proof, original_master)
                music = {**music, 'master_audio': str(master), 'master_audio_info': audio_info(master),
                         'narration_sha256': info['audio_sha256'], 'mixed_speech_audit': master_audit,
                         'silence_cleanup': report, 'score_reused': True, 'previous_master_audio': str(original_master),
                         'mix': {**music['mix'], 'levels': pcm_metrics(master), 'edit_policy': 'same sample-time cuts as narration'}}
            else:
                music = {**music, 'master_audio': str(audio), 'master_audio_info': audio_info(audio),
                         'narration_sha256': info['audio_sha256']}
            film['music'] = music
            new_films[duration] = film
        for old in delivery['outputs']:
            film = new_films[old['requested_seconds']]
            portrait = old['orientation'] == 'portrait'
            render_config = {**config, 'width': 1080 if portrait else 1920, 'height': 1920 if portrait else 1080}
            root, timeline = compile_timeline(render_config, store, film['script'], film['timing'], film['storyboard'],
                                            images, evidence, sources, old['requested_seconds'], creative_brief=brief, music=film['music'])
            video = render(render_config, store, root, timeline)
            if not read(root / 'qa.json')['passed']:
                raise RuntimeError('Rebuilt video failed delivery QA')
            outputs.append({**old, 'video': str(video), 'timeline': str(timeline), 'qa': str(root / 'qa.json'),
                            'actual_seconds': film['actual_seconds'], 'previous_video': old['video'],
                            'visual_metrics': visual_metrics(film['storyboard'], film['script'], film['timing']),
                            'silence_removed_seconds': film['silence_cleanup']['removed_seconds']})
        # Selections remain untouched until every rebuilt delivery is checked.
        for duration, film in new_films.items():
            write(project / f'film-{duration}/timing.json', film['timing'])
            write(project / f'film-{duration}/music-selection.json', film['music'])
            selected = {k:v for k,v in film.items() if k not in ['timing', 'music', 'storyboard']}
            write(project / f'film-{duration}/audio-selection.json', selected)
            write(project / f'film-{duration}/visual-metrics.json', visual_metrics(film['storyboard'], film['script'], film['timing']))
        write(project / 'delivery.json', {**delivery, 'outputs': outputs, 'revision': 'mechanical long-silence repair',
                                        'human_acceptance': 'pending'})
        destination = preview(store)
        write(revision / 'repair-result.json', {'outputs': outputs, 'portable_review': str(destination)})
        store.event('completed-film-pause-repair', 'succeeded', {'outputs': outputs, 'portable_review': str(destination)})
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
