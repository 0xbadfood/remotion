import struct
import contextlib
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from infofilm.core import Store, digest, read, write
from infofilm.media import audio_info
from infofilm.silence import analyze, cleanup, cut_pcm, retime, verify_pcm_edit


def wav(path, parts, rate=1000, channels=1):
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
        audio.writeframes(b''.join(struct.pack('<h', amplitude)*round(seconds*rate)*channels
                                   for seconds, amplitude in parts))
    return path


class SilenceGateTests(unittest.TestCase):
    def test_long_pause_removal_preserves_all_retained_pcm_and_normal_pauses(self):
        with tempfile.TemporaryDirectory() as folder:
            source = wav(Path(folder)/'voice.wav', [(1, 2000), (5, 0), (1, 3000), (.7, 0), (1, 4000)])
            original = source.read_bytes()
            cleaned, report = cleanup(source)
            self.assertEqual(report['cuts_samples'], [[1625, 5375]])
            self.assertEqual(report['removed_seconds'], 3.75)
            with wave.open(str(source)) as audio:
                pcm = audio.readframes(audio.getnframes())
            with wave.open(str(cleaned)) as audio:
                actual = audio.readframes(audio.getnframes())
            self.assertEqual(actual, pcm[:1625*2] + pcm[5375*2:])
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue(verify_pcm_edit(source, cleaned, report)['passed'])
            self.assertEqual(analyze(cleaned)['findings'], [])
            self.assertEqual(cleanup(source)[0], cleaned)
            self.assertEqual(cleanup(cleaned)[0], cleaned)

    def test_leading_and_trailing_silence_retain_speech_adjacent_padding(self):
        with tempfile.TemporaryDirectory() as folder:
            source = wav(Path(folder)/'voice.wav', [(5, 0), (1, 2000), (6, 0)])
            cleaned, report = cleanup(source)
            self.assertEqual(report['cuts_samples'], [[0, 3750], [7250, 12000]])
            self.assertEqual(audio_info(cleaned)['sample_count'], 3500)
            self.assertEqual([x['reason'] for x in report['findings']], ['leading_silence', 'trailing_silence'])

    def test_retime_keeps_word_ids_and_rejects_cuts_overlapping_words(self):
        with tempfile.TemporaryDirectory() as folder:
            source = wav(Path(folder)/'voice.wav', [(1, 2000), (5, 0), (1, 2000)])
            cleaned, report = cleanup(source)
            info = audio_info(source)
            timing = {'audio_sha256': info['audio_sha256'], 'audio_info': info,
                      'words': [{'id': 'w1', 'word': 'hello', 'start': .1, 'end': .9},
                                {'id': 'w2', 'word': 'world', 'start': 6.1, 'end': 6.9}],
                      'points': [{'point_id': 'P1', 'start': .1, 'end': 6.9}]}
            result = retime(timing, report, cleaned)
            self.assertEqual(result['words'][1]['id'], 'w2')
            self.assertEqual(result['words'][1]['start'], 2.35)
            self.assertEqual(result['points'][0]['end'], 3.15)
            self.assertEqual(timing['words'][1]['start'], 6.1)
            timing['words'][0]['end'] = 3
            with self.assertRaisesRegex(ValueError, 'overlaps MFA'):
                retime(timing, report, cleaned)

    def test_scored_master_uses_same_time_cuts_at_another_sample_rate(self):
        with tempfile.TemporaryDirectory() as folder:
            source = wav(Path(folder)/'voice.wav', [(1, 2000), (5, 0), (1, 2000)])
            report = analyze(source)
            master = wav(Path(folder)/'music.wav', [(7, 500)], rate=2000, channels=2)
            edited = cut_pcm(master, Path(folder)/'edited.wav', report)
            self.assertEqual(audio_info(edited)['sample_count'], 6500)
            self.assertEqual(audio_info(edited)['duration_seconds'], 3.25)
            self.assertTrue(verify_pcm_edit(master, edited, report)['passed'])
            with self.assertRaisesRegex(ValueError, 'Preserve'):
                cut_pcm(source, source, report)

    def test_changed_retained_samples_cannot_inherit_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            source = wav(Path(folder)/'voice.wav', [(1, 2000), (5, 0), (1, 2000)])
            edited, report = cleanup(source)
            wav(edited, [(3.25, 1999)])
            with self.assertRaisesRegex(ValueError, 'samples changed'):
                verify_pcm_edit(source, edited, report)

    def test_silent_recording_rejected_and_disable_leaves_audio_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            path = wav(Path(folder)/'silent.wav', [(8, 0)])
            with self.assertRaisesRegex(ValueError, 'Entire narration'):
                cleanup(path)
            path = wav(path, [(1, 1000), (5, 0), (1, 1000)])
            cleaned, report = cleanup(path, {'vibe_silence_cleanup': False})
            self.assertEqual(cleaned, path)
            self.assertEqual(report['removed_samples'], 0)

    def test_vibe_audit_and_mfa_receive_cleaned_clip(self):
        from infofilm.vibe_media import _check_clip
        with tempfile.TemporaryDirectory() as folder:
            path = wav(Path(folder)/'voice.wav', [(1, 2000), (5, 0), (1, 2000)])
            seen = []
            def audit(config, store, audio, text, earlier_audits=None):
                seen.append(audio)
                return {'status': 'passed', 'score': 1}
            def align(config, store, audio, script, name):
                seen.append(audio)
                return {'audio_sha256': digest(audio.read_bytes())}
            with patch('infofilm.vibe_media.audit_audio', side_effect=audit), patch('infofilm.vibe_media.align_audio', side_effect=align):
                value = _check_clip({}, folder, {'id': 'P1', 'narration': 'Hello world'}, path, 'test')
            self.assertEqual(seen, [Path(value['audio']), Path(value['audio'])])
            self.assertNotEqual(seen[0], path)
            self.assertEqual(read(value['timing'])['audio_sha256'], audio_info(value['audio'])['audio_sha256'])

    def test_acceptable_clip_edges_do_not_create_a_long_pause_at_the_join(self):
        from infofilm.vibe_media import render_vibevoice, align_vibevoice
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            reference = wav(Path(folder)/'reference.wav', [(1, 1000)], rate=24000)
            config = {'reference_audio': str(reference), 'tts_url': 'test', 'language': 'en_indian'}
            points = [{'id': 'P1', 'narration': 'Hello'}, {'id': 'P2', 'narration': 'World'}]
            def batch(config, store, root, group, reference, attempt, index, name):
                for point in group:
                    clip = root/'points'/point['id']/f'take-{attempt}'/'narration.wav'
                    clip.parent.mkdir(parents=True, exist_ok=True)
                    wav(clip, [(1, 1000), (3, 0)] if point['id'] == 'P1' else [(3, 0), (1, 1000)], rate=24000)
                return {'status': 'success'}
            def check(config, project, point, clip, name):
                start = .1 if point['id'] == 'P1' else 3.1
                timing = write(clip.parent/'timing.json', {'audio_sha256': audio_info(clip)['audio_sha256'],
                    'text_sha256': digest(point['narration']), 'words': [{'id': 'w1', 'word': point['narration'],
                    'start': start, 'end': start+.8, 'mfa_word_ids': ['mfa1']}]})
                return {'audit': {'status': 'passed', 'score': 1}, 'timing': str(timing)}
            with patch('infofilm.vibe_media.vibe_service', return_value=contextlib.nullcontext()), \
                 patch('infofilm.vibe_media._batch', side_effect=batch), \
                 patch('infofilm.vibe_media._check_clip', side_effect=check):
                audio, info = render_vibevoice(config, store, points, 'test')
            # RMS scanning uses 50 ms windows; retain the boundary window that
            # contains the first speech samples after the joined pause.
            self.assertAlmostEqual(info['joined_silence_cleanup']['removed_seconds'], 4.9)
            self.assertEqual(analyze(audio)['findings'], [])
            timing = align_vibevoice(config, store, audio, {'points': points}, info, 'timing')
            self.assertEqual(timing['words'][1]['start'], 2.38)
            self.assertEqual(timing['audio_sha256'], info['audio_sha256'])
            self.assertEqual(timing['audio_info']['duration_seconds'], 3.28)
            store.db.close()
