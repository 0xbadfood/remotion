import importlib.util
import tempfile
import unittest
from pathlib import Path

from infofilm.core import ROOT, read

spec = importlib.util.spec_from_file_location('subject_set_eval', ROOT/'scripts/evaluate-subject-set.py')
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


class SubjectSetEvaluationTests(unittest.TestCase):
    def test_schema_repair_merges_only_bad_rows_without_discarding_good_rows(self):
        class Models:
            def __init__(self): self.prompts = []
            def call(self, role, name, prompt, **kwargs):
                self.prompts.append(prompt)
                return {'items': [{'id':'S001','valid':True},{'id':'S002','valid':False}]} if len(self.prompts)==1 else {'items':[{'id':'S002','valid':True}]}
        models=Models()
        rows=evaluation.batch_call(models,'reviewer','test','Classify',{},['S001','S002'],
            lambda i,r: [] if r['valid'] else ['Invalid field'],100)
        self.assertEqual(rows,{'S001':{'id':'S001','valid':True},'S002':{'id':'S002','valid':True}})
        self.assertEqual(models.prompts[0],'Classify\nINPUT:\n{}')
        self.assertIn('ONLY for these repair IDs: ["S002"]',models.prompts[1])

    def test_batch_requires_exact_ids_without_duplicates_or_omissions(self):
        for rows in [[{'id':'S001'}], [{'id':'S001'}, {'id':'S001'}],
                     [{'id':'S001'}, {'id':'invented'}]]:
            with self.assertRaises(ValueError):
                evaluation.complete_items({'items':rows}, ['S001','S002'])
        rows = evaluation.complete_items({'items':[{'id':'S002'},{'id':'S001'}]}, ['S001','S002'])
        self.assertEqual(set(rows), {'S001','S002'})

    def test_scope_excerpts_cannot_introduce_unrequested_subjects(self):
        value={'coverage_intent':'focused','suggested_structure':'thematic','focus_terms':['core rope memory']}
        subject='Apollo guidance: core rope memory'
        self.assertFalse(evaluation.scope_errors(value,subject))
        self.assertTrue(evaluation.scope_errors({**value,'focus_terms':['Egyptian pyramids']},subject))
        self.assertTrue(evaluation.scope_errors({**value,'coverage_intent':'quota'},subject))

    def test_supplied_type_does_not_control_the_report_grouping(self):
        graph=read(ROOT/'subject-taxonomy.json')
        cases=[{'subject':'Quantum mechanics','type':'Empires & Civilizations'}]
        classification={'primary':'science.physics.quantum','secondary':['philosophy'],
            'lens':'explanation','ambiguity':'clear','coverage_intent':'focused'}
        state={'static':classification,'candidate':classification,'complete':True,'accepted':True,
            'review':{'pass':True,'issues':[]}}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            result=evaluation.report(root,cases,graph,{'S001':state})
            groups=read(root/'groups.json')
        self.assertEqual(result['primary_groups'],{'science.physics.quantum':1})
        self.assertEqual(result['family_groups'],{'science':1})
        self.assertEqual(list(groups),['science.physics.quantum'])
        self.assertNotIn('type_aligned',result)

    def test_reviewer_acceptance_does_not_erase_classification_uncertainty(self):
        graph=read(ROOT/'subject-taxonomy.json')
        cases=[{'subject':'Mercury','type':'Unused label'}]
        classification={'primary':'science','secondary':[],'lens':'explanation',
            'ambiguity':'ambiguous','coverage_intent':'overview'}
        state={'static':classification,'candidate':classification,'complete':True,'accepted':True,
            'review':{'pass':True,'issues':[]}}
        with tempfile.TemporaryDirectory() as directory:
            result=evaluation.report(Path(directory),cases,graph,{'S001':state})
        self.assertEqual(result['reviewer_accepted'],1)
        self.assertEqual(result['needs_review'],1)


if __name__=='__main__':unittest.main()
