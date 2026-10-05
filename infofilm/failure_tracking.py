"""Read-only generation tracking; reports are separate from pipeline checkpoints."""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

from .core import read, write


def image_snapshot(project):
    project = Path(project)
    plan_path = project / 'scene-image-prompts.json'
    manifest_path = project / 'image_manifest.json'
    plan = read(plan_path) if plan_path.exists() else {'assets': []}
    manifest = read(manifest_path) if manifest_path.exists() else {}
    report_path = project / 'image-recovery-report.json'
    report = read(report_path) if report_path.exists() else {}
    roots = {row['asset']: Path(row['checkpoint']) for row in report.get('unresolved', []) if row.get('checkpoint')}
    for asset_id, selected in manifest.items():
        if selected.get('file'):
            roots[asset_id] = Path(selected['file']).parent.parent.parent
    # Locate running candidates by the immutable message and original depiction.
    # Archived revisions are outside this images/ directory and never enter the rollup.
    states = []
    for path in (project / 'images').glob('*/message-state.json'):
        state = read(path)
        states.append((path, state))
    latest_events = {}
    db_path = project / 'jobs.sqlite3'
    if db_path.exists():
        with sqlite3.connect(f'file:{db_path.resolve()}?mode=ro', uri=True, timeout=2) as db:
            for stage, event_state in db.execute("SELECT stage,state FROM events WHERE stage LIKE 'A%' ORDER BY id"):
                latest_events[stage.split()[0]] = event_state
    assets = []
    known_failures = {row['asset']: row for row in report.get('unresolved', [])}
    for asset in plan.get('assets', []):
        asset_id = asset['id']
        state_path = roots.get(asset_id, Path('/nonexistent')) / 'message-state.json'
        state = read(state_path) if state_path.exists() else None
        if state is None:
            matches = [(path, item) for path, item in states if item.get('visual_message') == asset.get('visual_message')]
            if len(matches) > 1:
                matches = [(path,item) for path,item in matches if item.get('takes') and item['takes'][0]['scene'] == asset.get('prompt')]
            if len(matches) == 1:
                state_path, state = matches[0]
        selected = manifest.get(asset_id)
        takes = state.get('takes', []) if state else []
        latest = takes[-1] if takes else {}
        review = latest.get('review', {})
        status = ('needs_expert_review' if selected.get('needs_review') else 'accepted') if selected else state.get('status', 'pending') if state else 'pending'
        blocked = known_failures.get(asset_id)
        if not selected and status == 'running' and state.get('pending_repair'):
            if latest_events.get(asset_id) == 'repair_blocked':
                status = 'repair_blocked'
            elif blocked:
                status = blocked['status']
        assets.append({'asset': asset_id, 'status': status,
            'visual_message': asset.get('visual_message'), 'reviewed_candidates': len(takes),
            'selected_attempts': selected.get('total_attempts') if selected else None,
            'repair_modes': [r.get('repair_mode') for r in state.get('repairs', [])] if state else [],
            'reason_keys': review.get('reason_keys', []), 'issues': review.get('issues', []),
            'latest_requested_repair': review.get('repair_mode'), 'latest_correction': review.get('correction'),
            'latest_scene': latest.get('scene'), 'latest_candidate': latest.get('candidate'),
            'error': blocked.get('error') if blocked and not selected else None,
            'state_path': str(state_path) if state else None})
    accepted = len(manifest)
    unresolved = [a for a in assets if a['status'] in {'exhausted', 'repair_blocked', 'backend_error'}]
    repaired = [m for m in manifest.values() if m.get('total_attempts', 1) > 1]
    attempt_counts = [m['total_attempts'] for m in manifest.values() if type(m.get('total_attempts')) is int]
    return {'planned_images': len(plan.get('assets', [])), 'accepted_images': accepted,
            'needs_expert_review_images': sum(bool(v.get('needs_review')) for v in manifest.values()),
            'remaining_images': max(0, len(plan.get('assets', [])) - accepted),
            'unresolved_images': len(unresolved), 'reviewed_candidates': sum(a['reviewed_candidates'] for a in assets),
            'accepted_after_repair': len(repaired),
            'mean_attempts_for_accepted': round(sum(attempt_counts)/len(attempt_counts), 3) if attempt_counts else None,
            'assets': assets, 'unresolved': unresolved}


def snapshot(project):
    project = Path(project)
    status_path = project / 'longform-status.json'
    rows = read(status_path)['chapters'] if status_path.exists() else [
        {'chapter': {'id': 'film', 'title': project.name}, 'project': str(project), 'status': 'unknown'}]
    chapters = []
    for row in rows:
        chapters.append({'chapter': row['chapter']['id'], 'title': row['chapter'].get('title'),
                         'status': row.get('status'), 'error': row.get('error'),
                         'images': image_snapshot(row['project'])})
    return {'schema_version': 'infofilm.failure-tracking.v1', 'project': str(project.resolve()),
            'updated': time.time(), 'chapters': chapters}


def save_report(output, value):
    output = Path(output)
    write(output, value)
    lines = ['# Generation failure tracking', '',
             'This is a read-only snapshot. Selected images may include explicitly tagged best-available expert-review fallbacks.', '',
             '| Chapter | State | Planned | Selected | Expert review | Remaining | Unresolved |',
             '|---|---|---:|---:|---:|---:|---:|']
    for c in value['chapters']:
        s = c['images']
        lines.append(f"| {c['chapter']} | {c['status']} | {s['planned_images']} | {s['accepted_images']} | {s['needs_expert_review_images']} | {s['remaining_images']} | {s['unresolved_images']} |")
    for c in value['chapters']:
        failed = c['images']['unresolved']
        if not failed:
            continue
        lines.extend(['', '## ' + c['chapter'], ''])
        for a in failed:
            lines.append(f"- **{a['asset']}**: {a['status']}; {a['reviewed_candidates']} reviewed candidates; reasons: {', '.join(a['reason_keys'])}.")
            lines.append('  Message: ' + str(a['visual_message']))
            for issue in a['issues']:
                lines.append('  Issue: ' + issue)
            if a['error']:
                lines.append('  Repair error: ' + a['error'])
            lines.append('  Checkpoint: ' + str(a['state_path']))
    output.with_suffix('.md').write_text('\n'.join(lines)+'\n')


def queue_status(queue_db, project):
    if not queue_db:
        return None
    with sqlite3.connect(f'file:{Path(queue_db).resolve()}?mode=ro', uri=True, timeout=2) as db:
        row = db.execute('SELECT status FROM jobs WHERE project=?', (str(Path(project).resolve()),)).fetchone()
    return row[0] if row else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--interval', type=int, default=15)
    parser.add_argument('--queue-db', type=Path)
    args = parser.parse_args()
    if not 5 <= args.interval <= 60:
        parser.error('interval must be between 5 and 60 seconds')
    output = args.output or args.project / 'failure-tracking' / 'latest.json'
    last = None
    while True:
        try:
            value = snapshot(args.project)
            value['job_status'] = queue_status(args.queue_db, args.project)
            save_report(output, value)
            summary = [{'chapter':c['chapter'], 'state':c['status'],
                        'accepted':c['images']['accepted_images'], 'planned':c['images']['planned_images'],
                        'needs_expert_review':c['images']['needs_expert_review_images'],
                        'unresolved':[a['asset'] for a in c['images']['unresolved']],
                        'reviewed_candidates':c['images']['reviewed_candidates'],
                        'retrying':[a['asset'] for a in c['images']['assets']
                                    if a['status']=='running' and a['reason_keys']]} for c in value['chapters']]
            signature = json.dumps({'job_status':value['job_status'], 'chapters':summary}, sort_keys=True)
            if signature != last:
                print('[failure-tracker] ' + signature, flush=True)
                last = signature
            if not args.watch or value['job_status'] in {'failed', 'interrupted', 'succeeded'}:
                break
        except (OSError, ValueError, sqlite3.Error) as exc:
            print('[failure-tracker] checkpoint_read_retry: ' + str(exc), flush=True)
            if not args.watch:
                raise
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
