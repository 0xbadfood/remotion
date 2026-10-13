import copy
import tempfile
import unittest
from unittest.mock import Mock, patch

from infofilm.core import ROOT, Store, read
from infofilm.llm import ModelOutputError
from infofilm.external_planner import CONTRACT, RUBRIC, brief_errors, bounded_call, enrich_plan, metrics, passing_review, plan_errors, plan_film, plan_structure, revision_errors, scope_warnings, semantic_view, settings_errors


def fixture():
    settings = read(ROOT / 'external-planner.json')
    request = {'subject': 'Mughal-era India 1526 to 1857', 'advisory_seconds': 900,
               'start_year': 1526, 'end_year': 1857, 'orientation': 'landscape', 'narrator_id': 'default', 'language': 'en'}
    brief = {'sources': [{'id': 'S1', 'url': 'https://example.org/history', 'title': 'History',
                         'excerpts': ['Mughal political control changed substantially between 1526 and 1857.']}],
             'anchors': [{'id': f'F{i:02}', 'claim': f'Grounded topic {i}', 'source_ids': ['S1']} for i in range(1, 4)],
             'required_anchor_ids': ['F01', 'F02', 'F03']}
    plan = copy.deepcopy(CONTRACT)
    plan['film'].update({key: request[key] for key in ['subject','advisory_seconds','orientation','narrator_id','language']})
    plan['film'].update(structure='chronological', structure_rationale='Explain successive developments and their consequences.')
    template = copy.deepcopy(plan['chapters'][0])
    plan['coverage'], plan['chapters'] = [], []
    for index, (start, end) in enumerate([(1526,1605),(1605,1707),(1707,1857)], 1):
        cid, tid, aid = f'C{index:02}', f'T{index:02}', f'F{index:02}'
        chapter = copy.deepcopy(template)
        chapter.update(id=cid, topic_ids=[tid], source_ids=['S1'], advisory_seconds=150,
                       opening_role='film_intro' if index==1 else 'continuation',
                       ending_role='film_conclusion' if index==3 else 'handoff')
        chapter['scope'].update(start_year=start, end_year=end)
        chapter['mood']['direction']=f'Chapter {index}: measured, inquisitive delivery moving from uncertainty to explanation.'
        chapter['music']['direction']=f'Chapter {index}: instrumental strings beneath speech with a shared motif; vary the pulse and fade into the next chapter.'
        plan['chapters'].append(chapter)
        plan['coverage'].append({'id':tid,'question':f'Question {index}','priority':'essential','anchor_ids':[aid],'owner_chapter_id':cid})
    plan['assembly']['chapter_order']=['C01','C02','C03']
    return settings, request, brief, plan


def review(passing=True):
    return {'pass':passing,'rubric':{key:90 if passing else 75 for key in RUBRIC},
            'issues':[] if passing else [{'severity':'major','field':'chapters.C03.exit_handoff',
            'problem':'Ending does not answer the central question','evidence':'The ending restarts the introduction',
            'correction':'Close the central narrative instead'}], 'strengths':['Clear chronology']}


class ExternalPlannerTests(unittest.TestCase):
    def test_animation_route_is_explicit_grounded_and_opt_in(self):
        from infofilm.animation_frontier import chapter_requirement
        from infofilm.longform import chapter_context
        settings,request,brief,plan=fixture()
        chapter=plan['chapters'][0]
        chapter['production']={'kind':'animation','rationale':'A moving comparison explains the mechanism',
            'animation_brief':{'learning_objective':'Compare the changing quantities',
                'demonstrations':['Compare two quantities changing together'],'assumptions':[],
                'success_criteria':['Their causal relationship is visible']}}
        self.assertTrue(any('disabled' in e for e in plan_errors(plan,request,brief,settings)))
        request['animation_chapters']=True
        self.assertEqual(plan_errors(plan,request,brief,settings),[])
        context=chapter_context(plan,chapter,{},brief)
        requirement=chapter_requirement(context)
        self.assertIn('Compare two quantities',requirement['scene_requirement'])
        self.assertEqual(requirement['facts'],[brief['anchors'][0]['claim']])
        self.assertEqual(context['orientation_sources'],brief['sources'])
        chapter['production']['animation_brief']['demonstrations']=[]
        self.assertTrue(plan_errors(plan,request,brief,settings))
        chapter.pop('production')
        self.assertTrue(any('production kind' in e for e in plan_errors(plan,request,brief,settings)))
        request.pop('animation_chapters')
        self.assertEqual(plan_errors(plan,request,brief,settings),[])

    def test_graph_shape_guides_draft_and_review_without_duplicate_sources(self):
        from infofilm.graph_planning import freeze_graph, classify_subject, make_handoff
        settings, request, brief, plan = fixture()
        config = freeze_graph({'planner_url': 'planner', 'reviewer_url': 'reviewer'})
        models = Mock(); models.call.side_effect = [plan, review()]
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                classification = {'primary': 'history.era', 'secondary': [], 'lens': 'history', 'scope_extent': 'broad',
                    'ambiguity': 'clear', 'rationale': 'Era overview', 'model_classification_accepted': True,
                    'needs_classification_review': False}
                with patch('infofilm.graph_planning.model_classify', return_value=classification):
                    seeds, guidance = classify_subject(config, store, models, request['subject'])
                handoff = make_handoff(config, store, seeds, guidance, brief, 900, settings)
                selected, _ = plan_film(config, store, request, brief, settings, models=models, compare=False, subject_handoff=handoff)
                self.assertEqual(selected['film']['subject'], request['subject'])
                draft = models.call.call_args_list[0].args[2]
                self.assertIn(handoff['messages'][1]['content'], draft)
                self.assertEqual(draft.count('Mughal political control changed substantially'), 1)
                self.assertIn('subject_guidance', models.call.call_args_list[1].args[2])
                with self.assertRaisesRegex(ValueError, 'different inputs'):
                    plan_film(config, store, request, brief, settings, models=models, compare=False)
                with self.assertRaisesRegex(ValueError, 'must be ready'):
                    plan_film(config, store, request, brief, settings, subject_handoff={**handoff, 'ready_for_planning': False})
            finally:
                store.db.close()

    def test_malformed_json_has_a_bounded_format_retry_before_semantic_repair(self):
        settings,_,_,plan=fixture()
        models=Mock();models.call.side_effect=[ModelOutputError('Missing closing brace'),plan]
        self.assertEqual(bounded_call(models,'planner','draft','Plan',{},settings,1000),plan)
        self.assertEqual(models.call.call_count,2)
        self.assertEqual(models.call.call_args_list[1].args[1],'draft-format-repair')
        self.assertIn('FORMAT RECOVERY',models.call.call_args_list[1].args[2])
        models.call.side_effect=[ModelOutputError('Bad JSON')]*2
        with self.assertRaises(ModelOutputError):
            bounded_call(models,'planner','draft','Plan',{},settings,1000)

    def test_valid_plan_can_breathe_without_exact_duration_matching(self):
        settings, request, brief, plan = fixture()
        self.assertEqual(plan_errors(plan, request, brief, settings), [])
        self.assertEqual(metrics(plan, request, brief, settings)['chapter_advisory_seconds_total'], 450)
        plan['chapters'][0]['advisory_seconds'] = 220
        self.assertEqual(plan_errors(plan, request, brief, settings), [])
        self.assertEqual(metrics(plan, request, brief, settings)['downstream_duration_warnings'], ['C01'])

    def test_references_ownership_and_user_bounds_are_enforced(self):
        settings, request, brief, original = fixture()
        mutations = [lambda p:p['chapters'][1]['scope'].update(start_year=1510),
                     lambda p:p['chapters'][-1]['scope'].update(end_year=1900),
                     lambda p:p['coverage'][0].update(owner_chapter_id='C99'),
                     lambda p:p['coverage'][0].update(anchor_ids=['F99']),
                     lambda p:p['chapters'][1].update(topic_ids=['T01']),
                     lambda p:p['chapters'][1].update(opening_role='film_intro'),
                     lambda p:p['chapters'][0].update(ending_role='film_conclusion'),
                     lambda p:p['chapters'][0].update(source_ids=['S99'])]
        for mutation in mutations:
            plan = copy.deepcopy(original)
            mutation(plan)
            self.assertTrue(plan_errors(plan, request, brief, settings))

    def test_conceptual_subject_has_no_invented_year_bounds(self):
        settings, request, brief, plan = fixture()
        request.update(subject='How the Indian monsoon works', start_year=None, end_year=None)
        plan['film']['subject']=request['subject']
        plan['film']['structure']='thematic'
        for chapter in plan['chapters']:
            chapter['scope'].update(start_year=None,end_year=None)
        self.assertEqual(plan_errors(plan, request, brief, settings), [])
        plan['chapters'][0]['scope']['start_year']='unknown'
        self.assertTrue(plan_errors(plan, request, brief, settings))

    def test_single_year_chapter_is_valid_and_dated_anchors_cannot_drift(self):
        settings, request, brief, plan = fixture()
        plan['chapters'][1]['scope']['end_year']=1857
        plan['chapters'][2]['scope']['start_year']=1857
        brief['anchors'][2]['event_years']=[1857]
        self.assertEqual(plan_errors(plan,request,brief,settings),[])
        brief['anchors'][2]['event_years']=[1757]
        self.assertTrue(any('outside' in error for error in plan_errors(plan,request,brief,settings)))

    def test_adjacent_calendar_years_are_not_a_false_chronology_gap(self):
        settings,request,brief,plan=fixture()
        plan['chapters'][0]['scope']['end_year']=1604
        plan['chapters'][1]['scope']['end_year']=1706
        self.assertEqual(plan_errors(plan,request,brief,settings),[])
        plan['chapters'][1]['scope']['start_year']=1606
        self.assertEqual(plan_errors(plan,request,brief,settings),[])
        self.assertTrue(scope_warnings(plan,request))

    def test_history_without_user_dates_can_choose_chronology(self):
        settings,request,brief,plan=fixture()
        request.update(subject='The Chola dynasty',start_year=None,end_year=None)
        plan['film']['subject']=request['subject']
        self.assertEqual(plan_errors(plan,request,brief,settings),[])

    def test_hybrid_and_thematic_plans_may_overlap_even_with_user_year_bounds(self):
        settings,request,brief,plan=fixture()
        for structure in ['hybrid','thematic','comparison','procedure']:
            plan['film']['structure']=structure
            plan['chapters'][1]['scope'].update(start_year=1526,end_year=1857)
            plan['chapters'][2]['scope'].update(start_year=None,end_year=None)
            brief['anchors'][1]['event_years']=[1526]
            self.assertEqual(plan_errors(plan,request,brief,settings),[])
            self.assertEqual(scope_warnings(plan,request),[])

    def test_year_partition_gaps_are_review_warnings_not_rewrite_triggers(self):
        settings,request,brief,plan=fixture()
        plan['chapters'][1]['scope']['start_year']=1607
        plan['chapters'][-1]['scope']['end_year']=1803
        self.assertEqual(plan_errors(plan,request,brief,settings),[])
        self.assertEqual(len(scope_warnings(plan,request)),2)

    def test_legacy_plans_keep_their_structure_without_using_request_dates(self):
        settings,request,brief,plan=fixture()
        plan['film'].pop('structure');plan['film'].pop('structure_rationale')
        request.update(start_year=None,end_year=None)
        self.assertEqual(plan_structure(plan),'chronological')
        self.assertEqual(plan_errors(plan,request,brief,settings),[])
        plan['chapters'][1]['scope']['start_year']=1526
        self.assertEqual(plan_structure(plan),'hybrid')
        self.assertEqual(plan_errors(plan,request,brief,settings),[])

    def test_structure_and_date_shapes_cannot_be_disguised_as_editorial_choices(self):
        settings,request,brief,plan=fixture()
        for bad in ['ruler_sequence',[],None]:
            plan['film']['structure']=bad
            self.assertTrue(plan_errors(plan,request,brief,settings))
        plan['film']['structure']='thematic'
        plan['chapters'][0]['scope'].update(start_year=True,end_year=1605)
        self.assertTrue(plan_errors(plan,request,brief,settings))

    def test_unknown_date_endpoints_and_concurrent_developments_do_not_force_rewrite(self):
        settings,request,brief,plan=fixture()
        request.update(start_year=None,end_year=None)
        plan['chapters'][0]['scope']['start_year']=None
        plan['chapters'][1]['scope']['start_year']=1600
        self.assertEqual(plan_errors(plan,request,brief,settings),[])
        plan['chapters'][0]['scope']['start_year']=1526
        self.assertTrue(scope_warnings(plan,request))
        plan['chapters'][2]['scope']['start_year']=1590
        self.assertTrue(any('reverses' in e for e in plan_errors(plan,request,brief,settings)))

    def test_deterministic_enrichment_does_not_rewrite_narrative_or_hide_unknown_sources(self):
        settings, request, brief, original=fixture()
        original['chapters'][0]['source_ids']=[]
        original['chapters'][-1]['ending_role']='conclusion'
        enriched, changes=enrich_plan(original,brief)
        self.assertEqual(len(changes),2)
        self.assertEqual(enriched['chapters'][0]['source_ids'],['S1'])
        self.assertEqual(enriched['chapters'][-1]['ending_role'],'film_conclusion')
        self.assertEqual(enriched['chapters'][-1]['exit_handoff'],original['chapters'][-1]['exit_handoff'])
        self.assertEqual(original['chapters'][0]['source_ids'],[])
        original['chapters'][0]['source_ids']=['S99']
        enriched,_=enrich_plan(original,brief)
        self.assertTrue(plan_errors(enriched,request,brief,settings))
        _,_,_,original=fixture()
        original.pop('schema_version')
        enriched,changes=enrich_plan(original,brief)
        self.assertFalse(plan_errors(enriched,request,brief,settings))
        self.assertEqual(changes[0]['field'],'schema_version')
        original['schema_version']='unknown.external.contract'
        enriched,_=enrich_plan(original,brief)
        self.assertTrue(plan_errors(enriched,request,brief,settings))

    def test_important_priority_is_audited_and_preserves_coverage(self):
        settings, request, brief, original = fixture()
        original['coverage'][0]['priority'] = 'important'
        enriched, changes = enrich_plan(original, brief)
        expected = copy.deepcopy(original)
        expected['coverage'][0]['priority'] = 'essential'
        self.assertEqual(enriched, expected)
        self.assertEqual(original['coverage'][0]['priority'], 'important')
        self.assertEqual(changes[0]['field'], 'coverage.T01.priority')
        self.assertEqual(changes[0]['before'], 'important')
        self.assertEqual(changes[0]['after'], 'essential')
        self.assertEqual(plan_errors(enriched, request, brief, settings), [])

    def test_unknown_priority_and_empty_question_have_actionable_feedback(self):
        settings, request, brief, plan = fixture()
        plan['coverage'][0].update(priority='optional', question='')
        enriched, changes = enrich_plan(plan, brief)
        self.assertEqual(changes, [])
        errors = plan_errors(enriched, request, brief, settings)
        self.assertTrue(any('coverage.T01.question' in e for e in errors))
        self.assertTrue(any('priority=\'optional\'' in e and '"essential" or "supporting"' in e for e in errors))

    def test_semantic_reviewer_cannot_impose_absolute_runtime(self):
        _, _, _, plan=fixture()
        viewed=semantic_view(plan)
        self.assertNotIn('advisory_seconds',viewed['film'])
        self.assertTrue(all('advisory_seconds' not in c for c in viewed['chapters']))
        self.assertAlmostEqual(sum(c['relative_attention_percent'] for c in viewed['chapters']),99.9)
        self.assertIn('advisory_seconds',plan['film'])

    def test_pass_boolean_does_not_override_major_issues_or_invalid_scores(self):
        settings, _, _, _ = fixture()
        value=review()
        self.assertTrue(passing_review(value,settings))
        value['issues']=[{'severity':'major'}]
        self.assertFalse(passing_review(value,settings))
        value['issues']=[]
        value['rubric']['coverage']=True
        self.assertFalse(passing_review(value,settings))

    def test_generic_contract_directions_cannot_pass_even_with_high_model_score(self):
        settings,request,brief,plan=fixture()
        plan['chapters'][0]['music']['direction']=CONTRACT['chapters'][0]['music']['direction']
        self.assertTrue(any('placeholder' in e for e in plan_errors(plan,request,brief,settings)))
        for chapter in plan['chapters']:
            chapter['music']['direction']='Restrained instrumental underscore below speech; coherent motif with chapter-specific development'
        self.assertTrue(any('Identical music' in e for e in plan_errors(plan,request,brief,settings)))

    def test_focused_revision_preserves_seed_and_compares_against_it(self):
        settings,request,brief,baseline=fixture()
        selected=copy.deepcopy(baseline)
        selected['chapters'][0]['music']['direction']='Instrumental low strings with a sparse pulse; introduce the motif quietly and pass it to the next chapter.'
        models=Mock()
        models.call.side_effect=[selected,review(),{'preferred':'A','reasons':[],'regressions':[],'limitations':[]}]
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            plan,report=plan_film({'planner_url':'http://one','reviewer_url':'http://two'},store,request,brief,settings,
                models=models,seed_plan=baseline,seed_feedback={'issues':['Specify instruments']})
            self.assertEqual(read(store.root/'baseline.json'),baseline)
            self.assertEqual(len(report['attempts']),1)
            self.assertIn('previous_plan',models.call.call_args_list[0].args[2])
            self.assertIn('Specify instruments',models.call.call_args_list[0].args[2])
            self.assertEqual(plan,selected)
            store.db.close()

    def test_explicit_factual_corrections_cannot_be_silently_ignored(self):
        settings,request,brief,baseline=fixture()
        feedback={'must_change_fields':['chapters.C02.entry_context']}
        self.assertTrue(revision_errors(baseline,baseline,feedback))
        corrected=copy.deepcopy(baseline)
        corrected['chapters'][1]['entry_context']='Continue from the documented succession, without assuming later events.'
        self.assertEqual(revision_errors(corrected,baseline,feedback),[])
        self.assertTrue(revision_errors(corrected,baseline,{'must_change_fields':['chapters.C99.entry_context']}))
        models=Mock()
        models.call.side_effect=[baseline,review(),corrected,review()]
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            _,report=plan_film({'planner_url':'http://one','reviewer_url':'http://two'},store,request,brief,settings,
                models=models,compare=False,seed_plan=baseline,seed_feedback=feedback)
            self.assertFalse(report['attempts'][0]['accepted'])
            self.assertTrue(report['attempts'][1]['accepted'])
            self.assertIn('editorial_findings_to_verify',models.call.call_args_list[1].args[2])
            store.db.close()

    def test_real_loop_saves_baseline_repairs_comparison_and_never_runs_production(self):
        settings, request, brief, initial=fixture()
        selected=copy.deepcopy(initial)
        selected['chapters'][-1]['exit_handoff']='Conclude by linking changing political control with cultural continuity.'
        models=Mock()
        models.call.side_effect=[initial,review(False),selected,review(),
            {'preferred':'B','reasons':[],'regressions':[],'limitations':[]}]
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            with patch('infofilm.media.gpu_lease',side_effect=AssertionError('No production')):
                plan, report=plan_film({'planner_url':'http://one','reviewer_url':'http://two'},store,request,brief,settings,models=models)
            self.assertEqual(plan,selected)
            self.assertFalse(report['production_started'])
            self.assertEqual(len(report['attempts']),2)
            self.assertTrue((store.root/'comparison.json').is_file())
            self.assertTrue((store.root/'plan-review.md').is_file())
            self.assertEqual(read(store.root/'candidate-0.json'),initial)
            review_input=models.call.call_args_list[1].args[2]
            self.assertNotIn('"advisory_seconds"',review_input)
            self.assertEqual(read(store.root/'chapter-plan.json'),selected)
            store.db.close()

    def test_unresolved_plan_is_bounded_and_not_published_as_approved(self):
        settings, request, brief, plan=fixture()
        models=Mock()
        models.call.side_effect=[plan,review(False)]*settings['max_attempts']
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            with self.assertRaisesRegex(RuntimeError,'bounded attempts'):
                plan_film({'planner_url':'http://one','reviewer_url':'http://two'},store,request,brief,settings,models=models)
            self.assertEqual(models.call.call_count,6)
            self.assertFalse((store.root/'chapter-plan.json').exists())
            self.assertEqual(read(store.root/'evaluation.json')['status'],'needs_review')
            store.db.close()

    def test_context_bounds_and_inputs_fail_before_llm_calls(self):
        settings, request, brief, _=fixture()
        models=Mock()
        short=dict(settings,max_request_bytes=10000)
        with self.assertRaisesRegex(ValueError,'byte bound'):
            bounded_call(models,'planner','test','task',{'text':'a'*11000},short,1000)
        models.call.assert_not_called()
        missing=copy.deepcopy(brief)
        missing['anchors'][0]['source_ids']=['S99']
        self.assertTrue(brief_errors(missing,settings))
        self.assertTrue(settings_errors({**settings,'max_attempts':True}))

    def test_experiment_inputs_are_frozen_and_llm_roles_can_be_swapped(self):
        settings, request, brief, plan=fixture()
        settings['planner_role']='reviewer'
        models=Mock()
        models.call.side_effect=[plan,review()]
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            plan_film({'planner_url':'http://one','reviewer_url':'http://two'},store,request,brief,settings,models=models)
            saved=read(store.root/'request.json')
            self.assertEqual(saved['llms']['planner_url'],'http://two')
            with self.assertRaisesRegex(ValueError,'different inputs'):
                plan_film({'planner_url':'http://one','reviewer_url':'http://two'},store,{**request,'advisory_seconds':600},brief,settings,models=models)
            store.db.close()


class TargetedPlanRepairTests(unittest.TestCase):
    def test_review_fields_name_the_chapters_to_repair_and_the_rest_are_restored(self):
        from infofilm.external_planner import flagged_chapters, keep_unflagged_chapters
        before = {'chapters': [{'id': 'C01', 'title': 'Old one'}, {'id': 'C02', 'title': 'Old two'}, {'id': 'C03', 'title': 'Old three'}]}
        issues = [{'field': 'chapters[C02].scope; chapters[C02].sections'}, {'field': 'coverage[T08]'}, {'field': 'chapters[2].title'}]
        self.assertEqual(flagged_chapters(before, issues), ['C02', 'C03'])
        after = {'chapters': [{'id': 'C01', 'title': 'Drifted'}, {'id': 'C02', 'title': 'Fixed'}, {'id': 'C03', 'title': 'Also fixed'}]}
        kept = keep_unflagged_chapters(before, after, ['C02', 'C03'])
        self.assertEqual([c['title'] for c in kept['chapters']], ['Old one', 'Fixed', 'Also fixed'])
        renumbered = {'chapters': [{'id': 'C01'}, {'id': 'C02'}]}          # the planner dropped C03
        merged = keep_unflagged_chapters(before, renumbered, ['C02'])           # renumbering is ignored: merge by id
        self.assertEqual([c['id'] for c in merged['chapters']], [c['id'] for c in before['chapters']])


def stalled_review(field, problem='States an unsupported claim', severity='major', score=88):
    return {'pass': False, 'rubric': {key: score for key in RUBRIC},
            'issues': [{'severity': severity, 'field': field, 'problem': problem, 'evidence': 'S1 does not say this',
                        'correction': 'Qualify the claim'}], 'strengths': ['Clear chronology']}


class PlanUnitTests(unittest.TestCase):
    """Cuban Missile Crisis, 2026-10-13: three rounds, each review blocking on something new, and no way out."""

    def test_finding_fields_resolve_to_units_and_only_those_change(self):
        from infofilm.external_planner import issue_units, keep_unflagged_units
        _, _, _, plan = fixture()
        units = issue_units(plan, {'field': 'chapters[C02].exit_handoff; chapters[C03].scope.description, film.coverage'})
        self.assertEqual(units, ['chapters[C02].exit_handoff', 'chapters[C03].scope', 'coverage'])
        self.assertEqual(issue_units(plan, {'field': 'chapters.C03.exit_handoff'}), ['chapters[C03].exit_handoff'])
        self.assertEqual(issue_units(plan, {'field': 'research_gaps'}), ['research_gaps'])
        self.assertEqual(issue_units(plan, {'field': 'the whole plan'}), [])
        self.assertIn('chapters[C01].title', issue_units(plan, {'field': 'chapters[C01]'}))
        drifted = copy.deepcopy(plan)
        drifted['chapters'][1]['exit_handoff'] = 'Fixed handoff.'
        drifted['chapters'][1]['title'] = 'Unasked retitle'                     # outside the scope: restored by code
        drifted['film']['title'] = 'Unasked film title'
        merged = keep_unflagged_units(plan, drifted, ['chapters[C02].exit_handoff'])
        self.assertEqual(merged['chapters'][1]['exit_handoff'], 'Fixed handoff.')
        self.assertEqual(merged['chapters'][1]['title'], plan['chapters'][1]['title'])
        self.assertEqual(merged['film']['title'], plan['film']['title'])

    def test_review_cannot_newly_block_on_unchanged_text_and_source_needs_become_gaps(self):
        from infofilm.external_planner import screen_findings
        _, _, _, plan = fixture()
        candidate = copy.deepcopy(plan)
        candidate['chapters'][1]['exit_handoff'] = 'Repaired handoff.'
        review = {'pass': False, 'rubric': {k: 85 for k in RUBRIC}, 'issues': [
            {'severity': 'major', 'field': 'chapters[C03].title', 'problem': 'Title implies one channel'},   # unchanged, never asked
            {'severity': 'major', 'field': 'chapters[C02].exit_handoff', 'problem': 'Still overstated'},     # changed: stays
            {'severity': 'major', 'field': 'film.coverage; research_gaps',
             'problem': 'Soviet perspectives are not grounded', 'correction': 'Add primary or scholarly sources beyond S1'}]}
        screened = screen_findings(candidate, plan, copy.deepcopy(review), raised={'chapters[C02].exit_handoff'})
        self.assertEqual([i['severity'] for i in screened['issues']], ['minor', 'major', 'minor'])
        self.assertIn('research gap', screened['issues'][2]['downgraded'])
        self.assertEqual(candidate['research_gaps'][-1], {'question': 'Add primary or scholarly sources beyond S1', 'chapter_id': 'C03'})
        self.assertFalse(screened['pass'])
        # A finding the planner was asked to fix and returned unchanged still blocks.
        asked = screen_findings(plan, plan, copy.deepcopy(review), raised={'chapters[C03].title'})
        self.assertEqual(asked['issues'][0]['severity'], 'major')
        # An unsupported-claim finding that mentions sources is not a research gap when it points at narrative text.
        claim = {'pass': False, 'rubric': {k: 85 for k in RUBRIC}, 'issues': [{'severity': 'major', 'field': 'chapters[C02].exit_handoff',
                 'problem': 'Asserts a decision beyond the supplied sources'}]}
        self.assertEqual(screen_findings(candidate, plan, claim, raised=set())['issues'][0]['severity'], 'major')

    def test_stalemate_accepts_the_best_round_and_carries_its_findings(self):
        settings, request, brief, plan = fixture()
        models = Mock()
        models.call.side_effect = [plan, stalled_review('chapters[C02].exit_handoff', score=86),
                                   plan, stalled_review('chapters[C02].exit_handoff', score=90),
                                   plan, stalled_review('chapters[C02].exit_handoff', score=84),
                                   {'preferred': 'tie', 'reasons': [], 'regressions': [], 'limitations': []}]
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            selected, report = plan_film({'planner_url': 'http://one', 'reviewer_url': 'http://two'}, store, request, brief, settings, models=models)
            self.assertEqual(report['status'], 'accepted_with_findings')
            self.assertEqual(report['attempts'][1]['accepted_with_findings'], 1)
            self.assertEqual(selected['chapters'][1]['plan_findings'][0]['correction'], 'Qualify the claim')
            self.assertNotIn('plan_findings', selected['chapters'][0])
            self.assertEqual(read(store.root / 'chapter-plan.json'), selected)
            self.assertFalse(plan_errors(selected, request, brief, settings))
            # Round 3 is told the flagged field came back unchanged.
            third = models.call.call_args_list[4].args[2]
            self.assertIn('"unchanged_last_round": ["chapters[C02].exit_handoff"]', third)
            store.db.close()

    def test_structural_or_low_scoring_stalemates_still_stop(self):
        settings, request, brief, plan = fixture()
        for review_value in (stalled_review('coverage', problem='Required anchor is missing'),     # not narrative wording
                             stalled_review('chapters[C02].exit_handoff', score=70),              # below the minimum
                             stalled_review('chapters[C02].exit_handoff', severity='blocker')):
            models = Mock()
            models.call.side_effect = [plan, review_value] * settings['max_attempts']
            with tempfile.TemporaryDirectory() as directory:
                store = Store(directory)
                with self.assertRaisesRegex(RuntimeError, 'bounded attempts'):
                    plan_film({'planner_url': 'http://one', 'reviewer_url': 'http://two'}, store, request, brief, settings, models=models)
                store.db.close()
        models = Mock()
        models.call.side_effect = [plan, stalled_review('chapters[C02].exit_handoff')] * settings['max_attempts']
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with self.assertRaisesRegex(RuntimeError, 'bounded attempts'):
                plan_film({'planner_url': 'http://one', 'reviewer_url': 'http://two'}, store, request, brief,
                          {**settings, 'accept_with_findings': False}, models=models)
            store.db.close()

    def test_minor_findings_on_repaired_chapters_travel_with_the_repair(self):
        settings, request, brief, plan = fixture()
        first = stalled_review('chapters[C02].scope')
        first['issues'].append({'severity': 'minor', 'field': 'chapters[C02].exit_handoff', 'problem': 'Only channel is too strong',
                                'evidence': 'S1', 'correction': 'Say a secret channel'})
        first['issues'].append({'severity': 'minor', 'field': 'chapters[C03].mood', 'problem': 'Mood is flat',
                                'evidence': 'S1', 'correction': 'Vary it'})
        models = Mock()
        models.call.side_effect = [plan, first, plan, review()]
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            plan_film({'planner_url': 'http://one', 'reviewer_url': 'http://two'}, store, request, brief, settings, models=models, compare=False)
            repair = models.call.call_args_list[2].args[2]
            self.assertIn('Only channel is too strong', repair)
            self.assertNotIn('Mood is flat', repair)                                # another chapter: not in this repair
            self.assertIn('"units": ["chapters[C02].scope", "chapters[C02].exit_handoff"]', repair)
            store.db.close()
