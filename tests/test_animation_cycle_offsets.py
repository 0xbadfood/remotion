"""A continued cycle uses the same phase in Python geometry QA and Remotion."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from infofilm.animation_frontier import merge_animation
from infofilm.animation_motion import compile_motion_v2, transforms
from infofilm.core import ROOT
from test_animation_experiment import narration
from test_animation_frontier import canvas
import test_animation_workflow as fixtures


class CycleOffsetTests(unittest.TestCase):
    def test_legacy_zero_offset_and_seam_override_remain_unchanged(self):
        scene = fixtures.LinkedMotionTests().scene()
        initial = transforms(scene, 0, {})
        scene['cycles'][0]['phase_offset'] = .375
        shifted = transforms(scene, 0, {})
        self.assertAlmostEqual(shifted['crank']['rotation'], 135)
        self.assertEqual(transforms(scene, 0, {}, {'C': 0}), initial)
        self.assertAlmostEqual(transforms(scene, 0, {}, {'C': 1})['crank']['rotation'], 360)

    def test_compiler_accepts_normalized_offset_and_rejects_invalid_values(self):
        for offset in [0, .375, .999]:
            draft = fixtures.animation(); draft['cycles'][0]['phase_offset'] = offset
            plan, errors = compile_motion_v2(merge_animation(draft, fixtures.placement(), canvas()),
                fixtures.components(), {}, narration(), 1280, 720)
            self.assertFalse(errors, errors)
            self.assertEqual(plan['cycles'][0]['phase_offset'], offset)
        for offset in [-.1, 1, float('nan'), float('inf'), True, '0.5']:
            draft = fixtures.animation(); draft['cycles'][0]['phase_offset'] = offset
            _, errors = compile_motion_v2(merge_animation(draft, fixtures.placement(), canvas()),
                fixtures.components(), {}, narration(), 1280, 720)
            self.assertTrue(any('phase_offset' in error for error in errors), errors)

    def test_linked_mechanism_preserves_actual_typescript_parity_with_carried_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / 'motion.cjs'
            compile_script = "const ts=require('./node_modules/typescript'),fs=require('fs');fs.writeFileSync(process.argv[1],ts.transpileModule(fs.readFileSync('src/animation-experiment/motion.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText)"
            subprocess.run(['node', '-e', compile_script, str(module)], cwd=ROOT / 'renderer',
                check=True, capture_output=True)
            scene = fixtures.LinkedMotionTests().scene(); scene['cycles'][0]['phase_offset'] = .375
            times = [0, .1, .25, .75, 1.25, 2, 7.99, 8]
            script = "const m=require(process.argv[1]),p=JSON.parse(process.argv[2]);process.stdout.write(JSON.stringify(JSON.parse(process.argv[3]).map(t=>m.computeSceneTransforms(p,t,{}))))"
            output = subprocess.run(['node', '-e', script, str(module), json.dumps(scene), json.dumps(times)],
                check=True, capture_output=True, text=True)
            for seconds, actual in zip(times, json.loads(output.stdout)):
                expected = transforms(scene, seconds, {})
                for identity in expected:
                    for key in expected[identity]:
                        self.assertAlmostEqual(expected[identity][key], actual[identity][key], places=6)


if __name__ == '__main__':
    unittest.main()
