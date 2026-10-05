#!/usr/bin/env python3
"""Re-screen saved anatomy candidates; no image generation or GPU switch."""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infofilm.anatomy_image_experiment import write_report
from infofilm.anatomy_strict_review import POLICY, review_candidate
from infofilm.core import ROOT, Store, configuration, read, write, digest
from infofilm.llm import Models


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT/'artifacts/experiments/anatomy-digestive-v1')
    parser.add_argument('--output', type=Path, default=ROOT/'artifacts/experiments/anatomy-digestive-review-v2')
    args = parser.parse_args()
    source = args.source.resolve()
    store = Store(args.output)
    try:
        definition = read(source/'experiment.json')
        reference = dict(definition['reference'])
        ref = store.root/'reference/reference.png'; ref.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(reference['file'], ref)
        reference['file'] = str(ref)
        definition.update(reference=reference, review_policy=POLICY, generation_mode='saved candidates; no new images')
        write(store.root/'experiment.json', definition)
        files = ['report.html', 'summary.json', 'reference/reference.png']
        for provider in definition['providers']:
            for case in definition['cases']:
                old = source/'results'/provider/case['id']
                target = store.root/'results'/provider/case['id']; target.mkdir(parents=True, exist_ok=True)
                candidate = target/'candidate-0.png'
                shutil.copyfile(old/'candidate-0.png', candidate)
                files.append(candidate.relative_to(store.root).as_posix())
        write(store.root/'public-report.json', {'schema_version':'infofilm.public-experiment-report.v1','files':files})
        models = Models(configuration(), store)
        completed = 0
        for case in definition['cases']:
            for provider in definition['providers']:
                old = source/'results'/provider/case['id']
                target = store.root/'results'/provider/case['id']
                candidate = target/'candidate-0.png'
                request = read(old/'request.json')
                store.event('strict-calibration','reviewing', {'case':case['id'],'provider':provider,'completed':completed,'planned':12})
                try:
                    screened = review_candidate(models,target,case,candidate,request['params']['prompt'],reference)
                    result = {'case_id':case['id'],'provider':provider,'status':'complete','review':screened['review'],
                              'review_seconds':screened['seconds'],'generation_seconds':read(old/'generation-timing.json')['seconds']}
                except Exception as exc:
                    result = {'case_id':case['id'],'provider':provider,'status':'error','error':str(exc)}
                result.update(file=str(candidate),sha256=digest(candidate.read_bytes()),prompt=request['params']['prompt'],
                              baseline_review=read(old/'result.json').get('review'))
                write(target/'result.json',result)
                summary=write_report(store,definition);completed+=1
                store.event('strict-calibration','progress', {'case':case['id'],'provider':provider,'completed':completed,
                            'planned':12,'verdict':result.get('review',{}).get('verdict'),'errors':summary['errors']})
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
