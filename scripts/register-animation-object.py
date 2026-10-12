#!/usr/bin/env python3
"""Register a reviewed SVG requested by a paused animation experiment."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from infofilm.animation_objects import DEFAULT_LIBRARY, register_object


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", required=True, type=Path, help="object-requests.json from the paused run")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--asset-id", help="Requested identity, or an explicit unused version for a missing_capabilities fault; the latter requires --bindings when resuming")
    parser.add_argument("--svg", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path, help="JSON containing pivot, capabilities and provenance")
    parser.add_argument("--review", required=True, type=Path, help="JSON approval bound to the exact SVG SHA256")
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    args = parser.parse_args()
    try:
        result = register_object(json.loads(args.requests.read_text()), args.request_id, args.svg,
            json.loads(args.metadata.read_text()), json.loads(args.review.read_text()),
            library=args.library, asset_id=args.asset_id)
    except (ValueError, OSError) as exc:
        print(json.dumps({"state": "registration_rejected", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
