"""Saved supported primitive names must not become imaginary capability gaps."""
import unittest

from infofilm.animation_motion import capability_key
from infofilm.animation_workflow import motion_errors
from infofilm.animation_beat_workflow import _objective
from test_animation_workflow import teaching,animation,placement,components
from test_animation_frontier import canvas
from test_animation_experiment import narration


class CapabilityNamesTests(unittest.TestCase):
    def test_only_explicit_known_names_are_normalized(self):
        self.assertEqual(capability_key('translation: keyframes'),'translation')
        self.assertEqual(capability_key('continuous_rotation: unwrapped keyframe angles'),'continuous_rotation')
        self.assertEqual(capability_key('hold opacity'),'visibility')
        self.assertEqual(capability_key('unavailable: fluid-surface deformation'),'unavailable: fluid-surface deformation')
        self.assertEqual(capability_key('force-integrated solver'),'force-integrated solver')

    def test_keyframes_are_checked_for_actual_implementation(self):
        t=teaching();t['teaching_sequence'][0]['required_behaviors']=['keyframes','translation: keyframes']
        a=animation();a['motion_requirements']=[{'behavior':behavior,'importance':'essential','object_ids':['rotor'],
            'reason':'Use accepted keyed poses'} for behavior in ['keyframes','translation']]
        errors=motion_errors(a,placement(),canvas(),components(),{},narration(),t)
        self.assertTrue(any('no implementation: keyframes' in e for e in errors),errors)
        track=a['objects'][0]['tracks'][1]
        track['kind']='keyframes'
        track['keyframes']=[{'phase':phase,'x':.3,'y':.3,'rotation':rotation,'opacity':1}
            for phase,rotation in [(0,360),(1,720)]]
        for key in ['x','y','rotation_from','rotation_to']:track.pop(key,None)
        self.assertEqual(motion_errors(a,placement(),canvas(),components(),{},narration(),t),[])

    def test_scoped_context_keeps_actual_handoff_exclusions_and_exit(self):
        value=_objective({'id':'lesson','chapter_context':{'chapter':{'exclude':['Later applications'],
            'exit_handoff':'Next explain a rotating viewpoint','scope':{'description':'Inertial frame'}}}})
        self.assertEqual(value['chapter_scope']['exclude'],['Later applications'])
        self.assertEqual(value['chapter_scope']['exit_handoff'],'Next explain a rotating viewpoint')


if __name__=='__main__':unittest.main()
