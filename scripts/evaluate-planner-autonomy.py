#!/usr/bin/env python3
"""Evaluate subject-neutral parent planning on archived sourced briefs; no media."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infofilm.core import ROOT, Store, configuration, digest, read, write
from infofilm.external_planner import REVIEW_TASK, bounded_call, passing_review, plan_errors, plan_film, plan_structure, review_score, scope_warnings, semantic_view
from infofilm.llm import Models
from infofilm.longform import planner_settings


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'config.json')
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/external-planner/subject-autonomy-v7')
    parser.add_argument('--only',choices=['chola','monsoon','mughal'],help='Evaluate a single case in a fresh output directory')
    parser.add_argument('--planner-role',choices=['planner','reviewer'],help='Explicitly choose the planning endpoint; the other model reviews it')
    parser.add_argument('--validate-saved',type=Path,help='Freshly review unchanged first drafts from this experiment; generate normally for missing cases')
    args=parser.parse_args()
    config=configuration(args.config);settings=planner_settings(config)
    if args.planner_role: settings['planner_role']=args.planner_role
    cases=[
        ('chola','The Chola dynasty',600,None,None,
         ROOT/'artifacts/runs/the-chola-dynasty-d1160ec7a5/orientation-brief.json'),
        ('monsoon','How the Indian monsoon works',600,None,None,ROOT/'pilots/longform/monsoon-orientation.json'),
        ('mughal','The Mughal era in India, 1526 to 1857',900,1526,1857,ROOT/'pilots/longform/mughal-orientation.json'),
    ]
    if args.only: cases=[c for c in cases if c[0]==args.only]
    summary={'production_started':False,'cases':[],'limitations':[
        'Archived orientation briefs are limited evidence, not complete chapter research.',
        'These model assessments do not prove factual accuracy or human acceptance.',
        'This tests the generic planner; the subject graph is not production integrated.']}
    for position,(key,subject,duration,start,end,brief_path) in enumerate(cases,1):
        print(f'[planner-autonomy] {position}/{len(cases)} {subject}',flush=True)
        store=Store(args.output/key)
        request={'subject':subject,'advisory_seconds':duration,'start_year':start,'end_year':end,
                 'orientation':'landscape','language':'en','narrator_id':'default'}
        row={'id':key,'subject':subject,'brief':str(brief_path)}
        try:
            baseline=args.validate_saved/key/'candidate-0.json' if args.validate_saved else None
            if baseline and baseline.exists():
                plan=read(baseline);brief=read(brief_path)
                errors=plan_errors(plan,request,brief,settings)
                review_config=dict(config)
                if settings['planner_role']=='reviewer':
                    review_config['planner_url'],review_config['reviewer_url']=config['reviewer_url'],config['planner_url']
                review=bounded_call(Models(review_config,store),'reviewer','saved-draft-review',REVIEW_TASK,
                    {'request_scope':{k:v for k,v in request.items() if k!='advisory_seconds'},
                     'orientation_brief':brief,'candidate':semantic_view(plan),'scope_warnings':scope_warnings(plan,request)},
                    settings,settings['review_max_tokens'])
                accepted=not errors and passing_review(review,settings)
                write(store.root/'revalidation.json',{'baseline':str(baseline),'baseline_sha256':digest(plan),
                    'code_errors':errors,'review':review,'production_started':False})
                if accepted: write(store.root/'chapter-plan.json',plan)
                evaluation={'status':'accepted_saved_plan' if accepted else 'needs_review',
                    'attempts':[{'attempt':1,'code_errors':errors,'review_score':review_score(review,settings),'accepted':accepted}],
                    'total_reported_tokens':sum(read(p).get('usage',{}).get('total_tokens',0) for p in (store.root/'requests').glob('*.response.json'))}
            else:
                plan,evaluation=plan_film(config,store,request,read(brief_path),settings,compare=False)
            row.update(status=evaluation['status'],structure=plan_structure(plan),
                chapters=[{'id':c['id'],'title':c['title'],'scope':c['scope']} for c in plan['chapters']],
                attempts=evaluation['attempts'],reported_tokens=evaluation['total_reported_tokens'])
        except Exception as exc:
            row.update(status='needs_review',error=f'{type(exc).__name__}: {exc}')
        finally:
            store.db.close()
        summary['cases'].append(row)
        write(args.output/'summary.json',summary)
        print(f'[planner-autonomy] {position}/{len(cases)} {row["status"]}',flush=True)


if __name__=='__main__': main()
