from __future__ import annotations

import argparse
import re
import uuid
from datetime import datetime
from pathlib import Path

from .core import ROOT, Store, configuration
from .pipeline import create, research
from .creative import MOODS, INTENSITIES, BALANCES, MUSIC


def project_path(subject):
    slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:55] or "film"
    return ROOT / "artifacts" / f"{slug}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def main():
    parser = argparse.ArgumentParser(description="Create informational films; runtime guides scope and natural narration determines the final length.")
    parser.add_argument("command", choices=["create", "research", "evaluate", "serve"])
    parser.add_argument("--subject")
    parser.add_argument("--durations", "--duration-seconds", type=int, nargs="+", default=[60])
    parser.add_argument("--generation-mode", choices=["short", "long"], default="short")
    parser.add_argument("--subject-graph", action=argparse.BooleanOptionalAction, default=False,
                        help="Use reviewed subject taxonomy guidance for long-film planning (default: off)")
    parser.add_argument("--chapter-plan", type=Path, help="Reuse approved parent chapter JSON for a long film")
    parser.add_argument("--orientation-brief", type=Path, help="Prepared sourced parent orientation JSON for a long film")
    parser.add_argument("--project", type=Path, help="Project folder; new subjects get a unique folder when omitted")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--image-provider-policy", help="Policy ID from image-providers.json, for example standard_only")
    parser.add_argument("--image-review", action=argparse.BooleanOptionalAction, default=None,
                        help="Review and repair generated images (default: on); --no-image-review renders the planned images once")
    parser.add_argument("--sources", type=Path, help="Optional extracted source JSON with results[{url,raw_content}]")
    parser.add_argument("--reference-audio", type=Path)
    parser.add_argument("--narrator", help="Configured narrator ID, for example default or kane")
    parser.add_argument("--reference-transcript", help="Exact reference recording transcript required for OmniVoice")
    parser.add_argument("--tts-engine", choices=["vibevoice", "omnivoice"], default="vibevoice")
    parser.add_argument("--omni-speed", type=float, help="Native Omni speed, default 0.8; does not affect VibeVoice")
    parser.add_argument("--mood", choices=list(MOODS), default="auto")
    parser.add_argument("--mood-intensity", choices=list(INTENSITIES), default="restrained")
    parser.add_argument("--visual-balance", choices=list(BALANCES), default="auto")
    parser.add_argument("--music-engine", choices=list(MUSIC), default="none")
    parser.add_argument("--resume-from", choices=["music", "narration", "images", "scenes"], help="Continue from approved audio, revise pictures, repair narration, or replace the score")
    parser.add_argument("--script-dir", type=Path, help="Editor-supplied script-{duration}.json files")
    parser.add_argument("--visual-plan-dir", type=Path, help="Editor-reviewed library.json and storyboard-{duration}.json")
    parser.add_argument("--image-manifest", type=Path, help="Reuse selected images, verifying their hashes")
    parser.add_argument("--orientation", choices=["landscape", "portrait", "both"], default="landscape")
    parser.add_argument("--language", help="VibeVoice language key; alignment currently supports English")
    parser.add_argument("--review-mode", choices=["experimental", "off"], default="experimental")
    parser.add_argument("--until", choices=["plan", "evidence", "audio", "storyboard", "images", "video"], default="video")
    parser.add_argument("--use-evidence", action="store_true")
    parser.add_argument("--host", default="0.0.0.0", help="Web interface bind address")
    parser.add_argument("--port", type=int, default=8051)
    parser.add_argument("--web-root", type=Path, default=ROOT / "artifacts/interface")
    args = parser.parse_args()
    args.durations = list(dict.fromkeys(args.durations))
    if args.subject_graph and args.generation_mode != "long":
        parser.error("--subject-graph requires --generation-mode long")
    if args.generation_mode == "long":
        if args.command != "create" or len(args.durations) != 1 or not 300 <= args.durations[0] <= 1800:
            parser.error("Long generation requires create and one advisory length between 300 and 1800 seconds")
        if args.until not in {"plan", "audio", "video"}:
            parser.error("Long generation supports --until plan, audio or video")
    elif any(d < 30 or d > 180 for d in args.durations):
        parser.error("Advisory durations must be between 30 and 180 seconds")
    elif args.until == "plan" or args.chapter_plan or args.orientation_brief:
        parser.error("Parent plans require --generation-mode long")
    if args.omni_speed is not None and not .5 <= args.omni_speed <= 2:
        parser.error("--omni-speed must be between 0.5 and 2")
    if args.resume_from and (args.command != "create" or not args.project):
        parser.error("--resume-from requires create and --project")
    if args.command in {"create", "research"} and not args.resume_from and not (args.subject and args.subject.strip()):
        parser.error("--subject is required for a new film")
    if args.command == "evaluate" and not args.project:
        parser.error("--project is required for evaluate")
    if args.command == "serve":
        from .web import serve
        serve(args.config, args.web_root, args.host, args.port)
        return
    args.project = args.project or project_path(args.subject)
    print(f"Project: {args.project.resolve()}", flush=True)
    if args.command == "create":
        create(args)
    elif args.command == "research":
        research(configuration(args.config), Store(args.project), args.subject)
    else:
        from .evaluation import evaluate
        evaluate(Store(args.project))


if __name__ == "__main__":
    main()
