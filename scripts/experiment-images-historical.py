#!/usr/bin/env python3
"""Read-only historical reference for the frozen hard-case corpus; no API calls."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infofilm.core import ROOT, read, write
from infofilm.image_experiment import validate_corpus

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=ROOT / 'pilots/image-ladder/cases.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/image-ladder-experiment/historical-reference.json')
    args = parser.parse_args()
    cases = validate_corpus(read(args.corpus))
    rows = []
    for case in cases:
        times = []
        for take in case['historical_history']:
            candidate = Path(take['candidate'])
            result_path = candidate.with_name(candidate.name.replace('candidate-', 'provider-result-').replace('.png', '.json'))
            if result_path.exists():
                result = read(result_path)
                elapsed = result.get('generation_seconds')
                if elapsed is None and result.get('started_at') is not None and result.get('finished_at') is not None:
                    elapsed = result['finished_at'] - result['started_at']
                if type(elapsed) in {int, float} and elapsed >= 0:
                    times.append(elapsed)
        rows.append({'case_id': case['case_id'], 'asset_id': case['asset_id'], 'historical_status': case['historical_status'],
                     'reviewed_takes': case['historical_attempts'], 'takes_with_generation_timing': len(times),
                     'recorded_generation_seconds': round(sum(times), 3)})
    value = {'kind': 'historical_reference_not_a_matched_baseline', 'cases': rows,
             'summary': {'cases': len(rows), 'historically_recovered': sum(r['historical_status'] == 'recovered' for r in rows),
                         'historically_unresolved': sum(r['historical_status'] == 'unresolved' for r in rows),
                         'reviewed_takes': sum(r['reviewed_takes'] for r in rows),
                         'takes_with_generation_timing': sum(r['takes_with_generation_timing'] for r in rows),
                         'recorded_generation_seconds': round(sum(r['recorded_generation_seconds'] for r in rows), 3)},
             'limits': 'Timing is partial server/request metadata, not total pipeline time. Earlier prompts, review rules and repairs differ. Selected hard cases are not a representative success-rate sample.'}
    write(args.output, value)
    print(value['summary'])

if __name__ == '__main__':
    main()
