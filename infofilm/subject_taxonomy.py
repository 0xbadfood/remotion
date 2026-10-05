"""Inspectable static subject graph and deterministic segmentation seed sketch.

This standalone prototype does not change live production requests. It supplies
questions for research and planning; it does not manufacture source-backed facts.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
from string import Template
from pathlib import Path

from .core import ROOT, Store, configuration, digest, read, write

LENSES = {'explanation', 'history', 'biography', 'comparison', 'procedure', 'case_study'}
PROMPT_FIELDS = {'subject_json', 'sentinel_json', 'classification_json', 'profile_json',
                'secondary_profiles_json', 'source_brief_json', 'advisory_seconds_json',
                'output_contract_json', 'planning_limits_json'}


def validate_prompt_shape(shape, category):
    if (not isinstance(shape, dict) or shape.get('schema_version') != 'infofilm.planner-prompt-shape.v0'
            or shape.get('sentinel') != category):
        raise ValueError(f'{category} needs its own planner prompt shape')
    for field in ['system', 'domain_instruction', 'template']:
        if not isinstance(shape.get(field), str) or not shape[field].strip():
            raise ValueError(f'{category} has an empty prompt {field}')
    template = Template(shape['template'])
    if not template.is_valid() or set(template.get_identifiers()) != PROMPT_FIELDS:
        raise ValueError(f'{category} has invalid prompt placeholders; expected {sorted(PROMPT_FIELDS)}')


def ancestry(graph, category):
    nodes = graph['nodes']
    path, seen = [], set()
    while category is not None:
        if category in seen or category not in nodes:
            raise ValueError('Subject graph contains a cycle or unknown parent')
        seen.add(category); path.append(category)
        category = nodes[category]['parent']
    return list(reversed(path))


def validate_graph(graph):
    if graph.get('schema_version') != 'infofilm.subject-taxonomy.v0':
        raise ValueError('Unknown subject taxonomy schema')
    nodes, root = graph.get('nodes'), graph.get('root')
    if not isinstance(nodes, dict) or root not in nodes or nodes[root].get('parent') is not None:
        raise ValueError('Subject graph needs a valid root')
    for key, node in nodes.items():
        if not isinstance(node, dict) or not node.get('label') or not isinstance(node.get('attributes'), dict):
            raise ValueError(f'{key} needs a label and attributes')
        path = ancestry(graph, key)
        if path[0] != root:
            raise ValueError('All categories must connect to the root')
        if not isinstance(node.get('aliases', []), list) or any(not isinstance(a, str) or not a.strip() for a in node.get('aliases', [])):
            raise ValueError(f'{key} has invalid aliases')
        validate_prompt_shape(node.get('planner_prompt_shape'), key)
        profile = expand(graph, key)
        seeds = profile['seeds']
        if not seeds or any(not isinstance(s, dict) or not s.get('id') or not s.get('question') for s in seeds):
            raise ValueError(f'{key} needs segmentation questions')
        ids = [s['id'] for s in seeds]
        if len(set(ids)) != len(ids):
            raise ValueError(f'{key} has repeated seed IDs')
        edges = profile['relations']
        if any(e.get('from') not in ids or e.get('to') not in ids or e.get('type') != 'prerequisite' for e in edges):
            raise ValueError(f'{key} has invalid seed relations')
        # Verify that research prerequisites admit an order.
        remaining, done = set(ids), set()
        while remaining:
            ready = {seed for seed in remaining if all(e['from'] in done for e in edges if e['to'] == seed)}
            if not ready:
                raise ValueError(f'{key} has cyclic prerequisites')
            done |= ready; remaining -= ready
    return graph


def expand(graph, category):
    """Attributes inherit; a specialized node may replace the parent's seed set."""
    profile = {'category': category, 'path': ancestry(graph, category), 'attributes': {}, 'seeds': [], 'relations': []}
    for key in profile['path']:
        node = graph['nodes'][key]
        profile['attributes'].update(copy.deepcopy(node['attributes']))
        if 'seeds' in node:
            profile['seeds'] = copy.deepcopy(node['seeds'])
            profile['relations'] = copy.deepcopy(node.get('relations', []))
        for optional in ['scope_rule', 'whole_subject_axes']:
            if optional in node:
                profile[optional] = copy.deepcopy(node[optional])
    # The selected node owns the handoff shape, even when questions inherit.
    profile['planner_prompt_shape'] = copy.deepcopy(graph['nodes'][category]['planner_prompt_shape'])
    return profile


def classify(graph, subject):
    """Lexical first-pass routing, with explicit uncertainty for absent/ambiguous matches."""
    text = ' '.join(subject.casefold().split())
    matches = {}
    for key, node in graph['nodes'].items():
        hits = [alias for alias in node.get('aliases', []) if re.search(r'(?<!\w)' + re.escape(alias.casefold()) + r'(?!\w)', text)]
        if hits:
            matches[key] = hits
    # A matching specialization outranks its own matching ancestor. Phrase
    # specificity then helps choose between independent domain branches.
    terminal = [key for key in matches if not any(key != other and key in ancestry(graph,other) for other in matches)]
    candidates = sorted(terminal, key=lambda key: (-max(len(h.split()) for h in matches[key]),
                        -len(ancestry(graph, key)), -sum(len(h) for h in matches[key]), key))
    # The requested explanatory lens outranks a noun: history of anatomy is
    # historical, with anatomy as a secondary facet.
    historical_lens = bool(re.search(r'\b(?:history of|historical development|evolution of .+ over time)\b', text))
    historical = next((c for c in candidates if c == 'history' or c.startswith('history.')), None)
    primary = historical if historical_lens and historical else (candidates[0] if candidates else graph['root'])
    biographical_lens = bool(re.search(r'\b(?:biography|biographical|life of|life story)\b', text))
    if biographical_lens and not historical_lens:
        primary = next((c for c in candidates if c == 'biography' or c.startswith('biography.')), 'biography')
    primary_path = ancestry(graph, primary)
    facets = []
    for candidate in candidates:
        if candidate in primary_path or any(candidate in ancestry(graph, chosen) for chosen in facets):
            continue
        facets.append(candidate)
    roots = {ancestry(graph, c)[1] for c in candidates if len(ancestry(graph, c)) > 1}
    lens = ('comparison' if re.search(r'\b(?:versus|vs|compare|comparison|difference between)\b', text) else
            'procedure' if re.search(r'\b(?:how to|step by step)\b', text) else
            'case_study' if 'case study' in text else
            'history' if historical_lens or primary.startswith('history') else
            'biography' if biographical_lens or primary.startswith('biography') else 'explanation')
    return {'primary': primary, 'secondary': facets[:3], 'lens': lens, 'scope_extent': 'unknown',
            'ambiguity': 'unknown' if not candidates else 'ambiguous' if len(roots) > 1 and not historical_lens and not biographical_lens else 'clear',
            'method': 'static_aliases_with_explicit_historical_lens', 'matches': matches,
            'needs_classification_review': not candidates or (len(roots) > 1 and not historical_lens and not biographical_lens),
            'limitations': 'Lexical routing is a first-pass hint. Unknown and interdisciplinary subjects need a constrained classifier or human review; no confidence score is fabricated.'}


def classification_errors(value, graph):
    if not isinstance(value,dict):
        return ['Classification must be an object']
    errors = []
    primary=value.get('primary')
    known=isinstance(primary,str) and primary in graph['nodes']
    if not known:
        errors.append('Choose a primary category from the supplied graph')
    secondary = value.get('secondary')
    if (not isinstance(secondary, list) or len(secondary) > 3 or any(not isinstance(c, str) or c not in graph['nodes'] for c in secondary)
            or len(set(secondary)) != len(secondary) or primary in secondary):
        errors.append('Use at most three distinct known secondary categories')
    elif known and any(c in ancestry(graph,primary) for c in secondary):
        errors.append('Secondary categories must add facets, not repeat primary ancestors')
    for field, allowed in [('lens',LENSES),('scope_extent',{'broad','focused','unknown'}),('ambiguity',{'clear','ambiguous','unknown'})]:
        if not isinstance(value.get(field),str) or value[field] not in allowed:
            errors.append(f'Invalid {field}; choose from {sorted(allowed)}')
    if not isinstance(value.get('rationale'), str) or not 1 <= len(value['rationale']) <= 2000:
        errors.append('Provide a concise classification rationale')
    if primary == graph['root'] and value.get('ambiguity') == 'clear':
        errors.append('The general fallback must retain classification uncertainty')
    return errors


def model_classify(subject, graph, models, directory, max_attempts=2):
    """Models select/review existing nodes; the segmentation framework stays in code."""
    from .llm import ModelOutputError
    directory=Path(directory)/digest({'subject':subject,'graph':digest(graph)})[:16]
    write(directory/'taxonomy.json',graph)
    write(directory/'request.json',{'subject':subject,'taxonomy_sha256':digest(graph),'max_attempts':max_attempts})
    # Keep the complete catalog compact and before variable inputs so provider
    # prefix caching can reuse it. Labels/parents suffice for semantic selection;
    # the full attributes and lexical aliases remain in the archived graph.
    registry = [{'id':key,'label':node['label'],'parent':node['parent']} for key,node in graph['nodes'].items()]
    context={'known_categories':registry,'allowed_lenses':sorted(LENSES),
             'subject':subject,'lexical_hint':classify(graph,subject)}
    task=('Classify the requested informational subject into the supplied STATIC subject graph. '
          'Choose the most specific appropriate existing category, not the category with most matching words. '
          'Use at most three secondary facets only when substantively needed; a place name alone does not make history into geography. '
          'Honor the requested lens: history of anatomy is historical with an anatomy facet; heart anatomy is a focused structure explanation. '
          'For ambiguous or unfamiliar subjects choose an appropriate broad ancestor, or general, and retain ambiguity. '
          'Do not invent taxonomy IDs, facts, chapters, segmentation questions, dates or confidence percentages. '
          'Return {primary:known_id,secondary:[known_id],lens:allowed_lens,scope_extent:"broad"|"focused"|"unknown",'
          'ambiguity:"clear"|"ambiguous"|"unknown",rationale:string}.')
    feedback={}
    for attempt in range(max_attempts):
        prompt=task+'\nINPUT:\n'+json.dumps({**context,**({'repair':feedback} if feedback else {})},ensure_ascii=False)
        if len(prompt.encode())>90000:
            raise ValueError('Classification registry exceeds its bounded request; classify hierarchically before expanding it further')
        try:
            candidate=models.call('reviewer',f'subject-category-{attempt}',prompt,max_tokens=1600)
        except ModelOutputError as exc:
            feedback={'code_errors':['Return complete classification JSON: ' + str(exc)]}
            write(directory/f'attempt-{attempt}.json',feedback)
            continue
        errors=classification_errors(candidate,graph)
        review=None
        if not errors:
            try:
                review=models.call('planner',f'subject-category-review-{attempt}',
                'Review only the classification of this informational subject against the supplied known categories. '
                'Check requested lens, scope and appropriate specificity. Flag genuinely wrong category choices. '
                'Only substantive classification mistakes belong in issues; optional rationale wording is advisory. '
                'Ambiguous subjects can pass when uncertainty is retained; do not invent categories or require factual research to classify a familiar subject. '
                'Do not plan chapters or invent factual content. Return {pass:boolean,issues:[string]}.\nINPUT:\n'+
                    json.dumps({**context,'candidate':candidate},ensure_ascii=False),max_tokens=1200)
            except ModelOutputError as exc:
                errors.append('Classification review needs complete JSON: ' + str(exc))
        write(directory/f'attempt-{attempt}.json',{'candidate':candidate,'code_errors':errors,'review':review})
        if not errors and isinstance(review,dict) and review.get('pass') is True and isinstance(review.get('issues'),list) and not review['issues']:
            return {**candidate,'method':'known_graph_two_model_selection','needs_classification_review':candidate['ambiguity']!='clear',
                    'model_classification_accepted':True,
                    'matches':context['lexical_hint']['matches'],'limitations':'Passing model review does not establish classification accuracy on unseen subjects.'}
        feedback={'candidate':candidate,'code_errors':errors,'review':review}
    fallback=classify(graph,subject)
    return {**fallback,'needs_classification_review':True,'ambiguity':'ambiguous' if fallback['primary']!=graph['root'] else 'unknown',
            'model_classification_accepted':False}


def seed_subject(subject, graph, category=None, classification=None):
    if not subject.strip():
        raise ValueError('Provide a subject')
    validate_graph(graph)
    classification = copy.deepcopy(classification or classify(graph, subject))
    if category is not None:
        if category not in graph['nodes']:
            raise ValueError('Category must be a node in subject-taxonomy.json')
        classification.update(primary=category, secondary=[], method='explicit_category',
                              ambiguity='unknown' if category == graph['root'] else 'clear',
                              needs_classification_review=category == graph['root'])
    profile = expand(graph, classification['primary'])
    frame=graph.get('lenses',{}).get(classification.get('lens','explanation'),{})
    if frame.get('seeds'):
        profile['domain_questions']=profile['seeds']
        profile['seeds']=copy.deepcopy(frame['seeds'])
        profile['relations']=copy.deepcopy(frame['relations'])
    whole_axes = profile.pop('whole_subject_axes', [])
    text = ' '.join(subject.casefold().split())
    broad_anatomy = classification['primary'] == 'science.biology.anatomy' and text in {
        'human anatomy', 'the human anatomy', 'anatomy of the human body', 'human body', 'the human body'}
    # Broad-subject scaffolding is graph-owned for any category. The anatomy
    # title aliases above retain compatibility with the lexical-only preview;
    # reviewed model scope takes precedence for every domain.
    broad_subject = classification.get('scope_extent') == 'broad' or (classification.get('scope_extent') == 'unknown' and broad_anatomy)
    return {'schema_version': 'infofilm.subject-seeds.v0', 'subject': subject.strip(),
            'taxonomy_sha256': digest(graph), 'classification': classification, 'profile': profile,
            'segmentation_axes': whole_axes if broad_subject else [],
            'policy': graph['policy'], 'production_integrated': False,
            'next_steps': ['Validate the category against source evidence',
                           'Research the relevant seed questions and retain uncertainties',
                           'Group source-backed answers into connected chapters; seeds are not a fixed chapter count']}


def planner_request(seeds, graph, source_brief=None, advisory_seconds=900, settings=None):
    """Render a selected node's shape into messages for the existing chapter contract.

    A missing brief or uncertain classification leaves an inspectable draft
    handoff, explicitly pending research/review. This function makes no LLM call.
    """
    from .external_planner import CONTRACT, brief_errors, finite_number, settings_errors

    validate_graph(graph)
    if seeds.get('taxonomy_sha256') != digest(graph):
        raise ValueError('Planner handoff must use the same frozen taxonomy as classification')
    if not finite_number(advisory_seconds) or advisory_seconds <= 0:
        raise ValueError('Advisory duration must be a positive finite number')
    settings = copy.deepcopy(settings if settings is not None else read(ROOT/'external-planner.json'))
    errors = settings_errors(settings)
    if errors:
        raise ValueError('Invalid planner settings: ' + '; '.join(errors))
    classification = seeds['classification']
    errors = classification_errors({**classification, 'rationale': classification.get('rationale', 'Code-owned category selection')}, graph)
    if errors:
        raise ValueError('Invalid classification: ' + '; '.join(errors))
    category = classification['primary']
    # Rebuild the code-owned profile rather than trusting edited seed fields.
    selected = seed_subject(seeds['subject'], graph, classification=classification)
    profile = copy.deepcopy(selected['profile'])
    shape = profile.pop('planner_prompt_shape')
    profile['segmentation_axes'] = selected['segmentation_axes']
    secondary = []
    for facet in classification.get('secondary', []):
        item = expand(graph, facet)
        item.pop('planner_prompt_shape')
        item.pop('whole_subject_axes', None)
        secondary.append(item)
    pending = []
    if classification.get('needs_classification_review', True) or classification.get('ambiguity') != 'clear':
        pending.append('classification_review')
    if source_brief is None:
        pending.append('source_research')
    else:
        if not isinstance(source_brief, dict):
            raise ValueError('Source brief must be an object')
        errors = brief_errors(source_brief, settings)
        if errors:
            raise ValueError('Invalid source brief: ' + '; '.join(errors))
    contract = copy.deepcopy(CONTRACT)
    contract['film'].update(subject=selected['subject'], advisory_seconds=advisory_seconds)
    # Preserve the production schema while removing historical example values.
    contract['chapters'][0]['scope'].update(start_year=None, end_year=None)
    contract['chapters'][0]['section_card']['date_label'] = ''
    values = {
        'subject_json': selected['subject'], 'sentinel_json': category,
        'classification_json': classification, 'profile_json': profile,
        'secondary_profiles_json': secondary, 'source_brief_json': source_brief,
        'advisory_seconds_json': advisory_seconds, 'output_contract_json': contract,
        'planning_limits_json': {key: settings[key] for key in ['max_chapters', 'max_topics', 'recommended_chapter_seconds']},
    }
    user = shape['domain_instruction'] + '\n\n' + Template(shape['template']).substitute(
        {key: json.dumps(value, ensure_ascii=False) for key, value in values.items()})
    messages = [{'role': 'system', 'content': shape['system']}, {'role': 'user', 'content': user}]
    if len(json.dumps(messages, ensure_ascii=False).encode()) > settings['max_request_bytes']:
        raise ValueError('Planner handoff exceeds its request byte bound; condense the source brief')
    return {'schema_version': 'infofilm.taxonomy-planner-request.v0', 'subject': selected['subject'],
            'sentinel': category, 'taxonomy_sha256': digest(graph), 'prompt_shape_sha256': digest(shape),
            'ready_for_planning': not pending, 'pending': pending,
            'readiness_note': 'Readiness checks shape, references and classification status; factual grounding still needs independent review.',
            'output_schema': contract['schema_version'], 'messages': messages, 'production_integrated': False}


def main():
    parser = argparse.ArgumentParser(description='Preview static subject classification and segmentation questions, optionally using local models for classification; no media generation.')
    operation=parser.add_mutually_exclusive_group(required=True)
    operation.add_argument('--subject')
    operation.add_argument('--evaluate',type=Path,help='Independent labeled routing corpus')
    parser.add_argument('--taxonomy', type=Path, default=ROOT/'subject-taxonomy.json')
    parser.add_argument('--category', help='Optional known category override')
    parser.add_argument('--model-assisted',action='store_true',help='Classify and review against existing nodes using the two local models')
    parser.add_argument('--config',type=Path,default=ROOT/'config.json')
    parser.add_argument('--brief', type=Path, help='Source-backed orientation brief for a planner handoff')
    parser.add_argument('--advisory-seconds', type=float, default=900, help='Advisory film length for the handoff; narrative flow takes priority')
    parser.add_argument('--planner-prompt-output', type=Path, help='Save rendered planner messages for one selected subject')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.category and args.model_assisted:
        parser.error('Choose an explicit category or model-assisted routing')
    if args.category and args.evaluate:
        parser.error('Category overrides apply only to one subject')
    if args.evaluate and (args.brief or args.planner_prompt_output):
        parser.error('Planner handoffs apply only to one subject')
    if args.brief and not args.planner_prompt_output:
        parser.error('--brief requires --planner-prompt-output')
    graph=validate_graph(read(args.taxonomy))
    store=None
    models=None
    if args.model_assisted:
        from .llm import Models
        store=Store(args.output.parent/(args.output.stem+'-classification'))
        models=Models(configuration(args.config),store)
    try:
        if args.evaluate:
            cases=[]
            for case in read(args.evaluate):
                # Expected labels are never provided to either model.
                classification=model_classify(case['subject'],graph,models,store.root) if models else None
                result=seed_subject(case['subject'],graph,classification=classification)
                predicted=result['classification']['primary']
                matched=any(expected in ancestry(graph,predicted) for expected in case['expected_branches'])
                cases.append({'subject':case['subject'],'expected_branches':case['expected_branches'],
                              'matched_expected':matched,'result':result})
            result={'taxonomy_sha256':digest(graph),'tested':len(cases),'matched_expected':sum(c['matched_expected'] for c in cases),
                    'needs_review':sum(c['result']['classification']['needs_classification_review'] for c in cases),
                    'model_assisted':bool(models),'cases':cases,
                    'limitations':'This labeled subject set measures routing, not factual correctness, narrative quality or coverage of unseen topics.'}
        else:
            classification=model_classify(args.subject,graph,models,store.root) if models else None
            result=seed_subject(args.subject,graph,args.category,classification)
            if args.planner_prompt_output:
                handoff=planner_request(result, graph, read(args.brief) if args.brief else None, args.advisory_seconds)
                write(args.planner_prompt_output, handoff)
    finally:
        if store:
            store.db.close()
    write(args.output, result)
    if args.evaluate:
        print(f"{result['matched_expected']}/{result['tested']} matched expected branches; {result['needs_review']} need review → {args.output}")
    else:
        print(f"{result['classification']['primary']}: {len(result['profile']['seeds'])} seed questions → {args.output}")
        if args.planner_prompt_output:
            print(f"Planner handoff: {'ready' if handoff['ready_for_planning'] else ', '.join(handoff['pending'])} → {args.planner_prompt_output}")


if __name__ == '__main__':
    main()
