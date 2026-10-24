"""Key-idea films (user, 2026-10-20): after a concept film, one of its key ideas as a short simplified film.
Simplification first, no coverage demands; a learner check says whether the idea landed."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from infofilm import planning
from infofilm.curriculum import (evidence_coverage_errors, evidence_task, is_key_idea, key_idea_beat_errors,
                                 key_idea_script_errors, learner_check, script_coverage_errors, script_task)
from infofilm.core import read

IDEA = {'kind': 'key_idea', 'concept': "Bohr's Model of the Hydrogen Atom", 'audience': 'Class 11 students in India',
        'book': 'Class 11 Chemistry', 'chapter_no': 2, 'chapter': 'Structure of Atom', 'coverage': [],
        'idea': {'title': 'Why hydrogen gives sharp coloured lines',
                 'understanding': 'Electrons only have certain energies, so only certain jumps and colours exist.',
                 'questions': ['Why are there gaps between the lines?']}}


class Events:
    def __init__(self, root):
        self.root, self.events = Path(root), []

    def event(self, name, state, detail=None):
        self.events.append((name, state, detail))


class FakeModels:
    """Scripted learner and judge verdicts; every loop returns a new script version."""

    def __init__(self, root, verdicts):
        self.store, self.verdicts, self.loops, self.calls = Events(root), list(verdicts), [], []

    def loop(self, stage, task, context, validator, **kwargs):
        self.loops.append((stage, task, context))
        return {'title': 'Lines', 'points': [{'id': 'P01', 'heading': 'Ladder', 'fact_ids': ['F01'],
                                              'narration': f'Version {len(self.loops)}: a ladder of energies.'}]}

    def call(self, role, name, prompt, **kwargs):
        self.calls.append((role, name, prompt))
        if role == 'planner':
            return {'explanation': 'Electrons sit on rungs.', 'answers': [], 'lost_at': []}
        return self.verdicts.pop(0)


class KeyIdeaTests(unittest.TestCase):
    def test_prompts_put_simplification_first_and_ask_for_no_coverage(self):
        self.assertTrue(is_key_idea(IDEA))
        self.assertIn('Simplification comes before everything else', script_task(IDEA))
        self.assertIn('Why hydrogen gives sharp coloured lines', evidence_task(IDEA))
        self.assertIn('completeness is not wanted', evidence_task(IDEA))
        facts = {'facts': [{'id': 'F01', 'claim': 'Analogy: a ladder'}]}
        self.assertEqual(evidence_coverage_errors(facts, IDEA), [])
        self.assertEqual(script_coverage_errors({'points': []}, {**facts, 'curriculum': IDEA}), [])

    def test_a_script_is_rewritten_until_the_idea_lands(self):
        with tempfile.TemporaryDirectory() as root:
            models = FakeModels(root, [{'understood': 90, 'simple': 80, 'wrong_beliefs': ['electrons are planets'],
                                        'fixes': [{'point_id': 'P01', 'problem': 'x', 'suggestion': 'y'}]},
                                       {'understood': 85, 'simple': 90, 'wrong_beliefs': [], 'fixes': []}])
            evidence = {'title': 'Lines', 'facts': [{'id': 'F01'}], 'curriculum': IDEA}
            script = planning.make_script(models, evidence, 240, 120)
            self.assertIn('Version 2', script['points'][0]['narration'])
            stage, task, context = models.loops[1]
            self.assertEqual(stage, 'script-240-land1')
            self.assertEqual(context['learner_feedback']['wrong_beliefs'], ['electrons are planets'])
            self.assertIn('a short simplified explainer', models.loops[0][1])
            record = read(Path(root) / 'learner-check-240.json')
            self.assertTrue(record['landed'])
            self.assertEqual(len(record['rounds']), 2)
            # the learner sees only the narration, never the evidence or the idea's answer
            learner_prompt = models.calls[0][2]
            self.assertNotIn('Electrons only have certain energies', learner_prompt)
            self.assertIn('Why are there gaps between the lines?', learner_prompt)

    def test_a_wrong_belief_means_not_landed_and_rounds_are_bounded(self):
        miss = {'understood': 95, 'simple': 95, 'wrong_beliefs': ['light is made in the nucleus'], 'fixes': []}
        with tempfile.TemporaryDirectory() as root:
            models = FakeModels(root, [miss] * 3)
            planning.make_script(models, {'title': 'L', 'facts': [{'id': 'F01'}], 'curriculum': IDEA}, 240, 120)
            self.assertEqual(len(models.loops), 1 + planning.LANDING_ROUNDS)
            self.assertFalse(read(Path(root) / 'learner-check-240.json')['landed'])

    def test_understood_is_not_enough_it_must_be_simple(self):
        with tempfile.TemporaryDirectory() as root:
            script = {'points': [{'id': 'P01', 'narration': 'A ladder.'}]}
            for verdict, landed in (({'understood': 100, 'simple': 40}, False), ({'understood': 60, 'simple': 95}, False),
                                    ({'understood': 80, 'simple': 75}, True)):
                check = learner_check(FakeModels(root, [verdict]), script, IDEA, 'x')
                self.assertEqual(check['landed'], landed, verdict)
            self.assertEqual(check['verdict']['score'], 75)

    def test_a_confused_learner_means_not_landed(self):
        with tempfile.TemporaryDirectory() as root:
            models = FakeModels(root, [{'understood': 90, 'simple': 90}])
            models.call = (lambda role, name, prompt, **k: {'explanation': 'x', 'confusing': ['R H', 'nu', 'delta E']}
                           if role == 'planner' else {'understood': 90, 'simple': 90})
            self.assertFalse(learner_check(models, {'points': [{'id': 'P01', 'narration': 'n'}]}, IDEA, 'x')['landed'])

    def test_few_equations_and_the_analogy_carries_the_film(self):
        evidence = {'curriculum': IDEA}
        maths = [{'id': f'P0{n}', 'narration': 'Delta E equals minus 4.58 times 10 to the power minus 19 joules.',
                  'uses_analogy': False} for n in range(1, 4)]
        story = [{'id': f'P1{n}', 'narration': 'The electron hops down two steps of the staircase.', 'uses_analogy': True}
                 for n in range(1, 3)]
        errors = ' '.join(key_idea_script_errors({'points': maths + story}, evidence))
        self.assertIn('P01, P02, P03: 3 points speak equations', errors)
        self.assertIn('only 2 of 5 points use the analogy', errors)
        self.assertEqual(key_idea_script_errors({'points': maths[:1] + story}, evidence), [])
        self.assertEqual(key_idea_script_errors({'points': maths}, {'curriculum': {'kind': 'textbook_concept'}}), [])
        board = {'scenes': [{'id': 'S1', 'kind': 'figure'}, {'id': 'S2', 'kind': 'figure'}, {'id': 'S3', 'kind': 'animation'}]}
        self.assertIn('S1, S2: 2 equation figures', ' '.join(key_idea_beat_errors(board, evidence)))
        # the cap is on equations: the book's own figure beside one equation is fine (physics, 2026-10-24)
        book = {'scenes': [{'id': 'S1', 'kind': 'figure', 'figure': {'type': 'equation', 'latex': 'v = dx/dt'}},
                           {'id': 'S2', 'kind': 'figure', 'figure': {'type': 'library', 'id': 'Fig-2-1'}}]}
        self.assertNotIn('equation figures', ' '.join(key_idea_beat_errors(book, evidence)))
        self.assertIn('at most 1 point may speak an equation', script_task(IDEA))
        numbers = [{'id': 'P1', 'narration': 'Its frequency is about 6.91 hertz.', 'uses_analogy': True}]
        self.assertIn('P01, P1', ' '.join(key_idea_script_errors({'points': maths[:1] + numbers + story}, evidence)))
        self.assertIn('Book terms: only "energy level", "photon"',
                      script_task({**IDEA, 'idea': {**IDEA['idea'], 'terms': ['energy level', 'photon']}}))

    def test_understood_is_not_enough_it_must_be_simple(self):
        with tempfile.TemporaryDirectory() as root:
            script = {'points': [{'id': 'P01', 'narration': 'A ladder.'}]}
            for verdict, landed in (({'understood': 100, 'simple': 40}, False), ({'understood': 60, 'simple': 95}, False),
                                    ({'understood': 80, 'simple': 75}, True)):
                check = learner_check(FakeModels(root, [verdict]), script, IDEA, 'x')
                self.assertEqual(check['landed'], landed, verdict)
            self.assertEqual(check['verdict']['score'], 75)

    def test_a_confused_learner_means_not_landed(self):
        with tempfile.TemporaryDirectory() as root:
            models = FakeModels(root, [])
            models.call = (lambda role, name, prompt, **k: {'explanation': 'x', 'confusing': ['R H', 'nu', 'delta E']}
                           if role == 'planner' else {'understood': 90, 'simple': 90})
            self.assertFalse(learner_check(models, {'points': [{'id': 'P01', 'narration': 'n'}]}, IDEA, 'x')['landed'])

    def test_few_equations_and_the_analogy_carries_the_film(self):
        evidence = {'curriculum': IDEA}
        maths = [{'id': f'P0{n}', 'narration': 'Delta E equals minus 4.58 times 10 to the power minus 19 joules.',
                  'uses_analogy': False} for n in range(1, 4)]
        story = [{'id': f'P1{n}', 'narration': 'The electron hops down two steps of the staircase.', 'uses_analogy': True}
                 for n in range(1, 3)]
        errors = ' '.join(key_idea_script_errors({'points': maths + story}, evidence))
        self.assertIn('P01, P02, P03: 3 points speak equations', errors)
        self.assertIn('only 2 of 5 points use the analogy', errors)
        self.assertEqual(key_idea_script_errors({'points': maths[:1] + story}, evidence), [])
        self.assertEqual(key_idea_script_errors({'points': maths}, {'curriculum': {'kind': 'textbook_concept'}}), [])
        board = {'scenes': [{'id': 'S1', 'kind': 'figure'}, {'id': 'S2', 'kind': 'figure'}, {'id': 'S3', 'kind': 'animation'}]}
        self.assertIn('S1, S2: 2 equation figures', ' '.join(key_idea_beat_errors(board, evidence)))
        self.assertIn('at most 1 point may speak an equation', script_task(IDEA))
        numbers = [{'id': 'P1', 'narration': 'Its frequency is about 6.91 hertz.', 'uses_analogy': True}]
        self.assertIn('P01, P1', ' '.join(key_idea_script_errors({'points': maths[:1] + numbers + story}, evidence)))
        self.assertIn('Book terms: only "energy level", "photon"',
                      script_task({**IDEA, 'idea': {**IDEA['idea'], 'terms': ['energy level', 'photon']}}))

    def test_concept_films_get_no_learner_check(self):
        with tempfile.TemporaryDirectory() as root:
            models = FakeModels(root, [])
            curriculum = {**IDEA, 'kind': 'textbook_concept', 'coverage': []}
            planning.make_script(models, {'title': 'L', 'facts': [{'id': 'F01'}], 'curriculum': curriculum}, 240, 120)
            self.assertEqual((len(models.loops), models.calls), (1, []))


if __name__ == '__main__':
    unittest.main()


class IdeaPlanAndMotionTests(unittest.TestCase):
    """The idea plan before the script, scenes planned in batches, and motion beats made into LTX clips."""

    def plan(self, **change):
        beats = [{'id': f'B{n}', 'teaches': 'one step', 'analogy_moment': 'the guide steps down' if n % 2 else '',
                  'real_link': ''} for n in range(1, 9)]
        return {'takeaway': 'An electron can only sit on certain steps, and each drop gives out one colour of light.',
                'book_terms': ['energy level', 'photon'],
                'analogy': {'name': 'Staircase', 'picture': 'Energy levels are steps.', 'stops_working': 'Electrons are not people.'},
                'mapping': [{'analogy': 'step', 'real': 'energy level'}, {'analogy': 'step height', 'real': 'energy gap'}],
                'world': {'guide': 'a small round robot with big blue eyes and a yellow scarf',
                          'setting': 'a tall staircase of five wide pale-yellow landings on a plain dark navy background',
                          'style': 'flat cartoon'}, 'beats': beats, **change}

    def test_the_idea_plan_needs_an_analogy_that_carries_the_film(self):
        from infofilm import idea_plan
        self.assertEqual(idea_plan.errors(self.plan(), 2), [])
        thin = ' '.join(idea_plan.errors(self.plan(world={'guide': 'a robot', 'setting': 's', 'style': 'flat'}), 2))
        for problem in ('describe exactly how the guide looks', 'describe the setting exactly', 'background must be dark'):
            self.assertIn(problem, thin)
        beats = self.plan()['beats']
        for b in beats[1:]:
            b['analogy_moment'] = ''
        self.assertIn('only 1 of 8 beats have an analogy_moment', ' '.join(idea_plan.errors(self.plan(beats=beats), 2)))
        for takeaway in ('Delta E equals the energy gap between steps.', 'Each drop gives light of 434 nm colour.'):
            self.assertIn('no symbols, formulas or numbers', ' '.join(idea_plan.errors(self.plan(takeaway=takeaway), 1)))
        self.assertIn('book_terms: at most 3', ' '.join(idea_plan.errors(self.plan(book_terms=['a', 'b', 'c', 'd']), 1)))

    def test_the_script_reviewer_is_a_student_not_a_judge(self):
        # User 2026-10-21: the reviewer reads as a student; it gets the takeaway, the analogy and the facts only.
        from infofilm.curriculum import student_review
        evidence = {'idea_plan': {'takeaway': 'Each drop between steps lets out one flash of one colour.',
                                  'analogy': {'name': 'staircase'}},
                    'facts': [{'id': 'F01', 'claim': 'c'}],
                    'curriculum': {'idea': {'book_understanding': 'signed ΔE = R_H(...)'}}}
        task, context = student_review({'evidence': evidence})
        self.assertIn('one flash of one colour', task)
        self.assertIn('never ask for more content', task)
        self.assertEqual(set(context), {'analogy', 'facts'})
        self.assertNotIn('ΔE', str(context))

    def test_the_plan_review_does_not_force_completeness(self):
        # I4 v3: majors asking for the sign convention or policing words failed the plan twice.
        from infofilm import idea_plan
        review = {'score': 78, 'pass': False, 'issues': [
            {'severity': 'major', 'problem': 'The film never connects a downward jump to a negative energy change.'},
            {'severity': 'major', 'problem': 'The mapping uses technical terms not in book_terms, such as absorption.'},
            {'severity': 'major', 'problem': 'The real-world mapping starts before the analogy has played out.'},
            {'severity': 'major', 'problem': 'Evenly spaced landings contradict B6, where different drops differ.'}]}
        severities = [i['severity'] for i in idea_plan.screen(review, {})['issues']]
        self.assertEqual(severities, ['minor', 'minor', 'minor', 'major'])

    def test_action_beats_are_image_beats_of_3_to_10_seconds(self):
        from infofilm.creative import beat_errors
        from test_creative import example
        script, timing, storyboard, _ = example()
        storyboard['scenes'][0]['action'] = 'The puppy runs to the bowl and starts eating.'
        self.assertEqual(beat_errors(storyboard, script, timing), [])
        del storyboard['scenes'][1]                                   # S1 now runs 12 s
        self.assertIn('split the passage into more beats', ' '.join(beat_errors(storyboard, script, timing)))
        script, timing, storyboard, _ = example()
        storyboard['scenes'][2].update(kind='compare', labels=['a', 'b'], details=['c', 'd'], action='It moves along.')
        self.assertIn('S3: only image beats can carry an action', beat_errors(storyboard, script, timing))

    def test_scenes_are_planned_in_batches_and_numbered_in_order(self):
        from unittest import mock
        from infofilm import creative
        from test_creative import example
        script, timing, storyboard, _ = example()
        film = {'script': script, 'timing': timing, 'requested_seconds': 60, 'actual_seconds': 18}
        context = {'narration_points': [{'point_id': 'P1'}, {'point_id': 'P2'}], 'script': script}
        seen = []

        class Models:
            def loop(self, stage, task, context, validator, **kwargs):
                points = [r['point_id'] for r in context['narration_points']]
                scenes = [dict(s, id='X' + s['id'], **({'camera': 'still'} if s['kind'] == 'image' else {}))
                          for s in storyboard['scenes'] if s['point_id'] in points]
                seen.append((stage, points, context['batch'], [s['id'] for s in context['previous_scenes']]))
                self.errors = validator({'scenes': scenes})
                assert not self.errors, self.errors
                return {'scenes': scenes}
        with mock.patch.object(creative, 'BATCH_POINTS', 1):
            board = creative.batched_beats(Models(), film, {'curriculum': IDEA}, 'TASK', context, None, True)
        self.assertEqual([s['id'] for s in board['scenes']], ['S01', 'S02', 'S03'])
        self.assertEqual([(st, p) for st, p, _, _ in seen], [('visual-beats-60-b1', ['P1']), ('visual-beats-60-b2', ['P2'])])
        self.assertEqual(seen[1][2]['first_scene_id'], 'S03')
        self.assertEqual(seen[1][3], ['XS1', 'XS2'])

    def test_narration_says_names_not_formulas(self):
        # Alkenes film: P25 read "(CH3)3C-OH" aloud and failed three audio takes; P08 narrated a misprint.
        from infofilm.curriculum import heard_errors, spoken_formula
        for said in ('HBr', 'H2O', 'CO2,', 'H2SO4', 'KMnO4', 'UV', 'Markovnikov', '2-methylpropan-2-ol,'):
            self.assertFalse(spoken_formula(said), said)
        for unsayable in ('(CH3)3C-OH.', 'CH3-CHCl-CH2Cl.', 'CH2=CH2,', 'H-Cl', 'CH3CH2OH'):
            self.assertTrue(spoken_formula(unsayable), unsayable)
        script = {'points': [{'id': 'P01', 'narration': 'Its hydration gives 2-methylpropan-2-ol, also called '
                              'tert-butyl alcohol.'},
                             {'id': 'P02', 'narration': 'It is written as (CH3)3C-OH.'},
                             {'id': 'P03', 'narration': 'Here is a correction to the printed equation 9.40.'}]}
        problems = heard_errors(script, {'curriculum': {'concept': 'x'}})
        self.assertEqual([p[:3] for p in problems], ['P02', 'P03'])
        self.assertEqual(heard_errors(script, {}), [])                              # only curriculum films

    def test_key_idea_animations_show_the_real_thing(self):
        # Molecular-models film: coded beats asked to draw "friends" made grey circles of neither world.
        from infofilm.curriculum import key_idea_animation_errors
        real = {'id': 'S07', 'kind': 'animation', 'animation': {
            'title': 'Ethanol in three models', 'objective': 'See what each model shows',
            'visual': 'Ethanol turns, then morphs from framework to ball-and-stick to space-filling',
            'real': 'ethanol as framework, ball-and-stick and space-filling models'}}
        self.assertEqual(key_idea_animation_errors([real, {'id': 'S01', 'kind': 'image'}]), [])
        analogy = {'id': 'S12', 'kind': 'animation', 'animation': {
            'title': 'Friends swell into touching balls', 'objective': 'The ring fills up',
            'visual': 'Six friends swell into puffy balls'}}
        problems = ' '.join(key_idea_animation_errors([analogy]))
        self.assertIn('animation.real', problems)
        self.assertIn('not the analogy ("Friends")', problems)

    def test_motion_clips_cover_their_beat_and_a_failure_keeps_the_still(self):
        import hashlib
        from infofilm import action_clips
        from test_creative import example
        self.assertEqual((action_clips.frames_for(6, 24), action_clips.frames_for(12, 24)), (145, 241))
        script, timing, storyboard, _ = example()
        with tempfile.TemporaryDirectory() as root:
            still = Path(root) / 'still.png'
            still.write_bytes(b'png')
            for scene in storyboard['scenes']:
                scene['asset_id'] = 'A1' if scene['id'] == 'S1' else 'A2'
            storyboard['scenes'][0]['action'] = 'The puppy runs to the bowl.'
            images = {'A1': {'file': str(still), 'sha256': hashlib.sha256(b'png').hexdigest()}}
            film = {'script': script, 'timing': timing, 'storyboard': storyboard}

            renders = []

            class Worker:
                id = 'spark'

                def render_action(self, still, prompt, frames, target, key, settings):
                    renders.append((prompt, frames))
                    target.write_bytes(b'clip')

            class Broken(Worker):
                def render_action(self, *args):
                    raise RuntimeError('LTX fell over')

            def pool(worker):
                def run(settings, jobs, render_one, event):            # video_pool.run: a failure keeps the still
                    for key, payload in jobs:
                        try:
                            render_one(worker, key, payload)
                        except Exception as exc:                        # noqa: BLE001
                            event('fallback', {'asset': key, 'reason': str(exc)})
                return run
            store = Events(root)
            action_clips.make_action_clips({}, store, [film], images, pool(Worker()))
            scene = storyboard['scenes'][0]
            self.assertEqual(renders, [(action_clips.DEFAULTS['style'] + ' The puppy runs to the bowl.', 145)])
            self.assertEqual((scene['action_seconds'], len(scene['action_sha256'])), (6.0, 64))
            self.assertNotIn('action_file', storyboard['scenes'][1])
            action_clips.make_action_clips({}, store, [film], images, pool(Broken()))   # cached: nothing to render
            self.assertEqual(len(renders), 1)
            self.assertIn('action_file', scene)
            storyboard['scenes'][0]['action'] = 'The puppy sits down.'
            del storyboard['scenes'][0]['action_file']
            action_clips.make_action_clips({}, store, [film], images, pool(Broken()))
            self.assertNotIn('action_file', storyboard['scenes'][0])
            self.assertIn(('action-clips', 'fallback'), [(n, s) for n, s, _ in store.events])


class KeyIdeaStillTests(unittest.TestCase):
    def test_every_still_draws_the_plans_world_word_for_word(self):
        from infofilm.creative import library_from_beats
        from test_creative import example
        script, timing, storyboard, brief = example()
        world = {'guide': 'Lumi, a pale-blue ball with an antenna.', 'setting': 'Five yellow landings on dark navy.',
                 'style': 'Flat cartoon.'}
        library = library_from_beats([{'script': script, 'storyboard': storyboard}], {**brief, 'world': world})
        for asset in library['assets']:
            self.assertIn('Draw exactly: the guide is Lumi, a pale-blue ball with an antenna. The setting is Five '
                          'yellow landings on dark navy. Style: Flat cartoon.', asset['prompt'])
        script, timing, storyboard, brief = example()
        self.assertNotIn('Draw exactly', library_from_beats([{'script': script, 'storyboard': storyboard}], brief)
                         ['assets'][0]['prompt'])                       # other films: unchanged


class BestRoundTests(unittest.TestCase):
    def test_when_no_round_lands_the_best_round_is_kept(self):
        verdicts = [{'understood': 96, 'simple': 58, 'wrong_beliefs': [], 'fixes': []},
                    {'understood': 94, 'simple': 72, 'wrong_beliefs': ['x'], 'fixes': []},
                    {'understood': 94, 'simple': 64, 'wrong_beliefs': [], 'fixes': []}]
        with tempfile.TemporaryDirectory() as root:
            models = FakeModels(root, verdicts)
            script = planning.make_script(models, {'title': 'L', 'facts': [{'id': 'F01'}], 'curriculum': IDEA}, 240, 120)
            record = read(Path(root) / 'learner-check-240.json')
            self.assertEqual(record['chosen_round'], 1)
            self.assertIn('Version 2', script['points'][0]['narration'])


class GuideIsAPropTests(unittest.TestCase):
    def test_an_image_of_a_prop_guide_must_be_a_stage_beat(self):
        from infofilm.curriculum import key_idea_beat_errors
        evidence = {'curriculum': {'kind': 'key_idea', 'linkwork': {'subject': 'physics'}},
                    'idea_plan': {'world': {'guide': 'a stick figure', 'props': ['figure', 'road', 'marker']}}}
        board = {'scenes': [{'id': 'S03', 'kind': 'image', 'image_brief': 'A friendly stick figure beside a flagged marker.'},
                            {'id': 'S04', 'kind': 'image', 'image_brief': 'A busy railway station at dawn.'}]}
        errors = ' '.join(key_idea_beat_errors(board, evidence))
        self.assertIn('S03: the guide is the figure prop', errors)
        self.assertNotIn('S04', errors)
        painted_guide = {**evidence, 'idea_plan': {'world': {'guide': 'a fox in a hat', 'props': ['road']}}}
        self.assertNotIn('figure prop', ' '.join(key_idea_beat_errors(board, painted_guide)))
