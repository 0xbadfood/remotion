#!/usr/bin/env python3
"""Frozen, resumable classification/scope evaluation; never starts production."""
import argparse
import html
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infofilm.core import Store, configuration, digest, read, write
from infofilm.llm import Models
from infofilm.subject_taxonomy import (
    ancestry, classify, classification_errors, planner_request, seed_subject, validate_graph,
)

def scope_errors(value, subject):
    errors = []
    if value.get('coverage_intent') not in {'overview', 'focused', 'exhaustive', 'comparison', 'procedure'}:
        errors.append('Invalid coverage_intent')
    if value.get('suggested_structure') not in {'chronological', 'thematic', 'hybrid', 'comparison', 'procedure'}:
        errors.append('suggested_structure must be exactly chronological, thematic, hybrid, comparison or procedure; explanation and case_study are lenses, not structure values')
    terms = value.get('focus_terms')
    if not isinstance(terms, list) or not 1 <= len(terms) <= 10:
        errors.append('Need 1–10 focus_terms copied literally from the subject')
    elif any(not isinstance(t, str) or not t.strip() or t.casefold() not in subject.casefold() for t in terms):
        errors.append('focus_terms must be literal subject excerpts, not invented content')
    return errors


def complete_items(value, ids):
    rows = value.get('items') if isinstance(value, dict) else None
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError('Return items:[objects]')
    found = [r.get('id') for r in rows]
    if any(not isinstance(i, str) for i in found) or len(found) != len(set(found)) or set(found) != set(ids):
        raise ValueError('Cover every requested ID exactly once; no invented IDs')
    return {r['id']: r for r in rows}


def batch_call(models, role, name, task, packet, ids, validator, tokens):
    prompt = task + '\nINPUT:\n' + json.dumps(packet, ensure_ascii=False)
    if len(prompt.encode()) > 110000:
        raise ValueError('Batch request exceeds the code byte bound')
    feedback = ''
    accepted, requested = {}, list(ids)
    for attempt in range(2):
        try:
            value = models.call(role, name + f'-json-{attempt}', prompt + feedback, max_tokens=tokens)
            rows = complete_items(value, requested)
            problems = {i: validator(i, row) for i, row in rows.items()}
            problems = {i: e for i, e in problems.items() if e}
            accepted.update({i: row for i, row in rows.items() if i not in problems})
            if not problems:
                return {i: accepted[i] for i in ids}
            requested = list(problems)
            feedback = ('\nReturn items ONLY for these repair IDs: ' + json.dumps(requested) +
                        '. Preserve correct content, repairing these schema errors: ' + json.dumps(problems) +
                        '\nPrevious rows requiring repair: ' + json.dumps([rows[i] for i in requested]))
        except ValueError as exc:
            feedback = ('\nReturn a complete compact JSON object for ONLY these IDs: ' + json.dumps(requested) +
                        '. Schema error: ' + str(exc))
    raise ValueError('Batch output schema failed twice: ' + feedback)


CLASSIFY = (
    'Classify each informational subject into the supplied existing STATIC graph. '
    'Choose the most specific appropriate existing node from meaning, not keyword frequency. '
    'Historical development of technology may use history.intellectual with technology facets; '
    'a technical explanation of an old machine can remain engineering. A place or food word '
    'does not make an engineering/history subject geography/cooking. '
    'Use up to three substantively relevant secondary nodes. Preserve uncertainty for insufficient '
    'graph coverage. Broad ancestors are allowed; never invent IDs or confidence percentages. '
    'Respect every explicitly named focus and comparison. Extract focus_terms as literal title '
    'fragments. Infer coverage_intent from wording; a broad overview allows editorial selection, '
    'an explicit exhaustive request requires complete coverage. suggested_structure is advisory '
    'and may be chronological, thematic, hybrid, comparison or procedure. User-supplied years '
    'are optional scope bounds, never a prerequisite for historical classification. '
    'Do not research, assert facts, invent dates, plan chapters, or impose a chapter quota. '
    'Return {items:[{id,primary,secondary:[],lens,scope_extent:"broad"|"focused"|"unknown",'
    'ambiguity:"clear"|"ambiguous"|"unknown",rationale:string (one short sentence),'
    'coverage_intent:"overview"|"focused"|"exhaustive"|"comparison"|"procedure",'
    'focus_terms:[literal excerpt],suggested_structure}]}.'
)
REVIEW = (
    'Independently review each subject classification/scope against the existing graph. '
    'Check the primary lens, domain specificity, useful secondary facets, preservation of '
    'explicit subject focus, and user coverage intent. Both historical and domain-led treatments '
    'can be appropriate: reject substantively misleading choices, not legitimate editorial alternatives. '
    'A keyword match or a place name is not evidence of a correct category. '
    'suggested_structure is advisory and must leave editorial autonomy. Accept an honest broad '
    'ancestor when the graph lacks a specialization; do not demand invented nodes. '
    'Keep uncertainties explicit. Do not research or plan chapters. '
    'Return {items:[{id,pass:boolean,issues:[specific substantive problem string]}]}; '
    'a pass requires empty issues. Review all IDs exactly once.'
)


def report(root, cases, graph, states):
    rows = []
    for index, case in enumerate(cases):
        cid = f'S{index+1:03d}'
        state = states.get(cid)
        static = classify(graph, case['subject']) if not state else state['static']
        candidate = state.get('candidate') if state else None
        rows.append({'id': cid, **case, 'static': static, 'completed': bool(state and state.get('complete')),
                     'accepted_by_reviewer': bool(state and state.get('accepted')), 'classification': candidate,
                     'review': state.get('review') if state else None,
                     'code_errors': state.get('code_errors', []) if state else [],
                     'technical_error': state.get('technical_error') if state else None})
    value = {'tested': len(cases), 'completed': sum(r['completed'] for r in rows),
             'reviewer_accepted': sum(r['accepted_by_reviewer'] for r in rows),
             'technical_failures': sum(bool(r['technical_error']) for r in rows),
             'semantic_rejections': sum(r['completed'] and not r['technical_error'] and not r['accepted_by_reviewer'] for r in rows),
             'uncertain_classifications': sum(r['completed'] and r['classification'] is not None and r['classification'].get('ambiguity') != 'clear' for r in rows),
             'needs_review': sum(r['completed'] and (not r['accepted_by_reviewer'] or
                 (r['classification'] or {}).get('ambiguity') != 'clear') for r in rows),
             'taxonomy_sha256': digest(graph),
             'limitations': ['Model agreement is not classification accuracy; supplied types are unused metadata.',
                 'Focus terms and planner handoffs do not prove good decomposition or factual grounding.',
                 'No chapter plans, research, audio, images or videos are produced. The production graph is unchanged.'],
             'cases': rows}
    groups = {}
    for row in rows:
        classification = row['classification'] or row['static']
        category = classification['primary']
        group = groups.setdefault(category, {'label': graph['nodes'][category]['label'],
            'family': ancestry(graph, category)[1] if category != graph['root'] else graph['root'], 'subjects': []})
        group['subjects'].append({'id': row['id'], 'subject': row['subject'],
            'secondary': classification['secondary'], 'lens': classification['lens'],
            'completed': row['completed'], 'reviewer_accepted': row['accepted_by_reviewer']})
    value['primary_groups'] = {k: len(v['subjects']) for k,v in groups.items()}
    value['family_groups'] = dict(Counter(g['family'] for g in groups.values() for _ in g['subjects']))
    write(root/'groups.json', groups)
    write(root/'report.json', value)
    cards = []
    for row in rows:
        c = row['classification'] or {}
        cards.append('<tr><td>'+row['id']+'</td><td>'+html.escape(row['subject'])+'</td><td>'+html.escape(row['type'])+
            '</td><td>'+html.escape(c.get('primary','Pending'))+'</td><td>'+html.escape(', '.join(c.get('secondary',[])))+
            '</td><td>'+html.escape(c.get('lens',''))+' / '+html.escape(c.get('coverage_intent',''))+
            '</td><td>'+('Pass' if row['accepted_by_reviewer'] else 'Review' if row['completed'] else 'Pending')+
            ' / '+html.escape(c.get('ambiguity',''))+
            '</td><td>'+html.escape('; '.join((row['review'] or {}).get('issues',[]) + row['code_errors'] +
                ([row['technical_error']] if row['technical_error'] else [])))+'</td></tr>')
    (root/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Subject graph evaluation</title>'
        '<style>body{font:15px/1.5 system-ui;margin:24px;background:#f8fafc;color:#172033}table{border-collapse:collapse;width:100%}'
        'td,th{border:1px solid #cbd5e1;padding:8px;text-align:left;vertical-align:top}thead{position:sticky;top:0;background:#e2e8f0}'
        'input{padding:10px;width:70%}</style><h1>subjects1 graph evaluation</h1><p>'+str(value['completed'])+'/'+str(len(cases))+
        ' completed; '+str(value['reviewer_accepted'])+' reviewer accepted. These are classification/scope tests, not factual or chapter-quality approval.</p>'
        '<input placeholder="Filter subjects, categories or issues" oninput="for(const r of document.querySelectorAll(\'tbody tr\'))r.hidden=!r.textContent.toLowerCase().includes(this.value.toLowerCase())">'
        '<p><a href="groups.json">Graph groups</a> · <a href="report.json">Detailed JSON</a></p><table><thead><tr><th>ID</th><th>Subject</th><th>Supplied type</th><th>Primary</th>'
        '<th>Facets</th><th>Lens / scope</th><th>Reviewer</th><th>Issues</th></tr></thead><tbody>'+''.join(cards)+'</tbody></table>')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--subjects',type=Path,default=Path('subjects1.json'))
    parser.add_argument('--output',type=Path,default=Path('artifacts/subject-taxonomy/subjects1'))
    parser.add_argument('--batch-size',type=int,default=20)
    parser.add_argument('--execute',action='store_true')
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 20: parser.error('batch-size must be 1..20')
    cases = read(args.subjects); graph = validate_graph(read('subject-taxonomy.json'))
    identity = {'subjects_sha256':digest(cases),'taxonomy_sha256':digest(graph),'protocol':'subject-set-eval-v1'}
    root=args.output;root.mkdir(parents=True,exist_ok=True)
    if (root/'input.json').exists() and read(root/'input.json')['identity'] != identity:
        raise ValueError('Frozen subjects/graph changed; choose a new output')
    write(root/'input.json',{'identity':identity,'cases':cases})
    write(root/'taxonomy.json',graph)
    states={}
    for index, case in enumerate(cases):
        cid=f'S{index+1:03d}';path=root/'subjects'/cid/'state.json'
        states[cid]=read(path) if path.exists() else {'static':classify(graph,case['subject']), 'complete':False}
        write(path,states[cid])
    report(root,cases,graph,states)
    if not args.execute:
        print('Static baseline saved. Add --execute for two-model classification/scope review.',flush=True);return
    registry=[{'id':k,'label':n['label'],'parent':n['parent']} for k,n in graph['nodes'].items()]
    subjects={f'S{i+1:03d}':c['subject'] for i,c in enumerate(cases)}
    store=Store(root/'model-calls');models=Models(configuration(),store)
    try:
        pending=[i for i,s in states.items() if not s['complete']]
        for offset in range(0,len(pending),args.batch_size):
            ids=pending[offset:offset+args.batch_size];remaining=ids
            store.event('taxonomy-test','batch_running',{'position':offset//args.batch_size+1,'total_batches':(len(pending)+args.batch_size-1)//args.batch_size,'subjects':len(ids),'completed':sum(s['complete'] for s in states.values()),'total':len(cases)})
            for attempt in range(2):
                if not remaining: break
                name='batch-'+digest({'ids':remaining,'identity':identity,'attempt':attempt})[:12]
                packet={'known_categories':registry,'allowed_lenses':['explanation','history','biography','comparison','procedure','case_study'],
                        'subjects':[{'id':i,'subject':subjects[i]} for i in remaining]}
                if attempt: packet['repair']={i:{'candidate':states[i].get('candidate'),'review':states[i].get('review')} for i in remaining}
                try:
                    candidates=batch_call(models,'reviewer',name+'-classify',CLASSIFY,packet,remaining,
                        lambda i,r:classification_errors(r,graph)+scope_errors(r,subjects[i]),6500)
                    reviews=batch_call(models,'planner',name+'-review',REVIEW,{**packet,'candidates':list(candidates.values())},remaining,
                        lambda i,r:[] if type(r.get('pass')) is bool and isinstance(r.get('issues'),list)
                            and all(isinstance(x,str) for x in r['issues']) and r['pass']==(not r['issues']) else ['Need boolean pass and string issues consistent with it'],2500)
                except Exception as exc:
                    for i in remaining:
                        states[i].update(complete=True,accepted=False,technical_error=str(exc))
                        write(root/'subjects'/i/'state.json',states[i])
                    store.event('taxonomy-test','batch_failed',{'subjects':remaining,'error':str(exc)})
                    break
                for i in remaining:
                    states[i].pop('technical_error', None)
                    states[i].update(candidate=candidates[i],review=reviews[i],accepted=reviews[i]['pass'],attempt=attempt+1,
                                     complete=reviews[i]['pass'] or attempt==1,code_errors=[])
                    directory=root/'subjects'/i
                    write(directory/'state.json',states[i])
                    seeds=seed_subject(subjects[i],graph,classification={**candidates[i],
                        'needs_classification_review':not reviews[i]['pass'] or candidates[i]['ambiguity']!='clear'})
                    write(directory/'seeds.json',seeds)
                    write(directory/'planner-request.json',planner_request(seeds,graph))
                remaining=[i for i in remaining if not states[i]['accepted']]
                progress=report(root,cases,graph,states)
                store.event('taxonomy-test','progress',{k:progress[k] for k in ['tested','completed','reviewer_accepted','needs_review']})
            report(root,cases,graph,states)
    finally: store.db.close()


if __name__=='__main__':main()
