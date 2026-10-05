import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from infofilm.core import Store
from infofilm.media import audio_info
from infofilm.music import delivery_levels, level_master, mix_master, pcm_metrics, prepare_music, _generate, music_screen
from infofilm.core import write


def tone(path, frequency, amplitude, seconds=4, channels=1):
    with wave.open(str(path),'wb') as wav:
        wav.setparams((channels,2,16000,0,'NONE','not compressed'))
        wav.writeframes(b''.join(struct.pack('<h',round(amplitude*math.sin(2*math.pi*frequency*i/16000)))*channels for i in range(round(seconds*16000))))
    return path


class MusicMixTests(unittest.TestCase):
    def test_music_asr_distinguishes_low_information_ambiguity_from_coherent_words(self):
        from infofilm.music_vocals import classify_vocals
        vad = {'regions':[{'start':0,'end':5}]}
        def words(values):
            return {'transcript':' '.join(values),'segments':[{'words':[
                {'word':v,'start':i,'end':i+.4,'probability':.95} for i,v in enumerate(values)]}]}
        weak=classify_vocals(vad,words(['you','you']))
        self.assertEqual(weak['status'],'ambiguous')
        self.assertFalse(weak['suspected_vocals'])
        strong=classify_vocals(vad,words(['we','sing','a','song','together']))
        self.assertEqual(strong['status'],'suspected')
        self.assertTrue(strong['suspected_vocals'])
        # Real observed filler: a nearly impossible first word followed by a
        # final word stretched over 29 seconds must not establish sung lyrics.
        hallucinated={'transcript':'Thank you.','segments':[{'words':[
            {'word':'Thank','start':0,'end':.7,'probability':.00001},
            {'word':'you','start':.7,'end':29.96,'probability':.999}]}]}
        self.assertEqual(classify_vocals(vad,hallucinated)['status'],'ambiguous')
        self.assertEqual(classify_vocals({'regions':[]},words(['i','will','see','you']))['status'],'clear')

    def test_instrumental_speech_gate_skips_asr_and_cached_screen_needs_no_gpu(self):
        from infofilm.music_vocals import screen
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(root)
            music=tone(root/'score.wav',660,3000,seconds=25)
            def vad(command,**kwargs):
                write(command[-1],{'regions':[],'detected_seconds':0})
            with patch('infofilm.music_vocals.subprocess.run',side_effect=vad),\
                 patch('infofilm.music_vocals._stable_transcript',side_effect=AssertionError('Do not ask ASR for words over music')):
                value=screen({},store,music)
                self.assertEqual(value['status'],'clear')
                self.assertFalse(value['stable_ts_used'])
            with patch('infofilm.music_vocals.subprocess.run',side_effect=AssertionError('Reuse vocal screen')),\
                 patch('infofilm.music_vocals.gpu_lease',side_effect=AssertionError('Do not acquire GPU')):
                self.assertEqual(screen({},store,music),value)
            store.db.close()

    def test_completed_orphaned_music_take_is_reconciled_without_loading_gpu_services(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(root)
            request=write(root/'take/request.json',{})
            write(root/'take/worker-result.json',{'done':True})
            (root/'take/music.wav').write_bytes(b'done')
            config={'gpu_lock':str(root/'gpu.lock'),'acestep_python':'python'}
            with patch('infofilm.music.release_services',side_effect=AssertionError('Do not touch GPU services')),\
                 patch('infofilm.music.subprocess.Popen',side_effect=AssertionError('Do not generate twice')):
                _generate(config,store,request,'test')
            store.db.close()

    def test_stereo_master_preserves_duration_and_original_voice_and_passed_mix_is_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(root)
            narration=tone(root/'voice.wav',220,6000)
            score=tone(root/'music.wav',660,5000,channels=2)
            original=narration.read_bytes()
            master=root/'mixed.wav'
            info=mix_master(narration,score,master,4)
            self.assertEqual(info['sample_count'],192000)
            self.assertEqual(info['channels'],2)
            self.assertEqual(narration.read_bytes(),original)
            self.assertEqual(pcm_metrics(master)['clipped_sample_ratio'],0)
            film={'audio':str(narration),'audio_info':audio_info(narration),'requested_seconds':60,'actual_seconds':4,'script':{'points':[{'narration':'A test explanation.'}]}}
            score_info={'audio':str(score),**audio_info(score)}
            with patch('infofilm.music.generate_score',return_value=score_info),patch('infofilm.music.audit_audio',return_value={'status':'passed'}) as audit:
                value=prepare_music({},store,film,{},'acestep')
                self.assertEqual(value['engine'],'acestep')
                audit.assert_called_once()
            with patch('infofilm.music.generate_score',return_value=score_info),patch('infofilm.music.mix_master',side_effect=AssertionError('Do not remix cached audio')):
                self.assertEqual(prepare_music({},store,film,{},'acestep'),value)
            store.db.close()

    def test_delivery_boost_raises_complete_mix_four_db_without_changing_stems_or_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            voice = tone(root/'voice.wav', 220, 2500)
            score = tone(root/'score.wav', 660, 3000, channels=2)
            original = voice.read_bytes(), score.read_bytes()
            baseline = root/'baseline.wav'
            boosted = root/'boosted.wav'
            a = mix_master(voice, score, baseline, 4, delivery_gain_db=0)
            b = mix_master(voice, score, boosted, 4)
            measured = 20 * math.log10(pcm_metrics(boosted)['rms'] / pcm_metrics(baseline)['rms'])
            self.assertAlmostEqual(measured, 4, delta=.02)
            self.assertEqual(a['sample_count'], b['sample_count'])
            self.assertEqual(pcm_metrics(boosted)['clipped_sample_ratio'], 0)
            self.assertEqual((voice.read_bytes(), score.read_bytes()), original)

    def test_delivery_limiter_protects_loud_voice_and_preserves_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            voice = tone(root/'loud.wav', 220, 30000)
            output = root/'master.wav'
            info = level_master(voice, output, 4)
            self.assertEqual(info['sample_count'], 4*48000)
            # PCM16 rounds to the nearest sample value at the limiter ceiling.
            self.assertLessEqual(pcm_metrics(output)['peak'], .95 + 1/32768)
            self.assertEqual(pcm_metrics(output)['clipped_sample_ratio'], 0)

    def test_delivery_gain_validation_runs_before_music_generation(self):
        for invalid in [True, '4dB', 13, -13, float('nan'), float('inf')]:
            with self.subTest(value=invalid):
                with self.assertRaises(ValueError):
                    delivery_levels({'delivery_gain_db': invalid})
        self.assertEqual(delivery_levels()['gain_db'], 4)

    def test_disabled_score_needs_no_music_runtime_and_levels_only_delivery_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            voice=tone(store.root/'voice.wav',220,2500)
            original=voice.read_bytes()
            film={'audio':str(voice),'audio_info':audio_info(voice),'requested_seconds':60,'actual_seconds':4}
            with patch('infofilm.music.generate_score',side_effect=AssertionError('Disabled music')):
                result=prepare_music({},store,film,{},'none')
            self.assertNotEqual(result['master_audio'],str(voice))
            self.assertEqual(result['status'],'disabled')
            self.assertEqual(result['delivery_levels']['gain_db'],4)
            self.assertEqual(voice.read_bytes(),original)
            with patch('infofilm.music.level_master',side_effect=AssertionError('Reuse leveled master')):
                self.assertEqual(prepare_music({},store,film,{},'none'),result)
            # Setting changes create another delivery from the clean voice,
            # without applying gain twice or overwriting a checked master.
            changed=prepare_music({'delivery_gain_db':6},store,film,{},'none')
            self.assertNotEqual(changed['master_audio'],result['master_audio'])
            measured=20*math.log10(pcm_metrics(changed['master_audio'])['rms']/pcm_metrics(result['master_audio'])['rms'])
            self.assertAlmostEqual(measured,2,delta=.02)
            store.db.close()
