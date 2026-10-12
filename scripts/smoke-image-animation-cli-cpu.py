#!/usr/bin/env python3
"""Run the real image-animation CLI with explicit CPU-only synthetic fixtures.

The LLM is fake, audio is a tone with a declared word map, and PNGs are PIL
fixtures. Manim, FFmpeg, audio validation, imports, delivery, and resume are real.
HTTP and GPU leases are blocked. No production service is contacted or changed.
"""
import argparse
import contextlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from test_image_animation_cli_render import (CpuFixtureModels, RUNTIME_READY,
    RealWorkflow, run_cpu_cli_smoke)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/experiments/image-animation-cli-cpu-smoke')
    args = parser.parse_args()
    if not RUNTIME_READY:
        parser.error('CPU smoke requires .venv-manim-pilot, ffmpeg and ffprobe')
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch('infofilm.media.request', side_effect=AssertionError('HTTP is blocked in CPU smoke')))
        stack.enter_context(patch('infofilm.image_animation_assets.gpu_lease', side_effect=AssertionError('GPU is blocked in CPU smoke')))
        stack.enter_context(patch('httpx.Client', side_effect=AssertionError('LLM/network is blocked in CPU smoke')))
        stack.enter_context(patch('infofilm.image_animation_workflow.Workflow',
            side_effect=lambda parsed: RealWorkflow(parsed, models_factory=CpuFixtureModels)))
        report = run_cpu_cli_smoke(args.output)
    passed = all(report['checks'].values())
    print(json.dumps({'status': 'passed' if passed else 'failed', 'video': report['video'],
        'checks': report['checks'], 'report': str(args.output.resolve() / 'smoke-report.json')}, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
