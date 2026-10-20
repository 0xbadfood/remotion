"""Subject-aware creative direction shared by scripts, visuals and music."""
from __future__ import annotations

import re

MOODS = {
    "auto": "Auto",
    "professional": "Professional",
    "warm": "Warm / Friendly",
    "funny": "Lightly Funny",
    "curious": "Curious / Wonder",
    "serious": "Serious / Documentary",
    "reflective": "Reflective / Sad",
    "inspiring": "Inspiring / Hopeful",
}
BALANCES = {"auto": "Auto", "image-led": "Image-led", "balanced": "Balanced", "explanation-led": "Explanation-led"}
INTENSITIES = {"restrained": "Restrained", "standard": "Standard", "strong": "Strong"}
BALANCE_SHARES = {"image-led": (.75, .90), "balanced": (.55, .75), "explanation-led": (.35, .55)}
MUSIC = {"none": "Off", "acestep": "Auto · ACE-Step"}


def creative_errors(value, requested_mood="auto", requested_balance="auto"):
    errors = []
    if value.get("resolved_mood") not in set(MOODS) - {"auto"}:
        errors.append("resolved_mood: resolve the mood to an available named preset")
    if requested_mood != "auto" and value.get("resolved_mood") != requested_mood:
        errors.append("resolved_mood: preserve the explicitly selected mood")
    if value.get("visual_balance") not in BALANCE_SHARES:
        errors.append("visual_balance: resolve to image-led, balanced or explanation-led")
    if requested_balance != "auto" and value.get("visual_balance") != requested_balance:
        errors.append("visual_balance: preserve the explicitly selected visual balance")
    for key in ["rationale", "narration_direction", "visual_style", "pacing_direction"]:
        if not isinstance(value.get(key), str) or not 1 <= len(value[key]) <= 1800:
            errors.append(f"{key}: provide a concise {key}")
    theme = value.get("theme", {})
    if not isinstance(theme, dict):
        theme = {}
    for key in ["background", "accent", "text"]:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(theme.get(key, ""))):
            errors.append(f"theme: theme.{key} must be a six-digit hex colour")
    music = value.get("music", {})
    if not isinstance(music, dict):
        music = {}
    if not isinstance(music.get("caption"), str) or not 1 <= len(music["caption"]) <= 1800:
        errors.append("music: provide a concise instrumental music caption")
    if isinstance(music.get("bpm"), bool) or not isinstance(music.get("bpm"), int) or not 40 <= music["bpm"] <= 180:
        errors.append("music: provide music bpm between 40 and 180")
    return errors


# Repairable units (repair_units): each brief field; each visual beat, which keeps its timeline anchors.
CREATIVE_UNITS = {"lists": {}, "fields": True}
BEAT_UNITS = {"lists": {"scenes": "id"}, "anchors": {"scenes": ["point_id", "start_word_id"]}, "operations": ["scenes"]}


def make_creative_brief(models, evidence, mood="auto", intensity="restrained", balance="auto", *, experimental=True):
    if mood not in MOODS or intensity not in INTENSITIES or balance not in BALANCES:
        raise ValueError("Unknown creative controls")
    task = (
        "Direct one coherent general-audience informational film from this evidence and the user's creative choices. "
        "Preserve facts and qualifications; runtime is advisory. Choose a subject-appropriate mood when Auto is selected. "
        "Respect explicit mood and visual balance. Mood can evolve across the story without unrelated style changes. "
        "Professional means clear and measured; warm means approachable; funny means restrained wit; curious means discovery; "
        "serious means thoughtful documentary; reflective means empathetic; inspiring means grounded hope. "
        "Choose image-led for lighter everyday subjects when useful; complexity or a factual comparison may need native graphics in any mood. "
        "All imagery is generated. Choose photographic, painterly or editorial treatment from the subject; never imply generated scenes are archival photographs. "
        "Specify natural narration wording and rhythm, without inserting stage directions into speech, changing voice identity, or forcing unsupported TTS emotions. "
        "Give a coherent instrumental underscore brief with restrained energy under speech, no vocals or choir, useful ending and modest emotional development. "
        "Choose a dark theme background with light readable text and a contrasting accent; photographic scenes keep text legible over dark gradients. "
        "Return {resolved_mood,visual_balance,rationale,narration_direction,visual_style,pacing_direction, "
        "theme:{background:'#RRGGBB',accent:'#RRGGBB',text:'#RRGGBB'}, "
        "music:{caption,bpm:integer,keyscale:'C Major' or another simple key,timesignature:'4'}}."
    )
    if (evidence.get("curriculum") or {}).get("kind") == "key_idea":
        from .curriculum import KEY_IDEA_CREATIVE_TASK
        task += KEY_IDEA_CREATIVE_TASK
    result = models.loop("creative-brief", task, {"evidence": evidence, "requested_mood": mood,
        "intensity": intensity, "requested_visual_balance": balance, "presets": MOODS,
        "resolved_visual_balances": list(BALANCE_SHARES)},
        lambda x: creative_errors(x, mood, balance), experimental=experimental, units=CREATIVE_UNITS)
    return {**result, "requested_mood": mood, "intensity": intensity, "requested_visual_balance": balance,
            "narration_strategy": "Mood-aware wording and rhythm with the selected reference voice; direct emotional TTS control is not validated."}


def visual_guidance(timing, brief):
    duration = timing["audio_info"]["duration_seconds"]
    balance = brief["visual_balance"]
    hold = 9.5 if brief["resolved_mood"] == "reflective" else 6.5 if brief["resolved_mood"] == "funny" else 7.0 if balance == "image-led" else 8.0
    beats = max(len(timing["points"]), round(duration / hold))
    low, high = BALANCE_SHARES[balance]
    return {"actual_seconds": duration, "suggested_beats": beats,
            "suggested_distinct_images": max(1, round(beats * (low + high) / 2)),
            "suggested_image_screen_time_share": [low, high], "suggested_mean_hold_seconds": hold,
            "policy": "Soft guides. Semantic boundaries, explanatory clarity and natural flow take priority; do not alter narration."}


def resolved_scenes(storyboard, script, timing):
    """Resolve word anchors into contiguous intervals, also accepting legacy pilots."""
    scenes = storyboard["scenes"]
    points = {p["id"]: p for p in script["points"]}
    words = timing["words"]
    positions = {w["id"]: i for i, w in enumerate(words)}
    point_words = {p["point_id"]: p["word_ids"] for p in timing["points"]}
    indices = [positions[s.get("start_word_id", point_words[s["point_id"]][0])] for s in scenes]
    duration = timing["audio_info"]["duration_seconds"]
    starts = [timing.get("start_seconds", 0.0)] + [words[i]["start"] for i in indices[1:]]   # a batch starts later
    ends = starts[1:] + [duration]
    result = []
    for i, (scene, start, end, index) in enumerate(zip(scenes, starts, ends, indices)):
        stop = indices[i + 1] if i + 1 < len(indices) else len(words)
        point = points[scene["point_id"]]
        result.append({**scene, "id": scene.get("id", point["id"]), "start": start, "end": end,
                       "covered_narration": " ".join(w["word"] for w in words[index:stop]),
                       "fact_ids": point["fact_ids"]})
    return result


def beat_errors(value, script, timing, figures=None):
    """Validate coverage and references, never impose creative image-count quotas. `figures`: the film's figure
    options (figures.options); without them a figure beat is not allowed."""
    scenes = value.get("scenes", [])
    if not isinstance(scenes, list) or not scenes or len(scenes) > 120:
        return ["Need 1–120 executable visual beats"]
    if any(not isinstance(s, dict) for s in scenes):
        return ["Every visual beat must be an object"]
    errors = []
    point_ids = [p["id"] for p in script["points"]]
    point_words = {p["point_id"]: p["word_ids"] for p in timing["points"]}
    positions = {w["id"]: i for i, w in enumerate(timing["words"])}
    collapsed, indices = [], []
    ids = [s.get("id") for s in scenes]
    if len(set(ids)) != len(scenes):
        errors.append("Visual beat IDs must be unique; repeated: " + ", ".join(sorted({str(i) for i in ids if ids.count(i) > 1})))
    for scene in scenes:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", str(scene.get("id", ""))):
            errors.append(f"{scene.get('id')!r}: use a short alphanumeric visual beat ID")
        point_id, anchor = scene.get("point_id"), scene.get("start_word_id")
        if point_id not in point_words or anchor not in point_words.get(point_id, []):
            errors.append(f"{scene.get('id')}: anchor must be an aligned word in its point")
            continue
        if not collapsed or collapsed[-1] != point_id:
            collapsed.append(point_id)
            if anchor != point_words[point_id][0]:
                errors.append(f"First beat of {point_id} must start at its first word")
        indices.append(positions[anchor])
        kind, sid = scene.get("kind"), scene.get("id")
        if kind == "image":
            if not isinstance(scene.get("image_brief"), str) or not 1 <= len(scene["image_brief"]) <= 1800:
                errors.append(f"{sid}: image beats need a subject-aware image_brief")
        elif kind in {"flow", "compare", "cycle"}:
            n = 2 if kind == "compare" else 3
            labels, details = scene.get("labels"), scene.get("details")
            if not isinstance(labels, list) or not isinstance(details, list):
                errors.append(f"{sid}: diagram labels and details must be lists")
                continue
            if len(labels) != n or len(details) != n:
                errors.append(f"{sid}: {kind} needs {n} labels and details")
            if any(not isinstance(v, str) or len(v.split()) > 4 for v in labels) or any(not isinstance(v, str) or len(v.split()) > 10 for v in details):
                errors.append(f"{sid}: diagram labels/details are too long")
        elif kind == "figure":
            from .figures import scene_errors
            errors.extend(scene_errors(scene, figures))
        elif kind == "animation":
            pass                                      # checked with its words below (animations.scene_errors)
        else:
            errors.append(f"{sid}: unsupported visual beat kind")
        for key, limit in [("heading", 7), ("summary", 12)]:
            if not isinstance(scene.get(key), str) or len(scene[key].split()) > limit:
                errors.append(f"{sid}: {key} must be a string of at most {limit} words")
        if scene.get("motion") not in {"push", "pull", "drift", "hold"}:
            errors.append(f"{sid}: unsupported motion")
        if scene.get("copy_density") not in {"minimal", "standard"}:
            errors.append(f"{sid}: set copy_density to minimal or standard")
        if not scene.get("rationale"):
            errors.append(f"{sid}: explain the visual purpose of the beat")
    if any(s.get("action") is not None for s in scenes) and not errors:
        errors.extend(action_errors(value, script, timing))
    if any(s.get("kind") == "animation" for s in scenes) and not errors:
        from .animations import scene_errors as animation_errors
        errors.extend(animation_errors(scenes, timing, figures))
    if collapsed != point_ids:
        missing = [p for p in point_ids if p not in collapsed]
        errors.append("Visual beats must cover every narration point in unchanged order; expected " + " ".join(point_ids) +
                      ", got " + " ".join(collapsed) + (f" (missing {', '.join(missing)})" if missing else ""))
    if indices != sorted(set(indices)):
        anchored = [s for s in scenes if s.get("start_word_id") in positions]
        bad = [b.get("id") for a, b in zip(anchored, anchored[1:]) if positions[b["start_word_id"]] <= positions[a["start_word_id"]]]
        errors.append("Word anchors must increase strictly; no repeats or backward cuts" + (f" (at {', '.join(map(str, bad))})" if bad else ""))
    if not errors:
        if any(s["end"] <= s["start"] for s in resolved_scenes(value, script, timing)):
            errors.append("Visual beat intervals must have positive duration")
    return errors


ACTION_SECONDS = (3.0, 10.0)      # a motion (LTX) clip runs about as long as its beat: 3-10 s keeps it lively


def action_errors(value, script, timing):
    """Motion beats (key-idea films): an image beat whose still starts a short video of what happens in it."""
    errors = []
    for scene in resolved_scenes(value, script, timing):
        action = scene.get("action")
        if action is None:
            continue
        sid = scene.get("id")
        if scene.get("kind") != "image":
            errors.append(f"{sid}: only image beats can carry an action")
        elif not isinstance(action, str) or not 4 <= len(action.split()) <= 70:
            errors.append(f"{sid}: action must say in 4-70 words what visibly happens during the beat")
        seconds = scene["end"] - scene["start"]
        low, high = ACTION_SECONDS
        if not low <= seconds <= high + .5:
            errors.append(f"{sid}: an action beat lasts {low:.0f}-{high:.0f} s of narration; this one has {seconds:.1f} s "
                          f"({'split the passage into more beats' if seconds > high else 'join it with the next sentence'})")
    return errors


def visual_metrics(storyboard, script, timing):
    scenes = resolved_scenes(storyboard, script, timing)
    total = timing["audio_info"]["duration_seconds"]
    images = [s for s in scenes if s["kind"] == "image"]
    holds = {}
    for s in images:
        asset_id = s.get("asset_id", s["id"])
        holds[asset_id] = holds.get(asset_id, 0) + s["end"] - s["start"]
    return {"actual_seconds": total, "visual_beats": len(scenes), "point_count": len(script["points"]),
            "distinct_images": len(holds), "image_screen_time_share": round(sum(holds.values()) / total, 4),
            "longest_hold_seconds": round(max(s["end"] - s["start"] for s in scenes), 3),
            "asset_screen_time_seconds": {k: round(v, 3) for k, v in holds.items()},
            "graphic_justifications": [{"id": s["id"], "rationale": s["rationale"]} for s in scenes if s["kind"] != "image"]}


ABBREVIATIONS = {"dr.", "mr.", "mrs.", "ms.", "st.", "gen.", "col.", "lt.", "maj.", "capt.", "sgt.", "adm.", "prof.",
                 "no.", "vs.", "e.g.", "i.e.", "c.", "ca.", "jr.", "sr.", "u.s.", "u.k.", "mt.", "ft."}


def ends_sentence(word, following):
    if word.casefold() in ABBREVIATIONS or re.fullmatch(r"[A-Z]\.", word):     # "Gen.", "J." (initial)
        return False
    return bool(re.search(r"[.!?][\"'\u201d\u2019)\]]*$", word)) and (not following or following[:1].isupper() or following[:1].isdigit())


def sentence_timing(point, words, point_end):
    """Split one aligned point into sentences: [{start_word_id, start, seconds, text}]."""
    by_id = {w["id"]: w for w in words}
    rows = [by_id[i] for i in point["word_ids"] if i in by_id]
    sentences, current = [], []
    for index, word in enumerate(rows):
        current.append(word)
        following = rows[index + 1]["word"] if index + 1 < len(rows) else ""
        if ends_sentence(word["word"], following):
            sentences.append(current); current = []
    if current:
        sentences.append(current)
    result = []
    for index, sentence in enumerate(sentences):
        end = sentences[index + 1][0]["start"] if index + 1 < len(sentences) else point_end
        result.append({"start_word_id": sentence[0]["id"], "start": round(sentence[0]["start"], 2),
                       "seconds": round(end - sentence[0]["start"], 2), "text": " ".join(w["word"] for w in sentence)})
    return result


def make_visual_beats(models, film, evidence, brief, *, experimental=True):
    from .curriculum import key_idea_beat_errors
    task = (
        "Plan visual beats for this exact MFA-aligned narration, following the shared creative brief. "
        "One narration point may contain several shots. Longer audio needs proportionate visual coverage, not stretched identical stills. "
        "Use the per-point shot suggestions: a 13–16-second point containing multiple ideas usually benefits from two views at a meaningful boundary. "
        "Each narration point lists its sentences with start time and duration. A beat starts at a sentence: copy that sentence's "
        "start_word_id exactly into the beat's start_word_id. The first beat of every point starts at its first sentence; further "
        "beats can start at later sentences within that point. "
        "Cover all points in their existing order. Start/end seconds are derived by code, not invented. "
        "Soft beat/image guides and image screen-time shares are suggestions, not quotas; use useful distinct images and let diagrams remain readable. "
        "Choose generated imagery whenever scenes, objects, people or details explain the passage; use graphics only when an actual mechanism or comparison benefits. "
        "Do not make every factual passage a slide. All images are generated; subject determines photographic, editorial or painterly treatment. "
        "A spoken percentage can use a native banner over contextual imagery; it does not automatically need a chart. Earlier script visual_intent fields are suggestions, not binding. "
        "Keep raster image_brief free of exact labels, charts, arrows and requested overlays: banners and diagrams are composited later. "
        "Prefer one coherent natural scene per image, rather than split screens, collages or informational panels. Multiple views should be separate word-anchored beats. "
        "Image beats need a concrete image_brief tied to the covered words, with consistent visual world and useful changes of view. "
        "For image-led light subjects prefer copy_density=minimal; headings can be short and summary empty. "
        "For flow/cycle provide three labels (<=4 words) and details (<=10 words); compare needs two, without an implied causal arrow. "
        "Do not invent scientific/historical details. Preserve source qualifications. Never force narration compression for visual pacing. "
        "Return {scenes:[{id:'S01',point_id:'P01',start_word_id:'exact word ID',kind:'image'|'flow'|'compare'|'cycle', "
        "heading:'<=7 words',summary:'<=12 words or empty',labels:[],details:[],motion:'push'|'pull'|'drift'|'hold', "
        "copy_density:'minimal'|'standard',rationale,image_brief:'for images, else empty'}]}."
    )
    from .figures import FIGURE_TASK, options, planner_options
    figures = options(evidence)
    if figures:                                       # chemistry films: exact figures as a beat kind
        from .animations import ANIMATION_TASK, options as animation_options
        kinds = "'figure'|'animation'" if animation_options(figures) else "'figure'"
        task = task.replace("kind:'image'|'flow'|'compare'|'cycle'", "kind:'image'|'flow'|'compare'|'cycle'|" + kinds)
        task += FIGURE_TASK + (ANIMATION_TASK if animation_options(figures) else "")
    if (evidence.get("curriculum") or {}).get("kind") == "key_idea":
        from .curriculum import KEY_IDEA_BEATS_TASK
        task += KEY_IDEA_BEATS_TASK
    guidance = visual_guidance(film["timing"], brief)
    aligned = film["timing"]["points"]
    starts = [p["start"] for p in aligned]
    ends = starts[1:] + [film["actual_seconds"]]
    # Sentence timing only: the planner anchors beats at sentence starts, so the per-word MFA alignment (most of the
    # prompt's tokens) is not sent. Code still resolves exact word timings from the anchor.
    script = {**film["script"], "points": [{k: v for k, v in point.items() if k != "narration"} for point in film["script"]["points"]]}
    context = {"evidence": evidence, "creative_brief": brief, "script": script, "guidance": guidance,
               "narration_points": [{"point_id": p["point_id"], "seconds": round(end-start, 2),
                    "suggested_shots": max(1, round((end-start)/guidance["suggested_mean_hold_seconds"])),
                    "sentences": sentence_timing(p, film["timing"]["words"], end)}
                    for p,start,end in zip(aligned,starts,ends)]}
    if figures:
        context["figure_options"] = planner_options(figures)
    if (evidence.get("curriculum") or {}).get("kind") == "key_idea":
        return batched_beats(models, film, evidence, task, context, figures, experimental)
    return models.loop(f"visual-beats-{film['requested_seconds']}", task, context,
                      lambda x: beat_errors(x, film["script"], film["timing"], figures)
                      + key_idea_beat_errors(x, evidence), experimental=experimental,
                      metrics=lambda x: visual_metrics(x, film["script"], film["timing"]), units=BEAT_UNITS)


BATCH_POINTS = 6


def batch_timing(timing, points, start, end):
    """The film's timing cut down to some points: their words, starting at `start`, ending at `end`."""
    ids = {w for p in timing["points"] if p["point_id"] in points for w in p["word_ids"]}
    return {**timing, "points": [p for p in timing["points"] if p["point_id"] in points],
            "words": [w for w in timing["words"] if w["id"] in ids], "start_seconds": start,
            "audio_info": {**timing["audio_info"], "duration_seconds": end}}


def batched_beats(models, film, evidence, task, context, figures, experimental):
    """Key-idea films: scenes planned a few points at a time, each batch with the film's plan and the scenes just
    before it, so every batch gets the planner's full attention. Batches are validated as small films; the joined
    storyboard is validated as a whole and its beats numbered S01... in order."""
    from .curriculum import KEY_IDEA_FIGURES, key_idea_beat_errors
    script, timing = film["script"], film["timing"]
    rows = context["narration_points"]
    starts = [p["start"] for p in timing["points"]]
    ends = starts[1:] + [film["actual_seconds"]]
    scenes = []
    for first in range(0, len(rows), BATCH_POINTS):
        batch = rows[first:first + BATCH_POINTS]
        ids = [r["point_id"] for r in batch]
        part_script = {**script, "points": [p for p in script["points"] if p["id"] in ids]}
        start, end = (0.0 if first == 0 else starts[first]), ends[first + len(batch) - 1]
        part_timing = batch_timing(timing, ids, start, end)
        figures_left = KEY_IDEA_FIGURES - sum(1 for s in scenes if s.get("kind") == "figure")
        part_context = {**context, "narration_points": batch, "script": {**context["script"], "points": [
            p for p in context["script"]["points"] if p["id"] in ids]},
            "batch": {"number": first // BATCH_POINTS + 1, "of": -(-len(rows) // BATCH_POINTS),
                      "first_scene_id": f"S{len(scenes) + 1:02d}", "figure_beats_left": figures_left},
            "previous_scenes": scenes[-2:]}
        part_task = task + (" Plan only the narration points in this batch (context.batch); the film's other batches "
                            "are planned separately. Continue naturally from context.previous_scenes, number beats "
                            "from context.batch.first_scene_id, and use at most context.batch.figure_beats_left "
                            "figure beats.")

        def validate(x, part_script=part_script, part_timing=part_timing, figures_left=figures_left):
            extra = [s.get("id") for s in x.get("scenes") or [] if isinstance(s, dict) and s.get("kind") == "figure"]
            return beat_errors(x, part_script, part_timing, figures) + (
                [f"{', '.join(map(str, extra))}: only {figures_left} figure beats are left for this batch"]
                if len(extra) > figures_left else [])
        part = models.loop(f"visual-beats-{film['requested_seconds']}-b{first // BATCH_POINTS + 1}", part_task,
                           part_context, validate, experimental=experimental, units=BEAT_UNITS)
        scenes.extend(part["scenes"])
    for n, scene in enumerate(scenes, 1):
        scene["id"] = f"S{n:02d}"
    storyboard = {"scenes": scenes}
    errors = beat_errors(storyboard, script, timing, figures) + key_idea_beat_errors(storyboard, evidence)
    if errors:
        raise ValueError(f"Joined visual beats fail validation: {errors}")
    return storyboard


def library_from_beats(films, brief):
    """Allocate images from required shots; sharing never limits film coverage."""
    assets, shared = [], {}
    for film in films:
        points = {p["id"]: p for p in film["script"]["points"]}
        for scene in film["storyboard"]["scenes"]:
            if scene["kind"] != "image":
                continue
            prompt = scene["image_brief"]
            key = " ".join(prompt.split()).casefold()
            if key not in shared:
                asset_id = f"A{len(assets) + 1:03d}"
                shared[key] = asset_id
                assets.append({"id": asset_id, "purpose": scene["rationale"], "prompt": prompt,
                    "acceptance": ["Matches the exact narrated passage and subject", "Consistent with the shared creative brief", "No raster typography, charts or invented factual labels"],
                    "fact_ids": list(points[scene["point_id"]]["fact_ids"])})
            else:
                asset = next(a for a in assets if a["id"] == shared[key])
                asset["fact_ids"] = sorted(set(asset["fact_ids"]) | set(points[scene["point_id"]]["fact_ids"]))
            scene["asset_id"] = shared[key]
            if scene.get("action"):                   # the still is the first frame of a short video
                asset = next(a for a in assets if a["id"] == shared[key])
                asset["purpose"] += f" First frame of a short animation in which: {scene['action']}"
                asset["acceptance"] = asset["acceptance"] + [
                    "Shows the moment before the action starts, with room in the frame for it to happen"]
    return {"style": brief["visual_style"], "assets": assets, "creative_brief": brief}
