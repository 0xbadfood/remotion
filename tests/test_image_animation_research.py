"""Research stays bounded and only promotes validated fetched passages."""
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from infofilm.core import digest, read, write
from infofilm.image_animation_research import conduct_research
from infofilm.research_harness import ResearchTools, canonical_url


GOOD = "https://institution.test/source"
BAD = "https://institution.test/unreadable"
CONTENT = ("Solar heating causes warmer air to rise. Cooler air moves into the space it leaves. "
           "This circulation depends on temperature differences and local conditions. " * 5 + "\n\n") * 3
CLAIM = "Warmer air rises and cooler air can move into the space it leaves."


class ResearchWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "research"
        self.request = {"directory": str(self.directory), "subject": "How solar heating moves air",
                        "config": {"kbagent_root": "/unused"}, "registry": {}}
        self.models = Mock()
        self.models.fingerprint = "mock-research-route"
        self.queries = {"queries": ["solar heating circulation institutional", "warm air circulation primary evidence"]}
        self.selection = {"urls": [GOOD], "rationale": "Institutional source"}
        self.digests = []
        self.models.call_json.side_effect = self.model_response
        self.tavily = Mock()
        self.tavily.search.return_value = {"results": [{"title": "Institutional evidence", "url": GOOD,
            "content": "Discovery snippet is not accepted evidence.", "score": .9}]}
        self.tools = self.make_tools()
        self.contents = {GOOD: CONTENT}
        self.fetch_patch = patch("infofilm.research_harness.subprocess.run", side_effect=self.fetch_source)
        self.fetch = self.fetch_patch.start()
        self.addCleanup(self.fetch_patch.stop)

    def make_tools(self):
        return ResearchTools(tavily=self.tavily, obscura_path=Path("/unused/obscura"),
                             max_searches=4, max_fetches=8)

    def model_response(self, role, name, prompt, **kwargs):
        if name == "research-queries":
            return copy.deepcopy(self.queries)
        if name == "research-source-selection":
            return copy.deepcopy(self.selection)
        if name.startswith("source-digest-"):
            return copy.deepcopy(self.digests.pop(0)) if self.digests else {
                "status": "usable", "claims": [{"claim": CLAIM, "passage_ids": ["P01"]}],
                "cautions": ["Circulation varies with conditions."], "reason": ""}
        raise AssertionError("Unexpected model call " + name)

    def fetch_source(self, args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=self.contents[args[-1]], stderr="")

    def run_worker(self, *, tools=None, models=None):
        return conduct_research(self.request, tools=self.tools if tools is None else tools,
                                models=self.models if models is None else models)

    def test_accepted_digest_uses_fetched_passages_with_archived_offsets(self):
        result = self.run_worker()
        self.assertEqual(len(result["results"]), 1)
        item = result["results"][0]
        self.assertEqual(item["url"], GOOD)
        self.assertEqual(item["raw_content"], CONTENT)
        self.assertEqual(item["source_digest"]["claims"], [{"claim": CLAIM, "passage_ids": ["P01"]}])
        summary = read(next(path for path in (self.directory / "summaries").glob("*.json") if "candidate" not in path.name))
        self.assertEqual(summary["full_sha256"], digest(CONTENT))
        self.assertEqual(Path(summary["full_file"]).read_text(), CONTENT)
        self.assertTrue(summary["usable"])
        passage_ids = {passage["id"] for passage in summary["passages"]}
        self.assertTrue(set(summary["claims"][0]["passage_ids"]) <= passage_ids)
        for passage in summary["passages"]:
            self.assertEqual(CONTENT[passage["offset_start"]:passage["offset_end"]], passage["text"])
        digest_prompt = next(call.args[2] for call in self.models.call_json.call_args_list
                             if call.args[1].startswith("source-digest-"))
        self.assertNotIn("Discovery snippet is not accepted evidence", digest_prompt)
        self.assertIn("Only assert facts supported by supplied passages", digest_prompt)
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.tavily.search.call_count, 2)

    def test_unknown_passage_ids_are_repaired_before_claims_are_accepted(self):
        unsupported = "This claim cites a passage that the model was never given."
        self.digests = [{"claims": [{"claim": unsupported, "passage_ids": ["P99"]}]},
                        {"claims": [{"claim": CLAIM, "passage_ids": ["P01"]}]}]
        result = self.run_worker()
        self.assertEqual(result["results"][0]["source_digest"]["claims"][0]["claim"], CLAIM)
        self.assertNotIn(unsupported, json.dumps(result))
        calls = [call for call in self.models.call_json.call_args_list if call.args[1].startswith("source-digest-")]
        self.assertEqual(len(calls), 2)
        self.assertIn("Repair output structure", calls[1].args[2])
        self.assertIn("provided passages", calls[1].args[2])

    def test_invalid_digests_have_two_attempts_and_are_never_evidence(self):
        self.digests = [{"claims": [{"claim": CLAIM, "passage_ids": ["P99"]}]}] * 2
        with self.assertRaisesRegex(RuntimeError, "No readable source documents"):
            self.run_worker()
        self.assertNotIn(GOOD, self.tools.fetched_urls)
        candidates = list((self.directory / "summaries").glob("*-candidate-*.json"))
        self.assertEqual(len(candidates), 2)
        self.assertFalse((self.directory / "extracted.json").exists())

    def test_query_count_type_and_length_are_bounded_before_search(self):
        invalid_queries = [[], ["only one"], ["query"] * 5, ["", "valid"], ["x" * 401, "valid"],
                           [None, "valid"], "not a list"]
        for index, queries in enumerate(invalid_queries):
            self.request["directory"] = str(Path(self.temp.name) / f"invalid-queries-{index}")
            self.queries = {"queries": queries}
            with self.subTest(queries=queries), self.assertRaisesRegex(ValueError, "bounded queries"):
                self.run_worker()
        self.tavily.search.assert_not_called()
        self.fetch.assert_not_called()

    def test_only_discovered_source_urls_can_be_selected(self):
        self.selection["urls"] = ["https://undiscovered.test/not-a-lead"]
        with self.assertRaisesRegex(ValueError, "discovered URLs"):
            self.run_worker()
        self.fetch.assert_not_called()

    def test_source_selection_count_and_raw_duplicates_are_bounded(self):
        for index, urls in enumerate(([], [GOOD] * 2, [GOOD] * 9)):
            self.request["directory"] = str(Path(self.temp.name) / f"invalid-selection-{index}")
            self.selection["urls"] = urls
            with self.subTest(urls=urls), self.assertRaisesRegex(ValueError, "discovered URLs"):
                self.run_worker(tools=self.make_tools())
        self.fetch.assert_not_called()

    def test_archive_replay_does_not_repeat_search_fetch_or_model_calls(self):
        first = self.run_worker()
        self.tavily.search.reset_mock()
        self.fetch.reset_mock()
        replay_models = Mock()
        replay_models.fingerprint = self.models.fingerprint
        replay_models.call_json.side_effect = AssertionError("Archived research must not replan")
        replay_tools = self.make_tools()
        second = self.run_worker(tools=replay_tools, models=replay_models)
        self.assertEqual(second, first)
        self.tavily.search.assert_not_called()
        self.fetch.assert_not_called()
        replay_models.call_json.assert_not_called()
        self.assertEqual(replay_tools.searches_used, 0)
        self.assertEqual(replay_tools.fetches_used, 0)
        self.assertIn(GOOD, replay_tools.fetched_urls)

    def test_unreadable_source_is_skipped_and_its_failure_is_archived(self):
        self.selection["urls"] = [BAD, GOOD]
        self.tavily.search.return_value["results"].append({"title": "Unreadable", "url": BAD})
        self.contents[BAD] = "\x00\ufffdbinary" * 300
        result = self.run_worker()
        self.assertEqual([item["url"] for item in result["results"]], [GOOD])
        report = read(self.directory / "result.json")
        self.assertEqual(len(report["skipped"]), 1)
        self.assertEqual(report["skipped"][0]["url"], BAD)
        self.assertNotIn(BAD, self.tools.fetched_urls)
        calls = [call for call in self.models.call_json.call_args_list if call.args[1].startswith("source-digest-")]
        self.assertEqual(len(calls), 1)
        self.tavily.search.reset_mock()
        self.fetch.reset_mock()
        self.run_worker(tools=self.make_tools())
        self.tavily.search.assert_not_called()
        self.fetch.assert_not_called()

    def test_irrelevant_source_has_no_accepted_claims(self):
        self.selection["urls"] = [BAD, GOOD]
        self.tavily.search.return_value["results"].append({"title": "Irrelevant", "url": BAD})
        self.contents[BAD] = CONTENT
        self.digests = [{"status": "unusable", "claims": [], "reason": "No relevant supported claims."}]
        result = self.run_worker()
        self.assertEqual([item["url"] for item in result["results"]], [GOOD])
        self.assertNotIn(BAD, self.tools.fetched_urls)

    def test_environment_credentials_never_enter_research_artifacts(self):
        secrets = {"OPENAI_API_KEY": "cloud-secret-not-for-files", "TAVILY_API_KEY": "search-secret-not-for-files",
                   "VLLM_API_KEY": "local-secret-not-for-files"}
        with patch.dict(os.environ, secrets):
            self.run_worker()
        for path in self.directory.rglob("*.json"):
            for secret in secrets.values():
                self.assertNotIn(secret, path.read_text())

    def test_archived_queries_are_revalidated_before_any_io(self):
        write(self.directory / "queries.json", {"queries": ["a query"] * 5})
        with self.assertRaisesRegex(ValueError, "bounded queries"):
            self.run_worker()
        self.tavily.search.assert_not_called()
        self.fetch.assert_not_called()

    def test_archived_selection_still_requires_discovered_urls(self):
        write(self.directory / "source-selection.json", {"urls": ["https://undiscovered.test/source"]})
        with self.assertRaisesRegex(ValueError, "discovered URLs"):
            self.run_worker()
        self.fetch.assert_not_called()

    def test_unhashable_selection_items_have_a_clear_validation_error(self):
        self.selection["urls"] = [{"url": GOOD}]
        with self.assertRaisesRegex(ValueError, "discovered URLs"):
            self.run_worker()
        self.fetch.assert_not_called()

    def test_canonical_duplicates_do_not_spend_two_source_slots(self):
        self.selection["urls"] = [GOOD, GOOD + "/#fragment"]
        self.assertEqual(canonical_url(GOOD), canonical_url(self.selection["urls"][1]))
        with self.assertRaisesRegex(ValueError, "discovered URLs"):
            self.run_worker()
        self.fetch.assert_not_called()

    def test_search_error_cannot_write_an_echoed_api_key(self):
        secret = "search-provider-secret-not-for-files"
        self.tavily.search.side_effect = RuntimeError("Invalid API key " + secret)
        with patch.dict(os.environ, {"TAVILY_API_KEY": secret}), self.assertRaisesRegex(RuntimeError, "no source leads"):
            self.run_worker()
        for path in self.directory.rglob("*.json"):
            self.assertNotIn(secret, path.read_text())

    def test_archived_digest_cannot_promote_unknown_passage_ids_on_replay(self):
        self.run_worker()
        summary_path = next(path for path in (self.directory / "summaries").glob("*.json")
                            if "candidate" not in path.name)
        summary = read(summary_path)
        summary["claims"][0]["passage_ids"] = ["P99"]
        write(summary_path, summary)
        replay_models = Mock()
        replay_models.fingerprint = self.models.fingerprint
        with self.assertRaisesRegex(RuntimeError, "No readable source documents"):
            self.run_worker(tools=self.make_tools(), models=replay_models)
        replay_models.call_json.assert_not_called()
        report = read(self.directory / "result.json")
        self.assertEqual(report["source_count"], 0)
        self.assertIn("Archived source digest is invalid", report["skipped"][0]["error"])

    def test_cited_passage_offsets_match_the_visible_source_excerpt(self):
        self.contents[GOOD] = CONTENT.replace("\n\n", " ") * 5
        item = self.run_worker()["results"][0]
        # Retain the complete cited passage; a head-only preview can omit the
        # sentence that actually supports an otherwise valid digest claim.
        self.assertTrue(any(len(p["text"]) > 900 for p in item["source_digest"]["passages"]))
        self.assertLessEqual(sum(len(p["text"]) for p in item["source_digest"]["passages"]), 32000)
        for passage in item["source_digest"]["passages"]:
            self.assertEqual(item["raw_content"][passage["offset_start"]:passage["offset_end"]], passage["text"])


if __name__ == "__main__":
    unittest.main()
