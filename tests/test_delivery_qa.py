import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from infofilm.core import digest, write
from infofilm.delivery_qa import run_qa, spoken_window

RATE = 48000


def speech_with_pause(seconds, pause_from, pause_to):
    """Loud noise, as speech is to a correlation, with a dead pause beginning just before the window at 72%."""
    samples = np.random.default_rng(7).normal(0, 6000, seconds*RATE)
    samples[int(pause_from*RATE):int(pause_to*RATE)] = 0
    return samples


class SpokenWindow(unittest.TestCase):
    def test_a_window_in_a_pause_moves_on_to_speech(self):
        samples = speech_with_pause(100, 72.5, 76)
        start = spoken_window(samples, RATE, 72, 5)
        self.assertGreaterEqual(start, 76)
        self.assertLess(start, 77)

    def test_a_spoken_window_stays_put(self):
        self.assertEqual(spoken_window(speech_with_pause(100, 10, 12), RATE, 36, 5), 36)

    def test_a_film_with_pauses_everywhere_still_gets_its_best_window(self):
        samples = np.zeros(100*RATE)
        samples[80*RATE:82*RATE] = 4000
        self.assertLessEqual(spoken_window(samples, RATE, 72, 5), 82)


class SyncCheck(unittest.TestCase):
    def run_film(self, shift_seconds=0):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            samples = speech_with_pause(100, 72.5, 76).clip(-30000, 30000).astype('<i2')
            with wave.open(str(root/'master.wav'), 'wb') as f:
                f.setnchannels(1), f.setsampwidth(2), f.setframerate(RATE), f.writeframes(samples.tobytes())
            video = root/'video.mp4'
            delay = ['-af', f'adelay={int(shift_seconds*1000)}:all=1,atrim=0:100'] if shift_seconds else []
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=64x36:r=30:d=100',
                '-i', str(root/'master.wav'), *delay, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k',
                '-t', '100', str(video)], check=True)
            package = write(root/'package.json', {'source': {'audio_file': 'master.wav', 'sample_rate': RATE, 'duration_seconds': 100.,
                'audio_sha256': digest((root/'master.wav').read_bytes()), 'pcm_sha256': digest(samples.tobytes())}})
            timeline = write(root/'timeline.json', {'width': 64, 'height': 36, 'fps': 30, 'durationInFrames': 3000})
            return run_qa(video, package, timeline, root, root/'qa.json')

    def test_a_good_film_with_a_pause_in_the_window_passes(self):
        self.assertTrue(self.run_film()['checks']['audio_sync'])

    def test_audio_out_of_sync_still_fails(self):
        with self.assertRaises(SystemExit):
            self.run_film(shift_seconds=.1)
