import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from infofilm.core import ROOT,read
from infofilm.subject_taxonomy import classify,classification_errors,expand,model_classify,planner_request,seed_subject,validate_graph


class SubjectTaxonomyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph=read(ROOT/'subject-taxonomy.json')

    def test_inheritance_preserves_category_attributes_without_forcing_dates_into_anatomy(self):
        validate_graph(self.graph)
        anatomy=expand(self.graph,'science.biology.anatomy')
        self.assertIn('structures',anatomy['attributes'])
        self.assertIn('evidence',anatomy['attributes'])
        self.assertNotIn('events',anatomy['attributes'])
        history=expand(self.graph,'history.era')
        self.assertIn('events',history['attributes'])
        self.assertIn('periods',history['attributes'])

    def test_scoped_subjects_and_requested_lenses_do_not_inherit_an_unrelated_chapter_outline(self):
        broad=seed_subject('The human anatomy',self.graph)
        heart=seed_subject('Anatomy of the heart',self.graph)
        historical=seed_subject('History of human anatomy',self.graph)
        self.assertEqual(broad['classification']['primary'],'science.biology.anatomy')
        self.assertTrue(broad['segmentation_axes'])
        self.assertFalse(heart['segmentation_axes'])
        self.assertEqual(heart['classification']['primary'],'science.biology.anatomy.regional')
        self.assertEqual(historical['classification']['primary'],'history')
        self.assertIn('science.biology.anatomy',historical['classification']['secondary'])
        self.assertIn('actors',historical['profile']['attributes'])
        procedural=seed_subject('How to train a neural network',self.graph)
        self.assertEqual(procedural['classification']['lens'],'procedure')
        self.assertIn('domain_questions',procedural['profile'])

    def test_unknown_and_interdisciplinary_routing_is_explicitly_reviewable(self):
        unknown=classify(self.graph,'Xyzzor protocol')
        self.assertEqual(unknown['primary'],'general')
        self.assertTrue(unknown['needs_classification_review'])
        mixed=classify(self.graph,'The economics of climate change')
        self.assertTrue(mixed['needs_classification_review'])
        self.assertTrue(mixed['secondary'])

    def test_graph_owned_broad_axes_work_outside_anatomy_and_respect_focused_scope(self):
        graph=copy.deepcopy(self.graph)
        graph['nodes']['technology.computing']['whole_subject_axes']=['Components','Interactions','Tradeoffs']
        classification={'primary':'technology.computing','secondary':[],'lens':'explanation',
            'scope_extent':'broad','ambiguity':'clear','rationale':'Whole system explanation'}
        self.assertEqual(seed_subject('Computer systems',graph,classification=classification)['segmentation_axes'],
            ['Components','Interactions','Tradeoffs'])
        self.assertEqual(seed_subject('Core rope memory',graph,classification={**classification,'scope_extent':'focused'})['segmentation_axes'],[])
        classification.update(primary='science.biology.anatomy',scope_extent='focused')
        self.assertEqual(seed_subject('Human anatomy',graph,classification=classification)['segmentation_axes'],[])

    def test_graph_rejects_cycles_and_dangling_prerequisites(self):
        graph=copy.deepcopy(self.graph)
        graph['nodes']['science']['parent']='science.biology'
        with self.assertRaisesRegex(ValueError,'cycle'):validate_graph(graph)
        graph=copy.deepcopy(self.graph)
        graph['nodes']['history']['relations'][0]['from']='imaginary'
        with self.assertRaisesRegex(ValueError,'relations'):validate_graph(graph)

    def test_unknown_model_categories_retry_without_allowing_models_to_generate_the_seed_questions(self):
        good={'primary':'science.biology.anatomy','secondary':[],'lens':'explanation',
              'scope_extent':'broad','ambiguity':'clear','rationale':'The requested subject is whole-body anatomy.'}
        models=Mock();models.call.side_effect=[{**good,'primary':'science.imaginary'},good,{'pass':True,'issues':[]}]
        with tempfile.TemporaryDirectory() as directory:
            classification=model_classify('Human anatomy',self.graph,models,Path(directory))
        result=seed_subject('Human anatomy',self.graph,classification=classification)
        self.assertEqual(result['profile']['seeds'],expand(self.graph,good['primary'])['seeds'])
        self.assertTrue(classification['model_classification_accepted'])
        self.assertEqual([call.args[0] for call in models.call.call_args_list],['reviewer','reviewer','planner'])

    def test_rejected_classification_is_not_reported_as_accepted(self):
        good={'primary':'science.biology.anatomy','secondary':[],'lens':'explanation',
              'scope_extent':'broad','ambiguity':'clear','rationale':'Whole-body anatomy.'}
        models=Mock();models.call.side_effect=[good,{'pass':False,'issues':['Wrong scope']},good,{'pass':False,'issues':['Still wrong scope']}]
        with tempfile.TemporaryDirectory() as directory:
            result=model_classify('Human anatomy',self.graph,models,Path(directory))
        self.assertFalse(result['model_classification_accepted'])
        self.assertTrue(result['needs_classification_review'])
        self.assertTrue(classification_errors({**good,'primary':['not an id']},self.graph))
        self.assertTrue(classification_errors({**good,'secondary':['science']},self.graph))

    def test_each_node_owns_a_valid_prompt_shape_and_invalid_templates_fail(self):
        for category in self.graph['nodes']:
            shape=expand(self.graph,category)['planner_prompt_shape']
            self.assertEqual(shape['sentinel'],category)
        graph=copy.deepcopy(self.graph)
        del graph['nodes']['science.biology.anatomy']['planner_prompt_shape']
        with self.assertRaisesRegex(ValueError,'own planner prompt'):validate_graph(graph)
        graph=copy.deepcopy(self.graph)
        graph['nodes']['history']['planner_prompt_shape']['template']+=' ${invented_field}'
        with self.assertRaisesRegex(ValueError,'placeholders'):validate_graph(graph)

    def test_handoff_uses_selected_scope_and_lens_without_historical_example_years(self):
        for subject in ['The human anatomy','Anatomy of the heart','History of human anatomy','How to train a neural network']:
            seeds=seed_subject(subject,self.graph)
            handoff=planner_request(seeds,self.graph,advisory_seconds=120)
            prompt=handoff['messages'][1]['content']
            self.assertEqual(handoff['sentinel'],seeds['classification']['primary'])
            self.assertIn('ADVISORY DURATION IN SECONDS: 120',prompt)
            self.assertIn('"start_year": null',prompt)
            self.assertNotIn('1526',prompt)
            self.assertNotIn('1556',prompt)
            self.assertIn('"segmentation_axes": '+str(seeds['segmentation_axes']).replace("'",'"'),prompt)
            if subject.startswith('How to'):
                self.assertIn('"domain_questions"',prompt)
            if subject.startswith('History'):
                self.assertIn('"secondary": ["science.biology.anatomy"]',prompt)
                self.assertIn('"actors"',prompt)

    def test_pending_classification_and_sources_are_separate_from_grounded_handoff(self):
        brief={'sources':[{'id':'S1','title':'Fixture excerpt','url':'https://example.org/fixture','excerpts':['Fixture statement.']}],
               'anchors':[{'id':'F01','claim':'Fixture statement.','source_ids':['S1']}],'required_anchor_ids':['F01']}
        seeds=seed_subject('The human anatomy',self.graph)
        pending=planner_request(seeds,self.graph)
        self.assertFalse(pending['ready_for_planning'])
        self.assertEqual(pending['pending'],['source_research'])
        grounded=planner_request(seeds,self.graph,brief)
        self.assertTrue(grounded['ready_for_planning'])
        self.assertIn('Fixture statement.',grounded['messages'][1]['content'])
        self.assertIn('"required_anchor_ids": ["F01"]',grounded['messages'][1]['content'])
        uncertain=planner_request(seed_subject('Mercury',self.graph),self.graph,brief)
        self.assertFalse(uncertain['ready_for_planning'])
        self.assertIn('classification_review',uncertain['pending'])
        with self.assertRaisesRegex(ValueError,'Invalid source brief'):
            planner_request(seeds,self.graph,{**brief,'anchors':[{'id':'F01','claim':'Fixture','source_ids':['missing']}]})

    def test_handoff_rejects_stale_graph_and_preserves_input_literals(self):
        seeds=seed_subject('The human anatomy ${source_brief_json}',self.graph)
        handoff=planner_request(seeds,self.graph)
        self.assertIn('The human anatomy ${source_brief_json}',handoff['messages'][1]['content'])
        self.assertEqual(handoff['messages'][0]['role'],'system')
        stale=copy.deepcopy(seeds);stale['taxonomy_sha256']='wrong'
        with self.assertRaisesRegex(ValueError,'frozen taxonomy'):planner_request(stale,self.graph)
        for value in [0,-1,float('nan'),True]:
            with self.assertRaisesRegex(ValueError,'positive finite'):planner_request(seeds,self.graph,advisory_seconds=value)


if __name__=='__main__':unittest.main()
