import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from infofilm.core import ROOT,read,write
from infofilm.production_state import ProductionPaused
from infofilm.animation_production import create_animation,provider_settings,asset_waits
from infofilm.worker import run


class AnimationProductionTests(unittest.TestCase):
    def test_managed_audio_ignores_its_owner_and_queued_jobs_but_blocks_other_running_jobs(self):
        spec=importlib.util.spec_from_file_location('animation_speech',ROOT/'scripts/prepare-animation-narration.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);db=root/'queue.sqlite3';owner=root/'parent'
            with sqlite3.connect(db) as conn:
                conn.execute('create table jobs(id,status,project)')
                conn.executemany('insert into jobs values(?,?,?)',[('owner','running',str(owner)),('next','queued',str(root/'next'))])
            with patch.object(module,'request',return_value={}):
                module.preflight({},db,owner)
                with self.assertRaisesRegex(RuntimeError,'Webapp jobs are active'):module.preflight({},db)
                with sqlite3.connect(db) as conn:conn.execute("insert into jobs values('external','running',?)",(str(root/'external'),))
                with self.assertRaisesRegex(RuntimeError,'external'):module.preflight({},db,owner)

    def test_worker_pause_is_not_a_failed_outcome(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);request=write(root/'request.json',{'project':str(root/'project')})
            with patch('infofilm.worker.create',side_effect=ProductionPaused('waiting_for_assets','Fill cache')):
                self.assertEqual(run(request,root/'result.json'),0)
            self.assertEqual(read(root/'result.json')['status'],'waiting_for_assets')

    def test_production_adapter_preserves_natural_asset_pause_and_resume_flag(self):
        from test_longform import creative
        from test_external_planner import fixture
        from infofilm.longform import chapter_context
        _,_,brief,plan=fixture()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);project=root/'chapter'
            config=write(root/'config.json',{'reference_audio':str(root/'voice.wav'),'reviewer_url':'http://reviewer'})
            (root/'voice.wav').write_bytes(b'reference')
            context=write(project/'chapter-context.json',chapter_context(plan,plan['chapters'][0],creative(),brief))
            args=SimpleNamespace(project=project,config=config,chapter_context=context,durations=[120],
                reference_audio=None,reference_transcript=None,orientation='landscape',until='video',
                tts_engine='vibevoice',mood='auto',music_engine='none')
            args.subject=plan['film']['subject']
            def pause(workflow):
                write(workflow.output/'state.json',{'state':'waiting_for_components'})
                write(workflow.output/'inputs.json',{'fixture':True})
                return 3
            with patch('infofilm.animation_workflow.run',side_effect=pause) as runner, \
                 patch('infofilm.animation_production.ground_chapter',return_value=(read(context),context)):
                with self.assertRaises(ProductionPaused) as caught:create_animation(args)
                self.assertEqual(caught.exception.status,'waiting_for_assets')
                self.assertFalse(runner.call_args.args[0].resume)
                self.assertEqual(runner.call_args.args[0].execution_mode,'beats')
                with self.assertRaises(ProductionPaused):create_animation(args)
                self.assertTrue(runner.call_args.args[0].resume)
            args.tts_engine='omnivoice'
            with self.assertRaisesRegex(ValueError,'VibeVoice'):create_animation(args)

    def test_provider_settings_reject_unbounded_repairs(self):
        value=provider_settings({});value['max_attempts']=100
        with self.assertRaises(ValueError):provider_settings({'animation_provider':value})

    def test_provider_execution_defaults_old_snapshots_to_beats_without_mutating_them(self):
        value=provider_settings({});value.pop('execution_mode')
        snapshot={'animation_provider':value}
        self.assertEqual(provider_settings(snapshot)['execution_mode'],'beats')
        self.assertNotIn('execution_mode',snapshot['animation_provider'])
        value['execution_mode']='chapter'
        self.assertEqual(provider_settings(snapshot)['execution_mode'],'chapter')
        for mode in ('single',None,False,[]):
            value['execution_mode']=mode
            with self.subTest(mode=mode),self.assertRaises(ValueError):provider_settings(snapshot)

    def test_asset_readiness_uses_the_paused_beat_subset_and_rejects_external_checkpoint(self):
        from test_animation_objects import plan
        with tempfile.TemporaryDirectory() as folder:
            project=Path(folder);root=project/'animation';library=project/'library'
            write(library/'registry.json',{'schema_version':'infofilm.animation-library.v1','revision':'empty','objects':{}})
            write(root/'inputs.json',{'library':str(library)})
            write(root/'components-plan.json',{'objects':[],'component_requests':[]})
            scoped=write(root/'executions/beat-v1/source/landscape/beat-components.json',plan('geometry.beat-demand.v1'))
            write(root/'object-requests.json',{'requests':[{'description':'Circular ball'}]})
            state={'state':'waiting_for_components','component_plan':str(scoped)}
            write(root/'state.json',state)
            self.assertFalse(asset_waits(project)[0]['ready'])
            state['component_plan']=str(write(project/'external.json',read(scoped)))
            write(root/'state.json',state)
            with self.assertRaisesRegex(ValueError,'leaves its chapter'):asset_waits(project)

    def test_grounded_chapter_reuses_source_evidence_and_preserves_qualifications(self):
        from infofilm.animation_production import ground_chapter
        from infofilm.animation_frontier import chapter_requirement
        from infofilm.core import Store
        from test_longform import creative
        from test_external_planner import fixture
        from infofilm.longform import chapter_context
        _,_,brief,plan=fixture();context=chapter_context(plan,plan['chapters'][0],creative(),brief)
        sources={'results':[{'url':'https://example.org/physics','raw_content':'A qualified physical law.'}]}
        evidence={'facts':[{'id':'F01','claim':'A qualified mechanism','source_ids':['S1']}],
                  'uncertainties':['One detail remains uncertain']}
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder)
            try:
                with patch('infofilm.pipeline.research',return_value=sources) as research, \
                     patch('infofilm.planning.make_evidence',return_value=evidence) as extract:
                    value,path=ground_chapter({},store,context,'A concept')
                    self.assertEqual(value['chapter_evidence']['uncertainties'],evidence['uncertainties'])
                    self.assertEqual(chapter_requirement(value)['facts'],['A qualified mechanism'])
                    self.assertEqual(ground_chapter({},store,context,'A concept')[0],value)
                    research.assert_called_once();extract.assert_called_once()
                    write(path,{**value,'chapter_evidence':{'facts':[]}})
                    with self.assertRaisesRegex(ValueError,'grounding changed'):ground_chapter({},store,context,'A concept')
            finally:store.db.close()

if __name__=='__main__':unittest.main()
