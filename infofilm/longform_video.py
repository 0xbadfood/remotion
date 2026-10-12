"""Join checked chapter pictures with native cards and independent audio stems."""
from __future__ import annotations

import math
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from PIL import ImageFont

from .core import ROOT, digest, read, write
from .media import audio_info
from .music import delivery_levels, level_master, mix_master, pcm_metrics


def ffmpeg(args):
    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', *map(str, args)], check=True, capture_output=True)


def probe_picture(path):
    import json
    value = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-show_streams', '-of', 'json', str(path)],
                                    check=True, capture_output=True, text=True).stdout)
    video = next(s for s in value['streams'] if s['codec_type'] == 'video')
    return {'width': video['width'], 'height': video['height'], 'fps': video['r_frame_rate'],
            'frames': int(video.get('nb_read_frames') or video['nb_frames']), 'codec': video['codec_name']}


def section_asset(project, chapter):
    from .external_planner import production_kind
    if production_kind(chapter) == 'animation':
        root=Path(project)/'animation/renders'
        folder=next((root/o for o in ('landscape','portrait') if (root/o/'render-results.json').is_file()),None)
        if folder is None:raise ValueError('Animation section card has no checked checkpoint')
        receipt=read(folder/'render-results.json')
        if not receipt.get('complete') or not receipt.get('checkpoints'):raise ValueError('Animation section card is not checked')
        # Prefer a teaching checkpoint after the initial setup, using an existing rendered image.
        sample=receipt['checkpoints'][min(1,len(receipt['checkpoints'])-1)]
        image=(folder/sample['file']).resolve()
        if not image.is_relative_to(folder.resolve()):raise ValueError('Animation checkpoint escapes its render folder')
        return {'asset_id':chapter['id']+'-animation-checkpoint','file':str(image),'sha256':digest(image.read_bytes()),
                'selection_policy':'Checked incoming animation teaching checkpoint; no image generated'}
    images = read(Path(project) / 'image_manifest.json')
    library = read(Path(project) / 'visual-library.json')
    wanted = set(re.findall(r'[a-z]{4,}', chapter['title'].lower() + ' ' + chapter['section_card']['image_selection_brief'].lower()))
    choices = []
    for index, asset in enumerate(library['assets']):
        selected = images.get(asset['id'])
        if not selected:
            continue
        description = ' '.join(str(asset.get(k, '')) for k in ['purpose', 'prompt'])
        words = set(re.findall(r'[a-z]{4,}', description.lower()))
        choices.append((len(wanted & words), -index, asset['id'], selected))
    if not choices:
        raise ValueError(f"No approved incoming-chapter image is available for {chapter['id']}")
    _, _, asset_id, selected = max(choices, key=lambda row: row[:2])
    if digest(Path(selected['file']).read_bytes()) != selected['sha256']:
        raise ValueError('Section-card image changed after approval')
    return {'asset_id': asset_id, **selected, 'selection_policy': 'chapter-brief keyword match against selected image metadata; earliest matching asset breaks ties'}


def filter_path(path):
    # FFmpeg filter escaping; shell execution is never used.
    return str(Path(path).resolve()).replace('\\', '\\\\').replace(':', '\\:').replace("'", "'\\''")


def make_card(asset, chapter, destination, width, height, fps, frames):
    destination.parent.mkdir(parents=True, exist_ok=True)
    font_path = ROOT / 'renderer/public/fonts/info-serif.ttf'
    font_size = max(28, min(76, round(width * .055)))
    title = chapter['section_card']['title']
    date = chapter['section_card']['date_label']
    for size in range(font_size, 23, -2):
        font = ImageFont.truetype(str(font_path), size)
        lines, line = [], ''
        for word in title.split():
            trial = (line + ' ' + word).strip()
            if font.getlength(trial) > width - 160 and line:
                lines.append(line); line = word
            else:
                line = trial
        lines.append(line)
        content = '\n'.join(lines) + ('\n\n' + date if date else '')
        if all(font.getlength(part) <= width - 160 for part in content.splitlines()) and len(content.splitlines()) * (size + 16) < height * .65:
            break
    else:
        raise ValueError('Section-card title cannot fit safely within the frame')
    text_path = destination.parent / 'card-text.txt'
    text_path.write_text(content)
    graph = (f'scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,'
             f'drawbox=x=0:y=0:w=iw:h=ih:color=black@0.50:t=fill,'
             f"drawtext=fontfile='{filter_path(font_path)}':textfile='{filter_path(text_path)}':"
             f'expansion=none:fontsize={size}:fontcolor=white:line_spacing=16:x=(w-text_w)/2:y=(h-text_h)/2,format=yuv420p')
    if asset.get('needs_review'):
        from .image_terminal import REVIEW_TAG
        tag_file = destination.parent / 'expert-review-tag.txt'
        tag_file.write_text(REVIEW_TAG)
        tag_font = ROOT / 'renderer/public/fonts/info-sans.ttf'
        tag_size = 26 if height > width else 23
        tag_width = math.ceil(ImageFont.truetype(str(tag_font), tag_size).getlength(REVIEW_TAG)) + 26
        tag_left = round(width * .08) if height > width else 96
        graph += (f',drawbox=x={tag_left}:y=12:w={tag_width}:h={tag_size+16}:color=0x411e06@0.9:t=fill,'
                  f"drawtext=fontfile='{filter_path(tag_font)}':textfile='{filter_path(tag_file)}':"
                  f'expansion=none:fontsize={tag_size}:fontcolor=white:x={tag_left+13}:y=18')
    ffmpeg(['-loop', '1', '-framerate', fps, '-i', asset['file'], '-vf', graph, '-frames:v', frames,
            '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '19', '-r', fps, '-video_track_timescale', 90000, destination])
    return {'text': content, 'font_size': size, 'native_overlay': True, 'asset_id': asset['asset_id'],
            'image_sha256': asset['sha256'], 'image_file': asset['file'], 'frames': frames,
            'text_bounds_checked': True, 'new_image_generated': False, 'needs_expert_review': bool(asset.get('needs_review'))}


def concat_file(paths, destination, durations=None):
    lines = ['ffconcat version 1.0']
    for index, path in enumerate(paths):
        escaped = str(Path(path).resolve()).replace("'", "'\\''")
        lines.append("file '" + escaped + "'")
        if durations:
            lines.append(f'duration {durations[index]:.9f}')
    destination.write_text('\n'.join(lines) + '\n')
    return destination


def audio_stems(records, intervals, destination, total_seconds, gap_seconds, crossfade, config=None):
    """Narration is never stretched. Only instrumental cues extend into transitions."""
    destination.mkdir(parents=True, exist_ok=True)
    levels = delivery_levels(config)
    inputs, graph, speech_parts, music_parts = [], [], [], []
    for index, (row, interval) in enumerate(zip(records, intervals)):
        root = Path(row['project']); target = row['duration']
        narration = read(root / f'film-{target}/audio-selection.json')
        if digest(Path(narration['audio']).read_bytes()) != narration['audio_info']['audio_sha256']:
            raise ValueError('Chapter narration changed after its audit')
        inputs += ['-i', narration['audio']]
        graph.append(f'[{index}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,'
                     f"atrim=duration={interval['seconds']:.9f},asetpts=PTS-STARTPTS[n{index}]")
        if index:
            graph.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={gap_seconds:.9f},asetpts=PTS-STARTPTS[s{index}]')
            speech_parts.append(f'[s{index}]')
        speech_parts.append(f'[n{index}]')
    graph.append(''.join(speech_parts) + f'concat=n={len(speech_parts)}:v=0:a=1[speech]')
    narration_path = destination / 'narration.wav'
    ffmpeg([*inputs, '-filter_complex', ';'.join(graph), '-map', '[speech]', '-ar', '48000', '-ac', '2', '-c:a', 'pcm_s16le', narration_path])
    inputs, graph = [], []
    cue_reports = []
    gains = []
    extend = (gap_seconds + crossfade) / 2
    for index, (row, interval) in enumerate(zip(records, intervals)):
        music = read(Path(row['project']) / f"film-{row['duration']}/music-selection.json")
        if music.get('engine') == 'none':
            continue
        score = music['score']
        gains.append(music.get('mix', {}).get('music_gain', .14))
        if digest(Path(score['audio']).read_bytes()) != score['audio_sha256']:
            raise ValueError('Chapter score changed after audit')
        start = max(0, interval['start_seconds'] - (extend if index else 0))
        end = min(total_seconds, interval['start_seconds'] + interval['seconds'] + (extend if index+1 < len(records) else 0))
        cue_seconds = end - start
        source_seconds = audio_info(score['audio'])['duration_seconds']
        ratio = source_seconds / cue_seconds
        if not .5 <= ratio <= 2:
            raise ValueError('Instrumental cue needs too much stretching for the chapter transitions')
        number = len(music_parts)
        inputs += ['-i', score['audio']]
        fade_in = min(crossfade if index else .5, cue_seconds / 4)
        fade_out = min(crossfade if index+1 < len(records) else .8, cue_seconds / 4)
        graph.append(f'[{number}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,atempo={ratio:.9f},'
            f'apad,atrim=duration={cue_seconds:.9f},asetpts=PTS-STARTPTS,afade=t=in:d={fade_in:.9f},'
            f'afade=t=out:st={cue_seconds-fade_out:.9f}:d={fade_out:.9f},adelay={round(start*48000)}S:all=1[m{number}]')
        music_parts.append(f'[m{number}]')
        cue_reports.append({'chapter': row['chapter']['id'], 'source_audio_sha256': score['audio_sha256'],
            'start_seconds': start, 'end_seconds': end, 'music_only_tempo_ratio': ratio,
            'fade_in_seconds': fade_in, 'fade_out_seconds': fade_out})
    score_path = destination / 'score.wav'
    master_path = destination / 'master.wav'
    if music_parts:
        graph.append(''.join(music_parts) + f'amix=inputs={len(music_parts)}:normalize=0:duration=longest,apad,atrim=duration={total_seconds:.9f}[score]')
        ffmpeg([*inputs, '-filter_complex', ';'.join(graph), '-map', '[score]', '-ar', '48000', '-ac', '2', '-c:a', 'pcm_s16le', score_path])
        gain = min([.14, *gains])
        info = mix_master(narration_path, score_path, master_path, total_seconds, gain=gain,
                          delivery_gain_db=levels['gain_db'])
        engine = 'acestep'
    else:
        info = level_master(narration_path, master_path, total_seconds, levels['gain_db'])
        engine = 'none'
    if abs(info['duration_seconds'] - total_seconds) > 2 / 48000:
        raise ValueError('Assembled audio changed the picture duration')
    if pcm_metrics(master_path)['clipped_sample_ratio']:
        raise ValueError('Assembled master contains clipping')
    report = {'engine': engine, 'master_audio': str(master_path), 'master_audio_info': info,
              'narration_audio': str(narration_path), 'speech_speed_changed': False, 'cue_crossfades': cue_reports,
              'delivery_levels': levels,
              'speech_audit': 'Reused chapter-level clean and mixed speech audits; no Whisper transcription of the entire musical master'}
    report['mix'] = {'music_gain': min([.14, *gains]), 'speech_ducking': bool(music_parts)}
    if music_parts:
        report['score'] = {'audio': str(score_path), **audio_info(score_path)}
    write(destination / 'music.json', report)
    return report


def assemble(config, store, plan, records, orientation, target):
    fps = config['fps']; width, height = (1080, 1920) if orientation == 'portrait' else (1920, 1080)
    # Tests can explicitly request a smaller checked canvas; production uses the usual delivery sizes.
    width, height = config.get('assembly_width', width), config.get('assembly_height', height)
    chapter_inputs = []
    for row in records:
        delivery = read(Path(row['project']) / 'delivery.json')
        output = next(o for o in delivery['outputs'] if o['orientation'] == orientation)
        video = Path(output['video']); root = video.parent
        qa = read(root / 'qa.json'); layout = read(root / 'layout/layout-results.json')
        if not qa.get('passed') or not layout.get('passed') or digest(video.read_bytes()) != qa['video_sha256']:
            raise ValueError('A chapter has not passed its checked-delivery gate')
        picture = probe_picture(video)
        if picture['width'] != width or picture['height'] != height or picture['fps'] != f'{fps}/1' or picture['codec'] != 'h264':
            raise ValueError('Chapter picture formats are incompatible')
        chapter_inputs.append({'output': output, 'root': root, 'qa': qa, 'picture': picture})
    gap_frames = max(1, round(max(plan['assembly']['section_card_seconds'], plan['assembly']['narration_pause_seconds'],
                                  plan['assembly']['music_crossfade_seconds']) * fps))
    gap_seconds = gap_frames / fps
    cards = {row['chapter']['id']: section_asset(row['project'], row['chapter']) for row in records[1:]}
    identity = digest({'chapters': [r['qa']['video_sha256'] for r in chapter_inputs], 'cards': cards,
                       'plan': plan, 'orientation': orientation, 'fps': fps, 'gap_frames': gap_frames,
                       'delivery_levels': delivery_levels(config), 'policy': 'separate-stems-native-cards-clock-v3'})
    root = store.root / 'videos' / f'{target}s-{orientation}' / identity[:16]
    root.mkdir(parents=True, exist_ok=True)
    final = root / 'video.mp4'
    if final.exists() and (root / 'qa.json').exists() and read(root / 'qa.json')['passed']:
        if digest(final.read_bytes()) != read(root / 'qa.json')['video_sha256']:
            raise ValueError('Assembled delivery changed after checks')
        return read(root / 'output.json')
    paths, durations, intervals, scenes, captions, cards_report = [], [], [], [], [], []
    cursor = 0
    for index, (row, chapter) in enumerate(zip(records, chapter_inputs)):
        if index:
            card = root / 'cards' / row['chapter']['id'] / 'card.mp4'
            report = make_card(cards[row['chapter']['id']], row['chapter'], card, width, height, fps, gap_frames)
            cards_report.append({'chapter': row['chapter']['id'], 'from': cursor, **report})
            paths.append(card); durations.append(gap_seconds)
            scenes.append({'id': 'card-' + row['chapter']['id'], 'kind': 'chapter-card', 'from': cursor, 'duration': gap_frames})
            cursor += gap_frames
        frames = chapter['picture']['frames']
        intervals.append({'chapter_id': row['chapter']['id'], 'from': cursor, 'frames': frames,
                          'start_seconds': cursor/fps, 'seconds': frames/fps})
        # The concat demuxer requires matching stream time bases. Remotion
        # chapters and native x264 cards can otherwise use different clocks,
        # yielding duplicate/backward decoded timestamps at every boundary.
        # Normalize containers by stream copy, preserving all encoded pictures.
        normalized = root / 'concat-inputs' / (row['chapter']['id'] + '.mp4')
        normalized.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg(['-i', chapter['output']['video'], '-map', '0:v:0', '-an', '-c:v', 'copy',
                '-video_track_timescale', 90000, normalized])
        paths.append(normalized); durations.append(frames/fps)
        data = read(chapter['root'] / 'timeline.json')['data']
        scenes.extend({**scene, 'id': row['chapter']['id'] + '-' + scene['id'], 'from': scene['from'] + cursor,
                       'source_ids': [row['chapter']['id'] + '_' + sid for sid in scene.get('source_ids', [])]} for scene in data['scenes'])
        captions.extend({**caption, 'start': caption['start'] + cursor/fps, 'end': caption['end'] + cursor/fps} for caption in data['captions'])
        cursor += frames
    total_seconds = cursor / fps
    listing = concat_file(paths, root / 'pictures.ffconcat', durations)
    picture_path = root / 'picture.mp4'
    ffmpeg(['-f', 'concat', '-safe', '0', '-i', listing, '-map', '0:v:0', '-an', '-c:v', 'copy', picture_path])
    if probe_picture(picture_path)['frames'] != cursor:
        raise ValueError('Chapter picture join lost or duplicated frames')
    music = audio_stems(records, intervals, root, total_seconds, gap_seconds,
                        plan['assembly']['music_crossfade_seconds'], config=config)
    write(root / 'music.json', music)
    write(root / 'source.json', {**music['master_audio_info'], 'audio_file': 'master.wav'})
    source_refs = [{'id': row['chapter']['id'] + f'_S{index}', 'url': source['url'], 'title': source.get('title') or source['url']}
        for row in records for index, source in enumerate(read(Path(row['project']) / 'sources.json')['results'], 1)]
    timeline = write(root / 'timeline.json', {'data': {'title': plan['film']['title'], 'subject': plan['film']['subject'],
        'width': width, 'height': height, 'fps': fps, 'durationInFrames': cursor, 'scenes': scenes, 'captions': captions, 'sources': source_refs}})
    write(root / 'assembly-report.json', {'identity': identity, 'chapters': intervals, 'section_cards': cards_report,
        'narration_pause_seconds': gap_seconds, 'music': music, 'narration_speed_changed': False,
        'remotion_rerendered': False, 'picture_join': 'Encoded chapter picture reused with native FFmpeg section cards',
        'concat_video_timescale': 90000})
    write(root / 'compile-report.json', {'identity': identity, 'actual_seconds': total_seconds, 'frames': cursor,
        'source_audio_sha256': audio_info(root / 'narration.wav')['audio_sha256'],
        'delivery_master_sha256': music['master_audio_info']['audio_sha256'], 'full_coverage': True})
    sys.path.insert(0, config['filmora_root'])
    from backend.mux import continuous_audio_command
    from backend.qa import run_qa
    final.unlink(missing_ok=True)
    subprocess.run(continuous_audio_command(picture_path, root / 'master.wav', final), check=True, capture_output=True)
    qa_path = root / 'qa.json'
    qa_path.unlink(missing_ok=True)
    package = write(root / 'qa-package.json', {'source': read(root / 'source.json')})
    qa_timeline = write(root / 'qa-timeline.json', {'width': width, 'height': height, 'fps': fps, 'durationInFrames': cursor})
    try:
        qa = run_qa(final, package, qa_timeline, root, qa_path)
    except SystemExit as exc:
        raise RuntimeError('Assembled video failed delivery QA; retain chapter outputs and inspect the assembly checks') from exc
    layout = root / 'layout'; layout.mkdir(exist_ok=True)
    first_layout = read(chapter_inputs[0]['root'] / 'layout/layout-results.json')
    poster = chapter_inputs[0]['root'] / 'layout' / first_layout['samples'][0]['file']
    shutil.copyfile(poster, layout / 'poster.png')
    write(layout / 'layout-results.json', {'passed': True, 'samples': [{'file': 'poster.png'}],
        'method': 'Reused checked chapter layouts; native card text bounds checked during assembly',
        'chapter_layouts': [str(c['root'] / 'layout/layout-results.json') for c in chapter_inputs]})
    write(root / 'render-result.json', {'video': str(final), 'qa_passed': qa['passed'], 'remotion_rendered': False})
    points = []
    for row in records:
        script = read(Path(row['project']) / f"film-{row['duration']}/script.json")
        points.extend({**p, 'id': row['chapter']['id'] + '_' + p['id'], 'chapter_id': row['chapter']['id'],
                       'fact_ids': [row['chapter']['id'] + '_' + fid for fid in p.get('fact_ids', [])]} for p in script['points'])
    film = store.root / f'film-{target}'
    script = {'title': plan['film']['title'], 'subject': plan['film']['subject'], 'points': points}
    narration_info = audio_info(root / 'narration.wav')
    write(film / 'script.json', script)
    (film / 'script.txt').write_text('\n\n'.join(p['narration'] for p in points) + '\n')
    write(film / 'audio-selection.json', {'audio': str(root / 'narration.wav'), 'actual_seconds': total_seconds,
        'point_count': len(points), 'script': script, 'audio_info': narration_info})
    write(film / 'music-selection.json', music)
    output = {'requested_seconds': target, 'actual_seconds': total_seconds, 'orientation': orientation,
        'point_count': len(points), 'music_engine': music['engine'], 'video': str(final), 'timeline': str(timeline),
        'qa': str(qa_path), 'chapters': len(records),
        'needs_expert_review_assets': sorted({row['chapter']['id'] + ':' + asset
            for row, child in zip(records, chapter_inputs) for asset in child['output'].get('needs_expert_review_assets', [])})}
    write(root / 'output.json', output)
    store.event('pipeline:assembly', 'orientation_succeeded', {'orientation': orientation, 'actual_seconds': total_seconds,
        'chapters': len(records), 'section_cards': len(cards_report), 'remotion_rerendered': False})
    return output
