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
        errors.append("Resolve mood to an available named preset")
    if requested_mood != "auto" and value.get("resolved_mood") != requested_mood:
        errors.append("Preserve the explicitly selected mood")
    if value.get("visual_balance") not in BALANCE_SHARES:
        errors.append("Resolve visual_balance to image-led, balanced or explanation-led")
    if requested_balance != "auto" and value.get("visual_balance") != requested_balance:
        errors.append("Preserve the explicitly selected visual balance")
    for key in ["rationale", "narration_direction", "visual_style", "pacing_direction"]:
        if not isinstance(value.get(key), str) or not 1 <= len(value[key]) <= 1800:
            errors.append(f"Provide concise {key}")
    theme = value.get("theme", {})
    if not isinstance(theme, dict):
        theme = {}
    for key in ["background", "accent", "text"]:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(theme.get(key, ""))):
            errors.append(f"theme.{key} must be a six-digit hex colour")
    music = value.get("music", {})
    if not isinstance(music, dict):
        music = {}
    if not isinstance(music.get("caption"), str) or not 1 <= len(music["caption"]) <= 1800:
        errors.append("Provide a concise instrumental music caption")
    if isinstance(music.get("bpm"), bool) or not isinstance(music.get("bpm"), int) or not 40 <= music["bpm"] <= 180:
        errors.append("Provide music bpm between 40 and 180")
    return errors


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
    result = models.loop("creative-brief", task, {"evidence": evidence, "requested_mood": mood,
        "intensity": intensity, "requested_visual_balance": balance, "presets": MOODS,
        "resolved_visual_balances": list(BALANCE_SHARES)},
        lambda x: creative_errors(x, mood, balance), experimental=experimental)
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
    starts = [0.0] + [words[i]["start"] for i in indices[1:]]
    ends = starts[1:] + [duration]
    result = []
    for i, (scene, start, end, index) in enumerate(zip(scenes, starts, ends, indices)):
        stop = indices[i + 1] if i + 1 < len(indices) else len(words)
        point = points[scene["point_id"]]
        result.append({**scene, "id": scene.get("id", point["id"]), "start": start, "end": end,
                       "covered_narration": " ".join(w["word"] for w in words[index:stop]),
                       "fact_ids": point["fact_ids"]})
    return result


def beat_errors(value, script, timing):
    """Validate coverage and references, never impose creative image-count quotas."""
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
    if len({s.get("id") for s in scenes}) != len(scenes):
        errors.append("Visual beat IDs must be unique")
    for scene in scenes:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", str(scene.get("id", ""))):
            errors.append("Use short alphanumeric visual beat IDs")
        point_id, anchor = scene.get("point_id"), scene.get("start_word_id")
        if point_id not in point_words or anchor not in point_words.get(point_id, []):
            errors.append(f"{scene.get('id')}: anchor must be an aligned word in its point")
            continue
        if not collapsed or collapsed[-1] != point_id:
            collapsed.append(point_id)
            if anchor != point_words[point_id][0]:
                errors.append(f"First beat of {point_id} must start at its first word")
        indices.append(positions[anchor])
        kind = scene.get("kind")
        if kind == "image":
            if not isinstance(scene.get("image_brief"), str) or not 1 <= len(scene["image_brief"]) <= 1800:
                errors.append("Image beats need a subject-aware image_brief")
        elif kind in {"flow", "compare", "cycle"}:
            n = 2 if kind == "compare" else 3
            labels, details = scene.get("labels"), scene.get("details")
            if not isinstance(labels, list) or not isinstance(details, list):
                errors.append("Diagram labels and details must be lists")
                continue
            if len(labels) != n or len(details) != n:
                errors.append(f"{kind} needs {n} labels and details")
            if any(not isinstance(v, str) or len(v.split()) > 4 for v in labels) or any(not isinstance(v, str) or len(v.split()) > 10 for v in details):
                errors.append("Diagram labels/details are too long")
        else:
            errors.append("Unsupported visual beat kind")
        for key, limit in [("heading", 7), ("summary", 12)]:
            if not isinstance(scene.get(key), str) or len(scene[key].split()) > limit:
                errors.append(f"Invalid {key}")
        if scene.get("motion") not in {"push", "pull", "drift", "hold"}:
            errors.append("Unsupported motion")
        if scene.get("copy_density") not in {"minimal", "standard"}:
            errors.append("Set copy_density to minimal or standard")
        if not scene.get("rationale"):
            errors.append("Explain the visual purpose of each beat")
    if collapsed != point_ids:
        errors.append("Visual beats must cover every narration point in unchanged order")
    if indices != sorted(set(indices)):
        errors.append("Word anchors must increase strictly; no repeats or backward cuts")
    if not errors:
        if any(s["end"] <= s["start"] for s in resolved_scenes(value, script, timing)):
            errors.append("Visual beat intervals must have positive duration")
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


def make_visual_beats(models, film, evidence, brief, *, experimental=True):
    task = (
        "Plan visual beats for this exact MFA-aligned narration, following the shared creative brief. "
        "One narration point may contain several shots. Longer audio needs proportionate visual coverage, not stretched identical stills. "
        "Use the per-point shot suggestions: a 13–16-second point containing multiple ideas usually benefits from two views at a meaningful boundary. "
        "Cut at a natural sentence, clause or semantic transition using the supplied aligned WORD IDs exactly. "
        "The first beat of every point starts at that point's first word; further beats can start at later words within that point. "
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
    guidance = visual_guidance(film["timing"], brief)
    aligned = film["timing"]["points"]
    starts = [p["start"] for p in aligned]
    ends = starts[1:] + [film["actual_seconds"]]
    context = {"evidence": evidence, "creative_brief": brief, "script": film["script"],
               "aligned_points": film["timing"]["points"],
               "aligned_words": [{k: w[k] for k in ["id", "word", "start", "end"]} for w in film["timing"]["words"]],
               "guidance": guidance,
               "point_visual_guidance": [{"point_id": p["point_id"], "seconds": round(end-start, 2),
                    "suggested_shots": max(1, round((end-start)/guidance["suggested_mean_hold_seconds"]))}
                    for p,start,end in zip(aligned,starts,ends)]}
    return models.loop(f"visual-beats-{film['requested_seconds']}", task, context,
                      lambda x: beat_errors(x, film["script"], film["timing"]), experimental=experimental,
                      metrics=lambda x: visual_metrics(x, film["script"], film["timing"]))


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
    return {"style": brief["visual_style"], "assets": assets, "creative_brief": brief}
