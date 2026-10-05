"""Mechanical long-pause gate adapted from story_generator's gated deploy.

50 ms RMS windows at -40 dBFS flag pauses of four seconds or more. Keep
1.25 seconds of each flagged pause; copy all retained PCM samples verbatim.
This is a CPU edit before Whisper/MFA, never a speech speed adjustment.
"""
from __future__ import annotations

import array
import copy
import math
import sys
import wave
from pathlib import Path

from .core import digest, read, write

POLICY = 'long-pauses-pcm-v1'


def settings(config=None):
    config = config or {}
    value = {'policy': POLICY, 'enabled': config.get('vibe_silence_cleanup', True),
             'silence_db': config.get('silence_db', -40.0), 'window_ms': 50.0,
             'threshold_seconds': config.get('silence_max_pause_seconds', 4.0),
             'keep_seconds': config.get('silence_keep_pause_seconds', 1.25), 'min_cut_seconds': .25}
    if not isinstance(value['enabled'], bool):
        raise ValueError('vibe_silence_cleanup must be boolean')
    for key in ['silence_db', 'threshold_seconds', 'keep_seconds']:
        if isinstance(value[key], bool) or not isinstance(value[key], (int, float)) or not math.isfinite(value[key]):
            raise ValueError('Invalid silence setting: ' + key)
    if not -90 <= value['silence_db'] <= -20 or not 0 <= value['keep_seconds'] < value['threshold_seconds'] or value['threshold_seconds'] < 2:
        raise ValueError('Invalid silence detection thresholds')
    return value


def analyze(path, config=None):
    policy = settings(config)
    with wave.open(str(path), 'rb') as audio:
        if audio.getsampwidth() != 2 or audio.getcomptype() != 'NONE':
            raise ValueError('Silence gate requires PCM16 WAV')
        rate, channels, count = audio.getframerate(), audio.getnchannels(), audio.getnframes()
        window = max(1, round(rate * policy['window_ms'] / 1000))
        threshold = 32768 * 10 ** (policy['silence_db'] / 20)
        cursor, start, spans = 0, None, []
        while cursor < count:
            length = min(window, count - cursor)
            samples = array.array('h', audio.readframes(length))
            if sys.byteorder != 'little':
                samples.byteswap()
            rms = math.sqrt(sum(float(x)*x for x in samples) / max(1, len(samples)))
            if rms <= threshold:
                if start is None:
                    start = cursor
            elif start is not None:
                spans.append((start, cursor))
                start = None
            cursor += length
        if start is not None:
            spans.append((start, count))
    if spans == [(0, count)]:
        raise ValueError('Entire narration is silent; mechanical cleanup cannot recover speech')
    findings, cuts = [], []
    keep = round(policy['keep_seconds'] * rate)
    for start, end in spans:
        if (end-start)/rate < policy['threshold_seconds']:
            continue
        reason = ('leading_silence' if start/rate <= .15 else
                  'trailing_silence' if (count-end)/rate <= .15 else 'internal_pause')
        findings.append({'reason': reason, 'start': start/rate, 'end': end/rate, 'duration': (end-start)/rate})
        if not policy['enabled']:
            continue
        a, b = ((start, end-keep) if reason == 'leading_silence' else
                (start+keep, end) if reason == 'trailing_silence' else
                (start+keep//2, end-(keep-keep//2)))
        if b-a >= round(policy['min_cut_seconds']*rate):
            cuts.append([a, b])
    return {'settings': policy, 'source_sha256': digest(Path(path).read_bytes()),
            'sample_rate': rate, 'source_sample_count': count, 'channels': channels,
            'findings': findings, 'cuts_samples': cuts, 'removed_samples': sum(b-a for a,b in cuts),
            'removed_seconds': sum(b-a for a,b in cuts)/rate}


def cut_pcm(source, destination, report):
    """Apply the same time edits to narration or an existing scored master."""
    if Path(source).resolve() == Path(destination).resolve():
        raise ValueError('Preserve the source audio; choose a separate destination')
    with wave.open(str(source), 'rb') as audio:
        rate, count = audio.getframerate(), audio.getnframes()
        cuts = [[round(a*rate/report['sample_rate']), round(b*rate/report['sample_rate'])]
                for a,b in report['cuts_samples']]
        if any(a < 0 or b > count or a >= b for a,b in cuts):
            raise ValueError('Silence cuts exceed source audio')
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix('.pending.wav')
        with wave.open(str(temporary), 'wb') as output:
            output.setparams(audio.getparams())
            cursor = 0
            for a, b in cuts + [[count, count]]:
                audio.setpos(cursor)
                output.writeframes(audio.readframes(a-cursor))
                cursor = b
        temporary.replace(destination)
    return destination


def cleanup(path, config=None, store=None, name='silence-gate'):
    path = Path(path)
    report = analyze(path, config)
    if not report['cuts_samples']:
        return path, report
    root = path.parent / 'pause-cleanup' / digest(report)[:20]
    destination = root / 'narration.wav'
    saved = root / 'cleanup.json'
    if not (destination.exists() and saved.exists() and
            read(saved).get('output_sha256') == digest(destination.read_bytes())):
        cut_pcm(path, destination, report)
        remaining = analyze(destination, config)
        if remaining['findings']:
            raise ValueError('Mechanical pause cleanup left an excessive silence')
        report['output_sha256'] = digest(destination.read_bytes())
        report['output_sample_count'] = report['source_sample_count'] - report['removed_samples']
        write(saved, report)
    else:
        report = read(saved)
    if store:
        store.event(name, 'silence_compressed', {'source': str(path), 'audio': str(destination),
                    'pauses': len(report['cuts_samples']), 'removed_seconds': report['removed_seconds'],
                    'keep_seconds': report['settings']['keep_seconds']})
    return destination, report


def map_time(seconds, report):
    sample = seconds * report['sample_rate']
    return (sample - sum(max(0, min(sample, b)-a) for a,b in report['cuts_samples'] if sample > a)) / report['sample_rate']


def verify_pcm_edit(source, edited, report):
    """Prove the edited audio contains exactly the retained original samples."""
    with wave.open(str(source), 'rb') as original, wave.open(str(edited), 'rb') as result:
        if original.getparams()[:3] != result.getparams()[:3]:
            raise ValueError('PCM edit changed the audio format')
        rate = original.getframerate()
        cuts = [[round(a*rate/report['sample_rate']), round(b*rate/report['sample_rate'])]
                for a,b in report['cuts_samples']]
        cursor = 0
        for a,b in cuts + [[original.getnframes(), original.getnframes()]]:
            original.setpos(cursor)
            left = a-cursor
            while left:
                count = min(left, 65536)
                if original.readframes(count) != result.readframes(count):
                    raise ValueError('Retained speech/music samples changed during PCM edit')
                left -= count
            cursor = b
        if result.readframes(1):
            raise ValueError('PCM edit contains unexpected extra samples')
    return {'passed': True, 'method': 'byte-for-byte retained PCM comparison', 'removed_seconds': report['removed_seconds']}


def retime(timing, report, audio):
    """Keep MFA word IDs; require every cut to lie outside aligned words."""
    from .media import audio_info
    if timing['audio_sha256'] != report['source_sha256'] or timing['audio_info']['sample_rate'] != report['sample_rate']:
        raise ValueError('Silence map belongs to a different source narration')
    info = audio_info(audio)
    if info['sample_count'] != report['source_sample_count'] - report['removed_samples'] or info['sample_rate'] != report['sample_rate']:
        raise ValueError('Edited narration does not match the silence map')
    if report.get('output_sha256') and info['audio_sha256'] != report['output_sha256']:
        raise ValueError('Edited narration changed after silence cleanup')
    for word in timing['words']:
        for a,b in report['cuts_samples']:
            if word['start'] < b/report['sample_rate'] and word['end'] > a/report['sample_rate']:
                raise ValueError('Silence cut overlaps MFA word: ' + word['id'] + '; realign instead')
    result = copy.deepcopy(timing)
    for item in result['words'] + result['points']:
        for key in ['start', 'end']:
            item[key] = round(map_time(item[key], report), 6)
    result.update(audio=str(audio), audio_sha256=info['audio_sha256'], audio_info=info)
    result['provenance'] = {**result.get('provenance', {}), 'silence_cleanup': report,
                            'retiming': 'piecewise sample edits outside aligned words; original word IDs preserved',
                            'original_audio_sha256': timing['audio_sha256']}
    return result
