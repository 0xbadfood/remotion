import unittest
from infofilm.anatomy_strict_review import decide, errors, gates


class StrictAnatomyReviewTests(unittest.TestCase):
    def value(self):
        return {'checks':{k:{'status':'met','observation':'One continuous passage is visible',
                            'location':'centre of candidate'} for k in gates({'id':'D06'})},
                'component_inventory':[{'component':'esophageal segment','expected_count':1,'visible_count':1,
                                        'count_status':'met','placement_status':'met','reference_checked':True,
                                        'evidence':'One central tube matches the source arrangement'}],
                'limitations':[],'inferred_details':[]}

    def test_code_owns_verdict_and_uncertain_cannot_pass(self):
        v=self.value(); self.assertEqual(decide(v),'pass')
        v['checks']['G02']['status']='uncertain';self.assertEqual(decide(v),'uncertain')
        v['checks']['G03']['status']='not_met';v['verdict']='pass';v['score']=100
        self.assertEqual(decide(v),'fail')

    def test_missing_gate_or_observation_is_rejected(self):
        v=self.value();self.assertEqual(errors(v,{'id':'D06'}),[])
        del v['checks']['G02'];self.assertTrue(errors(v,{'id':'D06'}))
        v=self.value();v['checks']['G01']['location']='';self.assertTrue(errors(v,{'id':'D06'}))

    def test_object_shape_and_all_case_gate_definitions(self):
        v=self.value();v['checks']=list(v['checks'].values());self.assertTrue(errors(v,{'id':'D06'}))
        for case in ['D01','D02','D03','D04','D05','D06']:
            self.assertEqual(len(gates({'id':case})),4)

    def test_component_counts_override_a_generous_gate_status(self):
        v=self.value();v['component_inventory'][0]['visible_count']=2
        self.assertEqual(decide(v),'fail')
        del v['component_inventory'];self.assertTrue(errors(v,{'id':'D06'}))


if __name__=='__main__':unittest.main()
