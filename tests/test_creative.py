import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from infofilm.core import Store, digest, write
from infofilm.creative import (beat_errors, library_from_beats, make_visual_beats, resolved_scenes, sentence_timing,
                               visual_guidance, visual_metrics)
from infofilm.planning import make_image_prompts
from infofilm.video import compile_timeline
from test_audio import wav


def example():
    script = {"title": "Growing puppies", "points": [
        {"id": "P1", "heading": "Growing", "narration": "Puppies need food. Growth needs balance.", "fact_ids": ["F1"]},
        {"id": "P2", "heading": "Feeding", "narration": "Small meals help.", "fact_ids": ["F1"]}]}
    words = [{"id": f"w{i}", "word": word, "start": i*2., "end": i*2.+1.5}
             for i, word in enumerate("Puppies need food. Growth needs balance. Small meals help.".split())]
    timing = {"words": words, "points": [{"point_id": "P1", "start": 0, "end": 11.5, "word_ids": [f"w{i}" for i in range(6)]},
        {"point_id": "P2", "start": 12, "end": 17.5, "word_ids": [f"w{i}" for i in range(6, 9)]}],
        "audio_info": {"duration_seconds": 18}}
    scenes = [{"id": f"S{i}", "point_id": point, "start_word_id": anchor, "kind": "image", "heading": "Puppies",
        "summary": "", "labels": [], "details": [], "motion": "push", "copy_density": "minimal",
        "rationale": "Show the discussed feeding context", "image_brief": prompt}
        for i, (point, anchor, prompt) in enumerate([("P1", "w0", "Puppy eating"), ("P1", "w3", "Growing puppy"), ("P2", "w6", "Small food bowl")], 1)]
    brief = {"resolved_mood": "warm", "visual_balance": "image-led", "visual_style": "Natural generated scenes"}
    return script, timing, {"scenes": scenes}, brief


class VisualBeatTests(unittest.TestCase):
    def test_pipeline_plans_visuals_only_after_alignment_and_runs_music_before_delivery(self):
        from infofilm.pipeline import create
        from infofilm.media import audio_info
        script,timing,storyboard,brief=example()
        evidence={'title':'Puppies','facts':[{'id':'F1','source_ids':['S1']}]}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            audio=wav(root/'reference.wav',frames=144000)
            config=write(root/'config.json',{'planner_url':'http://p','reviewer_url':'http://r',
                'reference_audio':str(audio),'language':'en_indian','tts_url':'http://v','image_url':'http://i','fps':30})
            source=write(root/'sources.json',{'results':[{'url':'https://example.com','raw_content':'Source'}]})
            info=audio_info(audio);info['observed_words_per_minute']=135
            film={'script':script,'audio':str(audio),'audio_info':info,'requested_seconds':60,'actual_seconds':18,'point_count':2}
            timing.update({'audio':str(audio),'audio_info':info,'audio_sha256':info['audio_sha256']})
            sequence=[]
            def record(stage,result):
                def call(*args,**kwargs):
                    sequence.append(stage)
                    if stage=='beats':
                        self.assertIs(args[1]['timing'],timing)
                    return result
                return call
            args=SimpleNamespace(config=config,project=root/'project',subject='Puppies',durations=[60],orientation='landscape',
                tts_engine='vibevoice',reference_audio=None,language=None,reference_transcript=None,review_mode='off',
                sources=source,use_evidence=False,script_dir=None,visual_plan_dir=None,image_manifest=None,until='video')
            with patch('infofilm.pipeline.make_evidence',return_value=evidence),patch('infofilm.pipeline.make_creative_brief',return_value=brief),\
                patch('infofilm.pipeline.render_audio',return_value=(audio,info)),patch('infofilm.pipeline.prepare_audio',return_value=film),\
                patch('infofilm.pipeline.align_audio',side_effect=record('mfa',timing)),\
                patch('infofilm.pipeline.make_visual_beats',side_effect=record('beats',storyboard)),\
                patch('infofilm.pipeline.make_image_prompts',return_value={'assets':[],'banners':[]}),\
                patch('infofilm.pipeline.generate_images',side_effect=record('images',{})),\
                patch('infofilm.pipeline.prepare_music',side_effect=record('music',{'engine':'none'})),\
                patch('infofilm.pipeline.compile_timeline',side_effect=record('compile',(root,root/'timeline.json'))),\
                patch('infofilm.pipeline.render',side_effect=record('render',root/'video.mp4')),patch('infofilm.delivery.preview',return_value=root):
                create(args)
            self.assertEqual(sequence,['mfa','beats','images','music','compile','render'])

    def test_multiple_shots_per_point_preserve_complete_contiguous_audio_coverage(self):
        script, timing, storyboard, _ = example()
        self.assertEqual(beat_errors(storyboard, script, timing), [])
        resolved = resolved_scenes(storyboard, script, timing)
        self.assertEqual([(s['start'], s['end']) for s in resolved], [(0, 6), (6, 12), (12, 18)])
        self.assertEqual([s['covered_narration'] for s in resolved], ['Puppies need food.', 'Growth needs balance.', 'Small meals help.'])

    def test_invalid_or_backward_anchors_and_missing_points_are_rejected(self):
        script, timing, storyboard, _ = example()
        for index, anchor in [(1, 'w0'), (1, 'w6'), (2, 'invented')]:
            with self.subTest(anchor=anchor):
                candidate=copy.deepcopy(storyboard)
                candidate['scenes'][index]['start_word_id']=anchor
                self.assertTrue(beat_errors(candidate,script,timing))
        self.assertTrue(beat_errors({'scenes':storyboard['scenes'][:2]},script,timing))

    def test_actual_duration_scales_guidance_and_long_version_can_allocate_more_images(self):
        script, timing, storyboard, brief = example()
        short=visual_guidance(timing,brief)
        longer=copy.deepcopy(timing);longer['audio_info']['duration_seconds']=153.8
        long=visual_guidance(longer,brief)
        self.assertGreater(long['suggested_beats'],short['suggested_beats'])
        self.assertGreater(long['suggested_distinct_images'],short['suggested_distinct_images'])
        films=[{'script':script,'storyboard':storyboard,'requested_seconds':60,'timing':timing}]
        library=library_from_beats(films,brief)
        self.assertEqual(len(library['assets']),3)
        self.assertEqual(visual_metrics(storyboard,script,timing)['image_screen_time_share'],1)

    def test_image_prompt_context_uses_shot_words_and_unique_banner_keys(self):
        script,timing,storyboard,brief=example()
        films=[{'script':script,'storyboard':storyboard,'requested_seconds':60,'timing':timing}]
        library=library_from_beats(films,brief)
        captured={}
        class Model:
            def loop(self, stage, task, context, validator, **kwargs):
                captured.update(context)
                value={'style':library['style'],'assets':context['used_assets'],
                       'banners':[{'scene_key':f'60:S{i}','text':''} for i in range(1,4)]}
                self.errors=validator(value)
                return value
        model=Model()
        make_image_prompts(model,library,films,{'title':'Puppies','facts':[{'id':'F1'}]},['landscape','portrait'])
        second=captured['scene_contexts_by_asset']['A002'][0]
        self.assertEqual(second['narration'],'Growth needs balance.')
        self.assertEqual(second['point_narration'],'Puppies need food. Growth needs balance.')
        self.assertEqual(second['narrated_seconds'],6)
        self.assertEqual(model.errors,[])

    def test_compiler_uses_shot_boundaries_and_selected_delivery_master_in_both_orientations(self):
        script,timing,storyboard,brief=example()
        library_from_beats([{'script':script,'storyboard':storyboard}],brief)
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            audio=wav(Path(directory)/'narration.wav',frames=144000)
            from infofilm.media import audio_info
            timing.update({'audio':str(audio),'audio_sha256':digest(audio.read_bytes()),'audio_info':audio_info(audio)})
            master=wav(Path(directory)/'master.wav',sample=300,frames=144000)
            music={'engine':'acestep','master_audio':str(master),'master_audio_info':audio_info(master)}
            image=Path(directory)/'scene.png';image.write_bytes(b'fixture-image')
            images={f'A{i:03d}':{'file':str(image),'sha256':digest(image.read_bytes())} for i in range(1,4)}
            evidence={'facts':[{'id':'F1','source_ids':['S1']}]}
            sources={'results':[{'url':'https://example.com','title':'Source'}]}
            for width,height in [(1920,1080),(1080,1920)]:
                with patch('infofilm.video.ROOT',Path(directory)/'workspace'):
                    root,timeline=compile_timeline({'fps':30,'width':width,'height':height},store,script,timing,storyboard,images,evidence,sources,60,creative_brief=brief,music=music)
                import json
                data=json.loads(timeline.read_text())['data']
                self.assertEqual([s['from'] for s in data['scenes']],[0,180,360])
                self.assertEqual(sum(s['duration'] for s in data['scenes']),540)
                self.assertEqual(json.loads((root/'source.json').read_text())['audio_sha256'],digest(master.read_bytes()))
                self.assertEqual(digest((root/'narration.wav').read_bytes()),digest(audio.read_bytes()))
            store.db.close()


class SentenceTimingTests(unittest.TestCase):
    WORDS = [{'id': f'w{i:04d}', 'word': w, 'start': i * 0.5, 'end': i * 0.5 + 0.4} for i, w in
             enumerate(['Dr.', 'Beck', 'waited.', 'Then', 'Fromm', 'arrived', 'at', '4', 'p.m.', 'He', 'acted.'], 1)]

    def test_point_splits_into_timed_sentences_at_real_sentence_ends(self):
        point = {'point_id': 'P01', 'word_ids': [w['id'] for w in self.WORDS]}
        rows = sentence_timing(point, self.WORDS, 7.0)
        self.assertEqual([r['text'] for r in rows], ['Dr. Beck waited.', 'Then Fromm arrived at 4 p.m.', 'He acted.'])
        self.assertEqual([r['start_word_id'] for r in rows], ['w0001', 'w0004', 'w0010'])
        self.assertEqual(rows[0]['seconds'], 1.5); self.assertEqual(rows[-1]['seconds'], 2.0)

    def test_planner_gets_sentence_timing_not_word_alignment(self):
        captured = {}
        models = SimpleNamespace(loop=lambda stage, task, context, validator, **kw: captured.update(context=context) or {})
        timing = {'points': [{'point_id': 'P01', 'start': 0.5, 'end': 5.9, 'word_ids': [w['id'] for w in self.WORDS]}],
                  'words': self.WORDS, 'audio_info': {'duration_seconds': 6.0}}
        film = {'requested_seconds': 120, 'actual_seconds': 6.0, 'timing': timing,
                'script': {'points': [{'id': 'P01', 'narration': 'Dr. Beck waited. Then Fromm arrived at 4 p.m. He acted.', 'fact_ids': []}]}}
        make_visual_beats(models, film, {'facts': []}, {'visual_balance': 'balanced', 'resolved_mood': 'serious'}, experimental=False)
        context = captured['context']
        self.assertNotIn('aligned_words', context); self.assertNotIn('aligned_points', context)
        self.assertNotIn('narration', context['script']['points'][0])
        self.assertEqual(len(context['narration_points'][0]['sentences']), 3)

