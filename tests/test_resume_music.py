import copy
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from infofilm.core import Store, configuration, digest, read, write
from infofilm.media import audio_info
from infofilm.pipeline import create
from infofilm.planning import script_text
from infofilm.resume import music_checkpoint
from infofilm.video import replace_audio, video_stream_hash
from test_audio import wav


def checkpoint(project):
    outputs = []
    for duration in [60, 120]:
        audio = wav(project / f'audio/{duration}/voice.wav', frames=8000)
        info = audio_info(audio)
        script = {'title': 'Example', 'points': [{'id': 'P1', 'narration': 'An explanation.'}]}
        write(project / f'film-{duration}/audio-selection.json', {'script': script, 'audio': str(audio),
            'audio_info': info, 'requested_seconds': duration, 'actual_seconds': 1, 'point_count': 1})
        write(project / f'film-{duration}/timing.json', {'audio_sha256': info['audio_sha256'],
                                                     'text_sha256': digest(script_text(script))})
        for orientation in ['landscape', 'portrait']:
            root = project / 'videos' / f'{duration}s-{orientation}' / 'original'
            root.mkdir(parents=True)
            video = root / 'video.mp4'
            video.write_bytes(b'checked original picture ' + orientation.encode())
            write(root / 'qa.json', {'passed': True, 'video_sha256': digest(video.read_bytes())})
            write(root / 'compile-report.json', {'source_audio_sha256': info['audio_sha256'], 'actual_seconds': 1})
            write(root / 'layout/layout-results.json', {'passed': True})
            write(root / 'timeline.json', {'data': {'width': 1920, 'height': 1080}})
            outputs.append({'video': str(video), 'requested_seconds': duration, 'orientation': orientation})
    delivery = {'subject': 'Example', 'outputs': outputs, 'music_engine': 'none'}
    write(project / 'delivery.json', delivery)
    write(project / 'project.json', {'subject': 'Example', 'music_engine': 'none'})
    write(project / 'creative-brief.json', {'music': {'caption': 'Quiet instrumental.'}})
    write(project / 'config-snapshot.json', {})
    return delivery


class MusicResumeTests(unittest.TestCase):
    def test_completed_versions_reuse_every_upstream_stage_and_preserve_previous_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            original = checkpoint(project)
            preserved = {str(p): p.read_bytes() for p in project.glob('film-*/*.json')}
            args = SimpleNamespace(project=project, resume_from='music', music_engine='acestep', subject=None)
            def mix(config, store, film, brief, engine):
                return {'engine': engine, 'master_audio': film['audio'], 'master_audio_info': film['audio_info']}
            def remux(config, store, output, music):
                return Path(output['video']).parent.parent / 'revision/video.mp4'
            with patch('infofilm.resume.prepare_music', side_effect=mix) as music, \
                 patch('infofilm.resume.replace_audio', side_effect=remux) as mux, \
                 patch('infofilm.delivery.preview', return_value=project), \
                 patch('infofilm.pipeline.research', side_effect=AssertionError('No research')), \
                 patch('infofilm.pipeline.prepare_audio', side_effect=AssertionError('No narration')), \
                 patch('infofilm.pipeline.align_audio', side_effect=AssertionError('No MFA')), \
                 patch('infofilm.pipeline.generate_images', side_effect=AssertionError('No images')), \
                 patch('infofilm.pipeline.render', side_effect=AssertionError('No Remotion')):
                create(args)
            self.assertEqual(music.call_count, 2)
            self.assertEqual(mux.call_count, 4)
            self.assertEqual(read(project/'delivery.json')['music_engine'], 'acestep')
            self.assertEqual(read(next(project.glob('delivery-history/*/delivery.json'))), original)
            self.assertEqual({str(p): p.read_bytes() for p in project.glob('film-*/*.json')}, preserved)

    def test_changed_audio_script_or_delivery_fails_before_music_generation(self):
        for artifact in ['audio/60/voice.wav', 'videos/60s-landscape/original/video.mp4', 'film-60/timing.json']:
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                checkpoint(project)
                path = project / artifact
                if path.suffix == '.json':
                    write(path, {**read(path), 'text_sha256': 'wrong-script'})
                else:
                    path.write_bytes(b'changed')
                with patch('infofilm.resume.prepare_music', side_effect=AssertionError('Must preflight first')):
                    with self.assertRaises(ValueError):
                        create(SimpleNamespace(project=project, resume_from='music', music_engine='acestep'))

    def test_failure_keeps_previous_delivery_and_can_reuse_completed_music_on_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            original = checkpoint(project)
            with patch('infofilm.resume.prepare_music', side_effect=RuntimeError('Try another score')):
                with self.assertRaisesRegex(RuntimeError, 'Try another score'):
                    create(SimpleNamespace(project=project, resume_from='music', music_engine='acestep'))
            self.assertEqual(read(project / 'delivery.json'), original)
            self.assertEqual(music_checkpoint(project)[0], original)


class AudioReplacementTests(unittest.TestCase):
    def test_real_remux_keeps_encoded_picture_and_passes_master_sync_without_remotion(self):
        from infofilm.music import mix_master
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            store = Store(project)
            root = project / 'videos/60s-landscape/original'
            root.mkdir(parents=True)
            voice = root / 'narration.wav'
            score = project / 'music.wav'
            picture = root / 'picture.mp4'
            original = root / 'video.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                'anoisesrc=color=pink:seed=17:d=4:r=48000,volume=0.15', '-c:a', 'pcm_s16le', str(voice)], check=True)
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                'sine=frequency=100:duration=4:sample_rate=48000', '-c:a', 'pcm_s16le', str(score)], check=True)
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=30:duration=4',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-an', str(picture)], check=True)
            subprocess.run(['ffmpeg', '-v', 'error', '-i', str(picture), '-i', str(voice),
                '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', str(original)], check=True)
            info = audio_info(voice)
            old_hash = digest(original.read_bytes())
            write(root / 'qa.json', {'passed': True, 'video_sha256': old_hash})
            write(root / 'compile-report.json', {'source_audio_sha256': info['audio_sha256'], 'actual_seconds': 4})
            write(root / 'timeline.json', {'data': {'width': 160, 'height': 90, 'fps': 30, 'durationInFrames': 120}})
            write(root / 'layout/layout-results.json', {'passed': True, 'samples': []})
            master = project / 'master.wav'
            master_info = mix_master(voice, score, master, 4)
            selection = {'engine': 'acestep', 'master_audio': str(master), 'master_audio_info': master_info}
            output = {'video': str(original)}
            try:
                result = replace_audio(configuration(), store, output, selection)
                qa = read(result.parent / 'qa.json')
                self.assertTrue(qa['passed'])
                self.assertTrue(qa['checks']['audio_sync'])
                self.assertEqual(video_stream_hash(original), video_stream_hash(result))
                self.assertEqual(digest(original.read_bytes()), old_hash)
                self.assertEqual((result.parent / 'timeline.json').read_bytes(), (root / 'timeline.json').read_bytes())
                self.assertFalse(read(result.parent / 'render-result.json')['remotion_rendered'])
                with patch('infofilm.video.subprocess.run', side_effect=AssertionError('Reuse completed remux')):
                    self.assertEqual(replace_audio(configuration(), store, output, selection), result)
                altered = copy.deepcopy(selection)
                altered['master_audio_info']['duration_seconds'] = 5
                with self.assertRaisesRegex(ValueError, 'duration'):
                    replace_audio(configuration(), store, output, altered)
            finally:
                store.db.close()


if __name__ == '__main__':
    unittest.main()
