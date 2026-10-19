"""Revise a completed film's score without repeating narration or picture work."""
from __future__ import annotations

from pathlib import Path

from .core import Store, configuration, digest, read, write
from .music import prepare_music
from .planning import script_text
from .video import replace_audio


def music_checkpoint(project):
    """Validate the saved narration and checked deliveries before spending GPU time."""
    project = Path(project).resolve()
    try:
        delivery = read(project / "delivery.json")
        brief = read(project / "creative-brief.json")
        if not delivery["outputs"] or not brief.get("music", {}).get("caption"):
            raise ValueError("Saved deliveries and a music direction are required")
        films = {}
        for output in delivery["outputs"]:
            duration = output["requested_seconds"]
            if duration not in films:
                film = read(project / f"film-{duration}/audio-selection.json")
                timing = read(project / f"film-{duration}/timing.json")
                audio_hash = digest(Path(film["audio"]).read_bytes())
                if audio_hash != film["audio_info"]["audio_sha256"] or audio_hash != timing["audio_sha256"]:
                    raise ValueError(f"Saved narration changed: {duration}s brief")
                if timing["text_sha256"] != digest(script_text(film["script"])):
                    raise ValueError(f"Saved script no longer matches MFA: {duration}s brief")
                films[duration] = film
            root = Path(output["video"]).parent
            qa = read(root / "qa.json")
            layout = read(root / "layout/layout-results.json")
            compile_report = read(root / "compile-report.json")
            if not qa["passed"] or not layout["passed"]:
                raise ValueError("The existing video's delivery checks have not passed")
            if digest(Path(output["video"]).read_bytes()) != qa["video_sha256"]:
                raise ValueError("The existing video changed after delivery checks")
            if compile_report["source_audio_sha256"] != films[duration]["audio_info"]["audio_sha256"]:
                raise ValueError("The existing video uses a different narration")
            read(root / "timeline.json")
        return delivery, brief, films
    except (OSError, KeyError) as exc:
        raise ValueError(f"Cannot resume music: a completed checkpoint is missing or incomplete ({exc})") from exc


def resume_music(args):
    engine = getattr(args, "music_engine", "acestep")
    if engine not in {"none", "acestep"}:
        raise ValueError("Unknown background score engine")
    delivery, brief, films = music_checkpoint(args.project)
    if getattr(args, "subject", None) and args.subject != delivery["subject"]:
        raise ValueError("This folder belongs to a different subject")
    project = Path(args.project).resolve()
    snapshot = project / "config-snapshot.json"
    config = configuration(snapshot if snapshot.exists() else args.config)
    store = Store(project)
    try:
        # Retain the previous publication, selections and portable manifest.
        revision = project / "delivery-history" / digest(delivery)[:20]
        if not (revision / "delivery.json").exists():
            write(revision / "delivery.json", delivery)
            for duration in films:
                selected = project / f"film-{duration}/music-selection.json"
                if selected.exists():
                    write(revision / f"music-selection-{duration}.json", read(selected))
        for stage in ["research", "evidence", "creative", "narration", "timing", "scenes", "images"]:
            store.event("pipeline:" + stage, "succeeded", {"reused": True, "resume_from": "music"})
        store.event("pipeline:video", "pending", {"resume_from": "music"})
        store.event("pipeline:music", "running" if engine != "none" else "skipped", {"engine": engine, "resume_from": "music"})
        for film in films.values():
            store.event("music", "running", {"requested_seconds": film["requested_seconds"], "actual_seconds": film["actual_seconds"]})
            film["music"] = prepare_music(config, store, film, brief, engine)
            store.event("music", "succeeded", {"requested_seconds": film["requested_seconds"], "master_audio": film["music"]["master_audio"]})
        if engine != "none":
            store.event("pipeline:music", "succeeded", {"engine": engine})
        store.event("pipeline:video", "running", {"method": "copy existing video stream; replace audio"})
        outputs = []
        for old in delivery["outputs"]:
            video = replace_audio(config, store, old, films[old["requested_seconds"]]["music"])
            outputs.append({**old, "music_engine": engine, "video": str(video),
                            "timeline": str(video.parent / "timeline.json"), "qa": str(video.parent / "qa.json"),
                            "picture_reused": True, "previous_video": old["video"]})
        updated = {**delivery, "outputs": outputs, "music_engine": engine,
                   "human_acceptance": "pending", "resume_from": "music"}
        # Publish only when every remux has passed its checks.
        write(project / "delivery.json", updated)
        from .delivery import preview
        destination = preview(store)
        record = project / "project.json"
        if record.exists():
            write(record, {**read(record), "music_engine": engine})
        store.event("pipeline:video", "succeeded", {"output_directory": str(destination), "picture_reused": True})
        print(f"Portable review: {destination}", flush=True)
    finally:
        store.db.close()


def narration_checkpoint(project):
    """Validate reusable picture assets without trusting the failed narration."""
    project = Path(project).resolve()
    try:
        delivery = read(project / "delivery.json")
        brief = read(project / "creative-brief.json")
        images = read(project / "image_manifest.json")
        evidence = read(project / "evidence-final.json")
        sources = read(project / "sources.json")
        films = {}
        if not delivery["outputs"]:
            raise ValueError("A completed delivery is required")
        for output in delivery["outputs"]:
            duration = output["requested_seconds"]
            root = Path(output["video"]).parent
            qa = read(root / "qa.json")
            if not qa["passed"] or digest(Path(output["video"]).read_bytes()) != qa["video_sha256"]:
                raise ValueError("The saved video changed after delivery checks")
            if duration not in films:
                script = read(project / f"film-{duration}/script.json")
                storyboard = read(project / f"film-{duration}/storyboard.json")
                for scene in storyboard["scenes"]:
                    if scene["kind"] == "image":
                        image = images[scene["asset_id"]]
                        if digest(Path(image["file"]).read_bytes()) != image["sha256"]:
                            raise ValueError("An approved image changed after review")
                films[duration] = {"script": script, "storyboard": storyboard, "requested_seconds": duration}
        return delivery, brief, films, images, evidence, sources
    except (OSError, KeyError) as exc:
        raise ValueError(f"Cannot repair narration: saved script, visuals or delivery are incomplete ({exc})") from exc


def resume_narration(args):
    """Repair a completed film using its approved script and all existing images."""
    from .media import audit_audio, align_audio
    from .vibe_media import render_vibevoice, align_vibevoice
    from .omni_media import render_omnivoice
    from .creative import beat_errors, visual_metrics
    from .figures import options as figure_options
    from .video import compile_timeline, render
    from .narrators import select_reference
    from .audio_audit import AUDIT_POLICY

    delivery, brief, films, images, evidence, sources = narration_checkpoint(args.project)
    if getattr(args, "subject", None) and args.subject != delivery["subject"]:
        raise ValueError("This folder belongs to a different subject")
    project = Path(args.project).resolve()
    snapshot = project / "config-snapshot.json"
    config = configuration(snapshot if snapshot.exists() else args.config)
    config = select_reference(config, reference_audio=getattr(args, "reference_audio", None),
                              reference_transcript=getattr(args, "reference_transcript", None))
    engine = getattr(args, "tts_engine", delivery.get("tts_engine", "vibevoice"))
    config["tts_engine"] = engine
    store = Store(project)
    try:
        history = project / "delivery-history" / digest(delivery)[:20]
        if not (history / "delivery.json").exists():
            write(history / "delivery.json", delivery)
            write(history / "image_manifest.json", images)
            for duration in films:
                for name in ["audio-selection", "script", "timing", "storyboard", "music-selection"]:
                    original = project / f"film-{duration}/{name}.json"
                    if original.exists():
                        write(history / f"film-{duration}/{name}.json", read(original))
        for stage in ["research", "evidence", "creative"]:
            store.event("pipeline:"+stage, "succeeded", {"reused": True, "resume_from": "narration"})
        for stage in ["timing", "scenes", "images", "music", "video"]:
            store.event("pipeline:"+stage, "pending", {"resume_from": "narration"})
        store.event("pipeline:narration", "running", {"engine": engine, "point_clips": True, "batch_size": config.get("vibe_batch_size", 4)})
        for duration, film in films.items():
            points = film["script"]["points"]
            audio, info = (render_vibevoice(config, store, points, f"narration-{duration}-recovery")
                           if engine == "vibevoice" else
                           render_omnivoice(config, store, points, f"narration-{duration}-recovery"))
            audit = audit_audio(config, store, audio, script_text(film["script"]), points=points)
            if audit["status"] != "passed":
                store.event("narration-recovery", "audit_failed", {"reasons": audit["reasons"], "failed_points": audit.get("failed_points")})
                raise RuntimeError("Assembled narration did not pass completeness checks; previous video retained")
            film.update(audio=str(audio), audio_info=info, audit=audit, actual_seconds=info["duration_seconds"],
                point_count=len(points), duration_policy="advisory", audit_policy=AUDIT_POLICY,
                duration_deviation_seconds=round(info["duration_seconds"]-duration, 3), human_listening_review="pending")
        store.event("pipeline:narration", "succeeded", {"repaired": True})
        store.event("pipeline:timing", "running")
        for duration, film in films.items():
            film["timing"] = (align_vibevoice(config, store, film["audio"], film["script"], film["audio_info"], f"alignment-{duration}-recovery")
                              if engine == "vibevoice" else
                              align_audio(config, store, film["audio"], film["script"], f"alignment-{duration}-recovery",
                                          spoken_points=film["audio_info"].get("spoken_points")))
            errors = beat_errors(film["storyboard"], film["script"], film["timing"], figure_options(evidence))
            if errors:
                raise ValueError(f"Saved visual anchors no longer match repaired narration: {errors}")
        store.event("pipeline:timing", "succeeded", {"point_alignment": True})
        store.event("pipeline:scenes", "succeeded", {"reused": True, "retimed_from_word_anchors": True})
        store.event("pipeline:images", "succeeded", {"reused": True, "images": len(images), "new_generations": 0})
        music_engine = delivery.get("music_engine", "none")
        store.event("pipeline:music", "running" if music_engine != "none" else "skipped", {"engine": music_engine})
        for film in films.values():
            film["music"] = prepare_music(config, store, film, brief, music_engine)
        if music_engine != "none":
            store.event("pipeline:music", "succeeded", {"engine": music_engine})
        store.event("pipeline:video", "running", {"reason": "Recompose captions and scenes against corrected narration"})
        outputs = []
        for old in delivery["outputs"]:
            film = films[old["requested_seconds"]]
            portrait = old["orientation"] == "portrait"
            delivery_config = {**config, "width": 1080 if portrait else 1920, "height": 1920 if portrait else 1080}
            root, timeline = compile_timeline(delivery_config, store, film["script"], film["timing"], film["storyboard"],
                images, evidence, sources, film["requested_seconds"], creative_brief=brief, music=film["music"])
            video = render(delivery_config, store, root, timeline)
            outputs.append({**old, "actual_seconds": film["actual_seconds"], "video": str(video), "timeline": str(timeline),
                "qa": str(root/"qa.json"), "visual_metrics": visual_metrics(film["storyboard"], film["script"], film["timing"]),
                "previous_video": old["video"], "narration_repaired": True, "images_reused": True})
        # Publish selections and delivery only after all corrected videos pass.
        for duration, film in films.items():
            write(project / f"film-{duration}/audio-selection.json", {k:v for k,v in film.items() if k not in {"timing", "storyboard", "music"}})
            write(project / f"film-{duration}/timing.json", film["timing"])
            write(project / f"film-{duration}/visual-metrics.json", visual_metrics(film["storyboard"], film["script"], film["timing"]))
        write(project / "delivery.json", {**delivery, "outputs": outputs, "tts_engine": engine,
            "resume_from": "narration", "human_acceptance": "pending"})
        from .delivery import preview
        destination = preview(store)
        store.event("pipeline:video", "succeeded", {"output_directory": str(destination), "narration_repaired": True})
    finally:
        store.db.close()


def images_checkpoint(project, durations=None):
    """Images may be revised after a visual failure, without a completed video."""
    project = Path(project).resolve()
    try:
        record = read(project / "project.json")
        brief = read(project / "creative-brief.json")
        evidence = read(project / "evidence-final.json")
        sources = read(project / "sources.json")
        durations = durations or record["durations"]
        films = []
        for duration in durations:
            film = read(project / f"film-{duration}/audio-selection.json")
            timing = read(project / f"film-{duration}/timing.json")
            audio_hash = digest(Path(film["audio"]).read_bytes())
            if audio_hash != film["audio_info"]["audio_sha256"] or audio_hash != timing["audio_sha256"]:
                raise ValueError("Saved narration changed after MFA")
            if timing["text_sha256"] != digest(script_text(film["script"])):
                raise ValueError("Saved script no longer matches MFA")
            if film.get("audit", {}).get("status") != "passed":
                raise ValueError("Saved narration has not passed its audio audit")
            if not timing.get("words") or not timing.get("points"):
                raise ValueError("Saved MFA word/point timings are incomplete")
            film.update(timing=timing, creative_brief=brief)
            film["script"]["subject"] = record["subject"]
            films.append(film)
        return record, brief, evidence, sources, films
    except (OSError, KeyError) as exc:
        raise ValueError(f"Cannot resume images: incomplete narration/MFA checkpoint ({exc})") from exc


def resume_images(args):
    from .pipeline import finish_visuals
    from .llm import Models
    record, brief, evidence, sources, films = images_checkpoint(args.project, args.durations)
    if getattr(args, "subject", None) and args.subject != record["subject"]:
        raise ValueError("This folder belongs to a different subject")
    args.subject = record["subject"]
    config = configuration(args.config)
    config["tts_engine"] = record.get("tts_engine", "vibevoice")
    store = Store(args.project)
    try:
        resume_stage = getattr(args, 'resume_from', None) or 'images'
        for stage in ["research", "evidence", "creative", "narration", "timing"]:
            store.event("pipeline:" + stage, "succeeded", {"reused": True, "resume_from": resume_stage})
        for stage in ["scenes", "images", "music", "video"]:
            store.event("pipeline:" + stage, "pending", {"resume_from": resume_stage})
        chapter = read(args.chapter_context) if getattr(args, "chapter_context", None) else None
        return finish_visuals(args, config, store, Models(config, store), evidence, sources, brief, films, chapter)
    finally:
        store.db.close()
