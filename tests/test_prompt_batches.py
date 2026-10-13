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

    def test_planner_classifies_individual_assets_and_rejects_missing_profiles(self):
        library,film,evidence=self.fixture(2)
        def loop(stage,task,context,validator,**kwargs):
            self.assertIn('Classify the concrete image, not the film subject',task)
            result=self.planner([]).loop(stage,task,context,lambda x:[])
            self.assertTrue(validator(result))
            result['assets'][0]['review_profile']='scientific_diagram'
            result['assets'][1]['review_profile']='narrative'
            self.assertEqual(validator(result),[])
            return result
        result=make_image_prompts(Mock(loop=Mock(side_effect=loop)),library,[film],evidence,
                                  ['landscape'],message_recovery=True,classify_review=True)
        self.assertEqual([a['review_profile'] for a in result['assets']],['scientific_diagram','narrative'])

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
                with self.assertRaisesRegex(ModelOutputError,'9100-token'):      # retried once with 30% more room
                    models.call('planner','test','Prompt')
            self.assertEqual(len(list((Path(directory)/'requests').glob('*.response.json'))),2)
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

    def test_second_failure_simplifies_item_and_grants_extra_rounds(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory);models=Models({'planner_url':'p','reviewer_url':'r','max_repairs':1},store)
            batch=lambda v:{'assets':[{'id':'A001','prompt':'ok'},{'id':'A002','prompt':v}]}
            bad=lambda:{'pass':False,'score':70,'issues':[{'field':'assets[1].prompt','severity':'major','problem':'invented injury'}]}
            models.call=Mock(side_effect=[batch('x0'),bad(),batch('x1'),bad(),batch('x2'),
                                         {'pass':True,'score':90,'issues':[]},{'preferred':'B','reasons':[],'regressions':[]}])
            self.assertEqual(models.loop('batch','Task',{},lambda x:[],experimental=True),batch('x2'))
            drafts=[c.args[2] for c in models.call.call_args_list if c.args[0]=='planner']
            self.assertIn('FLAGGED UNITS',drafts[1]);self.assertNotIn('SIMPLIFY',drafts[1])
            self.assertIn('SIMPLIFY THESE UNITS',drafts[2]);self.assertIn('A002',drafts[2].split('SIMPLIFY THESE UNITS')[1])
            reviews=[c.args[2] for c in models.call.call_args_list if c.args[0]=='reviewer']
            self.assertNotIn('PRIOR FINDINGS',reviews[0]);self.assertIn('invented injury',reviews[1].split('PRIOR FINDINGS')[1])
            store.db.close()

    def test_scene_repair_cannot_insert_renumber_or_retime_scenes(self):
        from infofilm.llm import flagged_items, keep_unflagged
        scene=lambda i,w,kind='image',brief='ok':{'id':f'S{i:02d}','point_id':'P01','start_word_id':w,'kind':kind,'image_brief':brief}
        prior={'scenes':[scene(1,'w0001'),scene(2,'w0010','diagram','labels far too long'),scene(3,'w0020')]}
        flagged=flagged_items(prior,['S02: diagram labels/details are too long'],None)
        self.assertEqual(list(flagged),['S02'])
        # The draft fixes S02 but also splits it, renumbering S03 -> S04 and moving its start word.
        draft={'scenes':[scene(1,'w0001','image','rewritten'),scene(2,'w0012','image','short fixed'),
                         scene(3,'w0016','image','new split'),scene(4,'w0020','image','renumbered')]}
        merged=keep_unflagged(prior,draft,flagged)['scenes']
        self.assertEqual([x['id'] for x in merged],['S01','S02','S03'])
        self.assertEqual(merged[0],prior['scenes'][0]); self.assertEqual(merged[2],prior['scenes'][2])
        self.assertEqual(merged[1]['image_brief'],'short fixed'); self.assertEqual(merged[1]['start_word_id'],'w0010')

    def test_repair_keeps_unflagged_assets_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory);models=Models({'planner_url':'p','reviewer_url':'r','max_repairs':1},store)
            first={'assets':[{'id':'A001','prompt':'good'},{'id':'A002','prompt':'bad'}]}
            redraft={'assets':[{'id':'A001','prompt':'drifted'},{'id':'A002','prompt':'fixed'}]}
            models.call=Mock(side_effect=[first,{'pass':False,'score':70,'issues':[{'field':'A002.prompt','severity':'major'}]},
                                         redraft,{'pass':True,'score':90,'issues':[]},{'preferred':'B','reasons':[],'regressions':[]}])
            result=models.loop('batch','Task',{},lambda x:[],experimental=True)
            self.assertEqual([a['prompt'] for a in result['assets']],['good','fixed'])
            store.db.close()

    def test_batch_level_error_does_not_lock_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory);models=Models({'planner_url':'p','reviewer_url':'r','max_repairs':1},store)
            first={'assets':[{'id':'A001','prompt':'a'}],'v':0};second={'assets':[{'id':'A001','prompt':'b'}],'v':1}
            models.call=Mock(side_effect=[first,second])
            validator=lambda x:['assets must cover every scene'] if x['v']==0 else []
            self.assertEqual(models.loop('batch','Task',{},validator,experimental=False),second)
            store.db.close()

    def test_resume_after_an_exhausted_loop_gets_extra_rounds(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory);models=Models({'planner_url':'p','reviewer_url':'r','max_repairs':0},store)
            models.call=Mock(side_effect=ModelOutputError('malformed'))
            with self.assertRaisesRegex(RuntimeError,'exhausted'):
                models.loop('batch','Task',{},lambda x:[],experimental=False)
            self.assertEqual(models.call.call_count,1)
            models.call=Mock(side_effect=[ModelOutputError('malformed'),{'value':1}])
            self.assertEqual(models.loop('batch','Task',{},lambda x:[],experimental=False),{'value':1})
            self.assertEqual(models.call.call_count,2)
            store.db.close()

