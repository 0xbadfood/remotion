"""Opt-in, frozen subject taxonomy guidance for parent planning."""
from __future__ import annotations

import copy

from .core import ROOT, digest, read, write
from .subject_taxonomy import model_classify, planner_request, seed_subject, validate_graph


def freeze_graph(config, directory=None):
    """Queue snapshots win; CLI resumes reuse their archived graph."""
    path = directory / 'taxonomy.json' if directory is not None else None
    graph = config.get('subject_graph_snapshot')
    if graph is None:
        graph = read(path if path is not None and path.exists() else ROOT / 'subject-taxonomy.json')
    validate_graph(graph)
    if path is not None:
        if path.exists() and digest(read(path)) != digest(graph):
            raise ValueError('This project has a different frozen subject graph; choose a new project')
        write(path, graph)
    return {**config, 'subject_graph_snapshot': copy.deepcopy(graph)}


def classify_subject(config, store, models, subject):
    graph = config['subject_graph_snapshot']
    directory = store.root / 'subject-graph'
    signature = digest({'subject': subject, 'graph': digest(graph),
                        'planner': config['planner_url'], 'reviewer': config['reviewer_url'],
                        'policy': 'reviewed-subject-guidance-v1'})
    path = directory / 'selection.json'
    reused = path.exists()
    if reused:
        saved = read(path)
        if saved['signature'] != signature:
            raise ValueError('Subject classification inputs changed; choose a new project')
        seeds = saved['seeds']
    else:
        classification = model_classify(subject, graph, models, directory / 'classification')
        seeds = seed_subject(subject, graph, classification=classification)
        seeds['production_integrated'] = True
        write(path, {'signature': signature, 'seeds': seeds})
    classification = seeds['classification']
    usable = (classification.get('model_classification_accepted') is True
              and classification.get('ambiguity') == 'clear'
              and not classification.get('needs_classification_review', True))
    # An unresolved choice stays archived; generic planning remains autonomous.
    guidance = None
    if usable:
        profile = copy.deepcopy(seeds['profile'])
        profile.pop('planner_prompt_shape', None)
        guidance = {'classification': classification, 'profile': profile,
                    'segmentation_axes': seeds['segmentation_axes'], 'policy': seeds['policy']}
    store.event('pipeline:classification', 'succeeded', {
        'category': classification['primary'], 'lens': classification['lens'],
        'ambiguity': classification['ambiguity'], 'graph_applied': usable,
        'fallback': None if usable else 'Generic subject-driven planning',
        'taxonomy_sha256': seeds['taxonomy_sha256'], 'reused': reused})
    return seeds, guidance


def make_handoff(config, store, seeds, guidance, brief, duration, settings):
    if guidance is None:
        return None
    result = planner_request(seeds, config['subject_graph_snapshot'], brief, duration, settings)
    if not result['ready_for_planning']:
        raise ValueError('Reviewed subject graph handoff is not ready for planning')
    result.update(production_integrated=True, subject_guidance=guidance)
    write(store.root / 'subject-graph' / 'planner-request.json', result)
    return result
