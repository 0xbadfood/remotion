"""Real CLI imports, verified local audio, CPU Manim/FFmpeg, and cached resume.

The planner is fake and every pixel/audio sample is a declared test fixture.
The workflow's audio validation, asset importer, renderer, music-none delivery,
and final video joins remain real. This test never calls a model or GPU service.
"""
import array
import contextlib
import copy
import io
import json
import math
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

from PIL import Image, ImageDraw

from infofilm.core import ROOT, digest, read, write
from infofilm.image_animation_workflow import Workflow as RealWorkflow, allocate_canvas_frames, main
from infofilm.image_animation_controllers import compile_scene, compile_timeline
from test_image_animation_workflow import FakeModels


MANIM_PYTHON = ROOT / '.venv-manim-pilot/bin/python'
RUNTIME_READY = MANIM_PYTHON.is_file() and shutil.which('ffmpeg') and shutil.which('ffprobe')


class CpuFixtureModels(FakeModels):
    """Replace decisions only; retain the real workflow and all local compilers."""
    calls = []
    chapters = 1
    unknown = False

    def for_activity(self, activity):
        if activity not in {'canvas', 'grounding', 'motion'}:
            return super().for_activity(activity)
        parent = self

        class Facade:
            fingerprint = activity + parent.fingerprint

            def call_json(self, role, name, prompt, **kwargs):
                parent.calls.append((activity, role, name, kwargs))
                if role == 'reviewer':
                    return {'pass': True, 'score': 95, 'issues': [], 'strengths': ['Explicit local test fixture']}
                context = json.JSONDecoder().raw_decode(prompt.split('\nINPUT JSON:\n', 1)[1])[0]
                if activity == 'canvas':
                    return {'schema_version': 'infofilm.image-animation-canvases.v1', 'canvases': [{
                        'id': 'K01', 'beat_ids': ['B01'], 'visual_message': 'The radius stays constant.',
                        'controller_type': 'orbit_path', 'prompt': 'One blue circular guide and one orange ball.',
                        'component_roles': [{'role': 'orbiter', 'description': 'The complete orange ball',
                                             'required_landmarks': ['centre_px']}]}]}
                if activity == 'grounding':
                    width, height = context['actual_source_dimensions']
                    center = [width/2, height/2]
                    radius = min(width, height)*.2734375
                    start = [center[0]+radius, center[1]]
                    bounds = [start[0]-16, start[1]-16, start[0]+16, start[1]+16]
                    landmarks = {'centre_px': start}
                    return {'schema_version': 'infofilm.image-animation-grounding.v1', 'canvas_id': 'K01',
                        'background_prompt': 'Preserve the circular guide and remove the orange ball.',
                        'parts': [{'id': 'ball', 'role': 'orbiter',
                            'prompt': 'Complete orange ball at its original source placement on native RGBA canvas.',
                            'bbox_xyxy_px': bounds, 'landmarks': landmarks, 'z_index': 5}],
                        'scene': {'schema_version': 'infofilm.image-animation-scene.v1',
                            'canvas': {'width': width, 'height': height},
                            'assets': {'background': {'path': 'background'}, 'ball': {'path': 'ball',
                                'bbox_xyxy_px': copy.deepcopy(bounds), 'landmarks': copy.deepcopy(landmarks), 'z_index': 5}},
                            'controller': {'type': 'orbit_path', 'parameters': {'center_px': center,
                                'start_px': start, 'radius_px': radius, 'direction': 'counterclockwise'},
                                'bindings': {'orbiter': 'ball'}}, 'incomplete_flags': [],
                            'provenance': {'source': 'PIL test fixture with exact source landmarks'}},
                        'uncertainties': [{'kind': 'caveat', 'description': 'Audio is a synthetic integrity-test tone'}]}
                if activity == 'motion':
                    return {'schema_version': 'infofilm.image-animation-cues.v1', 'beat_id': 'B01', 'actions': [{
                        'id': 'orbit', 'kind': 'orbit', 'start_word_id': 'w1', 'end_word_id': 'w4',
                        'value_start': 0, 'value_end': 360, 'label': 'Constant radius',
                        'purpose': 'Demonstrate a complete orbit during the supplied word span'}]}
                raise AssertionError(activity)

        return Facade()


def verified_audio_fixture(directory):
    """Hash-bound synthetic PCM and exact transcript map, explicitly not real MFA."""
    directory.mkdir(parents=True, exist_ok=True)
    audio = directory / 'narration.wav'
    sample_rate, duration = 24000, 2
    samples = array.array('h')
    for index in range(sample_rate*duration):
        t = index/sample_rate
        envelope = min(1, t/.04, (duration-t)/.04)
        samples.append(round(2200*envelope*math.sin(2*math.pi*330*t)))
    with wave.open(str(audio), 'wb') as handle:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())
    transcript = 'The radius stays constant.'
    words = [{'id': 'w'+str(i+1), 'word': word, 'start': i*.4+.1, 'end': i*.4+.35}
             for i, word in enumerate(transcript.split())]
    timing = write(directory / 'timing.json', {'audio_sha256': digest(audio.read_bytes()),
        'duration_seconds': duration, 'words': words, 'provenance': 'Synthetic test fixture word map'})
    return write(directory / 'narration-input.json', {'schema_version': 'infofilm.animation-narration.v1',
        'transcript': transcript, 'transcript_sha256': digest(transcript), 'audio': str(audio.resolve()),
        'audio_sha256': digest(audio.read_bytes()), 'duration_seconds': duration,
        'timing': str(timing.resolve()), 'timing_sha256': digest(timing.read_bytes()), 'words': words,
        'provenance': {'engine': 'local sine-wave test fixture', 'alignment': 'synthetic exact word map; no MFA invoked'}})


def import_pil_fixtures(directory):
    """Fill only the exact immutable requests emitted by the real CLI importer."""
    template = read(directory / 'asset-import-template.json')
    for entry in template['assets']:
        identity = entry['id']
        image = Image.new('RGBA', (512, 512), (0, 0, 0, 0) if identity == 'ball' else (238, 246, 250, 255))
        draw = ImageDraw.Draw(image)
        if identity in {'canvas', 'background'}:
            draw.ellipse((116, 116, 396, 396), outline=(40, 120, 210, 255), width=4)
            draw.ellipse((252, 252, 260, 260), fill=(40, 120, 210, 255))
        if identity in {'canvas', 'ball'}:
            draw.ellipse((380, 240, 412, 272), fill=(245, 131, 42, 255), outline=(180, 69, 15, 255), width=2)
        image.save(directory / entry['file'])
        entry['sha256'] = digest((directory / entry['file']).read_bytes())
        entry['provenance'] = {'source': 'Hand-declared PIL CPU test fixture', 'generated_by_model': False}
    write(directory / 'asset-import.json', template)


def probe(path):
    return json.loads(subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-show_streams', '-show_format',
        '-of', 'json', str(path)], capture_output=True, text=True, check=True).stdout)


def run_cpu_cli_smoke(directory):
    """Reusable for a temporary test or a persistent, inspectable smoke artifact."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    sources = write(directory / 'sources.json', {'results': [{'url': 'https://institution.test/circle',
        'raw_content': 'A circular orbit preserves the distance from the moving ball to its centre. ' * 20}]})
    config = write(directory / 'config.json', {'planner_url': 'http://unused.test/planner',
        'reviewer_url': 'http://unused.test/reviewer', 'tts_url': 'http://unused.test/tts',
        'image_url': 'http://unused.test/images', 'omni_url': 'http://unused.test/omni',
        'image_steps': 25, 'delivery_gain_db': 0})
    narration = verified_audio_fixture(directory / 'input-audio')
    source_audio = Path(read(narration)['audio'])
    source_sha = digest(source_audio.read_bytes())
    run = directory / 'run'
    argv = ['--subject', 'Circular motion', '--sources', str(sources), '--narration-input', str(narration),
        '--no-subject-graph', '--output', str(run), '--config', str(config), '--canvas-width', '512',
        '--canvas-height', '512', '--width', '320', '--height', '180', '--fps', '10', '--review', 'on',
        '--music', 'none', '--manim-python', str(MANIM_PYTHON)]
    if (run / 'inputs.json').exists():
        argv.append('--resume')
    CpuFixtureModels.calls = []
    result = main(argv)
    waiting_states = []
    for _ in range(3):
        if result == 0:
            break
        state = read(run / 'state.json')
        chapter = state['chapters'][0]
        if chapter['state'] != 'waiting_execution':
            raise AssertionError('Unexpected CLI stop: ' + json.dumps(chapter))
        waiting_states.append(chapter['state'])
        import_pil_fixtures(Path(chapter['details']['import_template']).parent)
        result = main(['--output', str(run), '--resume', '--config', str(config), '--manim-python', str(MANIM_PYTHON)])
    if result != 0:
        raise AssertionError('CPU CLI did not finish: ' + json.dumps(read(run / 'state.json')))
    metric_path = run / 'chapters/C01/canvases/K01/render/landscape/clips/B01/metrics.json'
    metrics = read(metric_path)
    clip = Path(metrics['video'])
    clip_pin = {'sha256': digest(clip.read_bytes()), 'mtime_ns': clip.stat().st_mtime_ns,
                'metrics_mtime_ns': metric_path.stat().st_mtime_ns}
    model_calls = len(CpuFixtureModels.calls)
    resume_result = main(['--output', str(run), '--resume', '--config', str(config), '--manim-python', str(MANIM_PYTHON)])
    if resume_result != 0 or len(CpuFixtureModels.calls) != model_calls:
        raise AssertionError('Completed resume unexpectedly replanned or failed')
    cached = clip_pin == {'sha256': digest(clip.read_bytes()), 'mtime_ns': clip.stat().st_mtime_ns,
        'metrics_mtime_ns': metric_path.stat().st_mtime_ns}
    video = Path(read(run / 'delivery.json')['outputs'][0]['video'])
    observed = probe(video)
    streams = observed['streams']
    videos = [s for s in streams if s['codec_type'] == 'video']
    audios = [s for s in streams if s['codec_type'] == 'audio']
    subtitles = [s for s in streams if s['codec_type'] == 'subtitle']
    chapter = run / 'chapters/C01'
    audio_checkpoint = read(chapter / 'checkpoints/audio.json')
    source_pins_match = audio_checkpoint['files'] == [{'path': str(source_audio), 'sha256': source_sha},
        {'path': read(narration)['timing'], 'sha256': read(narration)['timing_sha256']}]
    with sqlite3.connect(run / 'jobs.sqlite3') as connection:
        stages = {r[0] for r in connection.execute('SELECT DISTINCT stage FROM events')}
    layer_paths = list((chapter / 'canvases/K01').glob('layers-*/asset-manifest.json'))
    if len(layer_paths) != 1:
        raise AssertionError('Expected one completed scoped layer manifest')
    layer_manifest = read(layer_paths[0])
    ball = layer_manifest['assets']['ball']
    original_ball = Path(ball['file'])
    native_rgba = ball['mode'] == 'RGBA' and ball['alpha']['channel_present'] and ball['alpha']['range'] == [0, 255]
    native_rgba = native_rgba and ball['alpha']['transparent_pixels'] > 0 and ball['alpha']['visible_pixels'] > 0
    decoded = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(video), '-map', '0:a:0', '-ac', '1', '-ar', '24000',
        '-f', 's16le', 'pipe:1'], capture_output=True, check=True).stdout
    decoded_samples = array.array('h'); decoded_samples.frombytes(decoded)
    decoded_rms = math.sqrt(sum(float(s)**2 for s in decoded_samples)/max(1, len(decoded_samples)))
    report = {'schema_version': 'infofilm.image-animation-cli-cpu-smoke.v1', 'video': str(video),
        'source_audio': str(source_audio), 'source_audio_sha256': source_sha,
        'planner': 'fake fixture', 'reviewer': 'fake reviewer supplied real rendered PNG frames',
        'renderer': 'real CPU Manim/Cairo', 'probe': observed,
        'checks': {'video_audio_present': len(videos) == len(audios) == 1,
            'transcript_subtitle_track_present': len(subtitles) == 1 and subtitles[0]['codec_name'] == 'mov_text',
            'subtitles_match_supplied_words': 'The radius stays constant.' in (chapter / 'narration.srt').read_text(),
            'resolution_correct': bool(videos) and [videos[0]['width'], videos[0]['height']] == [320, 180],
            'frame_count_correct': bool(videos) and int(videos[0]['nb_read_frames']) == 20,
            'fps_correct': bool(videos) and videos[0]['avg_frame_rate'] == '10/1',
            'duration_correct': abs(float(observed['format']['duration'])-2) <= .11,
            'original_audio_unchanged': digest(source_audio.read_bytes()) == source_sha,
            'delivery_audio_audible': decoded_rms > 100,
            'audio_checkpoint_pins_valid': source_pins_match, 'rendered_clips_reused': cached,
            'true_rgba_part_imported': native_rgba,
            'imported_part_bytes_preserved': digest(original_ball.read_bytes()) == ball['sha256'],
            'no_replanning_on_resume': len(CpuFixtureModels.calls) == model_calls,
            'music_none_delivered': read(chapter / 'music.json')['engine'] == 'none',
            'teaching_narration_audio_canvas_motion_stages_executed': all('image-animation:'+stage in stages
                for stage in ('teaching', 'narration', 'audio', 'canvas')) and
                any(stage.startswith('image-animation:motion-B01-') for stage in stages),
            'actual_rendered_frames_reviewed': any(stage.startswith('image-animation:rendered-frame-review') for stage in stages)},
        'asset_waits': waiting_states, 'model_fixture_calls': model_calls, 'delivery_audio_rms_pcm16': decoded_rms}
    write(directory / 'smoke-report.json', report)
    return report


@unittest.skipUnless(RUNTIME_READY, 'CPU smoke requires .venv-manim-pilot, ffmpeg and ffprobe')
class CliCpuRenderTests(unittest.TestCase):
    def test_real_cli_audio_asset_import_render_music_none_final_join_and_resume(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.ExitStack() as stack:
            # A call to any service or cooperative GPU lease fails this test.
            stack.enter_context(patch('infofilm.media.request', side_effect=AssertionError('No provider HTTP in CPU smoke')))
            stack.enter_context(patch('infofilm.image_animation_assets.gpu_lease', side_effect=AssertionError('No GPU in CPU smoke')))
            stack.enter_context(patch('httpx.Client', side_effect=AssertionError('No LLM/network in CPU smoke')))
            stack.enter_context(patch('infofilm.image_animation_workflow.Workflow',
                side_effect=lambda args: RealWorkflow(args, models_factory=CpuFixtureModels)))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            report = run_cpu_cli_smoke(Path(folder))
            self.assertTrue(all(report['checks'].values()), report)
            self.assertEqual(report['asset_waits'], ['waiting_execution', 'waiting_execution'])


class CumulativeFrameAllocationTests(unittest.TestCase):
    def test_three_fractional_canvas_windows_share_four_frames_without_altering_source_map(self):
        controller = compile_scene({'schema_version': 'infofilm.image-animation-scene.v1',
            'canvas': {'width': 512, 'height': 512}, 'assets': {'background': {'path': 'background.png'}},
            'controller': {'type': 'static_canvas', 'parameters': {}, 'bindings': {}}, 'incomplete_flags': []})
        source = {'schema_version': 'infofilm.image-animation-timing.v1', 'duration_seconds': .15,
            'scenes': [{'id': 'B01', 'start': 0, 'end': .15, 'actions': [{'id': 'label', 'kind': 'label',
                'start': .025, 'end': .125, 'label': 'A source-word cue', 'word_ids': ['w1', 'w2']}]}],
            'narration_provenance': {'audio_sha256': 'immutable-audio', 'timing_sha256': 'immutable-MFA'}}
        original = copy.deepcopy(source)
        ranges, frames = [], []
        for start, end in ((0, .15), (.15, .30), (.30, .45)):
            allocated = allocate_canvas_frames(source, start, end, 10)
            compiled = compile_timeline(allocated, controller, fps=10)
            allocation = allocated['frame_allocation']
            ranges.append((allocation['chapter_start_frame'], allocation['chapter_end_frame']))
            frames.append(compiled['frames'])
            self.assertEqual(allocation['source_mfa_start_seconds'], start)
            self.assertEqual(allocation['source_mfa_end_seconds'], end)
            self.assertEqual(allocated['narration_provenance'], original['narration_provenance'])
            self.assertEqual(allocated['scenes'][0]['actions'][0]['word_ids'], ['w1', 'w2'])
            self.assertLessEqual(abs(allocated['duration_seconds']-(end-start)), .1)
        self.assertEqual(ranges, [(0, 2), (2, 3), (3, 4)])
        self.assertEqual(frames, [2, 1, 1])
        self.assertEqual(sum(frames), round(.45*10))
        self.assertNotEqual(sum(frames), 3*round(.15*10))
        self.assertEqual(source, original)


if __name__ == '__main__':
    unittest.main()
