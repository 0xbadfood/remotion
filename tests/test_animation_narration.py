"""Narration generation stays tied to teaching beats; speech preserves point boundaries."""
import copy
import argparse
import importlib.util
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import patch

from infofilm.animation_narration import generate_script,script_errors,paragraph_errors,SCHEMA
from infofilm.core import Store,write,read,digest,ROOT
from test_animation_experiment import FakeModels,requirement
from test_animation_frontier import canvas


def narration_script():
    req=requirement()
    return {'schema_version':SCHEMA,'requirement_id':req['id'],'visual_message':req['visual_message'],
        'title':'A comparison','points':[{'id':'N01','beat_ids':['compare'],
        'narration':'Two balls begin falling together. Their vertical motion is the same.'}]}


class NarrationTests(unittest.TestCase):
    def test_beat_paragraphs_have_one_owner_and_order_without_a_word_limit(self):
        plan=canvas();plan['teaching_sequence'].append({'id':'recap','message':'Recap','demonstration':'Hold'})
        value=narration_script();value['points'].append({'id':'N02','beat_ids':['recap'],'narration':'They land together.'})
        self.assertEqual(paragraph_errors(value,requirement(),plan),[])
        value['points'][0]['beat_ids'].append('recap')
        self.assertTrue(any('exactly one' in e for e in paragraph_errors(value,requirement(),plan)))
        value['points'][0]['beat_ids']=['compare'];value['points'].reverse()
        self.assertTrue(any('teaching order' in e for e in paragraph_errors(value,requirement(),plan)))

    def test_known_teaching_beats_are_required_without_runtime_or_word_limits(self):
        self.assertEqual(script_errors(narration_script(),requirement(),canvas()),[])
        invalid=narration_script();invalid['points'][0]['beat_ids']=['invented']
        errors=script_errors(invalid,requirement(),canvas())
        self.assertTrue(any('known teaching' in e for e in errors))
        self.assertTrue(any('Uncovered' in e for e in errors))

    def test_duplicate_ids_and_empty_speech_are_rejected(self):
        value=narration_script();value['points'].append(copy.deepcopy(value['points'][0]))
        value['points'][1]['narration']=' '
        errors=script_errors(value,requirement(),canvas())
        self.assertTrue(any('unique safe' in e for e in errors))
        self.assertTrue(any('spoken narration' in e for e in errors))

    def test_script_can_precede_assets_and_placement_and_reuses_luna_output(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder));self.addCleanup(store.db.close)
            write(store.root/'inputs.json',{'requirement':requirement(),'orientations':['landscape'],
                'model':{'planner_url':'https://api.openai.com/v1','reviewer_url':'https://api.openai.com/v1',
                         'planner_model':'gpt-6-luna','reasoning_effort':'high'}})
            write(store.root/'canvas-landscape-plan.json',canvas())
            model=FakeModels([narration_script()])
            def factory(config,store):model.config=config;return model
            with patch('infofilm.animation_narration.OpenAIAnimationModels',side_effect=factory):
                result=generate_script(store)
                self.assertEqual(generate_script(store),result)
            self.assertEqual(len(model.calls),1);self.assertEqual(model.review_calls,[])
            self.assertEqual((store.root/'narration.txt').read_text(),result['points'][0]['narration']+'\n')
            self.assertFalse((store.root/'placement-landscape-plan.json').exists())
            self.assertEqual(read(store.root/'narration-script-provenance.json')['script_source'],'Luna')

    def test_speech_helper_preserves_generated_paragraphs_and_script_provenance(self):
        spec=importlib.util.spec_from_file_location('prepare_narration',ROOT/'scripts/prepare-animation-narration.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);output=root/'audio';audio=root/'voice.wav'
            with wave.open(str(audio),'wb') as handle:
                handle.setnchannels(1);handle.setsampwidth(2);handle.setframerate(16000)
                handle.writeframes(b'\0\0'*64000)
            script=write(root/'script.json',{'schema_version':SCHEMA,'points':[
                {'id':'N01','narration':'Intake begins.'},{'id':'N02','narration':'Exhaust ends.'}]})
            expected=[{'id':'N01','narration':'Intake begins.'},{'id':'N02','narration':'Exhaust ends.'}]
            words=[{'id':'w'+str(i),'word':w,'start':float(i),'end':i+.6}
                   for i,w in enumerate('Intake begins. Exhaust ends.'.split())]
            info={'duration_seconds':4,'reference_audio':'reference.wav','reference_audio_sha256':'ref',
                'reference_transcript_used_for_conditioning':False,'point_attempts':[],
                'silence_cleanup_policy':{}}
            with patch('sys.argv',['prepare','--script-plan',str(script),'--output',str(output)]), \
                 patch.object(module,'configuration',return_value={'tts_url':'http://voice'}), \
                 patch.object(module,'preflight',return_value={'idle':True}), \
                 patch.object(module,'render_vibevoice',return_value=(audio,info)) as render, \
                 patch.object(module,'align_vibevoice',return_value={'audio_sha256':digest(audio.read_bytes()),'words':words}) as align:
                module.main()
            self.assertEqual(render.call_args.args[2],expected)
            self.assertEqual(align.call_args.args[3]['points'],expected)
            bundle=read(output/'narration-input.json')
            self.assertEqual(bundle['transcript'],'Intake begins.\n\nExhaust ends.')
            self.assertEqual(bundle['provenance']['script_source'],'Luna')
            module.verify_bundle(bundle,bundle['transcript'])


if __name__=='__main__':unittest.main()
