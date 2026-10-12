"""Source-level failures preserve progress and archived pages remain resumable."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from infofilm.core import digest, read
from infofilm.image_animation_research import conduct_research
from infofilm.research_harness import ResearchTools


FIRST = "https://institution.test/first"
BROKEN = "https://institution.test/broken"
LAST = "https://institution.test/last"
CONTENT = ("Earth rotates around its axis, producing the daily cycle of day and night. "
           "It revolves around the Sun over a year, while axial tilt affects the seasons. " * 6 + "\n\n") * 3
CLAIM = "Earth's rotation around its axis causes the daily cycle of day and night."


def valid_digest():
    return {"status": "usable", "claims": [{"claim": CLAIM, "passage_ids": ["P01"]}],
            "cautions": [], "reason": ""}


class SourceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "research"
        self.request = {"directory": str(self.directory), "subject": "Earth's rotation and revolution",
                        "config": {"kbagent_root": "/unused"}, "registry": {}}
        self.urls = [FIRST, BROKEN, LAST]
        self.tavily = Mock()
        self.tavily.search.return_value = {"results": [
            {"url": url, "title": "Institutional source", "content": "Discovery only"}
            for url in self.urls]}
        self.fetch = patch("infofilm.research_harness.subprocess.run",
                           side_effect=lambda *_args, **_kwargs:
                           SimpleNamespace(returncode=0, stdout=CONTENT, stderr="")).start()
        self.addCleanup(patch.stopall)

    def tools(self):
        return ResearchTools(self.tavily, Path("/unused/obscura"), max_searches=4, max_fetches=8)

    def models(self, failures=None):
        failures = failures or {}
        model = Mock()
        model.fingerprint = "research-recovery-test"
        model.last_source = None

        def call_json(role, name, prompt, **kwargs):
            if name == "research-queries":
                return {"queries": ["rotation primary evidence", "revolution institutional evidence"]}
            if name == "research-source-selection":
                return {"urls": self.urls, "rationale": "Institutional sources"}
            if not name.startswith("source-digest-"):
                raise AssertionError("Unexpected model activity: " + name)
            for url in self.urls:
                if url in prompt:
                    model.last_source = url
                    break
            failure = failures.get(model.last_source)
            if isinstance(failure, BaseException):
                raise failure
            if isinstance(failure, list) and failure:
                result = failure.pop(0)
                if isinstance(result, BaseException):
                    raise result
                return copy.deepcopy(result)
            return valid_digest()

        model.call_json.side_effect = call_json
        return model

    def run_worker(self, model):
        return conduct_research(self.request, tools=self.tools(), models=model)

    def test_source_transport_failure_keeps_good_sources_and_visits_later_url(self):
        """The observed facade RuntimeError must not discard earlier source work."""
        result = self.run_worker(self.models({BROKEN: RuntimeError(
            "local model request failed; credentials omitted")}))
        self.assertEqual([item["url"] for item in result["results"]], [FIRST, LAST])
        report = read(self.directory / "result.json")
        self.assertEqual(report["source_count"], 2)
        self.assertTrue(any(item.get("url") == BROKEN for item in report["skipped"]))
        self.assertTrue((self.directory / "full-sources" / (digest(BROKEN)[:16] + ".md")).exists())
        self.assertEqual(self.fetch.call_count, 3)

    def test_transport_skipped_source_is_retried_from_archived_page_on_resume(self):
        self.run_worker(self.models({BROKEN: RuntimeError(
            "local model request failed; credentials omitted")}))
        self.fetch.reset_mock()
        self.tavily.search.reset_mock()
        replay = self.models()
        result = self.run_worker(replay)
        self.assertEqual([item["url"] for item in result["results"]], self.urls)
        self.fetch.assert_not_called()
        self.tavily.search.assert_not_called()
        calls = [call for call in replay.call_json.call_args_list if call.args[1].startswith("source-digest-")]
        self.assertTrue(calls, "A transient model failure must not permanently skip the source")

    def test_interrupted_digest_replays_full_page_without_fetch_snapshot(self):
        self.urls = [FIRST]
        interrupted = self.models({FIRST: KeyboardInterrupt("User interrupted digest")})
        with self.assertRaises(KeyboardInterrupt):
            self.run_worker(interrupted)
        full = self.directory / "full-sources" / (digest(FIRST)[:16] + ".md")
        self.assertEqual(full.read_text(), CONTENT)
        self.assertFalse((self.directory / "fetches" / (digest(FIRST)[:16] + ".json")).exists())
        self.fetch.reset_mock()
        self.tavily.search.reset_mock()
        result = self.run_worker(self.models())
        self.assertEqual(result["results"][0]["raw_content"], CONTENT)
        self.fetch.assert_not_called()
        self.tavily.search.assert_not_called()

    def test_exhausted_invalid_digest_is_not_evidence_but_keeps_neighbor_sources(self):
        invalid = {"status": "usable", "claims": [{"claim": CLAIM, "passage_ids": ["P99"]}]}
        result = self.run_worker(self.models({BROKEN: [invalid, invalid]}))
        self.assertEqual([item["url"] for item in result["results"]], [FIRST, LAST])
        report = read(self.directory / "result.json")
        self.assertEqual(report["source_count"], 2)
        self.assertTrue(any(item.get("url") == BROKEN for item in report["skipped"]))
        self.assertEqual(len(list((self.directory / "summaries").glob("*-candidate-*.json"))), 4)

    def overlong_digest(self):
        return {"status": "usable", "claims": [
            {"claim": f"Candidate fact {index}: Earth's rotation causes day and night.", "passage_ids": ["P01"]}
            for index in range(14)], "cautions": [], "reason": ""}

    def test_claim_count_repair_uses_candidate_and_cited_passages_without_whole_page(self):
        self.urls = [FIRST]
        page = CONTENT + "\n\nUNCITED_PAGE_MARKER: " + "General unrelated article detail. " * 20
        self.fetch.side_effect = lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=page, stderr="")
        model = self.models({FIRST: [self.overlong_digest(), valid_digest()]})
        result = self.run_worker(model)
        calls = [call for call in model.call_json.call_args_list if call.args[1].startswith("source-digest-")]
        self.assertEqual(len(calls), 2)
        original, repair = [call.args[2] for call in calls]
        self.assertIn("UNCITED_PAGE_MARKER", original)
        self.assertNotIn("UNCITED_PAGE_MARKER", repair)
        self.assertIn("Candidate fact 0", repair)
        self.assertIn("1–12", repair)
        self.assertLess(len(repair), len(original))
        self.assertEqual(result["results"][0]["source_digest"]["claims"], valid_digest()["claims"])

    def test_interrupted_repair_uses_saved_invalid_candidate_instead_of_redigesting(self):
        self.urls = [FIRST]
        initial = self.models({FIRST: [self.overlong_digest(), KeyboardInterrupt("Repair interrupted")]})
        with self.assertRaises(KeyboardInterrupt):
            self.run_worker(initial)
        self.assertEqual(len(list((self.directory / "summaries").glob("*-candidate-0.json"))), 1)
        self.fetch.reset_mock()
        replay = self.models()
        result = self.run_worker(replay)
        calls = [call for call in replay.call_json.call_args_list if call.args[1].startswith("source-digest-")]
        self.assertEqual(len(calls), 1, "Only the unfinished compact repair should invoke a model")
        self.assertIn("Candidate fact 0", calls[0].args[2])
        self.fetch.assert_not_called()
        self.assertEqual(result["results"][0]["source_digest"]["claims"], valid_digest()["claims"])

    def test_configuration_error_remains_fatal_instead_of_skipping_sources(self):
        with self.assertRaisesRegex(RuntimeError, "Set OPENAI_API_KEY"):
            self.run_worker(self.models({BROKEN: RuntimeError(
                "Set OPENAI_API_KEY or OPENAI_TOKEN in the environment for the Luna route")}))

    def test_downstream_excerpt_set_preserves_claim_citations_beyond_initial_passages(self):
        self.urls = [FIRST]
        page = "\n\n".join(
            f"Article passage {index}: " + "Earth's rotation causes day and night; revolution is motion around the Sun. " * 3
            for index in range(18))
        self.fetch.side_effect = lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=page, stderr="")
        candidate = valid_digest()
        candidate["claims"][0]["passage_ids"] = ["P13"]
        result = self.run_worker(self.models({FIRST: [candidate]}))
        item = result["results"][0]
        references = {pid for claim in item["source_digest"]["claims"] for pid in claim["passage_ids"]}
        excerpts = {passage["id"] for passage in item["source_digest"]["passages"]}
        self.assertTrue(references <= excerpts, "Every cited source passage must reach the evidence planner")
        for passage in item["source_digest"]["passages"]:
            self.assertEqual(page[passage["offset_start"]:passage["offset_end"]], passage["text"])

    def test_route_switch_reuses_validated_page_summary_without_old_provider(self):
        self.urls = [FIRST]
        first = self.run_worker(self.models())
        self.fetch.reset_mock()
        self.tavily.search.reset_mock()
        switched = Mock()
        switched.fingerprint = "different-research-route"
        switched.call_json.side_effect = AssertionError("Validated archived page evidence must survive route changes")
        second = self.run_worker(switched)
        self.assertEqual(second, first)
        self.fetch.assert_not_called()
        self.tavily.search.assert_not_called()
        switched.call_json.assert_not_called()

    def test_interruption_preserves_already_accepted_partial_extraction(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_worker(self.models({BROKEN: KeyboardInterrupt("User stopped the next source")}))
        partial = read(self.directory / "extracted.json")
        self.assertEqual([item["url"] for item in partial["results"]], [FIRST])
        report = read(self.directory / "result.json")
        self.assertEqual(report["source_count"], 1)


if __name__ == "__main__":
    unittest.main()
