"""Local repair cannot introduce prose, metadata inventions or semantic approval."""
import copy
import tempfile
import unittest

from infofilm.animation_schema_repair import scope, apply_verdict, LocalSchemaRepair
from infofilm.animation_experiment import phase, component_errors
from infofilm.animation_frontier import CANVAS_SCHEMA, COMPONENT_SCHEMA, canvas_errors
from infofilm.image_animation_contracts import GROUNDING_SCHEMA, grounding_errors
from infofilm.core import Store, read
from test_animation_experiment import FakeModels, good_review, requirement
from test_animation_frontier import canvas
from test_animation_workflow import teaching, components
from test_image_animation_contracts import fixture as image_grounding


class Local:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def call(self, role, name, prompt, **kwargs):
        self.calls.append(prompt)
        return copy.deepcopy(self.responses.pop(0))


def fixture():
    original = teaching()
    original['teaching_sequence'][0]['demonstration'] = 'Move together here.'
    value = canvas()
    value['teaching_sequence'] = copy.deepcopy(original['teaching_sequence'])
    value['teaching_sequence'][0]['demonstration'] = 'Move together.'
    context = {'learning_objective': requirement(), 'teaching_plan': original, 'output_schema': CANVAS_SCHEMA}
    validator = lambda v: canvas_errors(v, requirement(), value['viewport']) + (
        [] if v.get('teaching_sequence') == original['teaching_sequence'] else ['Canvas must preserve accepted teaching_sequence'])
    return value, context, validator


def verdict(limits):
    return {'patches': [{'op': 'copy', **p} for p in limits['allowed_copies']],
            'prose_checks': [{'field': p['field'], 'same_meaning': True, 'confidence': .99,
                             'reason': 'Only the redundant contextual word differs.'} for p in limits['prose_pairs']],
            'explanation': 'Restore authoritative fields only.'}


class SchemaRepairTests(unittest.TestCase):
    def test_wrong_nested_scene_version_offers_only_the_trusted_specific_copy(self):
        canvas_plan, value = image_grounding()
        value['scene']['schema_version'] = 'infofilm.animation-scene.v1'
        original = copy.deepcopy(value)
        context = {'output_schema': GROUNDING_SCHEMA}
        limits = scope(value, context)
        self.assertEqual(limits['allowed_copies'], [
            {'path': '/scene/schema_version', 'from': '/frozen_values/F001'}])
        self.assertEqual(limits['frozen_values'], {'F001': 'infofilm.image-animation-scene.v1'})
        self.assertEqual(limits['prose_pairs'], [])
        self.assertEqual(limits['geometry_constraints'], [])
        fixed = apply_verdict(value, limits, verdict(limits))
        expected = copy.deepcopy(value)
        expected['scene']['schema_version'] = GROUNDING_SCHEMA['scene']['schema_version']
        self.assertEqual(fixed, expected)
        self.assertEqual(grounding_errors(fixed, canvas_plan, (1000, 700)), [])
        self.assertEqual(value, original)

    def test_correct_or_untrusted_nested_scene_version_has_no_copy_offer(self):
        _, value = image_grounding()
        self.assertEqual(scope(value, {'output_schema': GROUNDING_SCHEMA})['allowed_copies'], [])
        value['scene']['schema_version'] = 'infofilm.animation-scene.v1'
        for scene_schema in ({'schema_version': 'untrusted.scene.v1'}, {'schema_version': None},
                {'schema_version': ['infofilm.image-animation-scene.v1']}, None, []):
            with self.subTest(scene_schema=scene_schema):
                output_schema = {'schema_version': value['schema_version'], 'scene': scene_schema}
                self.assertEqual(scope(value, {'output_schema': output_schema})['allowed_copies'], [])
        value['scene'] = None
        self.assertEqual(scope(value, {'output_schema': GROUNDING_SCHEMA})['allowed_copies'], [])

    def test_nested_scene_version_offer_cannot_authorize_geometry_or_another_source(self):
        _, value = image_grounding()
        value['scene']['schema_version'] = 'infofilm.animation-scene.v1'
        limits = scope(value, {'output_schema': GROUNDING_SCHEMA})
        for path, source in (('/scene/controller/parameters/radius_px', '/frozen_values/F001'),
                ('/scene/background_prompt', '/frozen_values/F001'),
                ('/scene/schema_version', '/untrusted/schema_version')):
            response = verdict(limits)
            response['patches'] = [{'op': 'copy', 'path': path, 'from': source}]
            with self.subTest(path=path, source=source), self.assertRaisesRegex(ValueError, 'unauthorized'):
                apply_verdict(value, limits, response)

    def test_equivalent_prose_restores_original_without_using_local_words(self):
        value, context, validate = fixture(); original = copy.deepcopy(value)
        limits = scope(value, context)
        fixed = apply_verdict(value, limits, verdict(limits))
        self.assertEqual(validate(fixed), [])
        self.assertEqual(value, original)
        self.assertEqual(fixed['teaching_sequence'], context['teaching_plan']['teaching_sequence'])
        for key in set(value)-{'teaching_sequence'}:
            self.assertEqual(fixed[key], value[key])

    def test_changed_or_uncertain_meaning_cannot_pass(self):
        value, context, _ = fixture(); limits = scope(value, context)
        for field, altered in [('same_meaning', False), ('confidence', .85), ('confidence', True), ('confidence', float('nan'))]:
            response = verdict(limits);response['prose_checks'][0][field] = altered
            with self.assertRaisesRegex(ValueError, 'meaning changed or equivalence'):
                apply_verdict(value, limits, response)

    def test_missing_or_duplicate_prose_checks_are_rejected(self):
        value, context, _ = fixture();limits = scope(value, context)
        for checks in [[], [None], verdict(limits)['prose_checks']*2]:
            response = verdict(limits);response['prose_checks'] = checks
            with self.assertRaises(ValueError):apply_verdict(value, limits, response)

    def test_local_cannot_write_prose_or_unapproved_metadata(self):
        value, context, _ = fixture();limits = scope(value, context)
        for patch in [{'op':'replace','path':'/learning_objective','value':'Invented'},
                      {'op':'copy','path':'/viewport','from':'/frozen_values/F001'},
                      {'op':'copy','path':'/teaching_sequence','from':'/untrusted/prose'},
                      {'op':'copy','path':'/teaching_sequence','from':'/frozen_values/F001','value':'Injected'}]:
            response = verdict(limits);response['patches'] = [patch]
            with self.assertRaisesRegex(ValueError, 'unauthorized'):
                apply_verdict(value, limits, response)

    def test_changed_beat_order_is_not_a_copy_repair(self):
        value, context, _ = fixture()
        value['teaching_sequence'][0]['id'] = 'new-plan'
        self.assertEqual(scope(value, context)['allowed_copies'], [])

    def test_circle_repair_preserves_width_center_and_all_other_geometry(self):
        value={'schema_version':'infofilm.animation-placement.v1','objects':[{'id':'ball','x':.3,'y':.4,'width':.033,'height':.033,'rotation':0}]}
        context={'output_schema':{'schema_version':'infofilm.animation-placement.v1'},
            'canvas_plan':{'viewport':{'width':1280,'height':720}},
            'accepted_components':{'objects':[{'id':'ball','kind':'svg','asset_id':'ball.svg'}]},
            'resolved_assets':{'ball.svg':{'capabilities':['circular']}}}
        limits=scope(value,context);fixed=apply_verdict(value,limits,verdict(limits))
        self.assertAlmostEqual(fixed['objects'][0]['height']*720,.033*1280)
        for key in ('x','y','width','rotation'):
            self.assertEqual(fixed['objects'][0][key],value['objects'][0][key])
        self.assertEqual(limits['prose_pairs'],[])
        self.assertEqual(len(limits['geometry_constraints']),1)
        self.assertEqual(scope(fixed,context)['allowed_copies'],[])

    def test_non_circular_artwork_cannot_get_circle_normalization(self):
        value={'schema_version':'infofilm.animation-placement.v1','objects':[{'id':'car','width':.1,'height':.1}]}
        context={'output_schema':{'schema_version':'infofilm.animation-placement.v1'},
            'canvas_plan':{'viewport':{'width':1280,'height':720}},
            'accepted_components':{'objects':[{'id':'car','kind':'svg','asset_id':'car.svg'}]},
            'resolved_assets':{'car.svg':{'capabilities':['top-down']}}}
        self.assertEqual(scope(value,context)['allowed_copies'],[])

    def test_missing_repeat_metadata_copies_frozen_values(self):
        value, context, validate = fixture()
        value['teaching_sequence'][0].pop('required_behaviors')
        fixed = apply_verdict(value, scope(value, context), verdict(scope(value, context)))
        self.assertEqual(validate(fixed), [])
        self.assertEqual(fixed['teaching_sequence'][0]['required_behaviors'], ['continuous_rotation','synchronized_cycle'])

    def test_component_capabilities_derive_from_requests_without_new_objects(self):
        value = components();original = copy.deepcopy(value)
        value['objects'][0].pop('required_capabilities')
        value['objects'].append({'id':'curve','kind':'svg','role':'Path','conveys':'Circular path','physics_role':'static',
                                'asset_id':None,'request_id':'curve-request'})
        value['component_requests']=[{'id':'curve-request','requested_asset_id':'geometry.curve.v1',
            'description':'Circle','required_capabilities':['circular','center-pivot'],'svg_requirements':{}}]
        context = {'requirement':requirement(),'output_schema':COMPONENT_SCHEMA}
        limits = scope(value, context)
        fixed = apply_verdict(value, limits, verdict(limits))
        self.assertEqual(component_errors(fixed,requirement()), [])
        self.assertEqual(len(fixed['objects']),len(value['objects']))
        self.assertEqual(fixed['objects'][0], original['objects'][0])
        self.assertEqual(fixed['objects'][-1]['required_capabilities'], ['circular','center-pivot'])
        self.assertEqual(fixed['component_requests'], value['component_requests'])

    def test_failure_only_and_full_review_still_required(self):
        value, context, validate = fixture();limits = scope(value, context)
        local = Local([verdict(limits)])
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                models = FakeModels([value],[good_review()])
                models.schema_repairer = LocalSchemaRepair(local,store)
                selected = phase(models,store,'canvas-landscape','Canvas',context,validate,2000)
                self.assertEqual(len(models.calls),1)
                self.assertEqual(len(models.review_calls),1)
                self.assertEqual(len(local.calls),1)
                self.assertIn('validator_errors',local.calls[0])
                self.assertIn('same teaching meaning',local.calls[0])
                evaluation = read(next(store.root.glob('planning/canvas-landscape/*/evaluation.json')))
                self.assertTrue(evaluation['attempts'][0]['local_schema_repair']['accepted'])
                fresh = FakeModels();fresh.schema_repairer = LocalSchemaRepair(Local([]),store)
                self.assertEqual(phase(fresh,store,'canvas-landscape','Canvas',context,validate,2000),selected)
                self.assertEqual(fresh.calls,[])
            finally:store.db.close()

    def test_passing_candidate_never_calls_local(self):
        value, context, validate = fixture();value['teaching_sequence'] = context['teaching_plan']['teaching_sequence']
        local = Local([])
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                models = FakeModels([value],[good_review()]);models.schema_repairer = LocalSchemaRepair(local,store)
                phase(models,store,'canvas-landscape','Canvas',context,validate,2000)
                self.assertEqual(local.calls,[])
            finally:store.db.close()

    def test_local_repair_does_not_override_semantic_rejection(self):
        value, context, validate = fixture();fixed = copy.deepcopy(value);fixed['teaching_sequence'] = context['teaching_plan']['teaching_sequence']
        bad_review = {'pass':False,'score':70,'issues':[{'severity':'major','field':'zones',
            'problem':'Wrong arrangement','correction':'Reconsider the comparison'}],'strengths':[]}
        local = Local([verdict(scope(value,context))])
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            try:
                models=FakeModels([value,fixed],[bad_review,good_review()]);models.schema_repairer=LocalSchemaRepair(local,store)
                phase(models,store,'canvas-landscape','Canvas',context,validate,2000,max_attempts=2)
                self.assertEqual(len(models.calls),2)
                self.assertEqual(len(models.review_calls),2)
                self.assertEqual(len(local.calls),1)
            finally:store.db.close()


if __name__=='__main__':unittest.main()
