import tempfile
import unittest
from pathlib import Path
from infofilm.core import Store, read, write
from infofilm.failure_tracking import image_snapshot, save_report, snapshot


class FailureTrackingTests(unittest.TestCase):
    def case(self, project):
        write(project/'scene-image-prompts.json', {'assets':[
            {'id':'A001','visual_message':'Accepted message','prompt':'Accepted original'},
            {'id':'A002','visual_message':'Failed message','prompt':'Failed original'}]})
        root=project/'images'/'root2'
        write(root/'message-state.json',{'visual_message':'Failed message','status':'exhausted','repairs':[{'repair_mode':'simplify'}],
            'takes':[{'scene':'Failed original','review':{'pass':False,'reason_keys':['factual_mismatch'],
                'issues':['Wrong map'],'repair_mode':'simplify','correction':'Use simpler illustration'},'candidate':str(root/'candidate.png')}]})
        write(project/'image_manifest.json',{'A001':{'file':str(project/'images/root1/takes/take-0/candidate.png'),
                                               'total_attempts':2}})
        return root

    def test_failed_assets_have_review_evidence_and_repaired_acceptances_are_separate(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.case(p)
            before={str(f):f.read_bytes() for f in p.rglob('*.json')}
            result=image_snapshot(p)
            self.assertEqual(result['accepted_images'],1)
            self.assertEqual(result['remaining_images'],1)
            self.assertEqual(result['unresolved_images'],1)
            self.assertEqual(result['accepted_after_repair'],1)
            self.assertEqual(result['mean_attempts_for_accepted'],2)
            self.assertEqual(result['unresolved'][0]['asset'],'A002')
            self.assertEqual(result['unresolved'][0]['issues'],['Wrong map'])
            self.assertEqual(before,{str(f):f.read_bytes() for f in p.rglob('*.json')})

    def test_blocked_pending_repair_is_visible_before_end_of_batch_report(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);root=self.case(p);state=read(root/'message-state.json')
            state.update(status='running',pending_repair=True);write(root/'message-state.json',state)
            store=Store(p);store.event('A002','repair_blocked',{'error':'unchanged scene'});store.db.close()
            result=image_snapshot(p)
            self.assertEqual(result['unresolved'][0]['status'],'repair_blocked')

    def test_archived_candidates_are_ignored_and_reports_do_not_change_pipeline_files(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.case(p)
            write(p/'image-revisions/old/images/other/message-state.json',{'status':'exhausted','visual_message':'Accepted message','takes':[]})
            before={str(f):f.read_bytes() for f in p.rglob('*.json')}
            result=snapshot(p);save_report(p/'failure-tracking/latest.json',result)
            self.assertEqual(result['chapters'][0]['images']['unresolved_images'],1)
            self.assertIn('A002',(p/'failure-tracking/latest.md').read_text())
            self.assertTrue(all(Path(path).read_bytes()==v for path,v in before.items()))

    def test_current_accepted_result_overrides_old_unresolved_report(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);root=self.case(p)
            write(p/'image-recovery-report.json',{'unresolved':[{'asset':'A002','status':'exhausted','checkpoint':str(root)}]})
            manifest=read(p/'image_manifest.json');manifest['A002']={'file':str(root/'takes/take-1/candidate.png'),'total_attempts':2}
            write(p/'image_manifest.json',manifest)
            result=image_snapshot(p)
            self.assertEqual(result['accepted_images'],2)
            self.assertEqual(result['unresolved_images'],0)
