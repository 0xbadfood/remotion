#!/usr/bin/env python3
"""Smoke the local external planner's chapter routes; never produce speech or images."""
import argparse
import html
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from infofilm.core import ROOT, Store, configuration, read, write
from infofilm.external_planner import plan_film, production_kind
from infofilm.longform import chapter_context, planner_settings
from infofilm.animation_frontier import chapter_handoff, chapter_requirement


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--config',type=Path,default=ROOT/'config.json')
    parser.add_argument('--revisions',type=Path,help='Optional case ID → saved accepted revision directory map')
    args=parser.parse_args();config=configuration(args.config);settings=planner_settings(config)
    rows=[];cases=read(args.cases);revisions=read(args.revisions) if args.revisions else {}
    for position,case in enumerate(cases,1):
        print(json.dumps({'science-smoke':'running','position':position,'total':len(cases),'case':case['id']}),flush=True)
        store=Store(Path(revisions[case['id']]) if case['id'] in revisions else args.output/case['id'])
        try:
            request={'subject':case['subject'],'advisory_seconds':case['advisory_seconds'],
                'start_year':None,'end_year':None,'language':'en','orientation':'landscape',
                'narrator_id':'default','animation_chapters':True}
            brief_path=Path(case['brief'])
            if not brief_path.is_absolute():brief_path=args.cases.parent/brief_path
            brief=read(brief_path)
            saved=store.root/'chapter-plan.json'
            if saved.exists():
                plan=read(saved);report=read(store.root/'evaluation.json')
            else:
                plan,report=plan_film(config,store,request,brief,settings,compare=False)
            chapters=[]
            for chapter in plan['chapters']:
                context=chapter_context(plan,chapter,{},brief)
                requirement=chapter_requirement(chapter_handoff(context))
                write(store.root/'handoffs'/(chapter['id']+'.json'),context)
                write(store.root/'handoffs'/(chapter['id']+'-requirement.json'),requirement)
                chapters.append({'id':chapter['id'],'title':chapter['title'],'kind':production_kind(chapter),
                    'objective':chapter['objective'],'production':chapter.get('production'),
                    'grounded_claims':len(requirement['facts'])})
            row={'id':case['id'],'subject':case['subject'],'status':'accepted','attempts':len(report['attempts']),
                 'chapters':chapters,'tokens':report['total_reported_tokens'],
                 'revision':case['id'] in revisions,'review_score':report['attempts'][-1]['review_score'],
                 'model_routes':read(store.root/'request.json')['llms']}
        except Exception as exc:
            row={'id':case['id'],'subject':case['subject'],'status':'failed','error':str(exc)}
        finally:store.db.close()
        rows.append(row);write(args.output/'summary.json',rows)
        print(json.dumps({'science-smoke':'finished','position':position,'total':len(cases),**row}),flush=True)
    body='<h1>Science chapter handoff smoke</h1><p>Local external planner + separate local review calls. Planning only; no audio, assets or video generated. Source-backed briefs were manually curated. Model review does not establish scientific correctness.</p>'
    for row in rows:
        body+='<h2>'+html.escape(row['subject'])+'</h2><p>'+html.escape(row['status'])+'</p>'
        if row['status']=='failed':body+='<pre>'+html.escape(row['error'])+'</pre>';continue
        if row['revision']:body+='<p>Source-backed revision selected after inspection; original draft and findings are retained.</p>'
        body+='<table><tr><th>Chapter</th><th>Route</th><th>Objective and demonstrations</th></tr>'
        for c in row['chapters']:
            brief=c['production'].get('animation_brief') or {}
            body+='<tr><td>'+html.escape(c['id']+' · '+c['title'])+'</td><td>'+html.escape(c['kind'])+'</td><td>'+html.escape(c['objective'])+'<ul>'+''.join('<li>'+html.escape(x)+'</li>' for x in brief.get('demonstrations',[]))+'</ul></td></tr>'
        body+='</table>'
    (args.output/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>Science animation handoffs</title><style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:20px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:12px;text-align:left;vertical-align:top}</style>'+body)
    files=['report.html','summary.json']
    files.extend(str(p.relative_to(args.output)) for p in args.output.glob('*/handoffs/*.json'))
    files.extend(str(p.relative_to(args.output)) for p in args.output.glob('*/chapter-plan.json'))
    write(args.output/'public-report.json',{'schema_version':'infofilm.public-experiment-report.v1','files':files})
    return int(any(row['status']=='failed' for row in rows))

if __name__=='__main__':sys.exit(main())
