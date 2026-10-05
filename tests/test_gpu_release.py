import subprocess
import unittest
from unittest.mock import Mock, patch

from infofilm.gpu_release import release_vibevoice


class VibeReleaseTests(unittest.TestCase):
    config = {"tts_url": "http://127.0.0.1:8092"}

    def test_null_model_with_large_allocations_recycles_only_idle_vibe(self):
        request = Mock(return_value={"model_loaded": None, "active_job": False})
        with patch("infofilm.gpu_release._pid", side_effect=[123, 456]), \
             patch("infofilm.gpu_release._memory", side_effect=[18142, 0, 256]), \
             patch("infofilm.gpu_release.subprocess.run") as command:
            result = release_vibevoice(self.config, request_fn=request)
        self.assertTrue(result["gpu_released"])
        self.assertTrue(result["recycled"])
        self.assertEqual(result["used_mib"], 256)
        self.assertEqual([c.args[0] for c in command.call_args_list], [
            ["systemctl", "--no-ask-password", "stop", "vibevoice-api-local.service"],
            ["systemctl", "--no-ask-password", "start", "vibevoice-api-local.service"]])
        self.assertTrue(any(c.args[3] == "/unload" for c in request.call_args_list))

    def test_small_cuda_context_needs_no_service_switch(self):
        request = Mock(return_value={"model_loaded": None})
        with patch("infofilm.gpu_release._pid", return_value=123), \
             patch("infofilm.gpu_release._memory", return_value=256), \
             patch("infofilm.gpu_release.subprocess.run") as command:
            result = release_vibevoice(self.config, request_fn=request)
        command.assert_not_called()
        self.assertFalse(result["recycled"])

    def test_busy_or_newly_busy_provider_is_never_stopped(self):
        for responses in [[{"active_job": True}],
                          [{"model_loaded": None}, {"model_loaded": None}, {"queued_or_running_jobs": 1}]]:
            with self.subTest(responses=responses), \
                 patch("infofilm.gpu_release._pid", return_value=123), \
                 patch("infofilm.gpu_release._memory", return_value=18142), \
                 patch("infofilm.gpu_release.subprocess.run") as command:
                with self.assertRaisesRegex(RuntimeError, "active work"):
                    release_vibevoice(self.config, request_fn=Mock(side_effect=responses))
                command.assert_not_called()

    def test_missing_downloads_defer_recycle(self):
        with patch("infofilm.gpu_release._pid", return_value=123), \
             patch("infofilm.gpu_release._memory", return_value=18142), \
             patch("infofilm.gpu_release.subprocess.run") as command:
            result = release_vibevoice(self.config, request_fn=Mock(return_value={"model_loaded": None}),
                                      allow_restart=False)
        self.assertFalse(result["gpu_released"])
        command.assert_not_called()

    def test_remote_provider_does_not_control_local_unit(self):
        request = Mock(side_effect=[{"model_loaded": "en"}, {"model_loaded": None}])
        with patch("infofilm.gpu_release.subprocess.run", side_effect=AssertionError("No local service control")):
            result = release_vibevoice({"tts_url": "http://remote:8092"}, request_fn=request)
        self.assertEqual(result["verification"], "provider_api")

    def test_cannot_measure_memory_blocks_handoff(self):
        with patch("infofilm.gpu_release._pid", return_value=123), \
             patch("infofilm.gpu_release._memory", side_effect=RuntimeError("Cannot verify")), \
             patch("infofilm.gpu_release.subprocess.run") as command:
            with self.assertRaisesRegex(RuntimeError, "Cannot verify"):
                release_vibevoice(self.config, request_fn=Mock(return_value={"model_loaded": None}))
            command.assert_not_called()

    def test_failed_stop_attempts_to_restore_service_and_surfaces_error(self):
        with patch("infofilm.gpu_release._pid", return_value=123), \
             patch("infofilm.gpu_release._memory", return_value=18142), \
             patch("infofilm.gpu_release.subprocess.run", side_effect=[
                 subprocess.CalledProcessError(1, "stop"), Mock()]) as command:
            with self.assertRaises(subprocess.CalledProcessError):
                release_vibevoice(self.config, request_fn=Mock(return_value={"model_loaded": None}))
            self.assertEqual(command.call_args.args[0][-2:], ["start", "vibevoice-api-local.service"])

    def test_memory_remaining_after_recycle_blocks_handoff(self):
        with patch("infofilm.gpu_release._pid", side_effect=[123, 456]), \
             patch("infofilm.gpu_release._memory", side_effect=[18142, 0, 18142]), \
             patch("infofilm.gpu_release.subprocess.run"):
            with self.assertRaisesRegex(RuntimeError, "still owns model VRAM"):
                release_vibevoice(self.config, request_fn=Mock(return_value={"model_loaded": None}))


if __name__ == "__main__":
    unittest.main()
