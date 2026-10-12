"""Verify final stream inheritance and exact frame ownership without providers."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from infofilm.core import read
from infofilm.image_animation_workflow import join_videos


def probe(frames=6, fps=12, *, audio=False, subtitles=False):
    streams = [{'codec_type': 'video', 'nb_read_frames': str(frames),
                'avg_frame_rate': f'{fps}/1', 'duration': str(frames/fps)}]
    if audio:
        streams.append({'codec_type': 'audio'})
    if subtitles:
        streams.append({'codec_type': 'subtitle'})
    return {'streams': streams, 'format': {'duration': str(frames/fps)}}


class JoinValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = [self.root / 'one.mp4', self.root / 'two.mp4']
        for path in self.sources:
            path.write_bytes(b'input video')
        self.output = self.root / 'delivery' / 'film.mp4'
        self.commands = []

    def run_join(self, sources, result, **kwargs):
        values = {str(path): value for path, value in zip(self.sources, sources)}
        values[str(self.output)] = result

        def run(command, **options):
            self.commands.append(command)
            if command[0] == 'ffprobe':
                return subprocess.CompletedProcess(command, 0, json.dumps(values[command[-1]]))
            if command[0] == 'ffmpeg':
                Path(command[-1]).write_bytes(b'joined video')
                return subprocess.CompletedProcess(command, 0)
            raise AssertionError(command)

        with patch('infofilm.image_animation_workflow.subprocess.run', side_effect=run):
            return join_videos(self.sources, self.output, **kwargs)

    def test_silent_join_reports_no_audio_and_derives_parent_frame_clock(self):
        joined = self.run_join([probe(), probe()], probe(12))
        self.assertFalse(joined['audio_present'])
        self.assertFalse(joined['subtitles_present'])
        self.assertTrue(all(joined['checks'].values()))
        clock = read(self.output.parent / 'concat.json')
        self.assertEqual(clock['expected_frames'], 12)
        self.assertEqual(clock['fps'], 12)
        self.assertFalse(read(self.output.parent / 'join-checks.json')['streams']['audio_present'])

    def test_missing_inherited_audio_fails_and_preserves_truthful_diagnostics(self):
        with self.assertRaisesRegex(ValueError, 'clock checks'):
            self.run_join([probe(audio=True), probe(audio=True)], probe(12))
        report = read(self.output.parent / 'join-checks.json')
        self.assertFalse(report['passed'])
        self.assertFalse(report['checks']['audio_presence_matches_inputs'])
        self.assertFalse(report['streams']['audio_present'])
        self.assertTrue(report['streams']['expected_audio'])

    def test_missing_inherited_subtitles_fails(self):
        with self.assertRaisesRegex(ValueError, 'clock checks'):
            self.run_join([probe(subtitles=True), probe(subtitles=True)], probe(12))
        self.assertFalse(read(self.output.parent / 'join-checks.json')['checks']['subtitle_presence_matches_inputs'])

    def test_parent_join_rejects_a_lost_frame_without_explicit_clock_arguments(self):
        with self.assertRaisesRegex(ValueError, 'clock checks'):
            self.run_join([probe(), probe()], probe(11))
        report = read(self.output.parent / 'join-checks.json')
        self.assertFalse(report['checks']['complete_narration_frames'])

    def test_wrong_explicit_clock_stops_before_mux(self):
        with self.assertRaisesRegex(ValueError, 'input frames differ'):
            self.run_join([probe(), probe()], probe(12), expected_frames=13, fps=12)
        self.assertFalse(any(command[0] == 'ffmpeg' for command in self.commands))

    def test_supplied_subtitles_are_muxed_without_supplied_audio(self):
        subtitles = self.root / 'captions.srt'
        subtitles.write_text('1\n00:00:00,000 --> 00:00:01,000\nA caption.\n')
        joined = self.run_join([probe(), probe()], probe(12, subtitles=True), subtitles=subtitles)
        command = next(command for command in self.commands if command[0] == 'ffmpeg')
        self.assertIn('1:s:0', command)
        self.assertFalse(joined['audio_present'])
        self.assertTrue(joined['subtitles_present'])

    def test_replaced_audio_preserves_inherited_subtitles(self):
        audio = self.root / 'narration.wav'
        audio.write_bytes(b'narration')
        joined = self.run_join([probe(subtitles=True), probe(subtitles=True)],
                               probe(12, audio=True, subtitles=True), audio=audio)
        command = next(command for command in self.commands if command[0] == 'ffmpeg')
        self.assertIn('1:a:0', command)
        self.assertIn('0:s:0', command)
        self.assertTrue(joined['audio_present'])
        self.assertTrue(joined['subtitles_present'])

    def test_mixed_inherited_streams_and_output_rate_change_fail(self):
        with self.assertRaisesRegex(ValueError, 'inconsistent inherited audio'):
            self.run_join([probe(audio=True), probe()], probe(12))
        with self.assertRaisesRegex(ValueError, 'clock checks'):
            self.run_join([probe(), probe()], probe(12, fps=24))
        self.assertFalse(read(self.output.parent / 'join-checks.json')['checks']['frame_rate_matches_clock'])

    def test_single_input_passthrough_preserves_bytes_and_same_path_is_safe(self):
        source = self.sources[0]
        original = source.read_bytes()
        report = probe(audio=True, subtitles=True)
        with patch('infofilm.image_animation_workflow.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 0, json.dumps(report))) as run:
            joined = join_videos([source], self.output)
            self.assertEqual(self.output.read_bytes(), original)
            self.assertEqual(joined['join_strategy'], 'single_input_copy')
            self.assertTrue(joined['audio_present'])
            self.assertTrue(joined['subtitles_present'])
            self.assertTrue(all(joined['checks'].values()))
            self.assertEqual(read(self.output.parent / 'concat.json')['strategy'], 'single_input_copy')
            same = join_videos([source], source)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(same['join_strategy'], 'single_input_copy')
        self.assertTrue(all(call.args[0][0] == 'ffprobe' for call in run.call_args_list))

    def test_single_input_with_replacement_audio_uses_mux(self):
        audio = self.root / 'narration.wav'
        audio.write_bytes(b'replacement narration')
        report = probe(audio=True)
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            if command[0] == 'ffprobe':
                return subprocess.CompletedProcess(command, 0, json.dumps(report))
            self.output.parent.mkdir(parents=True, exist_ok=True)
            self.output.write_bytes(b'muxed replacement')
            return subprocess.CompletedProcess(command, 0)

        with patch('infofilm.image_animation_workflow.subprocess.run', side_effect=run):
            joined = join_videos([self.sources[0]], self.output, audio=audio)
        self.assertEqual(joined['join_strategy'], 'ffmpeg_stream_copy')
        self.assertTrue(any(command[0] == 'ffmpeg' for command in commands))


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg/ffprobe unavailable')
class RealJoinSmokeTests(unittest.TestCase):
    def test_stream_copy_mux_and_parent_join_preserve_audio_subtitles_and_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            videos = []
            for colour in ('red', 'blue'):
                path = root / (colour + '.mp4')
                subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 'lavfi', '-i',
                    f'color=c={colour}:s=160x90:r=12:d=0.5', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)], check=True)
                videos.append(path)
            audio = root / 'narration.wav'
            subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 'lavfi', '-i',
                            'sine=frequency=440:sample_rate=48000:duration=1', str(audio)], check=True)
            subtitles = root / 'captions.srt'
            subtitles.write_text('1\n00:00:00,000 --> 00:00:01,000\nA verified caption.\n')
            chapter = join_videos(videos, root / 'chapter' / 'film.mp4', audio=audio,
                                  subtitles=subtitles, expected_frames=12, fps=12)
            self.assertTrue(chapter['audio_present'])
            self.assertTrue(chapter['subtitles_present'])
            film = join_videos([Path(chapter['video'])], root / 'parent' / 'film.mp4')
            self.assertTrue(film['audio_present'])
            self.assertTrue(film['subtitles_present'])
            self.assertTrue(all(film['checks'].values()))
            self.assertEqual(Path(film['video']).read_bytes(), Path(chapter['video']).read_bytes())
            self.assertEqual(film['duration_seconds'], chapter['duration_seconds'])
            self.assertEqual(read(root / 'parent' / 'concat.json')['expected_frames'], 12)


if __name__ == '__main__':
    unittest.main()
