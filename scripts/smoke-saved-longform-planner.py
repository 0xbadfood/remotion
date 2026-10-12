#!/usr/bin/env python3
"""Replay frozen long-film planning inputs against an isolated planner endpoint."""
import argparse
import json
import sys
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infofilm.core import Store, digest, read, write
from infofilm.external_planner import (
    bounded_call, plan_errors, plan_film, semantic_view,
)
from infofilm.llm import Models


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--saved-planning', required=True, type=Path)
    parser.add_argument('--planner-url', required=True)
    parser.add_argument('--reviewer-url', default='http://10.8.0.5:8001/v1')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    frozen = read(args.saved_planning / 'request.json')
    baseline = read(args.saved_planning / 'chapter-plan.json')
    settings = {**frozen['settings'], 'planner_role': 'planner'}
    config = {'planner_url': args.planner_url.rstrip('/'),
              'reviewer_url': args.reviewer_url.rstrip('/')}
    store = Store(args.output)
    models = Models(config, store)
    write(store.root / 'input-provenance.json', {
        'saved_planning': str(args.saved_planning.resolve()),
        'saved_request_sha256': digest(frozen), 'baseline_sha256': digest(baseline),
        'reuse': ['request', 'orientation_brief', 'subject_handoff', 'planning_settings'],
        'settings_override': {'planner_role': 'planner'},
        'policy': 'Current production planning prompts and validation; no baseline supplied to the planner',
        'production_started': False,
    })
    try:
        candidate, evaluation = plan_film(
            config, store, frozen['request'], frozen['brief'], settings,
            compare=False, models=models, subject_handoff=frozen.get('subject_handoff'),
        )
        # Alternate labels by input hash; endpoint/model names are withheld from the comparison.
        candidate_first = int(digest(frozen)[0], 16) % 2 == 0
        a, b = (candidate, baseline) if candidate_first else (baseline, candidate)
        comparison = bounded_call(
            models, 'reviewer', 'saved-baseline-blind-comparison',
            'Compare anonymized long-film plans A and B against the supplied frozen research and user scope. '
            'Neither model identity nor their earlier review scores are relevant. Do not match runtime. '
            'Assess source grounding, invented causal claims, complete subject coverage, distinct chapter '
            'ownership, transitions, research gaps and practical downstream handovers. A thematic chapter '
            'may overlap dates; overlap is not automatically repetition. Identify specific regressions. '
            'Return JSON {preferred:"A"|"B"|"tie", reasons:[{criterion,evidence_A,evidence_B}], '
            'regressions:[string], limitations:[string]}.',
            {'request': frozen['request'], 'orientation_brief': frozen['brief'],
             'A': semantic_view(a), 'B': semantic_view(b)},
            settings, settings['review_max_tokens'],
        )
        mapping = {'A': 'new_planner' if candidate_first else 'saved_baseline',
                   'B': 'saved_baseline' if candidate_first else 'new_planner'}
        comparison['label_mapping'] = mapping
        comparison['preferred_plan'] = mapping.get(comparison.get('preferred'), 'tie')
        write(store.root / 'baseline-comparison.json', comparison)
        summary = {
            'status': evaluation['status'], 'production_started': False,
            'attempts': evaluation['attempts'],
            'chapters': [{'id': c['id'], 'title': c['title']} for c in candidate['chapters']],
            'baseline_code_errors_under_current_policy': plan_errors(
                baseline, frozen['request'], frozen['brief'], settings),
            'provider_calls': evaluation['provider_calls'],
            'preferred_plan': comparison['preferred_plan'],
            'limitations': ['One saved subject tests parent planning only, not every main-planner role.',
                            'Historical baseline used an earlier prompt policy and a different reviewer.',
                            'Independent model review is not human fact verification.'],
        }
        write(store.root / 'smoke-summary.json', summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    except httpx.HTTPStatusError as exc:
        write(store.root / 'transport-failure.json', {
            'status_code': exc.response.status_code,
            'url': str(exc.request.url),
            'response_body': exc.response.text[:12000],
            'production_started': False,
        })
        raise
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
