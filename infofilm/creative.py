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


SUBJECT_HOLD = 12.0     # science and maths films: a beat per idea (a scene or figure explained), not a shot per few seconds


def visual_guidance(timing, brief, subject=False):
    duration = timing["audio_info"]["duration_seconds"]
    balance = brief["visual_balance"]
    hold = 9.5 if brief["resolved_mood"] == "reflective" else 6.5 if brief["resolved_mood"] == "funny" else 7.0 if balance == "image-led" else 8.0
    if subject:
        hold = max(hold, SUBJECT_HOLD)
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
    order = [p["id"] for p in script["points"]]
    result = []
    for i, (scene, start, end, index) in enumerate(zip(scenes, starts, ends, indices)):
        stop = indices[i + 1] if i + 1 < len(indices) else len(words)
        point = points[scene["point_id"]]
        covered = order[order.index(point["id"]):order.index(scene["through_point_id"]) + 1] if scene.get("through_point_id") in points else [point["id"]]
        facts = list(dict.fromkeys(f for pid in covered for f in points[pid]["fact_ids"]))
        result.append({**scene, "id": scene.get("id", point["id"]), "start": start, "end": end,
                       "covered_narration": " ".join(w["word"] for w in words[index:stop]),
                       "fact_ids": facts})
    return result


CAMERA_TASK = (
    " Every image beat chooses its camera: camera:'still' or camera:'motion'. A still gets a gentle 2.5D depth camera "
    "over the picture; it is the better choice for a pose, a place, an object, a sign or arrangement that must stay "
    "exactly as drawn, a thoughtful or establishing moment, a close-up of hands or faces, or anything a video model "
    "might warp; it is not a lesser option. Choose motion only when the point of the beat is something visibly "
    "happening that a still cannot show (a walk from one place to another, a throw, a climb, a jump, water pouring): "
    "then action:'what visibly happens during the beat, in one or two plain sentences', and image_brief describes "
    "the moment before it; a video model animates the still for the beat, which lasts 3-10 seconds of narration. "
    "Decide beat by beat; a film whose every image is motion has chosen badly.")


STILLS_TASK = (
    " Every image beat is a still with a gentle 2.5D depth camera: set camera:'still' and no action on each (motion "
    "video is switched off for this film).")


def motion_on(config):
    """Motion beats (an image animated into a short video) are switched off by default (user, 2026-10-24: spend the
    time on the real thing, not on making video); config motion_beats: true turns them on. The planner's choice, its
    checks and the clip step stay the same either way."""
    return bool((config or {}).get("motion_beats", False))


def camera_errors(value, motion=True):
    """The planner's still-or-motion choice for each image beat (user, 2026-10-24, concept and key-idea films). Checked
    while planning only: storyboards saved before the choice existed stay valid (no action means a still)."""
    out = []
    for s in value.get("scenes") or []:
        if not isinstance(s, dict) or s.get("kind") != "image":
            continue
        camera = s.get("camera")
        if camera not in ("still", "motion"):
            out.append(f"{s.get('id')}: an image beat chooses camera:'still' (depth camera) or camera:'motion' (video)")
        elif camera == "motion" and not has_action(s):
            out.append(f"{s.get('id')}: a motion beat needs its action: what visibly happens")
        elif camera == "still" and has_action(s):
            out.append(f"{s.get('id')}: a still beat has no action; choose camera:'motion' if something must move")
        elif camera == "motion" and not motion:
            out.append(f"{s.get('id')}: motion video is switched off for this film: make it camera:'still' with no action")
    return out


def unsupported_kind(scene, sid, figures, linkwork):
    """Say what a beat of no known kind should have been (2026-10-25: a rotated writer flattened equation figures into
    the beat, {type:'equation', latex} with no kind, and put a library figure in as a beat of its own; 'unsupported
    visual beat kind' alone never told it how to fix them)."""
    kinds = ["image", "flow", "compare", "cycle"] + (["figure"] if figures else []) + (["linkwork"] if linkwork else [])
    hint = ""
    if figures and scene.get("type") in {"equation", "library", "biology", "molecule", "reaction"}:
        hint = (f": a figure beat is kind:'figure' with its spec inside it, figure:{{type:'{scene['type']}', "
                + ("latex:'...'" if scene["type"] == "equation" else "id:'...'" if scene["type"] in {"library", "biology"} else "...")
                + "}, not type and its fields on the beat itself")
    elif linkwork and isinstance(scene.get("verb"), str):
        hint = ": a Linkwork beat is kind:'linkwork' with its spec inside it, linkwork:{verb:'...', ...}"
    if not re.fullmatch(r"S\d{2,3}", str(sid)):
        hint += (f"; beat ids are S01, S02, ...: {sid!r} looks like a library figure's id, which belongs inside a "
                 "figure beat as figure:{type:'library', id}" if figures and str(sid).startswith("Fig") else
                 "; beat ids are S01, S02, ...")
    return f"{sid}: unsupported visual beat kind {scene.get('kind')!r}; use one of {', '.join(kinds)}{hint}"


def anchor_errors(scenes, script, timing):
    """(per-beat errors, order errors) of beats' anchors: ids, aligned start words, points covered in order (a
    linkwork or figure beat may carry on through later points: through_point_id), strictly increasing cuts. Shared
    by full beats and the beat sheet's rows."""
    errors, order = [], []
    point_ids = [p["id"] for p in script["points"]]
    point_words = {p["point_id"]: p["word_ids"] for p in timing["points"]}
    positions = {w["id"]: i for i, w in enumerate(timing["words"])}
    collapsed, indices, spanned = [], [], {}
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
        if point_id in spanned:
            errors.append(f"{scene.get('id')}: {point_id} is carried by {spanned[point_id]} (its through_point_id); "
                          f"start this beat at a later point, or end {spanned[point_id]}'s through_point_id earlier")
            continue
        if not collapsed or collapsed[-1] != point_id:
            collapsed.append(point_id)
            if anchor != point_words[point_id][0]:
                errors.append(f"First beat of {point_id} must start at its first word; to show something only from a "
                              f"later sentence of {point_id} (an equation, a figure), open {point_id} with another beat "
                              "and start that one at the sentence that states it")
        indices.append(positions[anchor])
        kind, sid = scene.get("kind"), scene.get("id")
        through = scene.get("through_point_id")
        if through is not None and through != point_id:
            later = point_ids[point_ids.index(point_id) + 1:]
            if through not in later:
                errors.append(f"{sid}: through_point_id must be a later narration point ({', '.join(later[:3]) or 'none left'}"
                              f"{', ...' if len(later) > 3 else ''})")
            elif kind not in SPANNING:
                errors.append(f"{sid}: only {' or '.join(SPANNING)} beats carry a passage across points; a {kind} beat "
                              "ends with its point")
            else:
                for pid in later[:later.index(through) + 1]:
                    spanned[pid] = sid
                    collapsed.append(pid)
    if collapsed != point_ids:
        missing = [p for p in point_ids if p not in collapsed]
        order.append("Visual beats must cover every narration point in unchanged order; expected " + " ".join(point_ids) +
                      ", got " + " ".join(collapsed) + (f" (missing {', '.join(missing)})" if missing else ""))
    if indices != sorted(set(indices)):
        anchored = [s for s in scenes if s.get("start_word_id") in positions]
        bad = [b.get("id") for a, b in zip(anchored, anchored[1:]) if positions[b["start_word_id"]] <= positions[a["start_word_id"]]]
        order.append("Word anchors must increase strictly; no repeats or backward cuts" + (f" (at {', '.join(map(str, bad))})" if bad else "")
                     + ": two beats never start at one sentence; show both in the one beat (an equation beat can show two "
                       "results in turn), or start the second at a later sentence")
    return errors, order


def beat_errors(value, script, timing, figures=None, linkwork=None):
    """Validate coverage and references, never impose creative image-count quotas. `figures`: the film's figure
    options (figures.options); without them a figure beat is not allowed. `linkwork`: {config, cards} when the film
    may use Linkwork beats (linkwork_beats.py)."""
    scenes = value.get("scenes", [])
    if not isinstance(scenes, list) or not scenes or len(scenes) > 120:
        return ["Need 1–120 executable visual beats"]
    if any(not isinstance(s, dict) for s in scenes):
        return ["Every visual beat must be an object"]
    errors, order = anchor_errors(scenes, script, timing)
    point_words = {p["point_id"]: p["word_ids"] for p in timing["points"]}
    for scene in scenes:
        if scene.get("start_word_id") not in point_words.get(scene.get("point_id"), []):
            continue
        kind, sid = scene.get("kind"), scene.get("id")
        if kind == "image":
            if not isinstance(scene.get("image_brief"), str) or not 1 <= len(scene["image_brief"]) <= 1800:
                errors.append(f"{sid}: image beats need a subject-aware image_brief")
            elif figures:
                from .figures import image_errors
                errors.extend(image_errors(scene, figures))
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
        elif kind == "linkwork" and linkwork:
            pass                                      # compiled and checked with its beat's duration below
        else:
            errors.append(unsupported_kind(scene, sid, figures, linkwork))
        for key, limit in [("heading", 7), ("summary", 12)]:
            if not isinstance(scene.get(key), str):
                errors.append(f"{sid}: {key} must be a string of at most {limit} words")
            elif (count := len(scene[key].split())) > limit:
                # 2026-10-29: a reviewer rewrote one summary to 13-16 words three times running when told only the limit
                errors.append(f"{sid}: {key} has {count} words, at most {limit}: keep only its main claim in {limit} words "
                              f"or fewer; the narration carries the conditions and contrasts")
        if scene.get("motion") not in {"push", "pull", "drift", "hold"}:
            errors.append(f"{sid}: unsupported motion")
        if scene.get("copy_density") not in {"minimal", "standard"}:
            errors.append(f"{sid}: set copy_density to minimal or standard")
        if not scene.get("rationale"):
            errors.append(f"{sid}: explain the visual purpose of the beat")
    if any(has_action(s) for s in scenes) and not errors:
        errors.extend(action_errors(value, script, timing))
    if any(s.get("kind") == "animation" for s in scenes) and not errors:
        from .animations import scene_errors as animation_errors
        errors.extend(animation_errors(scenes, timing, figures))
    errors += order
    if not errors:
        if any(s["end"] <= s["start"] for s in resolved_scenes(value, script, timing)):
            errors.append("Visual beat intervals must have positive duration")
    if not errors and linkwork and any(s.get("kind") == "linkwork" for s in scenes):
        from .linkwork_beats import beat_errors as linkwork_errors
        errors.extend(linkwork_errors(linkwork["config"], resolved_scenes(value, script, timing), linkwork["cards"]))
    return errors


SPANNING = ("linkwork", "figure")     # beats that may carry their explanation on through later points


def has_action(scene):
    """A beat's action, if it has one: planners write action:'' (or null) on beats without one, which means none."""
    action = scene.get("action")
    return not (action is None or (isinstance(action, str) and not action.strip()))


ACTION_SECONDS = (3.0, 10.0)      # a motion (LTX) clip runs about as long as its beat: 3-10 s keeps it lively


def action_errors(value, script, timing):
    """Motion beats (key-idea films): an image beat whose still starts a short video of what happens in it."""
    errors = []
    for scene in resolved_scenes(value, script, timing):
        action = scene.get("action")
        if not has_action(scene):
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


SUBJECT_PACING_TASK = (
    " Pacing: this is a science or maths film whose pictures explain, so plan ONE BEAT PER IDEA, not a new shot every "
    "few seconds: a Linkwork scene or a figure usually stays on screen 10-20 s while the narration explains it, and "
    "the per-point shot suggestions are a ceiling here, not a target. When one explanation runs across narration "
    "points, keep its Linkwork or figure beat on screen through them: set through_point_id to the last point it "
    "covers (those points then have no beats of their own). Never split one explanation into short shots just to "
    "change the picture. A scene may take a little longer than its words: the narration waits for it. Do not carry a "
    "beat through a point that states an equation: that point needs its own equation figure beat, and the Linkwork "
    "scene can carry on after it as a new beat.")


def make_visual_beats(models, film, evidence, brief, *, experimental=True):
    from .curriculum import key_idea_beat_errors
    task = (
        "Plan visual beats for this exact MFA-aligned narration, following the shared creative brief. "
        "One narration point may contain several shots. Longer audio needs proportionate visual coverage, not stretched identical stills. "
        "Use the per-point shot suggestions: a 13–16-second point containing multiple ideas usually benefits from two views at a meaningful boundary. "
        "Each narration point lists its sentences with start time and duration. A beat starts at a sentence: copy that sentence's "
        "start_word_id exactly into the beat's start_word_id. The first beat of every point starts at its first sentence; further "
        "beats can start at later sentences within that point. When what a beat shows is stated only in a later sentence of "
        "its point (an equation, a figure, a result), open the point with another beat and start this one at that sentence. "
        "Reviewing: never ask for a point's first beat to start later than its first sentence (code refuses it); if something "
        "appears before it is stated, ask for an opening beat before it instead (2026-10-29: the scattering film's sheet "
        "ran out of tries on exactly this). "
        "One sentence opens at most one beat: two results stated in the same sentence share that beat (an equation beat shows "
        "both, one after the other); reviewing, never ask for a second beat at the sentence another beat starts at "
        "(2026-10-29: the electric power film's sheet ran out of tries asking for P = V²/R apart from P = I²R, both in one sentence). "
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
        "copy_density:'minimal'|'standard',rationale,image_brief:'for images, else empty',camera:'still'|'motion' "
        "(image beats only),action:'for motion beats only',through_point_id:'optional: the last point a linkwork or "
        "figure beat covers'}]}."
    )
    from .figures import options, planner_options, task_for
    figures = options(evidence)
    if figures:                                       # chemistry and biology films: exact figures as a beat kind
        from .animations import ANIMATION_TASK, options as animation_options
        kinds = "'figure'|'animation'" if animation_options(figures) else "'figure'"
        task = task.replace("kind:'image'|'flow'|'compare'|'cycle'", "kind:'image'|'flow'|'compare'|'cycle'|" + kinds)
        task += task_for(figures) + (ANIMATION_TASK if animation_options(figures) else "")
    config = getattr(models, "config", None) or {}
    task += CAMERA_TASK if motion_on(config) else STILLS_TASK
    linkwork = linkwork_for(config, evidence)
    if linkwork:                                      # physics and maths films: exact code-drawn scenes as a beat kind
        from .linkwork_beats import TASK as LINKWORK_TASK
        task = task.replace("kind:'image'|'flow'|'compare'|'cycle'", "kind:'image'|'flow'|'compare'|'cycle'|'linkwork'")
        task += LINKWORK_TASK
    if (evidence.get("curriculum") or {}).get("kind") == "key_idea":
        from .curriculum import KEY_IDEA_BEATS_TASK, KEY_IDEA_MOTION
        task += KEY_IDEA_BEATS_TASK.replace("{motion}", KEY_IDEA_MOTION if motion_on(config) else "")
    subject = bool(figures or linkwork) and not motion_on(config)
    if subject:                                       # science and maths: the picture explains, video is not the medium
        task += SUBJECT_PACING_TASK
    guidance = visual_guidance(film["timing"], brief, subject)
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
    replan = (getattr(models, "config", None) or {}).get("scene_replan")
    if replan:                                        # asked to plan again: a new request, so not the cached plan
        context["replan"] = {"number": replan, "note": "A fresh scene plan was asked for."}
    if linkwork:
        from .linkwork_beats import planner_options as linkwork_options
        context["linkwork_options"] = linkwork_options(linkwork["cards"], linkwork["config"])
    if (evidence.get("curriculum") or {}).get("kind") == "key_idea":
        return batched_beats(models, film, evidence, task, context, figures, experimental, linkwork)
    if config.get("scene_planning", "staged") == "staged" and len(aligned) > BATCH_POINTS:
        return staged_beats(models, film, evidence, task, context, figures, experimental, linkwork)
    return models.loop(f"visual-beats-{film['requested_seconds']}", task, context,
                      lambda x: beat_errors(x, film["script"], film["timing"], figures, linkwork)
                      + camera_errors(x, motion_on(config)) + key_idea_beat_errors(x, evidence), experimental=experimental,
                      metrics=lambda x: visual_metrics(x, film["script"], film["timing"]), units=BEAT_UNITS)


BATCH_POINTS = 6


def batch_timing(timing, points, start, end):
    """The film's timing cut down to some points: their words, starting at `start`, ending at `end`."""
    ids = {w for p in timing["points"] if p["point_id"] in points for w in p["word_ids"]}
    return {**timing, "points": [p for p in timing["points"] if p["point_id"] in points],
            "words": [w for w in timing["words"] if w["id"] in ids], "start_seconds": start,
            "audio_info": {**timing["audio_info"], "duration_seconds": end}}


def linkwork_for(config, evidence):
    """{config, cards} for a film that may use Linkwork beats (the shortlisted verb cards), else None."""
    from .linkwork_beats import options, shortlist
    opts = options(evidence)
    if not opts:
        return None
    cards = shortlist(config, evidence, opts)
    return {"config": config, "cards": cards} if cards else None


def batched_beats(models, film, evidence, task, context, figures, experimental, linkwork=None):
    models_config = getattr(models, "config", None) or {}
    """Key-idea films: scenes planned a few points at a time, each batch with the film's plan and the scenes just
    before it, so every batch gets the planner's full attention. Batches are validated as small films; the joined
    storyboard is validated as a whole and its beats numbered S01... in order."""
    from .curriculum import KEY_IDEA_FIGURES, key_idea_animation_errors, key_idea_beat_errors
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
            return beat_errors(x, part_script, part_timing, figures, linkwork) + camera_errors(x, motion_on(models_config)) + key_idea_animation_errors(x.get("scenes")) + (
                [f"{', '.join(map(str, extra))}: only {figures_left} figure beats are left for this batch"]
                if len(extra) > figures_left else [])
        part = models.loop(f"visual-beats-{film['requested_seconds']}-b{first // BATCH_POINTS + 1}", part_task,
                           part_context, validate, experimental=experimental, units=BEAT_UNITS)
        scenes.extend(part["scenes"])
    for n, scene in enumerate(scenes, 1):
        scene["id"] = f"S{n:02d}"
    storyboard = {"scenes": scenes}
    errors = beat_errors(storyboard, script, timing, figures, linkwork) + key_idea_beat_errors(storyboard, evidence)
    if errors:
        raise ValueError(f"Joined visual beats fail validation: {errors}")
    return storyboard


IMPORTANCE = ("core", "support")
SHEET_UNITS = {"lists": {"beats": "id"}, "anchors": {"beats": ["point_id", "start_word_id"]}, "operations": ["beats"]}
STAGED_BEATS = 6        # pass 2 writes this many beats per call (whole points only)
SHEET_INTENT_WORDS = 45 # the free-fall sheet (2026-10-26) lost 4 of 9 attempts to intents of 31-39 words at a cap of 30

SHEET_TASK = (
    " THIS CALL IS PASS 1 OF 2: THE BEAT SHEET. The rules above describe the film's full beats; in this pass decide "
    "only the film's shape. The full beats (specs, briefs, labels) are written afterwards from your sheet, a few at "
    "a time, so here give for every beat: id ('S01', 'S02', ... in order); point_id and start_word_id, as the rules "
    "above say; through_point_id when a linkwork or figure beat carries one explanation on through later points; "
    "kind; verb (linkwork beats: one of linkwork_options.verbs, whose typical_seconds says how long its scene "
    "usually runs and shortest_seconds, when given, how short it can be made; one linkwork beat can also set two "
    "cases side by side, such as a concave and a convex mirror, which pass 2 writes as a compound of two specs); figure (figure beats: 'equation', or the id of a figure_options.library figure); intent (one "
    "line: what the viewer sees and learns); importance ('core' for the ideas the film exists to teach, 'support' "
    "for the rest); and, for a core beat that needs time beyond its words, room: 'hold' (a pause to take in an "
    "equation or figure), 'look' or 'notice' (the narrator adds 'Look carefully.' or 'Notice what changes.' while "
    "the scene plays on), or 'replay' (a Linkwork scene plays again after 'Watch it once more.'; at most {replays} "
    "in the film, for the motion that is the heart of the idea). Give the core ideas the time they need: a longer "
    "beat, a scene carried through the next point, or room; never rush them to fit more beats in. A figure or "
    "scene that shows what a later sentence states (a rule's labelled mapping, a result, a case) starts at that "
    "sentence; when it is not the point's first sentence, open the point with another beat before it "
    "(2026-10-29: Fleming's rule figure, its fingers labelled, opened the point at 'stretch your fingers', five "
    "tries running, before the narration named what each finger shows). "
    "Reviewing: judge the film's shape: vector first, one beat per idea, whether every core idea gets the time and "
    "room it needs, and whether each beat's choice fits its passage. A room's cue ('Look carefully.', 'Notice what "
    "changes.', 'Watch it once more.') is spoken by code when the film is voiced, so it is never in the narration "
    "you see: never report it missing. "
    "Return ONLY {beats:[{id, point_id, start_word_id, through_point_id (optional), kind, verb (linkwork), figure "
    "(figure), intent, importance, room (optional)}]}; the scene shape above is for pass 2, not this call.")

STAGED_SCENES_TASK = (
    " THIS CALL IS PASS 2: write the full beats for exactly the rows of context.beat_sheet, one beat per row in the "
    "same order, keeping each row's id, point_id, start_word_id, through_point_id, kind and its verb (the linkwork "
    "spec's verb) or figure. The row's intent is what the beat must show: write its heading, summary, rationale and "
    "its spec or brief for that. Code keeps the rows' importance and room (the narration waits for a core beat's "
    "room). Continue naturally from context.previous_scenes; context.next_beats come after this batch, so do not "
    "show their ideas yet. The film's other beats are planned in other calls. Reviewing: each beat's point, start "
    "word, kind and its verb or figure were chosen and reviewed in pass 1 and are fixed here (code refuses a change), "
    "so never report the choice of kind, verb or figure, or where a beat starts, or ask for another; judge how well "
    "each beat's heading, summary and spec or brief carry its row's intent from where it starts, and when the "
    "narration names something only later in the beat, ask for the picture to fit (general until then), not for the "
    "beat to move (2026-10-28: C5 of Class 10 Light failed when this review kept asking for a Linkwork beat where the "
    "sheet had fixed an image; 2026-10-29: the scattering film's, when it kept asking to move a beat's start).")


def sheet_options(context, typical, shortest=None):
    """The beat sheet's context: the verbs without their fields and examples (pass 2 gets those), with each one's
    typical length, and its shortest when that is shorter; the rest as pass 2 sees it."""
    out, shortest = dict(context), shortest or {}
    options = context.get("linkwork_options")
    if options:
        out["linkwork_options"] = {**options, "verbs": [
            # The options' names (not their docs, which pass 2 gets) show pass 1 what a verb can draw: C5 of Class 10
            # Light chose images for a coin and a pencil under water that refraction's object_depth draws.
            {**{k: v for k, v in c.items() if k not in ("fields", "example", "shortest")},
             **({"options": sorted(c["fields"])} if isinstance(c.get("fields"), dict) else {}),
             **({"typical_seconds": typical[c["name"]]} if c["name"] in typical else {}),
             **({"shortest_seconds": shortest[c["name"]]} if shortest.get(c["name"], 1e9) < typical.get(c["name"], 0) else {})}
            for c in options["verbs"]]}
    return out


def sheet_errors(value, script, timing, figures=None, linkwork=None, typical=None, room=None):
    # typical: {verb: the shortest its scene can be} (typical_seconds(..., "shortest")). Judging by the card's example
    # alone kept ray_diagram, whose example showed four object positions (25 s), out of every short beat until the
    # plan ran out of tries, although one position takes 8 s (Class 10 Light C2, 2026-10-28). Pass 2 still checks
    # each written spec's real length.
    """Code checks of the beat sheet: the beats' anchors and order (as full beats), their kinds and choices, and that
    each Linkwork verb's typical scene fits its beat and the narration's room."""
    from .narration_room import ROOMS, settings
    room = room or settings()
    rows = value.get("beats")
    if not isinstance(rows, list) or not rows or len(rows) > 120:
        return ["Need a beat sheet of 1-120 beats: {beats:[...]}"]
    if any(not isinstance(r, dict) for r in rows):
        return ["Every beat of the sheet must be an object"]
    for r in rows:   # {"type": "equation"} for "equation": the planner kept the wrapper three tries running (electric
        f = r.get("figure")                      # power film, 2026-10-29); one plain name inside it is that name
        names = [v for v in f.values() if isinstance(v, str)] if isinstance(f, dict) else []
        if isinstance(f, dict) and len(f) == 1 and len(names) == 1 and next(iter(f)) in ("type", "id", "name", "figure"):
            r["figure"] = names[0]
    errors, order = anchor_errors(rows, script, timing)
    kinds = {"image", "flow", "compare", "cycle"} | ({"figure"} if figures else set()) | ({"linkwork"} if linkwork else set())
    if figures:
        from .animations import options as animation_options
        kinds |= {"animation"} if animation_options(figures) else set()
    verbs = {c["name"] for c in (linkwork or {}).get("cards") or []}
    library = {f["id"] for f in (figures or {}).get("library", [])}
    text = lambda v: v if isinstance(v, str) else None  # an object where a name belongs is an error to report, not
    for r in rows:                                      # a crash (the PDA film, 2026-10-27: figure was a whole spec)
        sid, kind = r.get("id"), text(r.get("kind"))
        if not re.fullmatch(r"S\d{2,3}[a-z]{0,3}", str(sid)):         # S03b: a beat a repair added; renumbered after
            errors.append(f"{sid!r}: beat ids are S01, S02, ...")
        if kind not in kinds:
            errors.append(unsupported_kind(r, sid, figures, linkwork))
        elif kind == "linkwork" and text(r.get("verb")) not in verbs:
            errors.append(f"{sid}: verb must be one of linkwork_options.verbs ({', '.join(sorted(verbs))})")
        elif kind == "figure" and figures and text(r.get("figure")) != "equation" and text(r.get("figure")) not in library:
            errors.append(f"{sid}: figure is the id alone (a string): 'equation' or a figure_options.library id"
                          + (f" ({', '.join(sorted(library)[:8])})" if library else ""))
        intent = r.get("intent")
        if not isinstance(intent, str) or not 3 <= len(intent.split()) <= SHEET_INTENT_WORDS:
            errors.append(f"{sid}: intent is one line (3-{SHEET_INTENT_WORDS} words) of what the viewer sees and learns")
        if r.get("importance") not in IMPORTANCE:
            errors.append(f"{sid}: importance is core or support")
        if r.get("room") not in (None, "", *ROOMS):
            errors.append(f"{sid}: room is one of {', '.join(ROOMS)}, or left out")
        elif r.get("room") and r.get("importance") != "core":
            errors.append(f"{sid}: only a core beat gets room; this one is {r.get('importance')}")
        elif r.get("room") == "replay" and kind != "linkwork":
            errors.append(f"{sid}: only a linkwork beat can replay; give a {kind} beat 'hold' or 'look'")
    replays = [r.get("id") for r in rows if r.get("room") == "replay"]
    if len(replays) > room["max_replays"]:
        errors.append(f"{', '.join(map(str, replays))}: at most {room['max_replays']} replays in a film; keep them for "
                      "the motion that is the heart of the film")
    errors += order
    if not errors and typical:
        from .linkwork_beats import TAIL
        wait = room["max_beat_seconds"] if room["enabled"] else 0.0
        for r in resolved_scenes({"scenes": rows}, script, timing):
            need = typical.get(r.get("verb")) if r.get("kind") == "linkwork" else None
            length = r["end"] - r["start"]
            if need and need - TAIL > length + wait:
                errors.append(f"{r['id']}: a {r['verb']} scene needs at least {need - TAIL:.0f} s but this beat has "
                              f"{length:.1f} s (and the narration can wait {wait:.0f} s more): carry it through the "
                              "next point (through_point_id), start it a sentence earlier, or choose another verb")
    return errors


# A Linkwork beat whose verb cannot carry its sheet row's intent fails every round (2026-10-29, Equations of Motion: a
# moving dot asked to show "the signs given to v0, v, a, t and x matter"), and pass 2 may not change the kind. The
# stage's ladder: rung 2 tells the writer to draw what the verb can and let the words carry the point; rung 3, owned
# by code, makes it a quiet image beat under the writer's heading and summary, so one beat never stops a film.
LADDER_IMAGE = ("A quiet abstract editorial backdrop: soft layered fields of light and muted colour, gentle depth, no people, "
                "no objects, no text.")


def beat_ladder(rows):
    kinds = {r["id"]: r.get("kind") for r in rows}

    def ladder(rung, ident, value):
        if not isinstance(value, dict) or kinds.get(value.get("id")) != "linkwork":
            return None
        if rung == 2:
            return ("The sheet's verb has not carried this beat's intent after several tries: draw only what the verb can "
                    "show truly, and let the heading and summary state the rest in words.")
        keep = {k: value[k] for k in ("id", "point_id", "start_word_id", "through_point_id", "heading", "summary")
                if value.get(k)}
        return {**keep, "kind": "image", "labels": [], "details": [], "motion": "drift", "copy_density": "minimal",
                "camera": "still", "image_brief": LADDER_IMAGE, "fallback": "ladder",
                "rationale": "Code fallback: the verb could not show this point; the heading, summary and narration carry it."}
    return ladder


def sheet_ladder(rung, ident, value):
    """The beat sheet's ladder (2026-10-29, Stopping Distance: seven rounds at 70-79, each finding a new detail one
    row's intent left out). A row that keeps failing review is told to cover what its sentences say, in order; then it
    is kept as written: pass 2 writes the full beat from the narration itself and reviews it there."""
    if not isinstance(value, dict):
        return None
    if rung == 2:
        return ("This row has failed review several times: make its intent name every claim, value and comparison of the "
                "sentences it spans, each shown only from the sentence that says it.")
    return dict(value)


def match_errors(value, rows):
    """A pass-2 batch keeps its sheet rows: same beats, anchors, kinds and choices (a compound keeps the sheet's verb
    among its parts)."""
    from .linkwork_beats import spec_verbs
    scenes = value.get("scenes")
    if not isinstance(scenes, list) or len(scenes) != len(rows):
        return [f"Write exactly {len(rows)} beats, one per row of context.beat_sheet ({', '.join(r['id'] for r in rows)})"]
    errors = []
    for scene, row in zip(scenes, rows):
        if not isinstance(scene, dict):
            continue
        fallback = scene.get("fallback") == "ladder"          # code's own replacement keeps the anchors only
        for key in ("id", "point_id", "start_word_id", "through_point_id") + (() if fallback else ("kind",)):
            if (scene.get(key) or None) != (row.get(key) or None):
                errors.append(f"{row['id']}: keep the sheet's {key} {row.get(key)!r} (got {scene.get(key)!r})")
        if row.get("kind") == "linkwork" and not fallback and row.get("verb") not in (spec_verbs(scene.get("linkwork")) or []):
            errors.append(f"{row['id']}: the sheet chose the verb {row.get('verb')!r}; write its spec")
        if row.get("kind") == "figure" and isinstance(scene.get("figure"), dict):
            figure = scene["figure"]
            chosen = "equation" if figure.get("type") == "equation" else figure.get("id")
            if row.get("figure") and chosen != row.get("figure"):
                errors.append(f"{row['id']}: the sheet chose the figure {row.get('figure')!r}; draw that one")
    return errors


def sheet_batches(rows):
    """Consecutive rows in batches of about STAGED_BEATS, cut only where a new point starts."""
    batches, current = [], []
    for row in rows:
        if current and len(current) >= STAGED_BEATS and row["point_id"] != current[-1]["point_id"]:
            batches.append(current)
            current = []
        current.append(row)
    return batches + ([current] if current else [])


def staged_beats(models, film, evidence, task, context, figures, experimental, linkwork=None):
    """Concept films: planned in two passes (user, 2026-10-25). Pass 1 writes the beat sheet: anchors, kinds, verbs
    or figures, intent, importance and room; small enough to review whole, so the film's shape is judged once.
    Pass 2 writes the full beats a batch at a time from the sheet, each batch with only the verb cards it uses and its
    neighbours, so repair and rotation work on a few beats. The joined storyboard is validated as a whole; the sheet's
    importance and room ride on its beats for the narration room."""
    from .linkwork_beats import typical_seconds
    from .narration_room import settings as room_settings
    config = getattr(models, "config", None) or {}
    room = room_settings(config)
    script, timing = film["script"], film["timing"]
    typical = typical_seconds(linkwork["config"], linkwork["cards"]) if linkwork else {}
    shortest = typical_seconds(linkwork["config"], linkwork["cards"], "shortest") if linkwork else {}
    stage = f"visual-sheet-{film['requested_seconds']}"
    sheet = models.loop(stage, task + SHEET_TASK.replace("{replays}", str(room["max_replays"])),
                        sheet_options(context, typical, shortest),
                        lambda x: sheet_errors(x, script, timing, figures, linkwork, shortest, room),
                        experimental=experimental, units=SHEET_UNITS, ladder=sheet_ladder)
    # A targeted repair that adds a beat names it after its neighbour (S03b): the sheet is renumbered in order.
    rows = [{**r, "id": f"S{n:02d}"} for n, r in enumerate(sheet["beats"], 1)]
    order = [p["id"] for p in script["points"]]
    starts = [p["start"] for p in timing["points"]]
    ends = starts[1:] + [film["actual_seconds"]]
    batches = sheet_batches(rows)
    scenes = []
    for number, batch in enumerate(batches, 1):
        first = order.index(batch[0]["point_id"])
        last = max(order.index(r.get("through_point_id") or r["point_id"]) for r in batch)
        ids = order[first:last + 1]
        part_script = {**script, "points": [p for p in script["points"] if p["id"] in ids]}
        part_timing = batch_timing(timing, ids, 0.0 if first == 0 else starts[first], ends[last])
        part_context = {**context, "narration_points": [r for r in context["narration_points"] if r["point_id"] in ids],
                        "script": {**context["script"], "points": [p for p in context["script"]["points"] if p["id"] in ids]},
                        "beat_sheet": batch, "next_beats": (batches[number] if number < len(batches) else [])[:2],
                        "previous_scenes": scenes[-2:], "batch": {"number": number, "of": len(batches)}}
        if context.get("linkwork_options"):
            used = {r.get("verb") for r in batch if r.get("kind") == "linkwork"}
            part_context["linkwork_options"] = {**context["linkwork_options"], "verbs": [
                v for v in context["linkwork_options"]["verbs"] if v["name"] in used]}

        def validate(x, part_script=part_script, part_timing=part_timing, batch=batch):
            return (beat_errors(x, part_script, part_timing, figures, linkwork) + camera_errors(x, motion_on(config))
                    + match_errors(x, batch))
        part = models.loop(f"visual-beats-{film['requested_seconds']}-p{number}", task + STAGED_SCENES_TASK,
                           part_context, validate, experimental=experimental, units=BEAT_UNITS, ladder=beat_ladder(batch))
        for scene, row in zip(part["scenes"], batch):
            scene.update({k: row[k] for k in ("importance", "room", "intent") if row.get(k)})
        scenes.extend(part["scenes"])
    storyboard = {"scenes": scenes}
    errors = beat_errors(storyboard, script, timing, figures, linkwork)
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
                if brief.get("world"):                # key-idea films: the plan's world, word for word, every time
                    world = brief["world"]
                    prompt = (f"{prompt} Draw exactly: the guide is {world['guide']} The setting is {world['setting']} "
                              f"Style: {world['style']}")
                # a ladder fallback is a quiet backdrop by design: judged as one, not against the passage it stands in for
                acceptance = (["No people, machines, places or text", "Matches the shared visual style"] if scene.get("fallback") == "ladder"
                              else ["Matches the exact narrated passage and subject", "Consistent with the shared creative brief", "No raster typography, charts or invented factual labels"])
                assets.append({"id": asset_id, "purpose": scene["rationale"], "prompt": prompt,
                    "acceptance": acceptance,
                    "fact_ids": list(points[scene["point_id"]]["fact_ids"])})
            else:
                asset = next(a for a in assets if a["id"] == shared[key])
                asset["fact_ids"] = sorted(set(asset["fact_ids"]) | set(points[scene["point_id"]]["fact_ids"]))
            scene["asset_id"] = shared[key]
            if has_action(scene):                     # the still is the first frame of a short video
                asset = next(a for a in assets if a["id"] == shared[key])
                asset["purpose"] += f" First frame of a short animation in which: {scene['action']}"
                asset["acceptance"] = asset["acceptance"] + [
                    "Shows the moment before the action starts, with room in the frame for it to happen"]
    return {"style": brief["visual_style"], "assets": assets, "creative_brief": brief}
