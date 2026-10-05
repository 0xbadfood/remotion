import copy
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from infofilm.audio_delivery import assemble_audio, delivery_checkpoint, finish_audio
from infofilm.core import ROOT, Store, digest, read, write
from infofilm.longform import create_long
from infofilm.media import audio_info
from infofilm.pipeline import create
from infofilm.planning import script_text
from infofilm.queue import Queue
from test_longform import accepted_plan, checked_stub, creative


def audio_fixture(project, duration=120, subject='An explanation'):
    project = Path(project)
    voice = project / 'audio/narration.wav'
    voice.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(voice), 'wb') as wav:
        wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b''.join(struct.pack('<h', round(3000*math.sin(2*math.pi*220*i/24000))) for i in range(48000)))
    script = {'title': 'A chapter', 'points': [{'id': 'P01', 'heading': 'An idea',
        'narration': 'A connected explanation.', 'fact_ids': ['F01']}]}
    timing = {'audio_sha256': digest(voice.read_bytes()), 'text_sha256': digest(script_text(script)),
        'words': [{'id': f'w{i+1:04d}', 'word': word, 'start': .2+i*.3, 'end': .4+i*.3}
                  for i, word in enumerate(script['points'][0]['narration'].split())],
        'points': [{'point_id': 'P01', 'start': .2, 'end': 1, 'word_ids': ['w0001', 'w0002', 'w0003']}]}
    film = {'script': script, 'audio': str(voice), 'audio_info': audio_info(voice),
            'audit': {'status': 'passed'}, 'requested_seconds': duration, 'actual_seconds': 2,
            'point_count': 1, 'timing': timing}
    write(project/'project.json', {'subject': subject, 'durations': [duration], 'tts_engine': 'vibevoice'})
    write(project/'creative-brief.json', creative())
    write(project/'evidence-final.json', {'title': 'An explanation', 'facts': []})
    write(project/'sources.json', {'results': [{'url': 'https://example.org', 'raw_content': 'Evidence'}]})
    write(project/f'film-{duration}/audio-selection.json', {k:v for k,v in film.items() if k != 'timing'})
    write(project/f'film-{duration}/timing.json', timing)
    write(project/f'film-{duration}/script.json', script)
    (project/f'film-{duration}/script.txt').write_text(script_text(script))
    return film


class AudioDeliveryTests(unittest.TestCase):
    def test_short_audio_target_stops_before_scene_planning_and_exports_listening_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);project=root/'short'
            film=audio_fixture(project,60)
            reference=root/'reference.wav';reference.write_bytes(b'reference')
            config=write(root/'config.json',{'reference_audio':str(reference),
                'planner_url':'http://p','reviewer_url':'http://r','language':'en_indian'})
            write(project/'voice_calibration.json',{'observed_words_per_minute':130,
                'calibration_key':digest({'reference_audio_sha256':digest(reference.read_bytes()),
                    'tts_engine':'vibevoice','reference_transcript':None})})
            args=SimpleNamespace(config=config,project=project,subject='An explanation',durations=[60],
                orientation='both',reference_audio=None,language='en_indian',tts_engine='vibevoice',
                mood='auto',mood_intensity='restrained',visual_balance='auto',music_engine='none',
                review_mode='off',sources=None,use_evidence=True,until='audio',script_dir=None)
            with patch('infofilm.audio_delivery.ROOT',root), \
                 patch('infofilm.pipeline.research',return_value=read(project/'sources.json')), \
                 patch('infofilm.pipeline.make_creative_brief',return_value=creative()), \
                 patch('infofilm.pipeline.prepare_audio',return_value=film), \
                 patch('infofilm.pipeline.align_audio',return_value=film['timing']), \
                 patch('infofilm.pipeline.make_visual_beats',side_effect=AssertionError('No scene planning')), \
                 patch('infofilm.pipeline.generate_images',side_effect=AssertionError('No images')), \
                 patch('infofilm.pipeline.render',side_effect=AssertionError('No Remotion')):
                create(args)
                self.assertTrue((project/'audio-delivery.json').is_file())
                delivery_checkpoint(project)
                self.assertFalse((project/'visual-library.json').exists())

    def test_real_audio_join_has_chapter_order_pause_boost_and_no_visual_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root/'parent')
            plan, _ = accepted_plan()
            plan = copy.deepcopy(plan)
            plan['chapters'] = plan['chapters'][:2]
            write(store.root/'chapter-plan.json', plan)
            records = []
            originals = []
            for chapter in plan['chapters']:
                project = store.root/'chapters'/f"{store.root.name}-{chapter['id'].lower()}"
                target = max(30, round(chapter['advisory_seconds']))
                film = audio_fixture(project, target)
                originals.append((Path(film['audio']), Path(film['audio']).read_bytes()))
                records.append({'chapter': chapter, 'project': str(project), 'duration': target})
            with patch('infofilm.audio_delivery.ROOT', root), \
                 patch('infofilm.music.generate_score', side_effect=AssertionError('Music is off')), \
                 patch('infofilm.pipeline.make_visual_beats', side_effect=AssertionError('No visuals')), \
                 patch('infofilm.pipeline.render', side_effect=AssertionError('No video')):
                destination = assemble_audio({}, store, plan, records, 900, 'none')
                manifest = delivery_checkpoint(store.root, long=True)
                output = manifest['outputs'][0]
                expected = 4 + plan['assembly']['narration_pause_seconds']
                self.assertAlmostEqual(output['actual_seconds'], expected)
                self.assertEqual(output['master_audio_info']['sample_count'], round(expected*48000))
                self.assertTrue(Path(output['mp3']).is_file())
                script = Path(output['script_file']).read_text()
                self.assertLess(script.index('C01'), script.index('C02'))
                self.assertTrue((destination/'index.html').is_file())
                clean = next(store.root.glob('audio-assembly/*/narration.wav'))
                with wave.open(str(clean)) as wav:
                    wav.setpos(2*48000)
                    gap = wav.readframes(round(plan['assembly']['narration_pause_seconds']*48000))
                self.assertFalse(any(gap))
                for path, pcm in originals:
                    self.assertEqual(path.read_bytes(), pcm)
                # A changed script cannot be continued as the listening version.
                altered = read(Path(records[0]['project'])/f"film-{records[0]['duration']}/audio-selection.json")
                altered['script']['points'][0]['narration'] = 'A different explanation.'
                write(Path(records[0]['project'])/f"film-{records[0]['duration']}/audio-selection.json", altered)
                with self.assertRaisesRegex(ValueError, 'script no longer matches MFA'):
                    delivery_checkpoint(store.root, long=True)
            store.db.close()

    def test_audio_job_queues_video_once_retains_audio_and_resumes_directly_at_scenes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root/'reference.wav'; reference.write_bytes(b'reference')
            config = write(root/'config.json', {'reference_audio': str(reference), 'planner_url':'http://p', 'reviewer_url':'http://r'})
            with patch('infofilm.queue.ROOT', root), patch('infofilm.audio_delivery.ROOT', root):
                queue = Queue(root/'queue', config)
                job = queue.submit({'subject':'An explanation', 'durations':[60], 'orientation':'both',
                    'review_mode':'off', 'reference_audio':str(reference), 'production_target':'audio'})
                project = Path(job['project']); store = Store(project)
                film = audio_fixture(project, 60)
                args = SimpleNamespace(subject='An explanation', music_engine='none')
                finish_audio(args, {}, store, [film], creative())
                queue._update(job['id'], status='succeeded')
                before = read(project/'audio-delivery.json')
                public = queue.public(job['id'], details=True)
                self.assertTrue(public['can_continue_video'])
                self.assertEqual(len(public['audio_deliveries']), 1)
                self.assertNotIn('images', [s['id'] for s in public['stages']])
                queued = queue.continue_video(job['id'])
                self.assertEqual(queued['request']['resume_from'], 'scenes')
                self.assertEqual(queued['request']['until'], 'video')
                self.assertEqual(queued['request']['production_target'], 'video')
                self.assertEqual(read(project/'audio-delivery.json'), before)
                with self.assertRaises(ValueError):
                    queue.continue_video(job['id'])
                child_args = SimpleNamespace(**queued['request'])
                child_args.config = Path(child_args.config)
                child_args.project = Path(child_args.project)
                with patch('infofilm.pipeline.finish_visuals') as visuals, \
                     patch('infofilm.pipeline.research', side_effect=AssertionError('No research')), \
                     patch('infofilm.pipeline.prepare_audio', side_effect=AssertionError('No speech render')), \
                     patch('infofilm.pipeline.align_audio', side_effect=AssertionError('No MFA')):
                    create(child_args)
                    visuals.assert_called_once()
                    saved = visuals.call_args.args[7][0]
                    self.assertEqual(saved['audio_info']['audio_sha256'], film['audio_info']['audio_sha256'])
                store.db.close()


class LongAudioFirstTests(unittest.TestCase):
    def test_all_chapters_halt_before_visuals_failure_continues_and_resume_reuses_audio(self):
        plan, brief = accepted_plan()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ref = root/'reference.wav'; ref.write_bytes(b'reference')
            config = write(root/'config.json', {'planner_url':'http://p','reviewer_url':'http://r',
                'reference_audio':str(ref),'language':'en_indian','tts_url':'http://v','image_url':'http://i'})
            sources = write(root/'sources.json', {'results':[{'url':'https://example.org','raw_content':'Evidence'}]})
            outline = write(root/'plan.json', plan); orientation = write(root/'brief.json', brief)
            args = SimpleNamespace(config=config,project=root/'parent',subject=plan['film']['subject'],durations=[900],
                orientation='landscape',reference_audio=None,reference_transcript=None,language='en_indian',
                tts_engine='vibevoice',omni_speed=.8,mood='auto',mood_intensity='restrained',visual_balance='auto',music_engine='none',
                review_mode='experimental',sources=sources,orientation_brief=orientation,chapter_plan=outline,until='audio')
            calls = []
            def child(payload, project, store, cid, *extra):
                calls.append(cid)
                self.assertEqual(payload['until'], 'audio')
                self.assertTrue(payload['audio_checkpoint_only'])
                if cid == 'C02':
                    raise RuntimeError('Narration incomplete')
                audio_fixture(project, payload['durations'][0], payload['subject'])
                return project/'attempts/audio'
            with patch('infofilm.creative.make_creative_brief',return_value=creative()), \
                 patch('infofilm.longform.run_chapter',side_effect=child), \
                 patch('infofilm.longform.continuity_review') as continuity, \
                 patch('infofilm.audio_delivery.assemble_audio') as join, \
                 patch('infofilm.longform_video.assemble',side_effect=AssertionError('No video assembly')):
                with self.assertRaisesRegex(RuntimeError,'C02'):
                    create_long(args)
                self.assertEqual(calls,[c['id'] for c in plan['chapters']])
                status = read(args.project/'longform-status.json')
                self.assertEqual(status['completed'],6)
                self.assertEqual(status['failed'],1)
                self.assertEqual(status['chapters'][0]['status'],'audio_ready')
                continuity.assert_not_called(); join.assert_not_called()
            calls.clear()
            def repair(payload, project, store, cid, *extra):
                calls.append(cid); audio_fixture(project,payload['durations'][0],payload['subject']); return project/'attempts/recovered'
            with patch('infofilm.longform.run_chapter',side_effect=repair), \
                 patch('infofilm.longform.continuity_review') as continuity, \
                 patch('infofilm.audio_delivery.ROOT',root):
                create_long(args)
                self.assertEqual(calls,['C02'])
                continuity.assert_called_once()
                self.assertEqual(read(args.project/'longform-status.json')['completed'],7)
                self.assertFalse((args.project/'delivery.json').exists())
                self.assertTrue((args.project/'audio-delivery.json').is_file())
            # Continue the same project: no parent research/plan or chapter TTS.
            before = read(args.project/'audio-delivery.json')
            args.until='video'; args.resume_from='scenes'
            calls.clear()
            def pictures(payload, project, store, cid, *extra):
                calls.append(cid)
                self.assertEqual(payload['resume_from'],'scenes')
                child_args = SimpleNamespace(**payload)
                with patch('infofilm.pipeline.finish_visuals') as visual, \
                     patch('infofilm.pipeline.research',side_effect=AssertionError('No child research')), \
                     patch('infofilm.pipeline.prepare_audio',side_effect=AssertionError('No TTS')):
                    create(child_args)
                    visual.assert_called_once()
                checked_stub(project)
                return project/'attempts/video'
            with patch('infofilm.audio_delivery.ROOT',root), \
                 patch('infofilm.longform.run_chapter',side_effect=pictures), \
                 patch('infofilm.pipeline.research',side_effect=AssertionError('No parent research')), \
                 patch('infofilm.longform.continuity_review'), \
                 patch('infofilm.longform_video.assemble',return_value={'video':'checked.mp4'}) as video, \
                 patch('infofilm.delivery.preview',return_value=root/'output'):
                create_long(args)
                self.assertEqual(calls,[c['id'] for c in plan['chapters']])
                video.assert_called_once()
                self.assertEqual(read(args.project/'audio-delivery.json'),before)


if __name__ == '__main__':
    unittest.main()
