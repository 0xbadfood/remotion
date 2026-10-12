import unittest
from pathlib import Path
from unittest.mock import Mock

from infofilm.educational_review import GUIDANCE, inventory_errors, normalize_inventory, component_checks_enabled
from infofilm.image_providers import image_review_errors
from infofilm.image_recovery import current_image_review


class EducationalReviewTests(unittest.TestCase):
    def review(self):
        return {'pass':True,'reason_keys':[],'issues':[],'limitations':[],'correction':'',
                'component_inventory':[{'component':'gallbladder','expected_count':1,'visible_count':1,
                                        'count_status':'met','placement_status':'met','reference_checked':True,
                                        'evidence':'One green sac under the liver matches the reference'}]}

    def test_duplicate_counts_and_wrong_placement_cannot_hide_in_a_pass(self):
        v=self.review();self.assertEqual(image_review_errors(v),[])
        self.assertTrue(image_review_errors(v,reference_available=False))
        v['component_inventory'][0]['visible_count']=2
        self.assertTrue(image_review_errors(v))
        v=self.review();v['component_inventory'][0]['placement_status']='not_met'
        self.assertTrue(image_review_errors(v))

    def test_uncertain_visibility_does_not_invent_a_missing_component(self):
        v=self.review();item=v['component_inventory'][0]
        item.update(visible_count=None,count_status='uncertain',placement_status='not_applicable',reference_checked=False,
                    evidence='The organ is obscured; no relevant reference image was supplied')
        self.assertEqual(image_review_errors(v),[])
        item['placement_status']='met'
        self.assertTrue(inventory_errors(v))

    def test_frozen_reviews_and_non_diagram_scenes_remain_usable(self):
        v=self.review();del v['component_inventory']
        self.assertEqual(image_review_errors(v),[])
        self.assertTrue(image_review_errors(v,require_inventory=True))
        v['component_inventory']=[];self.assertEqual(image_review_errors(v),[])
        v['component_inventory']=None;self.assertTrue(image_review_errors(v))

    def test_invalid_counts_and_duplicate_inventory_names_are_rejected(self):
        for number in [True,-1,1.5,'two']:
            v=self.review();v['component_inventory'][0]['visible_count']=number
            self.assertTrue(inventory_errors(v))
        v=self.review();v['component_inventory']*=2;self.assertTrue(inventory_errors(v))

    def test_production_review_sends_targeted_inventory_guidance_with_current_prompt(self):
        models=Mock();v={**self.review(),'visual_message':'The liver supports digestion.',
                         'repair_mode':'none','mode_reason':''};models.call.return_value=v
        v['component_inventory'][0].update(placement_status='not_applicable',reference_checked=False,
                                           evidence='One sac is visible; no source image was supplied')
        asset={'id':'A001','prompt':'One liver and one gallbladder.', 'review_profile':'scientific_diagram'}
        case={'visual_message':v['visual_message'],'library':{'assets':[asset]}}
        current_image_review(models,case,Path('/tmp/candidate.png'),asset['prompt'],'test')
        prompt=models.call.call_args.args[2]
        self.assertIn(GUIDANCE,prompt)
        self.assertIn(asset['prompt'],prompt)
        self.assertIn('casual viewers/junior students',prompt)

    def test_unchecked_reference_status_is_normalized_without_altering_counts_or_verdict(self):
        v=self.review();item=v['component_inventory'][0]
        item.update(reference_checked=False,visible_count=2)
        fixed=normalize_inventory(v)
        self.assertEqual(fixed['component_inventory'][0]['placement_status'],'not_applicable')
        self.assertEqual(item['placement_status'],'met', 'Raw response is preserved')
        self.assertEqual(fixed['component_inventory'][0]['visible_count'],2)
        self.assertTrue(image_review_errors(fixed), 'A known duplicate still blocks a pass')

    def test_historical_scene_does_not_activate_science_inventory(self):
        models=Mock();v={**self.review(),'visual_message':'A plane is parked on a tarmac.',
                         'repair_mode':'none','mode_reason':''}
        v['component_inventory'][0].update(reference_checked=False)
        models.call.return_value=v
        asset={'id':'A001','review_profile':'narrative'}
        case={'visual_message':v['visual_message'],'library':{'assets':[asset]}}
        result=current_image_review(models,case,Path('/tmp/candidate.png'),'A parked plane.','test')
        self.assertEqual(models.call.call_count,1)
        self.assertEqual(result['component_inventory'][0]['placement_status'],'not_applicable')
        self.assertNotIn(GUIDANCE,models.call.call_args.args[2])
        self.assertFalse(component_checks_enabled({}))
        self.assertTrue(component_checks_enabled({'review_profile':'explanatory_apparatus'}))


if __name__=='__main__':unittest.main()
