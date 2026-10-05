"""Remove explicitly selected narration points while preserving all other PCM/MFA."""
from __future__ import annotations

import copy

from .core import digest
from .planning import script_text


def plan_point_removal(script, timing, point_ids):
    removed = set(point_ids)
    points = script['points']
    ids = [p['id'] for p in points]
    if not removed or removed - set(ids) or removed == set(ids):
        raise ValueError('Select existing points and retain at least one narration point')
    if timing['text_sha256'] != digest(script_text(script)):
        raise ValueError('The script no longer matches its MFA checkpoint')
    words = timing['words']
    if {w['point_id'] for w in words} != set(ids):
        raise ValueError('Every narration point needs its aligned words')
    aligned = {pid: [w for w in words if w['point_id'] == pid] for pid in ids}
    rate = timing['audio_info']['sample_rate']
    count = timing['audio_info']['sample_count']
    cuts, start = [], None
    for index in range(len(ids) + 1):
        dropping = index < len(ids) and ids[index] in removed
        if dropping and start is None:
            start = index
        if not dropping and start is not None:
            first = min(w['start'] for w in aligned[ids[start]])
            last = max(w['end'] for w in aligned[ids[index-1]])
            previous_end = max(w['end'] for w in aligned[ids[start-1]]) if start else 0
            next_start = min(w['start'] for w in aligned[ids[index]]) if index < len(ids) else count / rate
            if previous_end > first or last > next_start:
                raise ValueError('Point boundaries overlap; targeted realignment is required')
            a = round((previous_end + first) / 2 * rate) if start else 0
            b = round((last + next_start) / 2 * rate) if index < len(ids) else count
            cuts.append([a, b]); start = None
    for word in words:
        overlaps = [word['start'] < b / rate and word['end'] > a / rate for a, b in cuts]
        if word['point_id'] not in removed and any(overlaps):
            raise ValueError('Removal overlaps a retained aligned word')
        if word['point_id'] in removed and not any(a / rate <= word['start'] and word['end'] <= b / rate for a, b in cuts):
            raise ValueError('Removal would retain part of a deleted point')
    revised = copy.deepcopy(script)
    revised['points'] = [p for p in revised['points'] if p['id'] not in removed]
    revised_timing = copy.deepcopy(timing)
    revised_timing['words'] = [w for w in words if w['point_id'] not in removed]
    revised_timing['points'] = [p for p in revised_timing['points'] if p['point_id'] not in removed]
    revised_timing['text_sha256'] = digest(script_text(revised))
    deleted = sum(b-a for a,b in cuts)
    report = {'policy': 'explicit-point-removal-v1', 'removed_point_ids': [pid for pid in ids if pid in removed],
              'retained_point_ids': [pid for pid in ids if pid not in removed],
              'source_sha256': timing['audio_sha256'], 'source_sample_count': count, 'sample_rate': rate,
              'cuts_samples': cuts, 'removed_samples': deleted, 'removed_seconds': deleted / rate}
    return revised, revised_timing, report
