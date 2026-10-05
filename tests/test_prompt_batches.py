import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from infofilm.core import Store, read
from infofilm.llm import ModelOutputError, Models
from infofilm.planning import image_prompt_evidence, make_image_prompts


class PromptBatchTests(unittest.TestCase):
    def fixture(self, count=20):
        facts=[{'id':f'F{i:02d}','claim':f'A sourced claim {i}.'} for i in range(1,count+1)]
        assets=[{'id':f'A{i:03d}','purpose':f'Explain point {i}', 'prompt':'A concrete original scene.',
                 'acceptance':['Correct context'], 'fact_ids':[f'F{i:02d}']} for i in range(1,count+1)]
        points=[{'id':f'P{i:02d}','narration':f'Point {i} explanation.', 'fact_ids':[f'F{i:02d}']} for i in range(1,count+1)]
        scenes=[{'point_id':point['id'],'kind':'image','asset_id':asset['id'], 'heading':f'Point {i+1}',
                 'rationale':'Support the explanation'} for i,(point,asset) in enumerate(zip(points,assets))]
        return {'style':'Cinematic natural documentary.', 'assets':assets}, \
            {'requested_seconds':240,'script':{'points':points},
             'timing':{'points':[{'start':i*8,'end':(i+1)*8} for i in range(count)]}, 'storyboard':{'scenes':scenes}}, \
            {'title':'Example','facts':facts, 'chapter_context':{'film':{'subject':'Whole subject','central_question':'What changed?', 'large_unused':'x'*10000},
                'chapter':{'title':'Section','scope':{'description':'Relevant era'},'objective':'Explain this phase.'},
                'research_gaps':['x'*10000]}}

    def planner(self, captured):
        def loop(stage,task,context,validator,**kwargs):
            captured.append((stage,copy.deepcopy(context)))
            result={'style':context['style'],'assets':copy.deepcopy(context['used_assets']),
                    'banners':[{'scene_key':row['scene_key'],'text':'','reason':'Narration suffices'}
                               for rows in context['scene_contexts_by_asset'].values() for row in rows]}
            for a in result['assets']:
                a.update(visual_message=a['purpose']+' illustrates the sourced relationship.',
                         setting='A concrete subject-appropriate setting.',visual_style='Cinematic natural documentary.')
            self.assertEqual(validator(result),[])
            return result
        return Mock(loop=Mock(side_effect=loop))

    def test_twenty_assets_split_8_8_4_and_merge_exactly_with_local_facts(self):
        library,film,evidence=self.fixture(); captured=[]
        result=make_image_prompts(self.planner(captured),library,[film],evidence,['landscape'],message_recovery=True)
        self.assertEqual([len(c['used_assets']) for _,c in captured],[8,8,4])
        self.assertEqual([a['id'] for a in result['assets']],[a['id'] for a in library['assets']])
        self.assertEqual(len(result['banners']),20)
        self.assertEqual(result['planning_batches'],3)
        for stage,context in captured:
            self.assertTrue(stage.startswith('scene-image-prompts-batch-'))
            self.assertEqual({f['id'] for f in context['evidence']['facts']},{f for a in context['used_assets'] for f in a['fact_ids']})
            self.assertEqual(context['evidence']['chapter_context']['chapter']['scope'], {'description':'Relevant era'})
            self.assertNotIn('research_gaps',context['evidence']['chapter_context'])
            self.assertNotIn('large_unused',context['evidence']['chapter_context']['film'])
            self.assertEqual(len(context['film_asset_outline']),20)
        self.assertEqual(captured[1][1]['scene_contexts_by_asset']['A009'][0]['previous_narration'],'Point 8 explanation.')
        self.assertEqual(captured[0][1]['scene_contexts_by_asset']['A008'][0]['next_narration'],'Point 9 explanation.')

    def test_shared_image_keeps_all_scene_contexts_together_across_batch_boundary(self):
        library,film,evidence=self.fixture(10)
        film['storyboard']['scenes'][-1]['asset_id']='A001'
        captured=[]
        result=make_image_prompts(self.planner(captured),library,[film],evidence,['portrait'],message_recovery=True)
        self.assertEqual(len(result['assets']),9)
        self.assertEqual(len(result['banners']),10)
        self.assertEqual(len(captured[0][1]['scene_contexts_by_asset']['A001']),2)
        self.assertIn('F10',{f['id'] for f in captured[0][1]['evidence']['facts']})
        self.assertEqual(captured[0][1]['scene_contexts_by_asset']['A001'][1]['narration'],'Point 10 explanation.')

    def test_empty_assets_and_oversized_batch_fail_without_calls(self):
        library,film,evidence=self.fixture(0);captured=[]
        result=make_image_prompts(self.planner(captured),library,[film],evidence,['landscape'],message_recovery=True)
        self.assertEqual(result['assets'],[]);self.assertEqual(result['planning_batches'],0)
        with self.assertRaisesRegex(ValueError,'between 1 and 8'):
            make_image_prompts(self.planner(captured),library,[film],evidence,['landscape'],message_recovery=True,batch_size=9)

    def test_completed_batch_checkpoint_is_reused_when_later_batch_failed(self):
        library,film,evidence=self.fixture(9)
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory);models=Models({'planner_url':'p','reviewer_url':'r','max_repairs':0},store)
            calls=[]
            def call(role,name,prompt,**kwargs):
                calls.append(name)
                context=json.loads(prompt.split('INPUT CONTEXT:\n')[1])
                if '-batch-002-' in name:raise ModelOutputError('Invalid output')
                return self.planner([]).loop('batch','task',context,lambda x:[])
            models.call=call
            with self.assertRaisesRegex(RuntimeError,'exhausted'):
                make_image_prompts(models,library,[film],evidence,['landscape'],message_recovery=True,experimental=False)
            saved=list((Path(directory)/'planning/scene-image-prompts-batch-001').glob('*/selected.json'))
            self.assertEqual(len(saved),1)
            calls.clear()
            def recovered(role,name,prompt,**kwargs):
                calls.append(name)
                return self.planner([]).loop('batch','task',json.loads(prompt.split('INPUT CONTEXT:\n')[1]),lambda x:[])
            models.call=recovered
            result=make_image_prompts(models,library,[film],evidence,['landscape'],message_recovery=True,experimental=False)
            self.assertEqual(len(result['assets']),9)
            self.assertEqual(len(calls),1)
            self.assertIn('-batch-002-',calls[0])
            store.db.close()


class OutputContractTests(unittest.TestCase):
    def test_length_finish_reason_detected_even_if_content_happens_to_be_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory);models=Models({'planner_url':'http://planner/v1'},store);models.identities['planner']='model'
            response=httpx.Response(200,json={'choices':[{'finish_reason':'length','message':{'content':'{"value":1}'}}]},
                                    request=httpx.Request('POST','http://planner/v1/chat/completions'))
            client=Mock();client.post.return_value=response
            with patch('infofilm.llm.httpx.Client') as constructor:
                constructor.return_value.__enter__.return_value=client
                with self.assertRaisesRegex(ModelOutputError,'7000-token'):
                    models.call('planner','test','Prompt')
            self.assertEqual(len(list((Path(directory)/'requests').glob('*.response.json'))),1)
            self.assertFalse(list((Path(directory)/'requests').glob('*.result.json')))
            store.db.close()

    def test_malformed_draft_retries_without_reviewer_and_without_partial_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory);models=Models({'planner_url':'p','reviewer_url':'r','max_repairs':1},store)
            models.call=Mock(side_effect=[ModelOutputError('output limit'),{'value':1}])
            result=models.loop('test','Task',{},lambda x:[],experimental=False)
            self.assertEqual(result,{'value':1});self.assertEqual(models.call.call_count,2)
            self.assertTrue(all(c.args[0]=='planner' for c in models.call.call_args_list))
            self.assertIn('compact JSON',models.call.call_args_list[1].args[2])
            store.db.close()

    def test_malformed_outputs_stop_at_budget_without_selecting_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory);models=Models({'planner_url':'p','reviewer_url':'r','max_repairs':1},store)
            models.call=Mock(side_effect=ModelOutputError('malformed output'))
            with self.assertRaisesRegex(RuntimeError,'exhausted'):
                models.loop('test','Task',{},lambda x:[],experimental=False)
            self.assertEqual(models.call.call_count,2)
            self.assertFalse(list((Path(directory)/'planning').glob('**/selected.json')))
            store.db.close()


    def test_malformed_review_retries_same_candidate_without_new_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory);models=Models({'planner_url':'p','reviewer_url':'r','max_repairs':0},store)
            models.call=Mock(side_effect=[{'value':1}, ModelOutputError('malformed review'),
                                         {'pass':True,'score':90,'issues':[]}])
            self.assertEqual(models.loop('test','Task',{},lambda x:[],experimental=True),{'value':1})
            self.assertEqual([c.args[0] for c in models.call.call_args_list],['planner','reviewer','reviewer'])
            self.assertTrue(models.call.call_args_list[-1].args[1].endswith('-format-repair'))
            store.db.close()


    def test_format_retry_retains_existing_semantic_repair_instructions(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory);models=Models({'planner_url':'p','reviewer_url':'r','max_repairs':2},store)
            bad_review={'pass':False,'score':70,'issues':[{'severity':'major','correction':'Preserve the sourced relationship.'}]}
            models.call=Mock(side_effect=[{'value':0},bad_review,ModelOutputError('malformed repair'),
                                         {'value':1},{'pass':True,'score':90,'issues':[]},
                                         {'preferred':'B','reasons':[],'regressions':[]}])
            self.assertEqual(models.loop('test','Task',{},lambda x:[],experimental=True),{'value':1})
            last_draft=[c for c in models.call.call_args_list if c.args[0]=='planner'][-1]
            self.assertIn('Preserve the sourced relationship.',last_draft.args[2])
            self.assertIn('complete compact JSON',last_draft.args[2])
            store.db.close()
