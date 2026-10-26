import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infofilm.pipeline import prepare_audio
from infofilm.core import Store


class NarrationFallbackTests(unittest.TestCase):
    def test_luxtts_failing_twice_goes_to_vibevoice(self):
        # User, 2026-10-26: LuxTTS said "a zic hat" for "A z k-hat" on both takes; VibeVoice gets the script next
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ref.wav").write_bytes(b"voice")
            store = Store(root / "project")
            config = {"tts_engine": "luxtts", "reference_audio": str(root / "ref.wav"), "language": "en",
                      "tts_url": "http://vibe", "speech_conditioning": False}
            script = {"points": [{"id": "P01", "narration": "A z k-hat."}]}
            info = {"duration_seconds": 3.0}
            audits = iter([{"status": "failed"}, {"status": "failed"}, {"status": "passed"}])
            with patch("infofilm.pipeline.make_script", return_value=script), \
                 patch("infofilm.pipeline.render_luxtts", return_value=(root / "lux.wav", info)) as lux, \
                 patch("infofilm.pipeline.render_vibevoice", return_value=(root / "vibe.wav", {**info, "provider": "VibeVoice"})) as vibe, \
                 patch("infofilm.pipeline.audit_audio", side_effect=lambda *a, **k: next(audits)):
                film = prepare_audio(config, store, None, {"facts": []}, 60, 150, False)
            self.assertEqual(lux.call_count, 2)
            self.assertEqual(vibe.call_count, 1)
            self.assertEqual(film["audio"], str(root / "vibe.wav"))
            self.assertEqual(config["tts_engine"], "vibevoice")             # timing and delivery follow the voice
            store.db.close()
