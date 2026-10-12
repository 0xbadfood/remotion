"""Isolated activity routing, image requests and credential-free caches."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from infofilm.core import Store, digest, read
from infofilm.image_animation_models import (ACTIVITIES, DEFAULT_ROUTES, ROUTES,
    ImageAnimationModels, ModelTransportError, OutputBudgetError, load_registry, validate_registry)
from infofilm.llm import ModelOutputError


def response(value, status=200):
    return httpx.Response(status, json=value, request=httpx.Request("POST", "https://example.test/v1"))


def completed(value):
    return {"status": "completed", "model": "gpt-6-luna", "output": [{"type": "message",
        "content": [{"type": "output_text", "text": json.dumps(value)}]}],
        "usage": {"input_tokens": 100, "output_tokens": 80}}


def chat(value, finish_reason="stop"):
    return {"choices": [{"finish_reason": finish_reason,
                         "message": {"content": json.dumps(value)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 80}}


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "trial")
        self.addCleanup(self.store.db.close)
        self.config = {"planner_url": "http://planner.test/v1", "reviewer_url": "http://reviewer.test/v1",
                       "max_repairs": 1}
        self.registry = load_registry()
        self.environment = patch.dict(os.environ, {"OPENAI_API_KEY": "secret-not-for-artifacts",
                                                  "VLLM_API_KEY": "local-secret-not-for-artifacts"}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client_patch = patch("infofilm.image_animation_models.httpx.Client")
        self.client = self.client_patch.start().return_value.__enter__.return_value
        self.addCleanup(self.client_patch.stop)
        self.client.get.return_value = response({"data": [{"id": "local-model-v1"}]})
        self.client.post.return_value = response(completed({"plan": True}))

    def router(self, **kwargs):
        return ImageAnimationModels(self.config, self.store, registry=self.registry, **kwargs)

    def image(self, name="canvas.png", suffix=b"pixels"):
        path = Path(self.temp.name) / name
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + suffix)
        return path

    def test_defaults_and_descriptions_need_no_model_connection(self):
        router = self.router()
        descriptions = router.describe_routes()
        self.assertEqual(router.registry["activities"], DEFAULT_ROUTES)
        self.assertEqual(set(descriptions), set(ACTIVITIES))
        self.assertEqual(descriptions["research"]["url"], self.config["planner_url"])
        self.assertEqual(descriptions["grounding"]["url"], self.config["reviewer_url"])
        self.assertEqual(descriptions["teaching"]["model"], "gpt-6-luna")
        self.assertEqual(descriptions["teaching"]["reasoning_effort"], "medium")
        self.assertEqual(len(router.fingerprint), 64)
        self.client.get.assert_not_called()
        self.client.post.assert_not_called()

    def test_every_activity_can_select_every_route(self):
        for activity in ACTIVITIES:
            for route in ROUTES:
                facade = self.router(routes={activity: route}).for_activity(activity)
                endpoint = ("https://api.openai.com/v1" if route == "luna" else
                            self.config["reviewer_url"] if route == "local-reviewer" else self.config["planner_url"])
                self.assertEqual(facade.config["planner_url"], endpoint)
                self.assertEqual(facade.config["image_animation_activity"], activity)
                self.assertEqual(facade.config["image_animation_route_identity"], facade.fingerprint)

    def test_inputs_and_descriptions_are_frozen(self):
        router = self.router()
        fingerprint = router.fingerprint
        self.registry["activities"]["teaching"] = "local"
        self.config["planner_url"] = "http://changed.test/v1"
        description = router.describe_routes()
        description["teaching"]["model"] = "changed"
        self.assertEqual(router.fingerprint, fingerprint)
        self.assertEqual(router.describe_routes()["teaching"]["model"], "gpt-6-luna")
        self.assertEqual(router.describe_routes()["research"]["url"], "http://planner.test/v1")

    def test_registry_rejects_unknown_routes_activities_secrets_and_unbounded_limits(self):
        mutations = [lambda r: r["activities"].update(research="cloud"),
                     lambda r: r["activities"].update(unknown="local"),
                     lambda r: r["activities"].pop("review"),
                     lambda r: r["providers"]["luna"].update(api_key="should-not-be-in-config"),
                     lambda r: r["providers"]["luna"].update(url="https://other-provider.test/v1"),
                     lambda r: r["providers"]["local"].update(url="http://user:secret@localhost/v1"),
                     lambda r: r["providers"]["local"].update(max_output_tokens=0),
                     lambda r: r["providers"]["local"].update(max_output_tokens=True),
                     lambda r: r["limits"].update(max_format_attempts=3)]
        for mutation in mutations:
            registry = copy.deepcopy(self.registry)
            mutation(registry)
            with self.subTest(registry=registry), self.assertRaises(ValueError):
                validate_registry(registry)
        with self.assertRaises(ValueError):
            self.router(routes={"unknown": "local"})
        with self.assertRaises(ValueError):
            self.router().for_activity("unknown")

    def test_luna_text_and_reference_images_use_official_responses(self):
        first, second = self.image(), self.image("reference.png", b"reference")
        facade = self.router().for_activity("canvas")
        self.assertEqual(facade.call("planner", "canvas-plan", "Explain the canvas", image_path=first,
                                     reference_image_paths=[second], max_tokens=3000), {"plan": True})
        url = self.client.post.call_args.args[0]
        body = self.client.post.call_args.kwargs["json"]
        self.assertEqual(url, "https://api.openai.com/v1/responses")
        self.assertEqual(body["model"], "gpt-6-luna")
        self.assertEqual(body["reasoning"], {"effort": "medium"})
        self.assertEqual(body["max_output_tokens"], 3000)
        self.assertFalse(body["store"])
        self.assertEqual(body["text"]["format"], {"type": "json_object"})
        self.assertNotIn("temperature", body)
        content = body["input"][0]["content"]
        self.assertEqual([part["type"] for part in content], ["input_text", "input_image", "input_image"])
        self.assertIn("JSON", content[0]["text"])
        self.assertTrue(content[1]["image_url"].startswith("data:image/png;base64,"))
        self.client.get.assert_not_called()
        request = read(next((self.store.root / "requests").glob("*.request.json")))
        self.assertEqual([image["content_hash"] for image in request["images"]],
                         [digest(first.read_bytes()), digest(second.read_bytes())])
        self.assertEqual(request["model"], "gpt-6-luna")
        self.assertEqual(request["settings"]["reasoning_effort"], "medium")
        for path in self.store.root.rglob("*.json"):
            artifact = path.read_text()
            self.assertNotIn("secret-not-for-artifacts", artifact)
            self.assertNotIn("data:image/", artifact)

    def test_cached_results_track_contents_settings_and_route(self):
        path = self.image()
        facade = self.router().for_activity("canvas")
        kwargs = {"image_path": path, "max_tokens": 2000}
        facade.call("planner", "canvas", "JSON canvas", **kwargs)
        facade.call("planner", "canvas", "JSON canvas", **kwargs)
        self.assertEqual(self.client.post.call_count, 1)
        equivalent = self.image("equivalent.png")
        facade.call("planner", "canvas", "JSON canvas", image_path=equivalent, max_tokens=2000)
        self.assertEqual(self.client.post.call_count, 1)
        path.write_bytes(path.read_bytes() + b"changed")
        facade.call("planner", "canvas", "JSON canvas", **kwargs)
        self.assertEqual(self.client.post.call_count, 2)
        facade.call("planner", "canvas", "JSON canvas", image_path=path, max_tokens=2500)
        self.assertEqual(self.client.post.call_count, 3)
        changed = copy.deepcopy(self.registry)
        changed["providers"]["luna"]["reasoning_effort"] = "high"
        ImageAnimationModels(self.config, self.store, registry=changed).for_activity("canvas").call(
            "planner", "canvas", "JSON canvas", **kwargs)
        self.assertEqual(self.client.post.call_count, 4)
        self.assertNotEqual(self.router().fingerprint, self.router(routes={"canvas": "local"}).fingerprint)

    def test_local_routes_reuse_models_with_actual_identity_and_images(self):
        path, reference = self.image(), self.image("reference.png", b"reference")
        self.client.post.return_value = response(chat({"grounded": True}))
        facade = self.router().for_activity("grounding")
        self.assertEqual(facade.call("planner", "grounding", "JSON grounding", image_path=path,
                                     reference_image_paths=[reference], max_tokens=2000), {"grounded": True})
        facade.call("planner", "grounding", "JSON grounding", image_path=path,
                    reference_image_paths=[reference], max_tokens=2000)
        self.assertEqual(self.client.get.call_count, 1)
        self.assertEqual(self.client.get.call_args.args[0], self.config["reviewer_url"] + "/models")
        self.assertEqual(self.client.post.call_count, 1)
        self.assertEqual(self.client.post.call_args.args[0], self.config["reviewer_url"] + "/chat/completions")
        body = self.client.post.call_args.kwargs["json"]
        self.assertEqual(body["model"], "local-model-v1")
        self.assertEqual(len(body["messages"][1]["content"]), 3)
        self.assertTrue(body["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        request = read(next((self.store.root / "requests").glob("*.request.json")))
        self.assertEqual(request["model"], "local-model-v1")
        self.assertEqual(request["images"][0]["content_hash"], digest(path.read_bytes()))
        self.assertTrue(list((self.store.root / "model-cache").glob("*/requests/*.result.json")))

    def test_local_cache_changes_when_endpoint_model_changes(self):
        self.client.post.return_value = response(chat({"plan": True}))
        self.router().for_activity("research").call("planner", "research", "JSON research")
        self.client.get.return_value = response({"data": [{"id": "local-model-v2"}]})
        self.router().for_activity("research").call("planner", "research", "JSON research")
        self.assertEqual(self.client.post.call_count, 2)
        models = {read(path)["model"] for path in (self.store.root / "requests").glob("*.request.json")}
        self.assertEqual(models, {"local-model-v1", "local-model-v2"})

    def test_semantic_reviewer_uses_review_route_even_when_planner_is_luna(self):
        self.client.post.return_value = response(chat({"pass": True, "score": 90, "issues": []}))
        self.router().for_activity("teaching").call_json("reviewer", "teaching-review", "JSON review", max_tokens=2000)
        self.assertEqual(self.client.post.call_args.args[0], self.config["reviewer_url"] + "/chat/completions")
        self.client.post.return_value = response(completed({"pass": True}))
        self.router(routes={"review": "luna"}).for_activity("research").call_json(
            "reviewer", "research-review", "JSON review", max_tokens=2000)
        self.assertEqual(self.client.post.call_args.args[0], "https://api.openai.com/v1/responses")

    def test_existing_models_loop_uses_activity_planner_and_review_routes(self):
        def post(url, **kwargs):
            value = {"pass": True, "score": 95, "issues": []} if "reviewer.test" in url else {"plan": True}
            return response(chat(value))
        self.client.post.side_effect = post
        facade = self.router().for_activity("research")
        result = facade.loop("research", "Produce JSON research", {"subject": "test"}, lambda value: [], experimental=True)
        self.assertEqual(result, {"plan": True})
        self.assertEqual([call.args[0] for call in self.client.post.call_args_list],
                         [self.config["planner_url"] + "/chat/completions", self.config["reviewer_url"] + "/chat/completions"])

    def test_output_ceilings_are_terminal_and_incomplete_requests_are_not_repeated(self):
        self.client.post.return_value = response({"status": "incomplete", "output": [],
            "incomplete_details": {"reason": "max_output_tokens"}, "usage": {"output_tokens": 24000}})
        facade = self.router().for_activity("motion")
        for _ in range(2):
            with self.assertRaises(OutputBudgetError) as caught:
                facade.call_json("planner", "motion", "JSON motion", max_tokens=50000)
            self.assertTrue(caught.exception.output_budget_exhausted)
        self.assertEqual(self.client.post.call_count, 1)
        self.assertEqual(self.client.post.call_args.kwargs["json"]["max_output_tokens"], 24000)
        self.assertEqual(list((self.store.root / "requests").glob("*.result.json")), [])
        self.assertEqual(len(list((self.store.root / "requests").glob("*.response.json"))), 1)

    def test_local_output_limit_is_terminal_too(self):
        self.client.post.return_value = response(chat({}, "length"))
        facade = self.router().for_activity("research")
        for _ in range(2):
            with self.assertRaises(OutputBudgetError):
                facade.call_json("planner", "research", "JSON research", max_tokens=3000)
        self.assertEqual(self.client.post.call_count, 1)

    def test_json_format_retry_is_bounded_and_keeps_images(self):
        path = self.image()
        invalid = completed({})
        invalid["output"][0]["content"][0]["text"] = "broken JSON"
        self.client.post.side_effect = [response(invalid), response(completed({"plan": True}))]
        facade = self.router().for_activity("canvas")
        self.assertEqual(facade.call_json("planner", "canvas", "JSON canvas", image_path=path, max_tokens=2000), {"plan": True})
        self.assertEqual(self.client.post.call_count, 2)
        content = self.client.post.call_args.kwargs["json"]["input"][0]["content"]
        self.assertIn("FORMAT RECOVERY", content[0]["text"])
        self.assertEqual(content[1]["type"], "input_image")
        self.client.post.side_effect = None
        self.client.post.return_value = response(invalid)
        with self.assertRaises(ModelOutputError):
            facade.call_json("planner", "always-invalid", "JSON canvas", max_tokens=2000)
        self.assertEqual(self.client.post.call_count, 4)

    def test_luna_failures_never_persist_or_report_credentials(self):
        self.client.post.return_value = response({"error": {"message": "secret-not-for-artifacts"}}, status=401)
        with self.assertRaisesRegex(RuntimeError, "HTTP 401") as caught:
            self.router().for_activity("teaching").call("planner", "teaching", "JSON teaching")
        self.assertNotIn("secret-not-for-artifacts", str(caught.exception))
        for path in self.store.root.rglob("*.json"):
            self.assertNotIn("secret-not-for-artifacts", path.read_text())
        self.client.post.return_value = response(completed({"echo": "secret-not-for-artifacts"}))
        with self.assertRaises(ModelOutputError):
            self.router().for_activity("canvas").call("planner", "echo", "JSON canvas")
        saved = read(next((self.store.root / "requests").glob("echo-*.response.json")))
        self.assertIn("[redacted]", json.dumps(saved))
        self.assertEqual(list((self.store.root / "requests").glob("echo-*.result.json")), [])

    def test_missing_key_and_invalid_inputs_never_call_cloud(self):
        facade = self.router().for_activity("canvas")
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
            facade.call("planner", "canvas", "JSON canvas")
        with self.assertRaises(ValueError):
            facade.call("planner", "../escaped", "JSON canvas")
        with self.assertRaises(ValueError):
            facade.call("planner", "canvas", "JSON canvas", max_tokens=True)
        invalid = Path(self.temp.name) / "invalid.png"
        invalid.write_text("not an image")
        with self.assertRaisesRegex(ValueError, "Unsupported image"):
            facade.call("planner", "invalid", "JSON canvas", image_path=invalid)
        self.client.post.assert_not_called()

    def test_malformed_provider_output_has_bounded_format_recovery(self):
        invalid = ({"status": "completed", "output": None},
                   {"status": "completed", "output": ["invalid"]},
                   {"status": "completed", "output": [{"type": "message", "content": None}]},
                   {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": {}}]}]},
                   {"status": "incomplete", "incomplete_details": "invalid"},
                   completed(["a JSON list is not an object"]))
        facade = self.router().for_activity("canvas")
        for index, raw in enumerate(invalid):
            self.client.post.return_value = response(raw)
            with self.subTest(raw=raw), self.assertRaises(ModelOutputError):
                facade.call_json("planner", f"malformed-{index}", "JSON canvas", max_tokens=2000)
        self.assertEqual(self.client.post.call_count, len(invalid) * 2)

    def test_generator_reference_images_survive_format_recovery(self):
        image = self.image()
        invalid = completed({})
        invalid["output"][0]["content"][0]["text"] = "invalid JSON"
        self.client.post.side_effect = [response(invalid), response(completed({"plan": True}))]
        self.router().for_activity("canvas").call_json("planner", "generator-images", "JSON canvas",
            reference_image_paths=iter([image]), max_tokens=2000)
        for call in self.client.post.call_args_list:
            self.assertEqual(len(call.kwargs["json"]["input"][0]["content"]), 2)

    def test_local_context_error_is_terminal_and_diagnostic_is_safe(self):
        self.client.post.return_value = response({"error": {
            "message": "Maximum context length exceeded secret-not-for-artifacts " + "x" * 2000,
            "api_key": "another-key-that-was-echoed"}}, status=400)
        with self.assertRaises(ModelTransportError) as caught:
            self.router().for_activity("research").call_json("planner", "research", "JSON research", max_tokens=2000)
        self.assertEqual(self.client.post.call_count, 1)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertFalse(caught.exception.retryable)
        detail = read(caught.exception.diagnostic_path)
        self.assertEqual(detail["classification"], "context_length")
        self.assertEqual(detail["attempt"], 1)
        self.assertLessEqual(len(detail["body_preview"]), 1200)
        self.assertTrue(detail["body_truncated"])
        for path in self.store.root.rglob("*.json"):
            self.assertNotIn("secret-not-for-artifacts", path.read_text())
            self.assertNotIn("another-key-that-was-echoed", path.read_text())

    def test_transient_local_http_failure_recovers_without_redrafting(self):
        self.client.post.side_effect = [response({"error": "busy"}, status=503),
                                      response(chat({"plan": True}))]
        with patch("infofilm.image_animation_models.time.sleep") as sleep:
            facade = self.router().for_activity("research")
            self.assertEqual(facade.call_json("planner", "research", "JSON research", max_tokens=2000), {"plan": True})
            self.assertEqual(facade.call_json("planner", "research", "JSON research", max_tokens=2000), {"plan": True})
        self.assertEqual(self.client.post.call_count, 2)
        sleep.assert_called_once_with(1)
        self.assertEqual(self.client.post.call_args_list[0].kwargs["json"],
                         self.client.post.call_args_list[1].kwargs["json"])
        ledger = read(next((self.store.root / "requests").glob("research-*.transport.json")))
        self.assertEqual(ledger["status"], "recovered")
        self.assertEqual(ledger["transport_attempts"][0]["status_code"], 503)

    def test_transport_retries_are_bounded_and_keep_exception_strings_private(self):
        self.client.post.side_effect = httpx.ReadTimeout("secret-not-for-artifacts")
        with patch("infofilm.image_animation_models.time.sleep") as sleep, self.assertRaises(ModelTransportError) as caught:
            self.router().for_activity("research").call("planner", "research", "JSON research", max_tokens=2000)
        self.assertEqual(self.client.post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("secret-not-for-artifacts", str(caught.exception))
        detail = read(caught.exception.diagnostic_path)
        self.assertEqual(len(detail["transport_attempts"]), 3)
        self.assertNotIn("body_preview", detail)
        self.assertTrue(all(item["exception_type"] == "ReadTimeout" for item in detail["transport_attempts"]))

    def test_luna_rate_limit_recovers_but_authentication_does_not_retry(self):
        self.client.post.side_effect = [response({"error": "slow down"}, status=429), response(completed({"plan": True}))]
        with patch("infofilm.image_animation_models.time.sleep"):
            self.assertEqual(self.router().for_activity("canvas").call("planner", "canvas", "JSON canvas"), {"plan": True})
        self.assertEqual(self.client.post.call_count, 2)
        self.client.post.side_effect = None
        self.client.post.return_value = response({"error": "Bearer unknown-sensitive-token"}, status=401)
        with self.assertRaises(ModelTransportError) as caught:
            self.router().for_activity("canvas").call("planner", "unauthorized", "JSON canvas")
        self.assertEqual(self.client.post.call_count, 3)
        self.assertEqual(read(caught.exception.diagnostic_path)["classification"], "authentication")
        self.assertNotIn("unknown-sensitive-token", caught.exception.diagnostic_path.read_text())

    def test_saved_incomplete_or_invalid_responses_are_not_sent_again(self):
        raw = completed({})
        raw["status"] = "incomplete"
        raw["incomplete_details"] = {"reason": "content_filter"}
        self.client.post.return_value = response(raw)
        facade = self.router().for_activity("canvas")
        for _ in range(2):
            with self.assertRaises(ModelOutputError):
                facade.call("planner", "incomplete", "JSON canvas", max_tokens=2000)
        self.assertEqual(self.client.post.call_count, 1)
        raw = chat({})
        raw["choices"][0]["message"]["content"] = "broken JSON"
        self.client.post.return_value = response(raw)
        facade = self.router().for_activity("research")
        for _ in range(2):
            with self.assertRaises(ModelOutputError):
                facade.call("planner", "invalid", "JSON research", max_tokens=2000)
        self.assertEqual(self.client.post.call_count, 2)

    def test_model_discovery_retries_and_reports_terminal_http(self):
        self.client.get.side_effect = [response({"error": "not ready"}, status=502),
                                     response({"data": [{"id": "local-model-v1"}]})]
        self.client.post.return_value = response(chat({"plan": True}))
        with patch("infofilm.image_animation_models.time.sleep"):
            self.router().for_activity("research").call("planner", "research", "JSON research")
        self.assertEqual(self.client.get.call_count, 2)
        self.client.get.side_effect = None
        self.client.get.return_value = response({"error": "missing"}, status=404)
        with self.assertRaises(ModelTransportError) as caught:
            self.router().for_activity("research").call("planner", "discovery-terminal", "JSON research")
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(read(caught.exception.diagnostic_path)["phase"], "model_discovery")


if __name__ == "__main__":
    unittest.main()
