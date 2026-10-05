import base64
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from infofilm.core import Store, digest, read, write
from infofilm.image_studio import park_idle_gpu_services, render_studio, studio_payload, user_systemd_environment
from infofilm.media import generate_images, image_identity
from infofilm.test_turbo_images import audit_selected_text, prepare, report, summary


class TurboImageTests(unittest.TestCase):
    def test_user_systemd_environment_fills_missing_bus_for_service_workers(self):
        with patch.dict(os.environ,{},clear=True),patch('infofilm.image_studio.os.getuid',return_value=1000):
            env=user_systemd_environment()
        self.assertEqual(env['XDG_RUNTIME_DIR'],'/run/user/1000')
        self.assertEqual(env['DBUS_SESSION_BUS_ADDRESS'],'unix:path=/run/user/1000/bus')
        with patch.dict(os.environ,{'XDG_RUNTIME_DIR':'/run/custom','DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/custom/bus'},clear=True):
            env=user_systemd_environment()
        self.assertEqual(env['DBUS_SESSION_BUS_ADDRESS'],'unix:path=/run/custom/bus')

    def test_user_bus_error_is_not_reported_as_inactive_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            config={**self.config(),'tts_url':'http://127.0.0.1:8092','qwen_url':'http://127.0.0.1:8075'}
            vibe,qwen=Mock(),Mock();vibe.json.return_value={};qwen.json.return_value={'busy':False,'state':'unloaded'}
            with patch('infofilm.image_studio.httpx.Client') as client,patch('infofilm.image_studio.subprocess.run') as run:
                client.return_value.__enter__.return_value.get.side_effect=[vibe,qwen]
                run.return_value=Mock(returncode=1,stderr='Failed to connect to bus: No medium found',stdout='')
                with self.assertRaisesRegex(RuntimeError,'Cannot reach or query the service manager'):
                    with park_idle_gpu_services(config,store):pass
                self.assertEqual(run.call_count,1)
                self.assertIn('DBUS_SESSION_BUS_ADDRESS',run.call_args.kwargs['env'])
            store.db.close()

    def config(self):
        return {"image_url": "http://studio", "qwen_url": "http://qwen", "tts_url": "http://tts",
                "image_engine": "zimage", "image_width": 1536, "image_height": 864,
                "image_steps": 10, "image_cfg_scale": 1.0, "image_max_attempts": 2,
                "image_continue_on_exhaustion": True, "planner_url": "http://planner", "reviewer_url": "http://reviewer"}

    def library(self):
        return {"style": "editorial", "assets": [
            {"id": "A006", "prompt": "A seal or document granting taxation rights.", "purpose": "Taxation rights"},
            {"id": "A007", "prompt": "Officers outside a warehouse in Bengal.", "purpose": "Company administration"}]}

    def test_payload_preserves_prompt_and_explicitly_disables_injected_negatives(self):
        params = {"prompt": "Original positive prompt", "width": "1536", "height": "864", "steps": "10", "seed": "99"}
        value = studio_payload(self.config(), params)
        self.assertEqual(value["prompt"], params["prompt"])
        self.assertEqual((value["width"], value["height"]), (1536, 896))
        self.assertEqual(value["negative_prompt"], "")
        self.assertEqual(value["style"], "custom")

    def test_initial_prompts_not_approved_repair_prompts_and_originals_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)/"production"
            write(project/"scene-image-prompts.json", self.library())
            image = project/"original.png"
            image.write_bytes(b"protected image")
            manifest = {"A006": {"file": str(image), "sha256": digest(image.read_bytes()), "prompt": "Already repaired prompt"}}
            write(project/"image_manifest.json", manifest)
            root, plan, original = prepare(project, self.config(), {"zimage": {}}, ["A006"], 2, Path(directory)/"benchmark")
            self.assertIn("seal or document", plan["library"]["assets"][0]["prompt"])
            self.assertEqual(original, manifest)
            self.assertEqual(read(project/"image_manifest.json"), manifest)
            self.assertEqual(image.read_bytes(), b"protected image")
            with self.assertRaises(ValueError):
                prepare(project, self.config(), {"zimage": {}}, ["A006"], 2, project/"bad")

    def test_provider_identity_separates_engines_without_changing_legacy_identity(self):
        config, library = self.config(), self.library()
        a = library["assets"][0]
        self.assertNotEqual(image_identity(config, library, a), image_identity({**config, "image_engine": "ernie-turbo"}, library, a))
        legacy = {k:v for k,v in config.items() if k != "image_engine"}
        self.assertEqual(image_identity(legacy, library, a), digest({"asset": a, "style": "editorial", "provider": "http://studio", "width":1536,"height":864,"steps":10}))

    def test_real_production_repair_loop_can_exhaust_one_asset_then_continue_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, library = Store(directory), self.config(), self.library()
            models = Mock()
            reject = {"pass": False, "reason_keys": ["semantic_mismatch"], "issues": ["Marine animal"], "correction": "Show a document stamp", "limitations": []}
            accept = {"pass": True, "reason_keys": [], "issues": [], "correction": "", "limitations": []}
            models.call.side_effect = [reject, reject, reject, accept]
            repaired = "A formal parchment document with a red wax imprint resting beside a quill on a plain wooden desk."
            models.loop.return_value = {"prompt": repaired, "simplifications": []}
            submissions = []
            def render(config, store, root, index, params):
                submissions.append(params)
                write(root/f"provider-{index}.json", {"params":params,"job_id":"test"})
                (root/f"candidate-{index}.png").write_bytes(b"generated image")
            with patch("infofilm.media.gpu_lease", return_value=contextlib.nullcontext()), \
                 patch("infofilm.media.request", return_value={}), \
                 patch("infofilm.image_studio.release_qwen"), \
                 patch("infofilm.image_studio.render_studio", side_effect=render):
                result = generate_images(config, store, library, models)
                self.assertEqual(set(result), {"A007"})
                self.assertIn("seal or document", submissions[0]["prompt"])
                self.assertIn(repaired, submissions[1]["prompt"])
                self.assertNotIn("seal or document", submissions[1]["prompt"])
                self.assertEqual(result["A007"]["provider"], "zimage")
                self.assertEqual(models.loop.call_count, 2)
                old_submissions = len(submissions)
                generate_images(config, store, library, models)
                self.assertEqual(len(submissions), old_submissions)
            store.db.close()

    def test_submission_uses_sd_endpoint_crop_is_exact_and_cached_take_never_resubmits(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            params = {"prompt":"positive prompt", "width":"1536", "height":"864", "steps":"10", "seed":"99"}
            raw = io.BytesIO()
            Image.new("RGB", (1536,896), (70,90,110)).save(raw, format="PNG")
            response = Mock(is_error=False)
            response.json.return_value = {"images":[base64.b64encode(raw.getvalue()).decode()],
                "info":json.dumps({"engine":"zimage","jobId":"native123","elapsedMs":3000})}
            with patch("infofilm.image_studio.httpx.Client") as client:
                api = client.return_value.__enter__.return_value
                api.post.return_value = response
                render_studio(self.config(), store, store.root/"take", 0, params)
                render_studio(self.config(), store, store.root/"take", 0, params)
                self.assertEqual(api.post.call_count, 1)
                self.assertEqual(api.post.call_args.args[0], "/sdapi/v1/txt2img")
                with Image.open(store.root/"take/candidate-0.png") as image:
                    self.assertEqual(image.size, (1536,864))
            store.db.close()

    def test_unknown_submission_is_reconciled_or_blocked_not_blindly_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            params = {"prompt":"positive prompt", "width":"1536", "height":"864", "steps":"10", "seed":"99"}
            write(store.root/"provider-0.json", {"params":params,"payload":studio_payload(self.config(),params),"submitted_at":123})
            with patch("infofilm.image_studio.httpx.Client") as client:
                api = client.return_value.__enter__.return_value
                api.get.return_value.json.return_value = {"history": []}
                with self.assertRaisesRegex(RuntimeError, "Unknown ImageStudio submission"):
                    render_studio(self.config(), store, store.root, 0, params)
                api.post.assert_not_called()
            store.db.close()

    def test_metrics_charge_failed_takes_and_distinguish_first_pass_from_repair_success(self):
        def take(seconds, passed):
            return {"generation_seconds":seconds,"review":{"pass":passed}}
        cases = [{"asset":"A1","accepted":True,"accepted_take":1,"takes":[take(12,True)]},
                 {"asset":"A2","accepted":True,"accepted_take":3,"takes":[take(2,False),take(3,False),take(4,True)]},
                 {"asset":"A3","accepted":False,"accepted_take":None,"takes":[take(5,False)]}]
        value = summary(cases)
        self.assertEqual(value["accepted_images"],2)
        self.assertEqual(value["first_pass_images"],1)
        self.assertEqual(value["accepted_within_two_takes"],1)
        self.assertEqual(value["generation_takes"],5)
        self.assertEqual(value["generation_seconds_per_accepted_image_including_failures"],13)
        self.assertEqual(value["warm_median_request_seconds"],3.5)

    def test_report_resolves_relative_output_and_matches_selected_take(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)/"production"
            write(project/"scene-image-prompts.json", self.library())
            image = project/"original.png"
            image.write_bytes(b"production")
            manifest = {"A006":{"file":str(image),"sha256":digest(image.read_bytes())}}
            write(project/"image_manifest.json",manifest)
            config = self.config()
            root,plan,original=prepare(project,config,{"zimage":{}},["A006"],2,Path(directory)/"benchmark")
            asset=plan["library"]["assets"][0]
            d=root/"zimage"
            write(d/"benchmark-config.json",config)
            image_root=d/"images"/image_identity(config,plan["library"],asset)[:20]
            candidate=image_root/"candidate-0.png"
            write(image_root/"provider-0.json",{"params":{"prompt":"initial"}})
            candidate.write_bytes(b"turbo")
            write(image_root/"provider-result-0.json",{"generation_seconds":3})
            write(image_root/"review-0.json",{"pass":True})
            write(image_root/"selected.json",{"file":str(candidate)})
            result=report(Path(os.path.relpath(root)),plan,original)
            self.assertEqual(result["engines"]["zimage"]["cases"][0]["accepted_take"],1)
            self.assertEqual(result["engines"]["zimage"]["summary"]["accepted_within_two_takes"],1)
            self.assertTrue(result["production_preserved"])

    def test_independent_typography_failure_does_not_change_original_loop_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            image=root/"candidate.png"
            image.write_bytes(b"image with title")
            selected={"A006":{"file":str(image),"sha256":digest(image.read_bytes()),"review":{"pass":True}}}
            write(root/"image_manifest.json",selected)
            with patch("infofilm.test_turbo_images.Models") as model:
                model.return_value.call.return_value={"has_overlaid_typography":True,"visible_text":["The Great Bengal Famine 1770"],"locations":["top"],"explanation":"Visible generated title"}
                audit_selected_text(root,self.config())
            self.assertFalse(read(root/"independent-text-audit.json")["A006"]["pass"])
            self.assertEqual(read(root/"image_manifest.json"),selected)

    def test_parking_releases_contexts_and_restores_services_after_render_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            config={**self.config(),"tts_url":"http://127.0.0.1:8092","qwen_url":"http://127.0.0.1:8075"}
            vibe,qwen=Mock(),Mock()
            vibe.json.return_value={"active_job":False,"queued_or_running_jobs":0}
            qwen.json.return_value={"busy":False,"state":"unloaded"}
            with patch("infofilm.image_studio.httpx.Client") as client, \
                 patch("infofilm.image_studio.httpx.get",return_value=Mock()), \
                 patch("infofilm.image_studio.subprocess.run",return_value=Mock(returncode=0)) as run, \
                 patch("infofilm.image_studio.subprocess.check_output",return_value=""):
                client.return_value.__enter__.return_value.get.side_effect=[vibe,qwen]
                with self.assertRaisesRegex(RuntimeError,"render failed"):
                    with park_idle_gpu_services(config,store):
                        raise RuntimeError("render failed")
            commands=[c.args[0] for c in run.call_args_list]
            self.assertIn(["systemctl","--user","stop","qimage21.service"],commands)
            self.assertIn(["systemctl","--no-ask-password","stop","vibevoice-api-local.service"],commands)
            self.assertEqual(commands[-2:],[
                ["systemctl","--no-ask-password","start","vibevoice-api-local.service"],
                ["systemctl","--user","start","qimage21.service"]])
            self.assertTrue(all('sudo' not in command for command in commands))
            self.assertTrue(all('is-active' in command for command in commands[:2]))
            for call in run.call_args_list:
                if '--user' in call.args[0]:
                    self.assertTrue(call.kwargs['env']['XDG_RUNTIME_DIR'])
                    self.assertTrue(call.kwargs['env']['DBUS_SESSION_BUS_ADDRESS'])
            store.db.close()

    def test_system_manager_query_failure_does_not_park_first_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            config={**self.config(),'tts_url':'http://127.0.0.1:8092','qwen_url':'http://127.0.0.1:8075'}
            vibe,qwen=Mock(),Mock();vibe.json.return_value={};qwen.json.return_value={'busy':False,'state':'unloaded'}
            with patch('infofilm.image_studio.httpx.Client') as client,patch('infofilm.image_studio.subprocess.run') as run:
                client.return_value.__enter__.return_value.get.side_effect=[vibe,qwen]
                run.side_effect=[Mock(returncode=0),Mock(returncode=1,stderr='System bus unavailable',stdout='')]
                with self.assertRaisesRegex(RuntimeError,'Cannot reach or query the service manager'):
                    with park_idle_gpu_services(config,store):pass
                self.assertEqual(run.call_count,2)
                self.assertTrue(all('is-active' in c.args[0] for c in run.call_args_list))
            store.db.close()

    def test_parking_refuses_active_narration_before_stopping_any_service(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            config={**self.config(),"tts_url":"http://127.0.0.1:8092","qwen_url":"http://127.0.0.1:8075"}
            vibe,qwen=Mock(),Mock()
            vibe.json.return_value={"active_job":True}
            qwen.json.return_value={"busy":False,"state":"unloaded"}
            with patch("infofilm.image_studio.httpx.Client") as client, \
                 patch("infofilm.image_studio.subprocess.run") as run:
                client.return_value.__enter__.return_value.get.side_effect=[vibe,qwen]
                with self.assertRaisesRegex(RuntimeError,"active work"):
                    with park_idle_gpu_services(config,store):pass
                run.assert_not_called()
            store.db.close()


if __name__ == "__main__":
    unittest.main()
