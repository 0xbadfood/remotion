from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

from .core import ROOT, Store, configuration, digest, read, write, resource_slot
from .llm import Models
from .media import align_audio, audit_audio, generate_images, render_audio
from .omni_media import render_omnivoice
from .vibe_media import render_vibevoice, align_vibevoice
from .audio_audit import AUDIT_POLICY, DEFAULT_FALLBACK_MODELS
from .silence import settings as silence_settings
from .creative import (MOODS, BALANCES, INTENSITIES, MUSIC, make_creative_brief,
                       make_visual_beats, library_from_beats, beat_errors, visual_metrics)
from .music import prepare_music
from .narrators import select_reference
from .llm_settings import endpoint, llm_config
from .planning import make_evidence, make_library, make_script, make_storyboard, make_image_prompts, library_errors, storyboard_errors, script_text, word_count
from .video import compile_timeline, render


def agent_role(config):
    """The research agent calls tools through chat completions: the reviewer as before, unless the reviewer is a
    Responses-API connection, then the planner."""
    return "reviewer" if endpoint(config, "reviewer")["type"] != "openai_responses" else "planner"


KEY_IDEA_SOURCES = 6              # web pages on how others explain a key idea, added after the book


def research(config, store, subject, *, subject_guidance=None, budget=None):
    """Run kbagent with its existing dependencies; archive and resume fetched evidence. `budget` ({max_searches,
    max_fetches}) widens film-level research, whose digest every chapter builds on."""
    identity = digest({"subject":subject, "model_url":config["reviewer_url"], "policy":"primary-evergreen-v1"})
    if subject_guidance is not None:
        identity = digest({'legacy_identity': identity, 'subject_guidance': subject_guidance})
    if budget:
        identity = digest({'identity': identity, 'budget': budget})
    directory = store.root / "research" / identity[:16]
    output = directory / "extracted.json"
    if output.exists() and (directory / "answer.txt").exists():
        return read(output)
    interpreter = Path(config.get("research_python", str(Path(config["kbagent_root"])/"env/bin/python")))
    if not interpreter.exists():
        raise RuntimeError(f"Research Python environment is missing: {interpreter}")
    request = write(directory/"request.json", {"subject":subject,"directory":str(directory),
        "kbagent_root":config["kbagent_root"],"model_url":config["reviewer_url"],
        **({"llm": llm_config(config), "agent_role": agent_role(config)} if config.get("llm_endpoints") else {}),
        **({"budget": budget} if budget else {}),
        **({'subject_guidance': subject_guidance} if subject_guidance is not None else {})})
    store.event("research","running",{"subject":subject,"python":str(interpreter)})
    try:
        subprocess.run([str(interpreter),"-u","-m","infofilm.research_worker",str(request)],cwd=ROOT,check=True)
    except subprocess.CalledProcessError as exc:
        failure_path = directory / 'failure.json'
        reason = read(failure_path).get('error') if failure_path.exists() else None
        if reason:
            store.event('research', 'failed', {'error': reason, 'archive': str(directory)})
            raise RuntimeError('Research failed: ' + reason) from exc
        raise
    sources = read(output)
    floor = read(directory / "result.json").get("source_floor") if (directory / "result.json").exists() else None
    store.event("research","succeeded",{"fetched_sources":len(sources["results"]),"archive":str(directory),
                                        **({"source_floor": floor} if floor and floor.get("min_sources") else {})})
    return sources


def voice_calibration(config, store):
    """Words per minute of the narrator on the configured voice engine (one short take, cached)."""
    calibration = store.root / "voice_calibration.json"
    ref_hash = digest(Path(config["reference_audio"]).read_bytes())
    calibration_key = digest({"reference_audio_sha256": ref_hash, "tts_engine": config["tts_engine"],
                              "reference_transcript": config.get("reference_transcript") if config["tts_engine"] == "omnivoice" else None,
                              **({"omni_speed": config.get("omni_speed", .8)} if config["tts_engine"] == "omnivoice" else {})})
    existing_calibration = read(calibration) if calibration.exists() else {}
    legacy_vibe_calibration = (config["tts_engine"] == "vibevoice" and not existing_calibration.get("calibration_key")
                               and existing_calibration.get("reference_audio_sha256") == ref_hash)
    if existing_calibration.get("calibration_key") != calibration_key and not legacy_vibe_calibration:
        text = "A clear explanation starts with the main idea. We then explore how it works, why it matters, and which details need care. Each step should connect naturally, giving the listener enough time to understand."
        audio, info = (render_omnivoice(config, store, [{"id": "calibration", "narration": text}], "voice-calibration")
                       if config["tts_engine"] == "omnivoice" else render_audio(config, store, text, "voice-calibration"))
        write(calibration, {"audio": str(audio), **info, "calibration_key": calibration_key})
    return read(calibration)["observed_words_per_minute"]


def prepare_audio(config, store, models, evidence, duration, rate, experimental, supplied_script=None, creative_brief=None):
    selected = store.root / f"film-{duration}" / "audio-selection.json"
    dependency_hash = digest({"evidence": evidence, "duration": duration,
        "reference_audio_sha256": digest(Path(config["reference_audio"]).read_bytes()), "language": config["language"],
        "provider": config["omni_url"] if config.get("tts_engine") == "omnivoice" else config["tts_url"],
        "tts_engine": config.get("tts_engine", "vibevoice"),
        "audio_policy": "point-batches-v1", "audit_policy": AUDIT_POLICY,
        "audio_audit_fallback_models": config.get("audio_audit_fallback_models", list(DEFAULT_FALLBACK_MODELS)),
        "audio_audit_llm_review": config.get("audio_audit_llm_review", True),
        "silence_cleanup": (silence_settings(config)
                            if config.get('tts_engine', 'vibevoice') == 'vibevoice' else None),
        "vibe_pause_seconds": config.get("vibe_pause_seconds", .18),
        "reference_transcript": config.get("reference_transcript") if config.get("tts_engine") == "omnivoice" else None,
        "omni_speed": config.get("omni_speed", .8) if config.get("tts_engine") == "omnivoice" else None,
        "narration_direction": ({k: creative_brief.get(k) for k in ["resolved_mood", "requested_mood", "intensity", "narration_direction"]} if creative_brief else None),
        "duration_policy": "advisory", "experimental": experimental,
        "supplied_script": supplied_script})
    if selected.exists():
        value = read(selected)
        if value.get("dependency_hash") == dependency_hash:
            return value
    script = supplied_script or make_script(models, evidence, duration, rate, experimental=experimental, creative_brief=creative_brief)
    if supplied_script:
        from .planning import script_errors
        errors = script_errors(script, evidence, duration=duration)
        if errors:
            raise ValueError(f"Supplied script is invalid: {errors}")
        # Both endpoints independently check semantics; deterministic code owns counts.
        prompt = ("Review this editor-supplied informational narration against its evidence. Assess factual meaning, causal order, natural Indian English and useful clarity. "
                  "The requested duration is advisory: flow, useful points and natural narration take priority. Do not reject a somewhat longer or shorter script, recalculate word counts or require all evidence facts in the shorter film. "
                  "Return {pass:boolean,score:0..100,issues:[{field,severity:'blocker'|'major'|'minor',problem,evidence,correction}],strengths:[string]}. "
                  "Pass means no major/blocker issues.\nEVIDENCE:\n"+json.dumps(evidence)+"\nSCRIPT:\n"+json.dumps(script))
        for role in ["planner","reviewer"]:
            review = models.call(role,f"supplied-script-{duration}-{role}",prompt,max_tokens=3000)
            write(store.root/f"film-{duration}"/f"script-semantic-review-{role}.json",review)
            if review.get("pass") is not True or any(i.get("severity") in {"major","blocker"} for i in review.get("issues",[])):
                raise RuntimeError(f"Supplied script needs semantic correction: {review}")
    measured = []
    def render(current_config, label):
        if current_config.get("tts_engine") == "omnivoice":
            return render_omnivoice(current_config, store, script["points"], label)
        return render_vibevoice(current_config, store, script["points"], label)
    from .speech import apply as speech_apply, lexicon as speech_lexicon
    entries = (speech_lexicon(config, store, script["points"])
               if config.get("speech_conditioning", True) and config.get("tts_engine") != "omnivoice" else {})
    audited = {**script, "points": [{**p, "narration": speech_apply(p["narration"], entries, audit=True)}
                                    for p in script["points"]]}
    for attempt in range(2):
        audio, info = render(config, f"narration-{duration}-{attempt}")
        audit = audit_audio(config, store, audio, script_text(audited), points=audited["points"])
        if audit["status"] != "passed":
            store.event(f"narration-{duration}", "audit_failed", {"audit": audit})
            if attempt == 1:
                raise RuntimeError("Narration did not pass bounded ASR review")
            # Preserve the failed take and render the same text as a new provider job.
            audio, info = render({**config, "audio_variant": attempt+1}, f"narration-{duration}-repair-{attempt}")
            audit = audit_audio(config, store, audio, script_text(audited), points=audited["points"])
            if audit["status"] != "passed":
                raise RuntimeError("Narration repair still needs review; both takes and audits are retained")
        actual = info["duration_seconds"]
        measured.append({"attempt": attempt, "audio": str(audio), "actual_seconds": actual, "words": word_count(script_text(script)), "audit": audit})
        result = {"script": script, "audio": str(audio), "audio_info": info, "audit": audit,
                  "requested_seconds": duration, "actual_seconds": actual, "point_count": len(script["points"]),
                  "duration_policy": "advisory", "duration_deviation_seconds": round(actual-duration, 3),
                  "audio_quality_attempts": measured, "evidence_hash": digest(evidence), "dependency_hash": dependency_hash,
                  "human_listening_review": "pending"}
        write(selected, result)
        write(selected.parent / "script.json", script)
        (selected.parent / "script.txt").write_text(script_text(script)+"\n")
        return result
    raise RuntimeError("Narration did not pass bounded audio quality checks")


def create(args):
    if getattr(args, 'generation_mode', 'short') == 'animation':
        from .animation_production import create_animation
        return create_animation(args)
    if getattr(args, "generation_mode", "short") == "long":
        from .longform import create_long
        return create_long(args)
    if getattr(args, "resume_from", None) in {"images", "scenes"}:
        from .resume import resume_images
        return resume_images(args)
    if getattr(args, "resume_from", None) == "narration":
        from .resume import resume_narration
        return resume_narration(args)
    if getattr(args, "resume_from", None) == "music":
        from .resume import resume_music
        return resume_music(args)
    config = configuration(args.config)
    chapter_context = read(args.chapter_context) if getattr(args, "chapter_context", None) else None
    curriculum = read(args.curriculum) if getattr(args, "curriculum", None) else None
    if getattr(args, "image_provider_policy", None):
        config["image_provider_policy"] = args.image_provider_policy
        if not config.get("image_provider_registry") or args.image_provider_policy not in config["image_provider_registry"]["policies"]:
            raise ValueError(f"Unknown image provider policy: {args.image_provider_policy}")
    if args.language:
        config["language"] = args.language
    config["tts_engine"] = getattr(args, "tts_engine", "vibevoice")
    config = select_reference(config, narrator=getattr(args, "narrator", None),
                              reference_audio=args.reference_audio,
                              reference_transcript=getattr(args, "reference_transcript", None))
    mood = getattr(args, "mood", "auto")
    intensity = getattr(args, "mood_intensity", "restrained")
    balance = getattr(args, "visual_balance", "auto")
    music_engine = getattr(args, "music_engine", "none")
    if mood not in MOODS or intensity not in INTENSITIES or balance not in BALANCES or music_engine not in MUSIC:
        raise ValueError("Unknown mood, intensity, visual balance or background score")
    if getattr(args, "omni_speed", None) is not None:
        config["omni_speed"] = args.omni_speed
    store = Store(args.project)
    project_record = store.root / "project.json"
    existing = read(project_record) if project_record.exists() else None
    delivery = store.root / "delivery.json"
    if not existing and delivery.exists():
        existing = read(delivery)
    if existing and existing.get("subject") != args.subject:
        raise ValueError("This folder belongs to a different subject. Choose a new --project folder.")
    write(project_record, {"subject": args.subject, "durations": args.durations,
        "orientation": args.orientation, "duration_policy": "advisory", "tts_engine": config["tts_engine"],
        "mood": mood, "mood_intensity": intensity, "visual_balance": balance, "music_engine": music_engine})
    def stage(name, state, **detail):
        store.event("pipeline:" + name, state, detail)
    models = Models(config, store)
    stage("research", "running")
    research_subject = (chapter_context["research_subject"] if chapter_context else args.subject)
    sources = read(args.sources) if args.sources else research(config, store, research_subject)
    if (curriculum or {}).get("kind") == "key_idea" and curriculum.get("research"):
        # Key-idea films: how others explain the idea simply (analogies, pictures, misconceptions) joins the book.
        # Optional: without it the planner still has the book and its own analogies.
        try:
            found = research(config, store, curriculum["research"])
            sources = {**sources, "results": sources["results"] + found["results"][:KEY_IDEA_SOURCES]}
        except Exception as exc:                                     # noqa: BLE001 - the film goes on from the book
            store.event("research", "skipped", {"error": str(exc)[:300]})
    write(store.root / "sources.json", sources)
    stage("research", "succeeded", source_count=len(sources["results"]))
    evidence_path = store.root / "evidence-final.json"
    experimental = args.review_mode == "experimental"
    stage("evidence", "running")
    evidence = read(evidence_path) if args.use_evidence and evidence_path.exists() else make_evidence(models, sources, args.subject, experimental=experimental, chapter_context=chapter_context,
                                                                                                            **({"curriculum": curriculum} if curriculum else {}))
    if chapter_context:
        evidence["chapter_context"] = chapter_context
    if curriculum:
        evidence["curriculum"] = curriculum      # the script step covers every point (planning.make_script)
    if (curriculum or {}).get("kind") == "key_idea":
        # Key-idea films: the analogy, its mapping, the visual world and the beat outline come before the script.
        from .idea_plan import make_idea_plan
        stage("evidence", "running", task="Planning the idea")
        evidence["idea_plan"] = make_idea_plan(models, evidence, experimental=experimental)
        write(store.root / "idea-plan.json", evidence["idea_plan"])
        # The script and the learner check aim at the plan's plain takeaway, not the book-level wording of the idea
        # (I4 v2, 2026-10-20: "signed energy change ΔE = R_H(...)" kept the formula in every rewrite).
        idea = curriculum.get("idea") or {}
        curriculum["idea"] = {**idea, "book_understanding": idea.get("book_understanding", idea.get("understanding")),
                              "understanding": evidence["idea_plan"]["takeaway"],
                              "terms": evidence["idea_plan"].get("book_terms") or []}
    write(store.root / "evidence-final.json", evidence)
    stage("evidence", "succeeded", facts=len(evidence["facts"]))
    if args.until == "evidence":
        return
    stage("creative", "running", mood=mood)
    if chapter_context:
        from .longform import chapter_creative
        brief = chapter_creative(chapter_context["shared_creative"], chapter_context["chapter"], mood)
    else:
        brief = make_creative_brief(models, evidence, mood, intensity, balance, experimental=experimental)
    write(store.root / "creative-brief.json", brief)
    stage("creative", "succeeded", resolved_mood=brief["resolved_mood"], visual_balance=brief["visual_balance"])
    stage("narration", "running", task="Voice calibration")
    rate = voice_calibration(config, store)
    if args.until == "script":
        # Long films, phase A: stop after the accepted script. The media phase reuses it from the cache.
        for duration in args.durations:
            accepted = store.root / f"film-{duration}" / "script.json"
            script = make_script(models, evidence, duration, rate, experimental=experimental, creative_brief=brief,
                                 seed=read(accepted) if accepted.exists() and (chapter_context or {}).get("continuity_notes") else None)
            write(store.root / f"film-{duration}" / "script.json", script)
            (store.root / f"film-{duration}" / "script.txt").write_text(script_text(script) + "\n")
            stage("script", "succeeded", requested_seconds=duration, points=len(script["points"]))
        return
    films = []
    for duration in args.durations:
        supplied = read(args.script_dir/f"script-{duration}.json") if args.script_dir else None
        film = prepare_audio(config, store, models, evidence, duration, rate, args.review_mode == "experimental", supplied, brief)
        film["creative_brief"] = brief
        stage("narration", "succeeded", requested_seconds=duration, actual_seconds=film["actual_seconds"], points=film["point_count"])
        film["script"]["subject"] = args.subject
        stage("timing", "running", requested_seconds=duration)
        film["timing"] = (align_vibevoice(config, store, film["audio"], film["script"], film["audio_info"], f"alignment-{duration}")
                          if config["tts_engine"] == "vibevoice" and film["audio_info"].get("point_clips") else
                          align_audio(config, store, film["audio"], film["script"], f"alignment-{duration}",
                                      spoken_points=film["audio_info"].get("spoken_points")))
        write(store.root / f"film-{duration}" / "timing.json", film["timing"])
        stage("timing", "succeeded", requested_seconds=duration)
        films.append(film)
    if args.until == "audio":
        if not getattr(args, 'audio_checkpoint_only', False):
            from .audio_delivery import finish_audio
            return finish_audio(args, config, store, films, brief)
        return
    return finish_visuals(args, config, store, models, evidence, sources, brief, films, chapter_context)


def finish_visuals(args, config, store, models, evidence, sources, brief, films, chapter_context=None):
    """Picture/score/delivery tail shared by new jobs and validated image revisions."""
    image_review = getattr(args, 'image_review', None)
    if image_review is not None:
        if type(image_review) is not bool:
            raise ValueError('image_review must be boolean')
        config = {**config, 'image_review': image_review}
    experimental = args.review_mode == "experimental"
    mood = getattr(args, "mood", "auto")
    intensity = getattr(args, "mood_intensity", "restrained")
    balance = getattr(args, "visual_balance", "auto")
    music_engine = getattr(args, "music_engine", "none")
    def stage(name, state, **detail):
        store.event("pipeline:" + name, state, detail)
    stage("scenes", "running")
    if args.visual_plan_dir:
        library = read(args.visual_plan_dir / "library.json")
        for film in films:
            film["storyboard"] = read(args.visual_plan_dir / f"storyboard-{film['requested_seconds']}.json")
    else:
        for film in films:
            film["storyboard"] = make_visual_beats(models, film, evidence, brief, experimental=experimental)
        library = library_from_beats(films, brief)
    errors = library_errors(library, evidence)
    if errors:
        raise ValueError(f"Invalid visual library: {errors}")
    write(store.root / "visual-library.json", library)
    from .figures import options as figure_options, render as render_figures
    figures = figure_options(evidence)
    for film in films:
        duration = film["requested_seconds"]
        errors = (beat_errors(film["storyboard"], film["script"], film["timing"], figures)
                  if any(s.get("start_word_id") for s in film["storyboard"]["scenes"])
                  else storyboard_errors(film["storyboard"], film["script"], library))
        if errors:
            raise ValueError(f"Invalid storyboard: {errors}")
        write(store.root / f"film-{duration}" / "storyboard.json", film["storyboard"])
    if figures and any(s.get("kind") == "figure" for film in films for s in film["storyboard"]["scenes"]):
        render_figures(store, films, figures)     # drawn by code before any image is made: a bad one fails fast
        for film in films:
            write(store.root / f"film-{film['requested_seconds']}" / "storyboard.json", film["storyboard"])
        write(store.root / f"film-{duration}" / "visual-metrics.json", visual_metrics(film["storyboard"], film["script"], film["timing"]))
    if figures and any(s.get("kind") == "animation" for film in films for s in film["storyboard"]["scenes"]):
        from .animations import render as render_animations
        stage("scenes", "running", task="Coding and reviewing the animated beats")
        render_animations(store, films, figures, evidence, brief,
                          log=lambda m: store.event("animations", "progress", {"message": m}))
        for film in films:
            write(store.root / f"film-{film['requested_seconds']}" / "storyboard.json", film["storyboard"])
    stage("scenes", "succeeded")
    if args.until == "storyboard":
        return
    orientations = ["landscape", "portrait"] if args.orientation == "both" else [args.orientation]
    stage("images", "running", task="Scene prompts and image rendering", image_review=config.get('image_review', True))
    from .person_references import reference_settings, prepare_person_references
    from .film_cast import SHORT_CHAPTER, cast_short, required_in_chapter
    reference_enabled = reference_settings(config)['enabled']
    # A short film gets the long films' cast step (one chapter). A run whose prompts were made uncast keeps them.
    if (reference_enabled and not chapter_context and not config.get('film_cast')
            and ((store.root / 'film-cast.json').exists() or not (store.root / 'scene-image-prompts.json').exists())):
        people = cast_short(config, store, models, films[0], evidence, experimental=experimental)
        if people:
            config = {**config, 'film_cast': people}
    cast_chapter = chapter_context['chapter']['id'] if chapter_context else SHORT_CHAPTER
    from .prompt_styles import prompt_style
    prompts = make_image_prompts(models, library, films, evidence, orientations, experimental=experimental,
                                 prompt_style=prompt_style(config),
                                 message_recovery=config.get("image_recovery_mode") == "message_v1",
                                 **({'classify_review': True} if config.get('image_classify_review', False) else {}),
                                 **({'person_references': True} if reference_enabled else {}),
                                 **({'film_cast': config['film_cast']} if reference_enabled and config.get('film_cast') else {}),
                                 **({'required_people': required_in_chapter(config['film_cast'], cast_chapter)}
                                    if reference_enabled and config.get('film_cast') else {}))
    if reference_enabled and not config.get('film_cast'):
        from .film_cast import limit_uncast_people
        prompts = limit_uncast_people(config, store, models, films[0], prompts, experimental=experimental)
    if reference_enabled:
        prompts = prepare_person_references(config, store, prompts)
    write(store.root/"scene-image-prompts.json", prompts)
    asset_by_id = {a["id"]: a for a in prompts["assets"]}
    for asset in library["assets"]:
        if asset["id"] in asset_by_id and "visual_message" in asset_by_id[asset["id"]]:
            asset.update({k: asset_by_id[asset["id"]][k] for k in ["visual_message", "setting", "visual_style"]})
        if asset['id'] in asset_by_id and 'review_profile' in asset_by_id[asset['id']]:
            asset['review_profile'] = asset_by_id[asset['id']]['review_profile']
        if reference_enabled and asset['id'] in asset_by_id:
            asset.update({k: asset_by_id[asset['id']][k] for k in ['people_in_frame', 'person_reference_status', 'reference_card', 'reference_faces']
                          if k in asset_by_id[asset['id']]})
    write(store.root / "visual-library.json", library)
    banner_by_scene = {b["scene_key"]: b["text"] for b in prompts["banners"]}
    for film in films:
        for scene in film["storyboard"]["scenes"]:
            if scene.get("asset_id") in asset_by_id and "visual_message" in asset_by_id[scene["asset_id"]]:
                scene["visual_message"] = asset_by_id[scene["asset_id"]]["visual_message"]
            if scene.get('asset_id') in asset_by_id and 'review_profile' in asset_by_id[scene['asset_id']]:
                scene['review_profile'] = asset_by_id[scene['asset_id']]['review_profile']
            if reference_enabled and scene.get('asset_id') in asset_by_id:
                asset = asset_by_id[scene['asset_id']]
                scene['people_in_frame'] = asset['people_in_frame']
                scene['person_reference_status'] = asset['person_reference_status']
                if asset.get('reference_card'):
                    scene['reference_card_sha256'] = asset['reference_card']['sha256']
            scene["banner"] = scene.get("banner_override", banner_by_scene.get(f"{film['requested_seconds']}:{scene.get('id', scene['point_id'])}", ""))
        write(store.root / f"film-{film['requested_seconds']}" / "storyboard.json", film["storyboard"])
    if not chapter_context:
        # Short films: the amdserver may still be switching back from the last film's clips; slow video workers
        # (the Spark's LTX service) start loading now, while the images are made.
        from .clips import ensure_image_mode, warm
        try:
            ensure_image_mode(config)
        except Exception as exc:                # the image stage itself reports a factory that is not there
            stage("images", "waiting", detail=f"image mode not confirmed: {exc}"[:300])
        warm(config)
    backdrop = None
    if any(s.get("kind") != "image" for film in films for s in film["storyboard"]["scenes"]):
        # Native-graphic scenes get the chapter's thematic backdrop instead of a flat gradient (advisory). It renders
        # alongside the scene images (one more job at the factory) rather than after them.
        from .backdrop import chapter_backdrop
        backdrop = threading.Thread(target=chapter_backdrop, args=(config, store), name="backdrop", daemon=True)
        backdrop.start()
    try:
        images = read(args.image_manifest) if args.image_manifest else generate_images(config, store, prompts, models)
    finally:
        if backdrop:
            backdrop.join()
    if args.image_manifest:
        needed = {a["id"] for a in prompts["assets"]}
        if not needed <= set(images):
            raise ValueError("Image manifest does not cover every planned asset")
        for asset_id in needed:
            value = images[asset_id]
            if digest(Path(value["file"]).read_bytes()) != value["sha256"]:
                raise ValueError(f"Image manifest hash mismatch: {asset_id}")
    from .depth import depth_maps
    depth_maps(config, store, images)           # 2.5D camera; advisory, cached by image hash
    if not chapter_context:                     # short films: slowed LTX clips from the accepted stills (advisory)
        from .clips import make_clips
        make_clips(config, store, models, images, prompts)
    if any(sc.get("action") for film in films for sc in film["storyboard"]["scenes"]):
        # Key-idea films: motion beats become short LTX videos from their accepted stills (action_clips.py).
        from .action_clips import make_action_clips
        make_action_clips(config, store, films, images)
        for film in films:
            write(store.root / f"film-{film['requested_seconds']}" / "storyboard.json", film["storyboard"])
    stage("images", "succeeded", images=len(images), image_review=config.get('image_review', True))
    if args.until == "images":
        return
    stage("music", "running" if music_engine != "none" else "skipped", engine=music_engine)
    for film in films:
        film["music"] = prepare_music(config, store, film, brief, music_engine)
    if music_engine != "none":
        stage("music", "succeeded", engine=music_engine)
    outputs = []
    stage("video", "running")
    for film in films:
        for orientation in orientations:
            delivery_config = {**config, "width": 1080 if orientation == "portrait" else 1920,
                               "height": 1920 if orientation == "portrait" else 1080}
            root, timeline = compile_timeline(delivery_config, store, film["script"], film["timing"], film["storyboard"], images, evidence, sources, film["requested_seconds"], creative_brief=brief, music=film["music"])
            with resource_slot("remotion-render", config.get("render_slots", 2)):    # parallel chapters share the CPU
                video = render(delivery_config, store, root, timeline)
            outputs.append({"requested_seconds": film["requested_seconds"], "actual_seconds": film["actual_seconds"],
                            "orientation": orientation, "point_count": film["point_count"], "visual_metrics": visual_metrics(film["storyboard"], film["script"], film["timing"]),
                            "music_engine": music_engine, "resolved_mood": brief["resolved_mood"],
                            "needs_expert_review_assets": sorted({s['asset_id'] for s in film['storyboard']['scenes'] if s.get('asset_id') in images and images[s['asset_id']].get('needs_review')}),
                            "video": str(video), "timeline": str(timeline), "qa": str(root / "qa.json")})
    write(store.root / "delivery.json", {"subject": args.subject, "outputs": outputs, "review_mode": args.review_mode,
                                        "image_review": config.get('image_review', True),
                                        "person_reference_gaps": {a['id']: a['person_reference_status']['unavailable']
                                            for a in prompts['assets'] if a.get('person_reference_status', {}).get('unavailable')},
                                        "generation_mode": "chapter" if chapter_context else "short",
                                        "tts_engine": config["tts_engine"],
                                        "mood": mood, "resolved_mood": brief["resolved_mood"], "mood_intensity": intensity,
                                        "visual_balance": balance, "music_engine": music_engine,
                                        "human_acceptance": "pending", "reviewer_loop_adoption": "experimental pending measured evaluation"})
    print(json.dumps(outputs, indent=2))
    from .claims import write_claims
    try:
        write_claims(store.root / "claims-sources.json", args.subject,
                     [{"id": None, "title": args.subject, "project": store.root, "duration": d} for d in args.durations],
                     config.get("llm_configuration"))
    except (KeyError, OSError, ValueError, TypeError) as exc:     # provenance is a report; never block a delivery
        store.event("claims", "failed", {"error": str(exc)[:300]})
    from .delivery import preview
    destination = preview(store)
    stage("video", "succeeded", output_directory=str(destination))
    print(f"Portable review: {destination}")
