#!/usr/bin/env python3
"""Re-screen saved anatomy candidates; no image generation or GPU switch."""
import argparse
import shutil
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infofilm.anatomy_image_experiment import write_report
from infofilm.anatomy_strict_review import POLICY, review_candidate
from infofilm.core import ROOT, Store, configuration, read, write, digest
from infofilm.llm import Models
from infofilm.person_references import validate_reference


def copy_reference(reference, source, output):
    reference = dict(reference)
    original = validate_reference(reference)
    relative = original.resolve().relative_to(source)
    destination = output / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(original, destination)
    reference['file'] = str(destination)
    return reference


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT/'artifacts/experiments/anatomy-digestive-v1')
    parser.add_argument('--output', type=Path, default=ROOT/'artifacts/experiments/anatomy-digestive-review-v2')
    parser.add_argument('--reviewer-url', help='Override the reviewer for this experiment only; accepts host URL or /v1 URL')
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    if output == source or source in output.parents or output in source.parents:
        parser.error('Source and output must be separate directories')
    config = configuration()
    if args.reviewer_url:
        url = args.reviewer_url.rstrip('/')
        parts = urlsplit(url)
        if parts.scheme not in {'http','https'} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            parser.error('Use a plain HTTP(S) model endpoint without credentials')
        config['reviewer_url'] = url if url.endswith('/v1') else url + '/v1'
    store = Store(args.output)
    try:
        definition = read(source/'experiment.json')
        reference = copy_reference(definition['reference'], source, store.root)
        case_references = {key:copy_reference(value,source,store.root)
                           for key,value in definition.get('case_references',{}).items()}
        definition.update(reference=reference, case_references=case_references, review_policy=POLICY,
                          baseline_reviewer_url=definition.get('reviewer_url'), reviewer_url=config['reviewer_url'],
                          generation_mode='saved candidates; no new images')
        checkpoint = store.root/'experiment.json'
        if checkpoint.exists() and digest(read(checkpoint)) != digest(definition):
            raise ValueError('Saved review experiment inputs changed; choose a new output directory')
        write(store.root/'experiment.json', definition)
        files = ['report.html', 'summary.json', 'comparison.json', reference['file']]
        files = [Path(p).relative_to(store.root).as_posix() if Path(p).is_absolute() else p for p in files]
        files += [Path(ref['file']).relative_to(store.root).as_posix() for ref in case_references.values()]
        for provider in definition['providers']:
            for case in definition['cases']:
                old = source/'results'/provider/case['id']
                target = store.root/'results'/provider/case['id']; target.mkdir(parents=True, exist_ok=True)
                candidate = target/'candidate-0.png'
                shutil.copyfile(old/'candidate-0.png', candidate)
                files.append(candidate.relative_to(store.root).as_posix())
        write(store.root/'public-report.json', {'schema_version':'infofilm.public-experiment-report.v1','files':files})
        models = Models(config, store)
        planned = len(definition['cases'])*len(definition['providers'])
        write_report(store,definition)
        completed = 0
        for case in definition['cases']:
            for provider in definition['providers']:
                old = source/'results'/provider/case['id']
                target = store.root/'results'/provider/case['id']
                candidate = target/'candidate-0.png'
                original = read(old/'result.json')
                request_path = old/'request.json'
                prompt = read(request_path)['params']['prompt'] if request_path.exists() else original['prompt']
                if digest(candidate.read_bytes()) != original['sha256']:
                    raise ValueError('Saved candidate checksum changed')
                scene_reference = case_references.get(case['id'],reference)
                store.event('strict-calibration','reviewing', {'case':case['id'],'provider':provider,'completed':completed,'planned':planned})
                try:
                    screened = review_candidate(models,target,case,candidate,prompt,scene_reference)
                    timing = old/'generation-timing.json'
                    result = {'case_id':case['id'],'provider':provider,'status':'complete','review':screened['review'],
                              'review_seconds':screened['seconds'],
                              'generation_seconds':read(timing)['seconds'] if timing.exists() else original['generation_seconds']}
                except Exception as exc:
                    result = {'case_id':case['id'],'provider':provider,'status':'error','error':str(exc)}
                result.update(file=str(candidate),sha256=digest(candidate.read_bytes()),prompt=prompt,
                              baseline_review=original.get('review'))
                write(target/'result.json',result)
                summary=write_report(store,definition);completed+=1
                store.event('strict-calibration','progress', {'case':case['id'],'provider':provider,'completed':completed,
                            'planned':planned,'verdict':result.get('review',{}).get('verdict'),'errors':summary['errors']})
                comparisons = [{'case_id':r['case_id'],'provider':r['provider'],
                                'previous':r.get('baseline_review',{}).get('verdict'),
                                'current':r.get('review',{}).get('verdict'),'error':r.get('error')}
                               for r in summary['results'] if r['status'] != 'pending']
                write(store.root/'comparison.json', {'reviewer_url':config['reviewer_url'],
                      'model':models.identities.get('reviewer'), 'completed':summary['completed'], 'errors':summary['errors'],
                      'verdicts':dict(Counter(r['current'] for r in comparisons)),
                      'disagreements':[r for r in comparisons if r['previous'] != r['current']], 'results':comparisons})
        store.event('strict-calibration','finished', {'completed':summary['completed'],'planned':planned,
                                                    'errors':summary['errors'],'report':str(store.root/'report.html')})
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
