import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from PIL import Image, ImageDraw

from infofilm.core import digest, read, write
from infofilm import image_animation_assets as assets


def png(path, size=(512, 512), *, mode="RGBA", transparent=False, faint=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    background = (0, 0, 0, 0) if transparent else (240, 230, 220, 255)
    image = Image.new("RGBA", size, background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((size[0] // 4, size[1] // 4, size[0] * 3 // 4, size[1] * 3 // 4),
                   fill=(70, 110, 200, 4 if faint else 255))
    image.convert(mode).save(path, format="PNG")
    return path


def request(identifier="canvas", kind="canvas", **fields):
    return {"id": identifier, "kind": kind, "prompt": "One correctly placed blue mechanical component.",
            "width": 512, "height": 512, **fields}


def capabilities(**fields):
    return {"schema_version": assets.CAPABILITY_SCHEMA, "url": "http://images",
            "text_generation": True, "referenced_edit": True, "native_alpha": True,
            "alpha_parameter": None, "dimension_multiple": 32, "min_dimension": 512,
            "max_dimension": 2816, **fields}


class ImageAnimationAssetTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name)
        self.config = {"image_url": "http://images", "image_steps": 25, "gpu_lock": str(self.directory / "gpu.lock")}
        self.store = Mock(root=self.directory)

    def prepare(self, items=None):
        return assets.prepare_asset_requests(items or [request()], self.directory, self.config)

    def manual_manifest(self, manifest, entries):
        planned = {item["id"]: item for item in manifest["requests"]}
        return write(self.directory / "manual" / "import.json", {
            "schema_version": assets.IMPORT_SCHEMA, "requests_sha256": manifest["requests_sha256"],
            "assets": [{"request_sha256": planned[entry["id"]]["request_sha256"], **entry} for entry in entries]})

    def generate(self, manifest, **kwargs):
        return assets.generate_assets(self.config, self.store, manifest, **kwargs)

    def fake_render(self, config, store, root, params, label):
        root = Path(root)
        output = png(root / "candidate-0.png", transparent="native RGBA" in params["prompt"])
        write(root / "provider-0.json", {"state": "submitted", "job_id": "job-" + label, "params": params})
        return output

    def test_read_only_capability_check_recognises_native_rgba_without_flag(self):
        schema = {"paths": {"/api/generate": {"post": {"requestBody": {"content": {
            "multipart/form-data": {"schema": {"$ref": "#/components/schemas/Generate"}}}}}}},
            "components": {"schemas": {"Generate": {"properties": {
                "prompt": {"type": "string"}, "references": {"type": "array"}, "output_format": {"type": "string"}}}}}}
        api = Mock(side_effect=[schema, {"state": "loaded", "busy": True}])
        with patch.object(assets, "_local_rgba_evidence", return_value=[{"finding": "four-channel VAE", "sha256": "hash"}]):
            result = assets.discover_capabilities(self.config, request_fn=api)
        self.assertTrue(result["referenced_edit"])
        self.assertTrue(result["native_alpha"])
        self.assertEqual(result["alpha_request_mode"], "prompt_native_rgba")
        self.assertEqual(result["dimension_multiple"], 32)
        self.assertTrue(result["service_status"]["busy"])
        self.assertEqual([call.args[2:] for call in api.call_args_list], [("GET", "/openapi.json"), ("GET", "/api/status")])

    def test_no_transparency_flag_without_runtime_evidence_is_unverified(self):
        api = Mock(side_effect=[{"paths": {"/api/generate": {"post": {"requestBody": {"content": {
            "multipart/form-data": {"schema": {"properties": {"prompt": {"type": "string"}}}}}}}}}}, {"state": "loaded"}])
        result = assets.discover_capabilities(self.config, request_fn=api)
        self.assertFalse(result["native_alpha"])
        self.assertFalse(result["referenced_edit"])

    def test_prepare_and_dry_run_are_offline_and_package_exact_requests(self):
        with patch.object(assets.media, "request", side_effect=AssertionError("Offline")), \
             patch.object(assets, "gpu_lease", side_effect=AssertionError("No GPU")):
            manifest = self.prepare([request(), request("part", "part", reference_ids=["canvas"], required_landmarks=["pin"])])
            result = self.generate(manifest)
        self.assertEqual(result["status"], "waiting_execution")
        missing = read(self.directory / "missing-asset-requests.json")["requests"]
        self.assertEqual(missing[1]["required_landmarks"], ["pin"])
        self.assertTrue(missing[1]["transparent_required"])
        self.assertEqual(read(self.directory / "asset-import-template.json")["assets"][1]["file"], "part.png")

    def test_invalid_requests_fail_before_provider_work(self):
        bad = [[request("../escape")], [request(), request()],
               [request(reference_ids=["missing"])],
               [request("a", reference_ids=["b"]), request("b", reference_ids=["a"])],
               [request("part", "part", transparent_required=False)],
               [request(landmarks={"pin": [float("nan"), 10]})]]
        for items in bad:
            with self.subTest(items=items), self.assertRaises(ValueError):
                self.prepare(items)

    def test_changed_request_package_is_immutable(self):
        manifest = self.prepare()
        with self.assertRaisesRegex(ValueError, "Saved.*changed"):
            self.prepare([request(prompt="Changed appearance")])
        manifest["requests"][0]["prompt"] = "Tampering"
        with self.assertRaisesRegex(ValueError, "hash changed"):
            self.generate(manifest)

    def test_unsupported_alpha_only_run_never_acquires_gpu(self):
        manifest = self.prepare([request("part", "part")])
        with patch.object(assets, "gpu_lease", side_effect=AssertionError("No useful GPU work")), \
             patch.object(assets, "render_take", side_effect=AssertionError("No submit")):
            result = self.generate(manifest, execute=True, capabilities=capabilities(native_alpha=False))
        self.assertEqual(result["status"], "waiting_manual_assets")
        self.assertEqual(result["unresolved"][0]["status"], "unsupported_native_alpha")

    def test_busy_provider_is_preserved_without_load_unload_or_submission(self):
        manifest = self.prepare()
        api = Mock(return_value={"state": "loaded", "busy": True})
        with patch.object(assets.media, "request", api), \
             patch.object(assets, "render_take", side_effect=AssertionError("Busy provider")):
            result = self.generate(manifest, execute=True, capabilities=capabilities())
        self.assertEqual(result["status"], "waiting_provider_busy")
        self.assertEqual([call.args[2:] for call in api.call_args_list], [("GET", "/api/status")])

    def test_initially_loaded_model_remains_loaded_after_generation(self):
        manifest = self.prepare()
        api = Mock(return_value={"state": "loaded", "busy": False})
        with patch.object(assets.media, "request", api), \
             patch.object(assets, "render_take", side_effect=self.fake_render):
            result = self.generate(manifest, execute=True, capabilities=capabilities())
        self.assertEqual(result["status"], "assets_ready")
        self.assertEqual([call.args[2:] for call in api.call_args_list], [("GET", "/api/status")])

    def test_model_loaded_by_helper_is_unloaded_exactly_once_when_idle(self):
        manifest = self.prepare()
        api = Mock(side_effect=[{"state": "unloaded", "busy": False}, {"state": "loading"},
                                {"state": "loaded", "busy": False}, {"state": "loaded", "busy": False},
                                {"state": "unloaded"}])
        with patch.object(assets.media, "request", api), \
             patch.object(assets, "render_take", side_effect=self.fake_render):
            result = self.generate(manifest, execute=True, capabilities=capabilities())
        self.assertEqual(result["status"], "assets_ready")
        self.assertEqual([call.args[2:] for call in api.call_args_list], [
            ("GET", "/api/status"), ("POST", "/api/load"), ("GET", "/api/status"),
            ("GET", "/api/status"), ("POST", "/api/unload")])

    def test_active_audio_worker_blocks_new_image_work_without_service_switch(self):
        manifest = self.prepare()
        self.config["tts_url"] = "http://audio"
        api = Mock(side_effect=[{"state": "loaded", "busy": False}, {"active_job": "audio-job"}])
        with patch.object(assets.media, "request", api), \
             patch.object(assets, "render_take", side_effect=AssertionError("Audio owns shared GPU")):
            result = self.generate(manifest, execute=True, capabilities=capabilities())
        self.assertEqual(result["status"], "waiting_provider_busy")
        self.assertEqual([call.args[2:] for call in api.call_args_list], [("GET", "/api/status"), ("GET", "/health")])

    def test_generation_binds_actual_reference_hash_and_keeps_returned_alpha(self):
        # Caller order is intentionally reversed to exercise dependency ordering.
        manifest = self.prepare([request("part", "part", reference_ids=["canvas"]), request()])
        with patch.object(assets, "_provider_session", return_value=contextlib.nullcontext(True)), \
             patch.object(assets, "render_take", side_effect=self.fake_render) as render:
            result = self.generate(manifest, execute=True, capabilities=capabilities())
        self.assertEqual(result["status"], "assets_ready")
        canvas, part = result["assets"]["canvas"], result["assets"]["part"]
        self.assertEqual(part["reference_sha256"], {"canvas": canvas["sha256"]})
        self.assertEqual(render.call_args_list[1].args[3]["reference_images"][0]["sha256"], canvas["sha256"])
        self.assertEqual(part["alpha"]["range"], [0, 255])
        self.assertEqual(part["mode"], "RGBA")
        self.assertEqual(part["sha256"], digest(Path(part["file"]).read_bytes()))
        with patch.object(assets, "gpu_lease", side_effect=AssertionError("Ready assets")), \
             patch.object(assets, "discover_capabilities", side_effect=AssertionError("Ready assets")), \
             patch.object(assets, "render_take", side_effect=AssertionError("Ready assets")):
            resumed = self.generate(manifest, execute=True)
        self.assertEqual(resumed["assets"], result["assets"])

    def test_native_alpha_rejection_is_checkpointed_and_not_resubmitted(self):
        manifest = self.prepare([request("part", "part")])
        def opaque(config, store, root, params, label):
            return png(Path(root) / "candidate-0.png", mode="RGB")
        with patch.object(assets, "_provider_session", return_value=contextlib.nullcontext(True)), \
             patch.object(assets, "render_take", side_effect=opaque) as render:
            result = self.generate(manifest, execute=True, capabilities=capabilities())
        self.assertEqual(render.call_count, 1)
        self.assertEqual(result["status"], "waiting_manual_assets")
        self.assertEqual(result["unresolved"][0]["status"], "invalid_native_alpha")
        with patch.object(assets, "gpu_lease", side_effect=AssertionError("No artistic retry")):
            resumed = self.generate(manifest, execute=True, capabilities=capabilities())
        self.assertEqual(resumed["status"], "waiting_manual_assets")

    def test_part_validation_rejects_opaque_faint_empty_and_wrong_dimensions(self):
        part = self.prepare([request("part", "part")])["requests"][0]
        examples = [("opaque.png", {"mode": "RGB"}), ("faint.png", {"transparent": True, "faint": True}),
                    ("wrong.png", {"size": (256, 256), "transparent": True})]
        for name, options in examples:
            path = png(self.directory / name, **options)
            with self.subTest(name=name), self.assertRaises(ValueError):
                assets.inspect_asset(path, part)

    def test_native_alpha_can_supply_shape_for_solid_colour_part(self):
        part = self.prepare([request("part", "part")])["requests"][0]
        image = Image.new("RGBA", (512, 512), (50, 80, 220, 0))
        ImageDraw.Draw(image).ellipse((128, 128, 384, 384), fill=(50, 80, 220, 255))
        path = self.directory / "silhouette.png"
        image.save(path)
        metadata = assets.inspect_asset(path, part)
        self.assertEqual(metadata["alpha"]["range"], [0, 255])
        self.assertGreater(metadata["alpha"]["opaque_pixels"], 0)

    def test_manual_import_preserves_bytes_landmarks_and_cpu_only_resume(self):
        manifest = self.prepare([request(), request("part", "part", reference_ids=["canvas"], required_landmarks=["pin"])])
        png(self.directory / "manual/canvas.png")
        original = png(self.directory / "manual/part.png", transparent=True).read_bytes()
        package = self.manual_manifest(manifest, [{"id": "part", "file": "part.png", "landmarks": {"pin": [256, 256]}},
                                                  {"id": "canvas", "file": "canvas.png"}])
        with patch.object(assets.media, "request", side_effect=AssertionError("CPU import")), \
             patch.object(assets, "gpu_lease", side_effect=AssertionError("CPU import")):
            result = assets.import_assets(package, manifest, self.directory)
            resumed = self.generate(manifest, execute=True)
        self.assertEqual(result["status"], "assets_ready")
        self.assertEqual(resumed["assets"], result["assets"])
        part = result["assets"]["part"]
        self.assertEqual(Path(part["file"]).read_bytes(), original)
        self.assertEqual(part["landmarks"], {"pin": [256, 256]})
        self.assertEqual(part["reference_sha256"], {"canvas": result["assets"]["canvas"]["sha256"]})

    def test_import_rejects_escape_symlink_hash_dimensions_and_bad_landmark(self):
        manifest = self.prepare([request("part", "part", required_landmarks=["pin"])])
        path = png(self.directory / "manual/part.png", transparent=True)
        external = png(self.directory / "outside.png", transparent=True)
        (path.parent / "escape.png").symlink_to(external)
        invalid = [{"file": "../outside.png"}, {"file": str(external)}, {"file": "escape.png"},
                   {"file": "part.png", "landmarks": {}},
                   {"file": "part.png", "landmarks": {"pin": [512, 10]}},
                   {"file": "part.png", "sha256": "wrong"}]
        for override in invalid:
            package = self.manual_manifest(manifest, [{"id": "part", "file": "part.png", "landmarks": {"pin": [2, 3]}, **override}])
            with self.subTest(override=override), self.assertRaises(ValueError):
                assets.import_assets(package, manifest, self.directory)
        self.assertFalse((self.directory / "assets").exists())

    def test_invalid_final_import_entry_does_not_partly_fill_cache(self):
        manifest = self.prepare([request(), request("part", "part")])
        png(self.directory / "manual/canvas.png")
        png(self.directory / "manual/part.png", mode="RGB")
        package = self.manual_manifest(manifest, [{"id": "canvas", "file": "canvas.png"}, {"id": "part", "file": "part.png"}])
        with self.assertRaises(ValueError):
            assets.import_assets(package, manifest, self.directory)
        self.assertFalse((self.directory / "assets").exists())

    def test_actual_adapter_resumes_job_and_recovers_missing_id_with_same_reference_bytes(self):
        manifest = self.prepare([request(), request("part", "part", reference_ids=["canvas"])])
        png(self.directory / "manual/canvas.png")
        imported = assets.import_assets(self.manual_manifest(manifest, [{"id": "canvas", "file": "canvas.png"}]), manifest, self.directory)
        canvas = imported["assets"]["canvas"]
        request_item = manifest["requests"][1]
        root = assets._asset_root(self.directory, request_item)
        params = {"prompt": request_item["prompt"] + "\nOutput one isolated moving part with native RGBA transparency. Background pixels have zero alpha; "
                  "the part is clearly visible and opaque. Preserve the referenced appearance and source-canvas coordinates.",
                  "width": "512", "height": "512", "steps": "25", "guidance": "0", "output_format": "png",
                  "seed": str(int(request_item["request_sha256"][:7], 16)),
                  "reference_images": [{"file": canvas["file"], "sha256": canvas["sha256"]}]}
        write(root / "provider-0.json", {"state": "submitted", "job_id": "lost", "params": params})
        missing_request = httpx.Request("GET", "http://images/api/jobs/lost")
        missing_response = httpx.Response(404, request=missing_request)
        missing = httpx.HTTPStatusError("Missing job", request=missing_request, response=missing_response)
        def api_response(config, service, method, path, **kwargs):
            if path == "/api/status":
                return {"state": "loaded", "busy": False}
            self.assertEqual(kwargs["files"][0][0], "references")
            self.assertEqual(kwargs["files"][0][1][1].read(), Path(canvas["file"]).read_bytes())
            return {"id": "replacement"}
        api = Mock(side_effect=api_response)
        wait = Mock(side_effect=[missing, {"status": "complete", "url": "/outputs/new.png"}])
        raw = png(self.directory / "download.png", transparent=True).read_bytes()
        with patch.object(assets, "_provider_session", return_value=contextlib.nullcontext(True)), \
             patch.object(assets.media, "request", api), patch.object(assets.media, "wait_job", wait), \
             patch("infofilm.image_recovery.httpx.Client") as client:
            response = Mock(content=raw)
            client.return_value.__enter__.return_value.get.return_value = response
            result = self.generate(manifest, execute=True, capabilities=capabilities())
        self.assertEqual(result["status"], "assets_ready")
        self.assertEqual(api.call_args.kwargs["data"], {key: value for key, value in params.items() if key != "reference_images"})
        self.assertEqual(read(root / "provider-0.json")["job_id"], "replacement")
        self.assertEqual(Path(result["assets"]["part"]["file"]).read_bytes(), raw)

    def test_unknown_submission_never_repeats_post(self):
        manifest = self.prepare()
        root = assets._asset_root(self.directory, manifest["requests"][0])
        item = manifest["requests"][0]
        params = {"prompt": item["prompt"], "width": "512", "height": "512", "steps": "25", "guidance": "0",
                  "output_format": "png", "seed": str(int(item["request_sha256"][:7], 16))}
        write(root / "provider-0.json", {"state": "submission_started", "params": params})
        with patch.object(assets, "_provider_session", return_value=contextlib.nullcontext(True)), \
             patch.object(assets.media, "request", side_effect=AssertionError("No duplicate POST")), \
             self.assertRaisesRegex(RuntimeError, "Unknown Qwen submission"):
            self.generate(manifest, execute=True, capabilities=capabilities())


if __name__ == "__main__":
    unittest.main()
