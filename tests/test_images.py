import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from infofilm.core import Store, digest, read, write
from infofilm.llm import Models
from infofilm.composition import image_composition
from infofilm.media import generate_images, image_identity, repair_image_prompt, replacement_prompt_errors, review_scene_image


CORRECTED = "A parchment document bearing a round red wax stamp beside a quill and a closed tax ledger. Place the inanimate objects right of centre, leaving a quiet upper-right area for a banner."
REJECTED = {"pass": False, "reason_keys": ["semantic_mismatch"], "issues": ["A marine animal represents the wrong meaning of the legal document stamp"],
            "correction": "Show a historical parchment document bearing a red wax stamp.", "limitations": [], "component_inventory": []}
ACCEPTED = {"pass": True, "reason_keys": [], "issues": [], "correction": "", "limitations": [], "component_inventory": []}


class ImageRecoveryTests(unittest.TestCase):
    def test_review_format_repair_reuses_the_same_image_and_strict_reason_vocabulary(self):
        models = Mock()
        models.call.side_effect = [{**REJECTED, 'reason_keys': ['invented_key']}, REJECTED]
        library = {'style': 'editorial', 'assets': [], 'scene_contexts_by_asset': {}}
        asset = {'id': 'A001', 'prompt': 'A stone fortress', 'acceptance': []}
        candidate = Path('/tmp/same-candidate.png')
        result = review_scene_image(models, library, asset, candidate, {'prompt': asset['prompt']}, 'image-test', {})
        self.assertEqual(result, REJECTED)
        self.assertEqual(models.call.call_count, 2)
        self.assertTrue(all(call.kwargs['image_path'] == candidate for call in models.call.call_args_list))
        self.assertEqual(models.call.call_args.args[1], 'image-test-format-repair')
        self.assertIn('unwanted_text', models.call.call_args.args[2])
        self.assertIn('Previous response failed', models.call.call_args.args[2])
        models.call.side_effect = None
        models.call.return_value = {**REJECTED, 'reason_keys': 'unwanted_text'}
        with self.assertRaisesRegex(ValueError, 'strict reason-key contract'):
            review_scene_image(models, library, asset, candidate, {'prompt': asset['prompt']}, 'bad-image-test', {})

    def test_composition_tracks_actual_text_and_crop_for_shared_assets(self):
        minimal=[{'scene_key':'60:S01','copy_density':'minimal'}]
        full=image_composition(minimal,['landscape','portrait'])
        self.assertFalse(full['left_text_area_required'])
        self.assertFalse(full['right_portrait_crop_required'])
        self.assertIn('centred',full['generation_direction'])
        shared=image_composition(minimal+[{'copy_density':'standard'}],['landscape','portrait'])
        self.assertTrue(shared['left_text_area_required'])
        self.assertTrue(shared['right_portrait_crop_required'])
        self.assertIn('right half',shared['generation_direction'])
        portrait=image_composition([{'copy_density':'standard'}],['portrait'])
        self.assertFalse(portrait['left_text_area_required'])
        self.assertTrue(portrait['right_portrait_crop_required'])
        banner=[{'scene_key':'60:S01','text':'A useful label'}]
        self.assertTrue(image_composition(minimal,['landscape'],banner)['upper_right_banner_required'])
        contained=image_composition(minimal,['portrait'],banner)
        self.assertFalse(contained['upper_right_banner_required'])
        self.assertNotIn('lower quarter',contained['generation_direction'])
        minimal[0]['banner_override']=''
        self.assertFalse(image_composition(minimal,['landscape'],banner)['upper_right_banner_required'])

    def test_generation_review_and_repair_share_minimal_scene_composition(self):
        with tempfile.TemporaryDirectory() as directory:
            store,config,library,models=self.setup_run(directory)
            library['orientations']=['landscape']
            library['scene_contexts_by_asset']['A006'][0]['copy_density']='minimal'
            models.call.side_effect=[REJECTED,ACCEPTED]
            with self.provider() as (api,_):
                generate_images(config,store,library,models)
            submissions=[c.kwargs['data'] for c in api.call_args_list if c.args[3]=='/api/generate']
            self.assertTrue(all(p['prompt'].startswith('Composition: Use a natural full-frame') for p in submissions))
            spec=json.loads(models.call.call_args.args[2].split('SPEC:\n',1)[1])
            self.assertFalse(spec['composition_contract']['left_text_area_required'])
            self.assertEqual(spec['composition_contract'],models.loop.call_args.args[2]['composition_contract'])
            self.assertIn('overrides optional positional wording',models.call.call_args.args[2])
            self.assertIn('actual overlay/crop',models.call.call_args.args[2])
            store.db.close()

    def setup_run(self, directory):
        store = Store(directory)
        config = {"image_url": "http://image", "tts_url": "http://tts", "image_width": 1536,
                  "image_height": 864, "image_steps": 25, "image_max_attempts": 3}
        library = {"style": "editorial", "assets": [{"id": "A006", "prompt": "An illustration of a seal or document granting taxation rights.",
                   "purpose": "Diwani taxation rights", "acceptance": ["Matches narration"], "fact_ids": ["F03"]}],
                   "scene_contexts_by_asset": {"A006": [{"point_narration": "The Company acquired taxation rights in 1765."}]}}
        models = Mock()
        models.call.return_value = ACCEPTED
        def repair(stage, task, context, validator, **kwargs):
            result = {"prompt": CORRECTED}
            self.assertEqual(validator(result), [])
            self.assertFalse(kwargs["experimental"])
            return result
        models.loop.side_effect = repair
        return store, config, library, models

    def root(self, store, config, library, asset=None):
        return store.root / "images" / image_identity(config, library, asset or library["assets"][0])[:20]

    def checkpoint(self, root, attempt, *, candidate=True, review=True, submitted=True):
        params = {"prompt": "Shared visual style: editorial\nScene illustration: A seal or document.",
                  "width": "1536", "height": "864", "steps": "25", "guidance": "0", "seed": str(100 + attempt), "output_format": "png"}
        if attempt:
            params["prompt"] += "\nTargeted correction: " + REJECTED["correction"]
        record = {"state": "submitted" if submitted else "submission_started", "params": params, "asset": "A006"}
        if submitted:
            record["job_id"] = "preserved-job-" + str(attempt)
        write(root / f"provider-{attempt}.json", record)
        if candidate:
            (root / f"candidate-{attempt}.png").write_bytes(b"preserved rejected image " + str(attempt).encode())
        if review:
            write(root / f"review-{attempt}.json", REJECTED)
        return params

    @contextlib.contextmanager
    def provider(self):
        def request(config, service, method, path, **kwargs):
            if path == "/health":
                return {}
            if path == "/api/status":
                return {"state": "loaded"}
            if path == "/api/generate":
                return {"id": "new-job"}
            return {}
        download = Mock(content=b"new generated image")
        with patch("infofilm.media.gpu_lease", return_value=contextlib.nullcontext()), \
             patch("infofilm.media.request", side_effect=request) as api, \
             patch("infofilm.media.wait_job", return_value={"id": "new-job", "url": "/images/new.png"}) as wait, \
             patch("infofilm.media.httpx.Client") as client:
            client.return_value.__enter__.return_value.get.return_value = download
            yield api, wait

    def test_rejection_replaces_prompt_and_reviews_actual_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, library, models = self.setup_run(directory)
            models.call.side_effect = [REJECTED, ACCEPTED]
            with self.provider() as (api, _):
                result = generate_images(config, store, library, models)
            submissions = [call.kwargs["data"] for call in api.call_args_list if call.args[3] == "/api/generate"]
            self.assertEqual(len(submissions), 2)
            self.assertIn("seal or document", submissions[0]["prompt"])
            self.assertIn(CORRECTED, submissions[1]["prompt"])
            self.assertNotIn("seal or document", submissions[1]["prompt"])
            self.assertNotIn("marine", submissions[1]["prompt"])
            self.assertNotIn("Targeted correction", submissions[1]["prompt"])
            self.assertNotIn("negative_prompt", submissions[1])
            review_spec = json.loads(models.call.call_args.args[2].split("SPEC:\n", 1)[1])
            self.assertIn("unambiguous synonyms", models.call.call_args.args[2])
            self.assertIn("unambiguous synonyms", models.loop.call_args.args[1])
            self.assertEqual(review_spec["asset"]["prompt"], submissions[1]["prompt"])
            self.assertEqual(review_spec["scene_contexts"], library["scene_contexts_by_asset"]["A006"])
            self.assertEqual(result["A006"]["settings"], submissions[1])
            states = [row[0] for row in store.db.execute("SELECT state FROM events ORDER BY id")]
            self.assertEqual(states, ["planned", "rejected", "retrying", "prompt_repaired", "accepted"])
            store.db.close()

    def test_global_subject_and_source_dates_reach_generation_repair_and_review(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, library, models = self.setup_run(directory)
            write(store.root / "project.json", {"subject": "The Great Bengal Famine of 1770"})
            write(store.root / "evidence-final.json", {"title": "Bengal famine", "facts": [
                {"id": "F01", "claim": "The famine occurred in 1769–1770."},
                {"id": "F03", "claim": "The Company acquired taxation rights in 1765."}]})
            models.call.side_effect = [REJECTED, ACCEPTED]
            with self.provider() as (api, _):
                generate_images(config, store, library, models)
            submissions = [call.kwargs["data"] for call in api.call_args_list if call.args[3] == "/api/generate"]
            self.assertTrue(all("The Great Bengal Famine of 1770" in value["prompt"] for value in submissions))
            repair = models.loop.call_args.args[2]["source_context"]
            self.assertEqual(repair["subject"], "The Great Bengal Famine of 1770")
            self.assertIn("1769–1770", repair["facts"][0]["claim"])
            review = json.loads(models.call.call_args.args[2].split("SPEC:\n", 1)[1])
            self.assertEqual(review["source_context"], repair)
            self.assertIn("era and setting", models.call.call_args.args[2])
            store.db.close()

    def test_legacy_two_failed_takes_get_one_replacement_without_repeating_reviews(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, library, models = self.setup_run(directory)
            root = self.root(store, config, library)
            self.checkpoint(root, 0)
            self.checkpoint(root, 1)
            preserved = {path.name: path.read_bytes() for path in root.iterdir()}
            with self.provider() as (api, _):
                result = generate_images(config, store, library, models)
            self.assertEqual(models.call.call_count, 1)
            models.loop.assert_called_once()
            history = models.loop.call_args.args[2]["previous_attempts"]
            self.assertEqual([item["attempt"] for item in history], [1, 2])
            self.assertEqual(history[-1]["review"], REJECTED)
            self.assertEqual(len([call for call in api.call_args_list if call.args[3] == "/api/generate"]), 1)
            self.assertTrue(result["A006"]["file"].endswith("candidate-2.png"))
            self.assertEqual(read(root / "prompt-repair-2.json")["prompt"], CORRECTED)
            for name, content in preserved.items():
                self.assertEqual((root / name).read_bytes(), content)
            store.db.close()

    def test_accepted_images_are_reused_without_loading_gpu(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, library, models = self.setup_run(directory)
            root = self.root(store, config, library)
            path = root / "candidate-0.png"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"accepted image")
            value = {"asset": "A006", "file": str(path), "sha256": digest(path.read_bytes()), "review": ACCEPTED}
            write(root / "selected.json", value)
            with patch("infofilm.media.gpu_lease", side_effect=AssertionError("No GPU for accepted images")):
                self.assertEqual(generate_images(config, store, library, models), {"A006": value})
            models.call.assert_not_called()
            models.loop.assert_not_called()
            self.assertEqual(read(store.root / "image_manifest.json"), {"A006": value})
            planned = json.loads(store.db.execute("SELECT detail FROM events WHERE state='planned'").fetchone()[0])
            self.assertEqual(planned["reused_images"], 1)
            self.assertEqual(planned["remaining_images"], 0)
            path.write_bytes(b"corrupted image")
            with self.assertRaisesRegex(ValueError, "checkpoint is invalid"):
                generate_images(config, store, library, models)
            store.db.close()

    def test_submitted_retry_keeps_actual_parameters_and_downloads_original_job(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, library, models = self.setup_run(directory)
            root = self.root(store, config, library)
            self.checkpoint(root, 0)
            params = self.checkpoint(root, 1, candidate=False, review=False)
            with self.provider() as (api, wait):
                result = generate_images(config, store, library, models)
            models.loop.assert_not_called()
            self.assertEqual(result["A006"]["settings"], params)
            self.assertEqual(wait.call_args.args[2], "/api/jobs/preserved-job-1")
            self.assertFalse(any(call.args[3] == "/api/generate" for call in api.call_args_list))
            store.db.close()

    def test_unknown_submission_is_not_rewritten_or_resubmitted(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, library, models = self.setup_run(directory)
            root = self.root(store, config, library)
            self.checkpoint(root, 0, candidate=False, review=False, submitted=False)
            before = (root / "provider-0.json").read_bytes()
            with self.provider() as (api, wait):
                with self.assertRaisesRegex(RuntimeError, "Unknown image submission outcome"):
                    generate_images(config, store, library, models)
            self.assertFalse(any(call.args[3] == "/api/generate" for call in api.call_args_list))
            models.loop.assert_not_called()
            wait.assert_not_called()
            self.assertEqual((root / "provider-0.json").read_bytes(), before)
            store.db.close()

    def test_exhausted_asset_preserves_partial_manifest_and_bounds_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, library, models = self.setup_run(directory)
            library["assets"] = [{**library["assets"][0], "id": "A005"}, library["assets"][0]]
            models.call.side_effect = [ACCEPTED, REJECTED, REJECTED, REJECTED]
            with self.provider() as (api, _):
                with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
                    generate_images(config, store, library, models)
            manifest = read(store.root / "image_manifest.json")
            self.assertEqual(list(manifest), ["A005"])
            self.assertEqual(len([call for call in api.call_args_list if call.args[3] == "/api/generate"]), 4)
            self.assertEqual(models.loop.call_count, 2)
            self.assertFalse((self.root(store, config, library, library["assets"][1]) / "selected.json").exists())
            self.assertTrue(any(call.args[3] == "/api/unload" for call in api.call_args_list))
            states = [row[0] for row in store.db.execute("SELECT state FROM events ORDER BY id")]
            self.assertEqual(states[-1], "exhausted")
            store.db.close()

    def test_negative_clauses_and_nested_headers_need_repair_before_generation(self):
        original = "The original scene description, with an ambiguous subject."
        self.assertEqual(replacement_prompt_errors({"prompt": CORRECTED}, original), [])
        for suffix in [" No live animals, no marine mammals.", " Without lettering.", " Avoid map labels.",
                       " The background is rendered without any text labels.", " The sky is clear with no text or objects."]:
            self.assertTrue(replacement_prompt_errors({"prompt": CORRECTED + suffix}, original))
        self.assertTrue(replacement_prompt_errors({"prompt": "Scene illustration: " + CORRECTED}, original))
        self.assertTrue(replacement_prompt_errors({"prompt": original}, original))

    def test_planner_repairs_nonpositive_description_before_selecting_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, library, _ = self.setup_run(directory)
            config.update(planner_url="http://planner", reviewer_url="http://reviewer")
            root = self.root(store, config, library)
            self.checkpoint(root, 0)
            models = Models(config, store)
            with patch.object(models, "call", side_effect=[{"prompt": CORRECTED + " No marine mammals."}, {"prompt": CORRECTED}]) as llm:
                result = repair_image_prompt(models, library, library["assets"][0], root, 1)
            self.assertEqual(result, CORRECTED)
            self.assertEqual(llm.call_count, 2)
            self.assertIn("Use positive descriptions only", llm.call_args.args[2])
            self.assertIn("Remove optional decorations", llm.call_args.args[2])
            self.assertEqual(read(root / "prompt-repair-1.json")["prompt"], CORRECTED)
            store.db.close()


if __name__ == "__main__":
    unittest.main()
