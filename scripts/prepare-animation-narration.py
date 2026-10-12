#!/usr/bin/env python3
"""Prepare supplied narration and machine-derived MFA input bundle.

This isolated experiment uses the current native VibeVoice point-render path,
including its audits, retries and silence gating. Existing approved provider
outputs are reused; neither production endpoints nor baseline scenes change.
Human timing and content inspection remains explicitly pending.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infofilm.core import Store, configuration, digest, read, write
from infofilm.media import request
from infofilm.vibe_media import align_vibevoice, render_vibevoice


def preflight(config, queue_path, owner_project=None):
    """Check external work without creating a queue database or stopping jobs."""
    if queue_path.is_file():
        with sqlite3.connect(f"file:{queue_path}?mode=ro", uri=True, timeout=3) as db:
            rows = db.execute("SELECT id,status,project FROM jobs WHERE status IN ('running','queued')").fetchall()
        if owner_project:
            # A queued job cannot start while this single queue worker owns the GPU.
            # Only this exact owner's running job is exempt; other running work blocks.
            owner = Path(owner_project).resolve()
            if any(r[1] == 'running' and Path(r[2]).resolve() == owner for r in rows):
                rows = [r for r in rows if r[1] == 'running' and Path(r[2]).resolve() != owner]
        if rows:
            raise RuntimeError("Webapp jobs are active; leave them undisturbed: " + ", ".join(row[0] for row in rows))
    voice = request(config, "tts", "GET", "/health", timeout=15)
    if voice.get("active_job") or voice.get("queued_or_running_jobs"):
        raise RuntimeError("VibeVoice has active external work; leave it undisturbed")
    image = request(config, "image", "GET", "/api/status", timeout=15)
    if image.get("busy") or image.get("state") == "loading":
        raise RuntimeError("Qwen Image has active external work; leave it undisturbed")
    return {"queue_database": str(queue_path), "queue_owner_project": str(owner_project) if owner_project else None,
            "voice": voice, "image": image}


def verify_bundle(value, transcript):
    if value.get("schema_version") != "infofilm.animation-narration.v1":
        raise ValueError("Unsupported narration input bundle")
    if value["transcript"] != transcript or value["transcript_sha256"] != digest(transcript):
        raise ValueError("Narration input text differs; choose a new --output directory")
    for field in ("audio", "timing"):
        path = Path(value[field])
        if not path.is_file() or digest(path.read_bytes()) != value[field + "_sha256"]:
            raise ValueError(f"Saved {field} changed; choose a new --output directory")
    timing = read(value["timing"])
    if timing["audio_sha256"] != value["audio_sha256"]:
        raise ValueError("Saved timing no longer matches its audio")
    if [word["word"] for word in value["words"]] != transcript.split():
        raise ValueError("Narration words do not cover the supplied transcript")
    expected = [{key: word[key] for key in ("id", "word", "start", "end")} for word in timing["words"]]
    if value["words"] != expected:
        raise ValueError("Narration word spans differ from the saved timing")
    previous_end = 0.0
    for word in value["words"]:
        if not previous_end <= word["start"] <= word["end"] <= value["duration_seconds"] + .001:
            raise ValueError("Invalid or overlapping narration word spans")
        previous_end = word["end"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/experiments/gravity-animation-llm/input-audio")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    source=parser.add_mutually_exclusive_group()
    source.add_argument("--narration", type=Path)
    source.add_argument("--script-plan",type=Path,help="Luna narration-script plan; render its paragraphs as audited points")
    parser.add_argument("--reference-audio", type=Path, help="Override the configured narrator reference")
    parser.add_argument("--queue-db", type=Path, default=ROOT / "artifacts/interface/queue.sqlite3")
    parser.add_argument('--owner-project',type=Path,help='Managed queue owner; other running jobs still block')
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if args.script_plan:
        narration_source=args.script_plan.expanduser().resolve();plan=read(narration_source)
        if plan.get('schema_version')!='infofilm.animation-narration-script.v1':parser.error('Invalid narration script plan')
        points=[{'id':p['id'],'narration':p['narration'].strip()} for p in plan['points']]
        transcript='\n\n'.join(p['narration'] for p in points)
        script_source='Luna'
    else:
        narration_source=(args.narration or ROOT/'animation-experiments/gravity/narration.txt').expanduser().resolve()
        transcript=narration_source.read_text().strip()
        points=[{'id':'N01','narration':transcript}];script_source='manual'
    if not transcript:
        parser.error("Narration cannot be empty")
    manifest = output / "narration-input.json"
    if manifest.exists():
        value = read(manifest)
        verify_bundle(value, transcript)
        print(f"Reusing verified narration bundle: {manifest}", flush=True)
        return
    config = configuration(args.config.expanduser().resolve())
    if args.reference_audio:
        config["reference_audio"] = str(args.reference_audio.expanduser().resolve())
    output.mkdir(parents=True, exist_ok=True)
    store = Store(output)
    try:
        write(output / "preflight.json", preflight(config, args.queue_db.expanduser().resolve(), args.owner_project))
        write(output / "script.json", {"schema_version": "infofilm.animation-speech-script.v1",
            'script_source':script_source,'points':points})
        audio, info = render_vibevoice(config, store, points, "animation-narration")
        write(output / "audio-info.json", info)
        timing = align_vibevoice(config, store, audio, {"points": points}, info, "animation-narration-timing")
        timing_path = write(output / "timing.json", timing)
        value = {
            "schema_version": "infofilm.animation-narration.v1",
            "transcript": transcript,
            "transcript_sha256": digest(transcript),
            "audio": str(audio.resolve()),
            "audio_sha256": digest(audio.read_bytes()),
            "duration_seconds": info["duration_seconds"],
            "timing": str(timing_path.resolve()),
            "timing_sha256": digest(timing_path.read_bytes()),
            "words": [{key: word[key] for key in ("id", "word", "start", "end")} for word in timing["words"]],
            "provenance": {
                "engine": "VibeVoice", "alignment": "MFA", "script_source": script_source,
                "human_review": "pending", "alignment_method": "machine-derived",
                "narration_source": str(narration_source),
                "reference_audio": info["reference_audio"],
                "reference_audio_sha256": info["reference_audio_sha256"],
                "reference_transcript_used_for_conditioning": info["reference_transcript_used_for_conditioning"],
                "provider_url": config["tts_url"], "point_attempts": info["point_attempts"],
                "silence_cleanup_policy": info["silence_cleanup_policy"],
                "audio_info": str((output / "audio-info.json").resolve()),
            },
        }
        verify_bundle(value, transcript)
        write(manifest, value)
        store.event("animation-narration-input", "ready_for_human_review", {
            "manifest": str(manifest), "duration_seconds": value["duration_seconds"],
            "words": len(value["words"]), "human_review": "pending"})
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
