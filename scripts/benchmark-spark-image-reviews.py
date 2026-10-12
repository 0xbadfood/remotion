#!/usr/bin/env python3
"""Replay ordinary saved production image reviews; no image generation."""
import argparse
from pathlib import Path
import statistics
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from infofilm.core import Store, configuration, read, write
from infofilm.llm import Models


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://10.8.0.4:8001/v1')
    parser.add_argument('--output', type=Path, default=ROOT/'artifacts/experiments/service-switch/spark-normal-image-reviews')
    args = parser.parse_args()
    store = Store(args.output)
    config = configuration(); config['reviewer_url'] = args.url.rstrip('/')
    models = Models(config, store)
    summary = {'complete': False, 'url': args.url, 'rows': [], 'warmup_excluded': True,
               'workload': 'Unmodified saved production prompts and images; no generated assets',
               'limits': 'One measured call per case. Server prefix/image caches are allowed, as in production. '
                         'Output lengths and model runtime differ from the original reviews. Agreement is not ground truth.'}
    try:
        base = args.url.rstrip('/').removesuffix('/v1')
        slots = httpx.get(base+'/slots', timeout=10); slots.raise_for_status()
        if any(row.get('is_processing') for row in slots.json()):
            raise RuntimeError('Spark is busy; defer timing rather than include queue wait')
        def source(subject, asset):
            project = next((ROOT/'artifacts/runs').glob(subject+'*'))
            paths = sorted((project/'requests').glob(f'message-review-{asset}-*-0-json-0-*.request.json'))
            return next(p for p in paths if p.with_name(p.name.replace('.request.json','.result.json')).exists())
        paths = [source(subject, asset) for subject, asset in [
            ('pan-am-flight', 'A002'), ('pan-am-flight', 'A007'),
            ('partition-of-india', 'A001'), ('partition-of-india', 'A003'),
            ('why-hrms', 'A001'), ('why-hrms', 'A005')]]
        project = next((ROOT/'artifacts/runs').glob('partition-of-india*'))
        seen = set()
        for p in sorted((project/'requests').glob('message-review-*-0-json-0-*.request.json')):
            x = read(p); asset = p.name.split('-')[2]
            if x.get('reference_images') and asset not in seen and p.with_name(p.name.replace('.request.json','.result.json')).exists():
                seen.add(asset); paths.append(p)
        write(store.root/'cases.json', {'source_requests':[str(p) for p in paths]})
        def replay(path, name):
            x = read(path)
            value = models.call('reviewer', name, x['prompt'], image_path=Path(x['image']),
                                reference_image_paths=[Path(p) for p in x.get('reference_images',[])],
                                max_tokens=x['settings']['max_tokens'])
            raw = read(next((store.root/'requests').glob(name+'-*.response.json')))
            return x, value, raw
        print('Warm-up review (excluded)', flush=True)
        _, _, warm = replay(source('pan-am-flight','A001'), 'warmup')
        summary['warmup_seconds'] = warm['measured_latency_seconds']
        write(store.root/'summary.json', summary)
        for index, path in enumerate(paths):
            print(f'Normal review {index+1}/{len(paths)}: {path.parent.parent.name} {path.name.split("-")[2]}', flush=True)
            row = {'source_request':str(path)}
            started = time.monotonic()
            try:
                req, value, raw = replay(path, f'normal-{index:02d}')
                prior = read(path.with_name(path.name.replace('.request.json','.result.json')))
                old_response = path.with_name(path.name.replace('.request.json','.response.json'))
                old = read(old_response) if old_response.exists() else {}
                row.update(status='complete', seconds=raw['measured_latency_seconds'], usage=raw.get('usage'),
                           image_count=1+len(req.get('reference_images',[])), current_review=value,
                           original_pass=prior.get('pass'), original_seconds=old.get('measured_latency_seconds'),
                           original_model=req['model'], original_url=req['url'], original_usage=old.get('usage'),
                           same_verdict=value.get('pass') == prior.get('pass'))
            except Exception as exc:
                row.update(status='error', seconds=round(time.monotonic()-started,3), error=f'{type(exc).__name__}: {exc}')
            summary['rows'].append(row); write(store.root/'summary.json', summary)
        def stats(rows):
            times=[r['seconds'] for r in rows]
            return {'count':len(times),'mean_seconds':round(statistics.mean(times),3),
                    'median_seconds':round(statistics.median(times),3),'min_seconds':min(times),'max_seconds':max(times)} if times else {'count':0}
        valid = [r for r in summary['rows'] if r['status']=='complete']
        summary.update(complete=True, model=models.identities['reviewer'], measured_cases=len(valid),
                       errors=len(summary['rows'])-len(valid), agreement=sum(r['same_verdict'] for r in valid),
                       overall=stats(valid), single_image=stats([r for r in valid if r['image_count']==1]),
                       reference_card=stats([r for r in valid if r['image_count']>1]))
        print(str({k:summary[k] for k in ['overall','single_image','reference_card','agreement','errors']}),flush=True)
    except Exception as exc:
        summary['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        write(store.root/'summary.json', summary)
        store.db.close()


if __name__ == '__main__':
    main()
