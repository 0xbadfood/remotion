import tempfile
import unittest
from pathlib import Path

from PIL import Image

from infofilm.anatomy_image_experiment import (CASE_FILE, CRITERIA, PROTOCOL,
    prepare_reference, review_errors, saved_result, scene_prompt, write_report)
from infofilm.core import Store, digest, read, write


class AnatomyExperimentTests(unittest.TestCase):
    def setUp(self):
        self.cases = read(CASE_FILE)["cases"]

    def review(self, case):
        return {"case_id": case["id"], "verdict": "pass", "scores": dict.fromkeys(CRITERIA, 90),
                "issues": [], "limitations": [], "reference_supported_observations": [],
                "inferred_details": ["Internal detail is absent from the source diagram"],
                "checks": [{"criterion": c, "status": "met", "evidence": "Visible in candidate"} for c in case["checks"]]}

    def test_false_pass_with_failed_anatomy_check_is_rejected(self):
        case = self.cases[0]
        value = self.review(case)
        value["checks"][0]["status"] = "not_met"
        self.assertTrue(review_errors(value, case))

    def test_cross_sections_cannot_claim_all_details_are_source_verified(self):
        case = self.cases[4]
        value = self.review(case)
        value["inferred_details"] = []
        self.assertTrue(review_errors(value, case))
        value["inferred_details"] = ["The whole-organ source does not show villi"]
        self.assertEqual(review_errors(value, case), [])

    def test_invalid_scores_and_mismatched_check_order_are_rejected(self):
        case = self.cases[0]
        for number in [True, float("nan"), 101, -1]:
            value = self.review(case)
            value["scores"][CRITERIA[0]] = number
            self.assertTrue(review_errors(value, case))
        value = self.review(case)
        value["checks"].reverse()
        self.assertTrue(review_errors(value, case))

    def test_matched_prompt_for_reference_control(self):
        for case in self.cases:
            baseline = scene_prompt(case, "none")
            conditioned = scene_prompt(case, "reference")
            self.assertTrue(conditioned.startswith(baseline))
            self.assertLessEqual(len(conditioned), 4000)
            self.assertIn(case["scene"], baseline)

    def test_saved_candidate_tampering_is_rejected_before_gpu_use(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = root / "results/qwen_local/D01"
            path.mkdir(parents=True)
            image = path / "candidate-0.png"
            Image.new("RGB", (32, 32), "red").save(image)
            write(path / "result.json", {"case_id": "D01", "provider": "qwen_local", "status": "complete",
                  "file": str(image), "sha256": digest(image.read_bytes())})
            self.assertEqual(saved_result(root, "D01", "qwen_local")["status"], "complete")
            image.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "checksum"):
                saved_result(root, "D01", "qwen_local")

    def test_reference_cache_verified_and_report_is_portable(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            try:
                refdir = store.root / "reference"
                refdir.mkdir()
                image = refdir / "reference.png"
                Image.new("RGB", (32, 32), "red").save(image)
                (refdir / "source.jpg").write_bytes(b"archived-source")
                reference = {"file": str(image), "sha256": digest(image.read_bytes()),
                             "source_sha256": digest(b"archived-source"), "credit": "NIH"}
                write(refdir / "source.json", reference)
                self.assertEqual(prepare_reference(store.root), reference)
                definition = {"schema_version": PROTOCOL, "cases": self.cases[:1],
                              "providers": {"qwen_local": {}, "flux_dev_local": {}},
                              "conditioning": "reference", "reference": reference}
                summary = write_report(store, definition)
                self.assertEqual(summary["planned"], 2)
                failed = store.root/'results/qwen_local/D01'
                failed.mkdir(parents=True)
                Image.new('RGB', (32,32), 'blue').save(failed/'candidate-0.png')
                write(failed/'result.json', {'case_id':'D01', 'provider':'qwen_local',
                                            'status':'error', 'error':'Malformed review JSON'})
                summary = write_report(store, definition)
                self.assertEqual(summary['errors'], 1)
                self.assertIn('file', summary['results'][0])
                report = (store.root / "report.html").read_text()
                self.assertIn('src="reference/reference.png"', report)
                self.assertIn('src="results/qwen_local/D01/candidate-0.png"', report)
                self.assertNotIn(d, report)
                image.write_bytes(b"tampered")
                with self.assertRaises(ValueError):
                    prepare_reference(store.root)
            finally:
                store.db.close()


if __name__ == "__main__":
    unittest.main()
