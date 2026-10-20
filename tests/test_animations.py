import unittest

from infofilm import animations
from infofilm.creative import beat_errors
from test_creative import example

OPTIONS = {'code_root': '/home/nitin/manim', 'library': [],
           'animation': {'manim_python': '/x/python', 'python_base': '/x', 'kits': ['chem']}}


def spec(*phrases):
    return {'title': 'Feeding makes puppies grow', 'objective': 'See food turn into growth over weeks',
            'complexity': 'routine', 'visual': 'A puppy grows as food bowls empty, then a balance tips',
            'cues': [{'phrase': p, 'show': f'show {p}'} for p in phrases]}


def with_animation(animation, whole_point=True):
    """S1 animated; with whole_point it covers all of P1 (0-12 s), otherwise only its first sentence (0-6 s)."""
    script, timing, storyboard, _ = example()
    scenes = storyboard['scenes']
    scenes[0].update(kind='animation', animation=animation, image_brief='', copy_density='standard',
                     heading='Food and growth', summary='')
    if whole_point:
        del scenes[1]
    return script, timing, storyboard


class AnimationBeatTests(unittest.TestCase):
    def test_films_without_animation_options_cannot_use_them(self):
        script, timing, storyboard = with_animation(spec('Puppies need', 'Growth needs balance'))
        self.assertIn('S1: unsupported visual beat kind', beat_errors(storyboard, script, timing))
        figures_only = {k: v for k, v in OPTIONS.items() if k != 'animation'}
        self.assertIn('S1: unsupported visual beat kind', beat_errors(storyboard, script, timing, figures_only))

    def test_a_valid_animation_passes(self):
        script, timing, storyboard = with_animation(spec('Puppies need', 'Growth needs balance'))
        self.assertEqual(beat_errors(storyboard, script, timing, OPTIONS), [])

    def test_cue_phrases_must_be_spoken_in_order_in_the_beat(self):
        for phrases in (('Puppies need', 'small meals'), ('Growth needs', 'Puppies need')):
            script, timing, storyboard = with_animation(spec(*phrases))
            errors = beat_errors(storyboard, script, timing, OPTIONS)
            self.assertTrue(any('is not spoken (in order) in this beat' in e for e in errors), (phrases, errors))

    def test_short_beats_are_too_short_to_animate(self):
        script, timing, storyboard = with_animation(spec('Puppies need', 'food'), whole_point=False)
        self.assertTrue(any('at least 8 s' in e for e in beat_errors(storyboard, script, timing, OPTIONS)))

    def test_cues_run_on_the_beat_clock_from_phrase_to_phrase(self):
        script, timing, storyboard = with_animation(spec('Puppies need', 'Growth needs balance'))
        (start, end, words), _ = animations.spans(storyboard['scenes'], timing)
        clock = animations.cues(storyboard['scenes'][0], start, end, words)
        self.assertEqual([(c['start'], c['end']) for c in clock], [(0.0, 6.0), (6.0, 12.0)])

    def test_phrases_ignore_case_and_punctuation(self):
        _, timing, _, _ = example()
        self.assertEqual(animations.find_phrase(timing['words'], 'food growth'), 2)
        self.assertIsNone(animations.find_phrase(timing['words'], 'food growth', start=3))


class AnimationRenderTests(unittest.TestCase):
    def test_each_beat_is_coded_on_its_own_clock_and_noted_on_the_scene(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest import mock
        script, timing, storyboard = with_animation(spec('Puppies need', 'Growth needs balance'))
        seen = {}

        def code_chapter(job, plan, chapter, sc, clock_timing, cues, duration, evidence, folder, kit=''):
            seen.update(plan=plan, chapter=chapter, cues=cues, duration=duration, kit=kit, evidence=evidence)
            Path(folder).mkdir(parents=True, exist_ok=True)
            video = Path(folder) / 'chapter.mp4'
            video.write_bytes(b'clip')
            return video, {'attempts': 2, 'tier': 'routine', 'kept_attempt': 2, 'notes': ['open point']}
        coded = SimpleNamespace(KIT='KIT', code_chapter=code_chapter)
        kit = SimpleNamespace(CHEM='CHEM')
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(animations, '_scivid', lambda figures: (coded, kit)), \
                mock.patch.object(animations, 'CoderJob', lambda *a: None):
            film = {'storyboard': storyboard, 'timing': timing, 'script': script}
            evidence = {'facts': [{'id': 'F1', 'claim': 'Puppies grow on balanced food.'}]}
            animations.render(SimpleNamespace(root=directory), [film], OPTIONS, evidence,
                              {'theme': {'background': '#102030'}})
        scene = storyboard['scenes'][0]
        self.assertEqual(seen['duration'], 12.0)
        self.assertEqual([c['start'] for c in seen['cues']], [0.0, 6.0])
        self.assertEqual(seen['kit'], 'KIT\nCHEM')
        self.assertEqual(seen['plan']['theme']['background'], '#102030')
        self.assertIn('balanced food', seen['evidence'])
        self.assertEqual(scene['animation_seconds'], 12.0)
        self.assertFalse(scene['animation_review']['passed'])          # kept after failing review: tagged for review
        self.assertEqual(len(scene['animation_sha256']), 64)


if __name__ == '__main__':
    unittest.main()
