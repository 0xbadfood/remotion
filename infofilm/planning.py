from __future__ import annotations

import re
from pathlib import Path

from .core import read, write
from .composition import image_composition


def word_count(text):
    return len(re.findall(r"\b[\w]+(?:['’][\w]+)*\b", text))


def evidence_context(extraction, subject):
    sources = []
    for i, row in enumerate(extraction["results"], 1):
        text = row["raw_content"]
        if row.get("source_digest"):
            summary = row["source_digest"]
            sources.append({"id": f"S{i}", "url": row["url"], "title": row.get("title") or row["url"],
                "source_summary": summary["claims"], "source_cautions": summary.get("cautions", []),
                "passages": summary["passages"],
                "policy": "Claims are a first-pass model digest. Excerpts map to the archived full page; verify contested meaning."})
            continue
        # Archive complete documents; expose relevant, overlapping windows to models.
        intervals = []
        keywords = [w for w in re.findall(r"\w+", subject) if len(w) > 3]
        terms = "|".join(re.escape(w) for w in keywords) or r"\w+"
        for match in re.finditer(terms, text, flags=re.I):
            start, end = max(0, match.start() - 220), min(len(text), match.end() + 550)
            if intervals and start <= intervals[-1][1]:
                intervals[-1] = (intervals[-1][0], max(end, intervals[-1][1]))
            else:
                intervals.append((start, end))
        passages = []
        remaining = 15000
        for start, end in intervals:
            if remaining <= 0:
                break
            end = min(end, start + remaining)
            passages.append({"offset_start": start, "offset_end": end, "text": text[start:end]})
            remaining -= end - start
        sources.append({"id": f"S{i}", "url": row["url"], "title": row.get("title") or row["url"], "passages": passages})
    return {"sources": sources, "policy": "Evergreen educational explanation; omit current forecasts and dated economic/population statistics."}


# Smallest repairable units per stage (repair_units): code maps findings to these ids and repairs only them.
EVIDENCE_UNITS = {"lists": {"facts": "id", "uncertainties": "@U", "narrative_priorities": "@N"}, "operations": ["facts"]}
SCRIPT_UNITS = {"lists": {"points": "id"}, "operations": ["points"]}
LIBRARY_UNITS = {"lists": {"assets": "id"}}
STORYBOARD_UNITS = {"lists": {"scenes": "point_id"}, "anchors": {"scenes": ["point_id"]}}
# A prompt asking the raster generator to write words (Manhattan C07: "labelled as an illustration").
RASTER_LABEL = re.compile(r"\b(?:labell?ed|marked|captioned|identified|titled)\s+(?:clearly\s+)?as\s+(?:an?\s+)?"
                          r"(?:illustration|reconstruction|artist'?s impression)\b|\bwith\s+(?:the\s+)?(?:caption|words|text)\s+[\"'“]",
                          re.I)
IMAGE_PROMPT_UNITS = {"lists": {"assets": "id", "banners": "scene_key"}, "anchors": {"banners": ["scene_key"]}}


def make_evidence(models, extraction, subject="How the Indian monsoon works", audit_notes=None, experimental=True, chapter_context=None):
    context = evidence_context(extraction, subject)
    if audit_notes:
        context["source_audit"] = audit_notes
    if chapter_context:
        context["chapter_context"] = chapter_context
    source_ids = {s["id"] for s in context["sources"]}
    def validate(value):
        errors = []
        facts = value.get("facts", [])
        if not 8 <= len(facts) <= 20:
            errors.append(f"Provide 8–20 distinct, useful facts: this draft has {len(facts)}" +
                          ("; merge overlapping facts or drop the least useful ones, keeping every fact the narration needs"
                           if len(facts) > 20 else "; add verified facts from the sources"))
        if len({f.get("id") for f in facts}) != len(facts):
            errors.append("Fact IDs must be unique")
        for fact in facts:
            if not fact.get("claim") or not fact.get("source_ids") or not set(fact["source_ids"]) <= source_ids:
                errors.append(f"{fact.get('id')}: needs a claim and source_ids drawn only from the supplied sources")
        return errors
    task = (f"Prepare a source-grounded evidence brief on {subject}, usable for short informational films. "
            "Explain the core concepts, causal mechanisms or chronology, relevant context, significance and important qualifications supported by the sources. "
            "Distinguish tendencies from guarantees and local mechanisms from large-scale systems. Avoid dated statistics and absolute guarantees. "
            "Return {title, facts:[{id:'F01',claim,source_ids:['S1'],evidence_note:'paraphrased support and section'},...], uncertainties:[string], narrative_priorities:[string]}.")
    if chapter_context:
        task += (" This is one chapter of a longer film. Research its owned questions, scope and objective thoroughly. "
                 "Earlier/later chapter context may be a short reminder or handoff; do not retell their material. "
                 "Resolve the assigned research gaps and retain uncertainty where sources cannot establish a claim.")
    return models.loop("evidence-audit" if audit_notes else "evidence", task, context, validate, experimental=experimental,
                       units=EVIDENCE_UNITS)


def script_text(script):
    return "\n\n".join(p["narration"].strip() for p in script["points"])


def script_errors(value, evidence, budget=None):
    errors = []
    points = value.get("points", [])
    facts = {f["id"] for f in evidence["facts"]}
    if not points or len(points) > 25:
        errors.append("Need a concise, nonempty duration-appropriate point sequence")
    if len({p.get("id") for p in points}) != len(points):
        errors.append("Point IDs must be unique")
    for p in points:
        if not p.get("narration") or not p.get("heading") or not p.get("fact_ids") or not set(p["fact_ids"]) <= facts:
            errors.append(f"{p.get('id')}: needs narration, a heading and fact_ids drawn only from the evidence")
        if word_count(p.get("heading", "")) > 7:
            errors.append(f"{p.get('id')}: heading has {word_count(p.get('heading', ''))} words; at most 7")
    return errors


def continuity_findings(notes, chapter_id):
    """Continuity notes as script findings for this chapter: 'C02 P11' -> P11; a pointer naming another chapter (or
    none) stays stage level, so it can never flag this chapter's points by accident."""
    findings = []
    for note in notes or []:
        field = str(note.get("field", ""))
        chapters = set(re.findall(r"\bC\d{2,}\b", field))
        own = re.sub(r"\b" + re.escape(chapter_id) + r"\b", "", field) if chapters == {chapter_id} else None
        findings.append({**note, "field": own if own and re.search(r"\bP\d+", own) else "stage"})
    return findings


def make_script(models, evidence, duration, rate=135, experimental=True, suffix="", correction=None, creative_brief=None,
                seed=None):
    budget = round((duration - 1.0) * rate / 60)
    task = (f"Write engaging Indian English narration for a general-audience informational video about {evidence['title']}. "
            "Create a hook, a clear causal progression and a useful closing thought. "
            f"The advisory duration is about {duration} seconds; the chosen narrator measures roughly {rate:.1f} words/minute. About {budget} spoken words is a planning reference, not a limit. "
            "Prioritize useful points, their logical flow and natural narration. A somewhat longer or shorter result is acceptable; do not pad, rush or cut a good explanation to match an exact time or word count. "
            "Choose point count from duration, subject complexity and natural sentence lengths. About ten points for a minute is a starting estimate, not a requirement. "
            "Select only the facts that comfortably fit this duration; shorter films can omit subsidiary concepts that belong in longer versions. Prioritize a clear main explanation over covering every evidence item. "
            "Do not cram facts, repeat conclusions, read headings aloud separately, or include stage directions in narration. Use complete natural sentences. "
            "Use only the evidence brief, with factual qualifications preserved. Each point may have one or two connected sentences. "
            "Return {title, requested_seconds, points:[{id:'P01',heading:'short on-screen heading',narration:'exact spoken sentence(s)',fact_ids:['F01'],visual_intent:'what helps explain this'}]}. "
            "Do not generate an unreferenced separate script field.")
    context = {"evidence": evidence, "advisory_duration_seconds": duration, "rough_word_budget": budget,
               "duration_policy": "advisory; flow and narration quality take priority"}
    if creative_brief:
        context["creative_brief"] = creative_brief
        task += (" Follow the shared creative brief's mood, intensity and narration direction through wording, examples and sentence rhythm. "
                 "Keep the narrator identity consistent. Never include delivery instructions as spoken words; humour must preserve the facts.")
    if evidence.get("chapter_context"):
        task += (" This is a chapter in one continuous film. Follow chapter_context opening_role and ending_role exactly: "
                 "only film_intro introduces the overall subject; continuation opens with a natural bridge from entry_context. "
                 "A handoff ending leads into the next chapter's question, without an overall summary or sign-off. "
                 "Only film_conclusion answers the film-wide central question and closes the narrative. "
                 "Advance the owned topics; respect exclusions and avoid repeating previous chapters. "
                 "The outline gives research intentions, not additional factual evidence; use verified chapter facts for narration.")
        if evidence["chapter_context"].get("continuity_notes"):
            task += (" chapter_context.continuity_notes lists problems a continuity review found between the previous chapter's "
                     "accepted script and this chapter: resolve each one (chronology, scope, duplicated exposition, handoff) "
                     "while keeping this chapter's own facts and roles. The previous chapter will not change.")
    if correction:
        context["measured_duration_feedback"] = correction
    chapter = evidence.get("chapter_context") or {}
    repair = None
    if seed is not None and chapter.get("continuity_notes"):
        repair = (seed, continuity_findings(chapter["continuity_notes"], (chapter.get("chapter") or {}).get("id", "")))
    return models.loop(f"script-{duration}{suffix}", task, context, lambda x: script_errors(x, evidence, budget), experimental=experimental,
                       units=SCRIPT_UNITS, seed=repair)


def make_library(models, evidence, scripts, experimental=True):
    task = (f"Plan a shared library of six to eight distinct landscape still illustrations for two informational films on {evidence['title']}. "
            "Use polished editorial illustrations appropriate to the subject. Warm ochre and deep teal are accent colours; natural greens, blues, earth and cloud colours are welcome for realistic contextual details. This is not an exclusive palette. "
            "Scientific mechanisms, maps, arrows, exact labels, calendars and charts will be native Remotion graphics; do not ask generated images to encode these. "
            "Cover the important contexts and semantic changes in the actual scripts, without inventing settings or unrelated decorative scenes. "
            "Main detail should be anchored on the right for a portrait crop. Keep the left 45% relatively quiet for a dark text gradient; low-detail sky, water or ground is acceptable. Do not demand completely empty left space. No generated words, labels, borders, logos or split panels. "
            "These stills provide context and atmosphere. Clouds, rain, mist and heat haze are ordinary landscape features, not forbidden scientific diagrams. Do not force every image to communicate a complete mechanism; the storyboard and narration provide that. "
            "Keep prompts concise but concrete, and provide observable visual acceptance criteria. This is educational general-audience design, not a children's cartoon. "
            "Return {style, assets:[{id:'A01',purpose,prompt,acceptance:[string],fact_ids:[string]}]}.")
    return models.loop("visual-library", task, {"evidence": evidence, "scripts": scripts}, lambda x: library_errors(x, evidence),
                       experimental=experimental, units=LIBRARY_UNITS)


def library_errors(x, evidence):
    facts = {f["id"] for f in evidence["facts"]}
    assets = x.get("assets", [])
    errors = []
    if not isinstance(assets, list) or len(assets) > 240 or len({a.get("id") for a in assets}) != len(assets):
        errors.append("Need at most 240 assets with unique IDs, allocated from actual visual beats")
    for a in assets:
        if not a.get("prompt") or len(a["prompt"]) > 3500 or not a.get("acceptance") or not set(a.get("fact_ids", [])) <= facts:
            errors.append(f"{a.get('id')}: needs a prompt under 3500 characters, acceptance criteria and evidence fact_ids")
    return errors


def make_storyboard(models, script, library, evidence, duration, experimental=True):
    task = ("Create an executable storyboard for the exact narration. One scene per point, in unchanged order, anchored to that point's first aligned word. "
            "Choose a library image when atmosphere/context helps; choose a native process diagram when scientific causality benefits from explicit explanation. "
            "Use distinct native diagrams where they clarify the actual subject, distributed across the film, and contextual images elsewhere. There is no required diagram count; do not force diagrammatic explanations onto unsuitable subjects. "
            "Native kinds are 'flow' (three sequential concept cards), 'compare' (two contrasted explanatory cards), or 'cycle' (three causally linked concept cards). "
            "For native kinds provide exactly three labels for flow/cycle, two labels for compare, each <=4 words, and explanatory details for each <=10 words. "
            "A compare diagram has no directional arrow; flow/cycle arrows must imply only evidence-supported relations. "
            "Text heading <=7 words and summary <=12 words, expressing the spoken point without introducing new claims. "
            "Avoid consecutive reuse of the same image when an appropriate alternate exists. Images use gentle push/pull/drift/hold with text in left 43%; diagrams use the right 55%. "
            "Return {scenes:[{point_id:'P01',kind:'image'|'flow'|'compare'|'cycle',asset_id:'A01' or null,heading,summary,labels:[],details:[],motion:'push'|'pull'|'drift'|'hold',rationale}]}.")
    return models.loop(f"storyboard-{duration}", task, {"script": script, "evidence": evidence, "library": library}, lambda x: storyboard_errors(x, script, library), units=STORYBOARD_UNITS, experimental=experimental)


def storyboard_errors(x, script, library):
    ids = {a["id"] for a in library["assets"]}
    scenes = x.get("scenes", [])
    errors = []
    if [s.get("point_id") for s in scenes] != [p["id"] for p in script["points"]]:
        errors.append("Scene point IDs must exactly cover narration order")
    diagrams = 0
    for s in scenes:
        kind = s.get("kind")
        if kind == "image":
            if s.get("asset_id") not in ids:
                errors.append(f"{s.get('point_id')}: unknown image asset {s.get('asset_id')!r}")
        elif kind in {"flow", "compare", "cycle"}:
            diagrams += 1
            n = 2 if kind == "compare" else 3
            if len(s.get("labels", [])) != n or len(s.get("details", [])) != n:
                errors.append(f"{s.get('point_id')}: {kind} needs {n} labels and {n} details")
            if any(word_count(v) > 4 for v in s.get("labels", [])) or any(word_count(v) > 10 for v in s.get("details", [])):
                errors.append(f"{s.get('point_id')}: diagram labels must be at most 4 words and details at most 10")
        else:
            errors.append(f"{s.get('point_id')}: unsupported scene kind {kind!r}")
        if word_count(s.get("heading", "")) > 7 or word_count(s.get("summary", "")) > 12:
            errors.append(f"{s.get('point_id')}: heading at most 7 words and summary at most 12")
        if s.get("motion") not in {"push", "pull", "drift", "hold"}:
            errors.append(f"{s.get('point_id')}: unsupported motion {s.get('motion')!r}")
        if word_count(s.get("banner_override", "")) > 8:
            errors.append(f"{s.get('point_id')}: banner override is longer than 8 words")
    return errors


def image_prompt_evidence(evidence, fact_ids):
    result = {key: evidence[key] for key in ["title", "uncertainties", "narrative_priorities"] if key in evidence}
    result["facts"] = [fact for fact in evidence["facts"] if fact["id"] in fact_ids]
    chapter = evidence.get("chapter_context")
    if chapter:
        result["chapter_context"] = {
            "film": {key: chapter.get("film", {}).get(key) for key in ["subject", "central_question"]},
            "chapter": {key: chapter.get("chapter", {}).get(key) for key in
                        ["title", "scope", "objective", "exclusions", "visual_direction"]},
            "previous_chapter": chapter.get("previous_chapter"), "next_chapter": chapter.get("next_chapter")}
    return result


def shown_people(assets):
    return {str(person.get("name", "")).casefold() for asset in assets for person in asset.get("people_in_frame") or []
            if isinstance(person, dict)}


def cover_required_people(models, task, base, result, used, contexts, evidence, validator, required, experimental):
    """Targeted repair when the chapter's prompts leave out cast members it must show: the planner rewrites only the
    assets it picks for them, and those replace the originals. Advisory: a failed repair keeps the prompts as they are."""
    missing = [name for name in required if name.casefold() not in shown_people(result["assets"])]
    report = {"required": list(required), "missing": missing, "repaired_assets": []}
    if not missing:
        return report
    by_id = {a["id"]: a for a in used}
    current = {a["id"]: a for a in result["assets"]}
    all_facts = {f["id"] for f in evidence["facts"]}
    banners = {b["scene_key"]: b for b in result["banners"]}

    def check(value):
        assets = value.get("assets")
        if not isinstance(assets, list) or not assets or any(not isinstance(a, dict) for a in assets):
            return ["assets must be a non-empty array of the rewritten asset objects"]
        ids = [a.get("id") for a in assets]
        if len(set(ids)) != len(ids) or not set(ids) <= set(by_id) or len(ids) > len(missing) + 1:
            return [f"Rewrite at most {len(missing) + 1} distinct existing assets, chosen from " + " ".join(by_id)]
        chosen = {i: contexts[i] for i in ids}
        keys = {c["scene_key"] for rows in chosen.values() for c in rows}
        errors = validator([by_id[i] for i in ids], chosen, all_facts)(
            {"assets": assets, "banners": [banners[k] for k in keys if k in banners]})
        still = [name for name in missing if name.casefold() not in shown_people(assets)]
        if still:
            errors.append("Not depicted in any rewritten asset's people_in_frame: " + ", ".join(still))
        return errors

    packet = {**base, "missing_people": missing,
              "current_assets": [{k: a.get(k) for k in ("id", "purpose", "visual_message", "prompt", "people_in_frame")}
                                 for a in result["assets"]]}
    repair = (task + " Repair only: the people in missing_people appear in no asset of this chapter. Choose the asset(s) "
              "whose narration and facts best support showing each of them and rewrite those assets so the person is "
              "visibly depicted, keeping each asset's visual_message meaning. Return {assets:[...]} with only the "
              "rewritten assets, each complete with every field the original has.")
    try:
        value = models.loop("scene-image-prompts-cast", repair, packet, check, experimental=experimental, units=IMAGE_PROMPT_UNITS)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"[:300]
        if hasattr(models, "store"):
            models.store.event("image-prompts", "cast_coverage_failed", report)
        return report
    for asset in value["assets"]:
        current[asset["id"]] = asset
    result["assets"] = [current[a["id"]] for a in result["assets"]]
    report["repaired_assets"] = [a["id"] for a in value["assets"]]
    if hasattr(models, "store"):
        models.store.event("image-prompts", "cast_coverage_repaired", report)
    return report


def make_image_prompts(models, library, films, evidence, orientations, experimental=True, message_recovery=False, batch_size=8, person_references=False, classify_review=False, film_cast=None, required_people=None):
    """Bind prompts to actual spoken scenes, including reuse and banner intent."""
    contexts = {}
    scene_ids = set()
    for film in films:
        points = film["script"]["points"]
        point_indices = {p["id"]: i for i, p in enumerate(points)}
        if any(s.get("start_word_id") for s in film["storyboard"]["scenes"]):
            from .creative import resolved_scenes
            scenes = resolved_scenes(film["storyboard"], film["script"], film["timing"])
        else:
            scenes = film["storyboard"]["scenes"]
        for j, scene in enumerate(scenes):
            i = point_indices[scene["point_id"]]
            if scene["kind"] != "image":
                continue
            key = f"{film['requested_seconds']}:{scene.get('id', scene['point_id'])}"
            scene_ids.add(key)
            context = {"scene_key": key, "narration": scene.get("covered_narration", points[i]["narration"]),
                       "point_narration": points[i]["narration"], "heading": scene["heading"],
                       "visual_purpose": points[i].get("visual_intent", scene["rationale"]),
                       "previous_narration": (scenes[j-1]["covered_narration"] if j else None) if "covered_narration" in scene else (points[i-1]["narration"] if i else None),
                       "next_narration": (scenes[j+1]["covered_narration"] if j+1 < len(scenes) else None) if "covered_narration" in scene else (points[i+1]["narration"] if i+1 < len(points) else None),
                       "fact_ids": points[i]["fact_ids"],
                       "narrated_seconds": (scene["end"]-scene["start"] if "start" in scene else
                           film["timing"]["points"][i]["end"]-film["timing"]["points"][i]["start"]),
                       "copy_density": scene.get("copy_density", "standard")}
            if 'banner_override' in scene:
                context['banner_override'] = scene['banner_override']
            contexts.setdefault(scene["asset_id"], []).append(context)
    used = [a for a in library["assets"] if a["id"] in contexts]
    composition = {a['id']: image_composition(contexts[a['id']], orientations) for a in used}
    task = ("Write the final image-generation prompts for the actual informational film scenes in the context below. "
            "Read the subject, narration, evidence, scene purpose and neighbouring beats before choosing what each image should show. "
            "Keep existing asset IDs; a shared still must suit every scene that uses it. No unrelated stock scenery or invented historical/scientific claims. "
            "Use the shared subject-aware visual style and creative brief, with coherent visual transitions. All images are generated. "
            "Choose photorealistic or illustrated treatment from the actual subject and brief; do not assume every film needs editorial drawings. "
            "Follow composition_by_asset, derived from the actual Remotion layout. For standard landscape scenes, put the complete hero subject in the right half with a low-detail left 43%; "
            "give specific desired background material such as subdued sky, earth or a wall instead of vague 'quiet area'. "
            "Use camera distance and angle to keep wide objects and groups inside the usable subject area. "
            "Standard portrait scenes need essential details near the right-biased crop. Minimal-copy scenes use full-frame imagery with text below; centred subjects and natural left-side detail are appropriate. "
            "Do not require empty left space or exact right-of-centre placement for minimal-copy scenes. State framing once, at the beginning of each prompt, before decorative details. "
            "Use contextual banners, season/place labels or short process labels where they help the viewer understand the actual scene. "
            "Return exact banner text separately: the compositor places crisp banners over the image. Plan a clean upper-right banner area with no important subject underneath. "
            "Do not ask the raster generator to draw typography, maps or arrows; native diagrams and banners supply those precisely. "
            "Describe text-free surfaces positively in the scene itself: screens, gauges, dials, documents, signs, patches, "
            "badges and maps appear as plain, blank or unmarked surfaces (for example 'a dial with plain unlabeled tick marks', "
            "'a softly glowing blank grey screen', 'a sheet of plain off-white paper'). Image models draw what a prompt names, "
            "even after 'no', so never end a prompt with exclusion lists such as 'no text, no labels, no numerals'. "
            "Leave out writing-bearing objects the scene does not need. "
            "Marking a reconstruction as an illustration is the compositor's job (a banner or caption over the image), "
            "so a prompt describes only the scene, never a label, caption or words saying it is an illustration. "
            "Do not label a generic illustrative landscape as an exact photographed location. Avoid split panels and atmospheric features that imply a wrong mechanism. "
            "Return {style, assets:[{id,purpose,prompt,acceptance:[string],fact_ids:[string]}], banners:[{scene_key,text:'at most 8 words',reason}]}. "
            "Exactly cover the supplied used assets and image scene keys; an empty banner text is allowed if a banner adds no useful context.")
    if classify_review:
        task += (" Additionally classify each asset with review_profile: 'scientific_diagram', "
                 "'explanatory_apparatus', 'narrative' or 'symbolic'. Classify the concrete image, not the "
                 "film subject. scientific_diagram depicts literal components, anatomy or a scientific "
                 "mechanism; explanatory_apparatus depicts equipment whose counts/relationships teach the "
                 "lesson. narrative covers people, places and historical scenes; symbolic covers metaphors. "
                 "A science film can include narrative scenes; a historical film can include a scientific "
                 "diagram. This classification activates component-count and source-placement review only "
                 "for the first two profiles. Choose based on what the current prompt actually depicts.")
    if message_recovery:
        task += (" Additionally every asset must include visual_message, setting and visual_style. "
                 "visual_message is the informative meaning the illustration supports, independent of its chosen staging. "
                 "It must suit all covered passages and preserve essential historical/scientific relationships. "
                 "setting specifies concrete sourced era/place/surroundings; visual_style specifies visual treatment and mood. "
                 "Keep literal film/chapter titles, banners and overlay instructions out of setting, visual_style and prompt; "
                 "banner entries are separate. prompt is only a concrete standalone scene, not a wrapper or abstract summary. "
                 "Use a simpler subject-specific scene rather than an overloaded diagram or generic symbolism. "
                 "These fields remain available through reviews and repairs, allowing staging to change while meaning stays fixed.")
    if person_references:
        task += (" Additionally every asset must include people_in_frame:[{name,appearance_context,reference_kind,birth_year}]. "
                 "List only recognizable real public/historical figures who are visibly depicted in this concrete scene; "
                 "a person merely mentioned in narration does not belong in the list. Use [] for scenery, objects, "
                 "generic crowds and unnamed people. Use consistent canonical full names across batches and films. "
                 "appearance_context gives the sourced life period/age/role needed in this scene, with consistent "
                 "wording for the same appearance across scenes. reference_kind is photograph when authentic photos "
                 "exist, or historical_portrait for figures before photography. birth_year is the person's birth year as an integer "
                 "(negative for BCE; best known estimate). People born before 1850 get no reference search, because "
                 "reliable photographs of them are rare; describe them from the evidence. Do not fabricate a photograph of an "
                 "ancient ruler or use a modern actor as their likeness. Limit a scene to at most six named figures. "
                 "Keep the scene prompt under 2600 characters to leave room for appearance-reference conditioning. "
                 "Research will source these references and attach a labeled card; describe the actual scene and "
                 "treatment rather than requesting a contact sheet or raster name labels. "
                 "This is a historical documentary, so show the real people involved where the sources place them "
                 "(meetings, briefings, public appearances, documented places); empty rooms and distant anonymous "
                 "figures in every scene take the realism out of the film. Keep violence, wounds and death indirect, "
                 "but do not leave the people out.")
        if required_people and not message_recovery:
            task += (" required_people must each be visibly depicted, listed in people_in_frame, in at least one asset "
                     "of this chapter; pick the asset whose narration and facts best support showing them.")
        if film_cast:
            task += (" film_cast lists the film's people with canonical names, birth years and reference kinds: every "
                     "people_in_frame entry must use one of these names, birth_year and reference_kind exactly. Do not "
                     "depict a named person who is not in film_cast; show them generically instead.")
    def validator(batch_assets, batch_contexts, allowed_facts):
        batch_scene_ids = {c["scene_key"] for rows in batch_contexts.values() for c in rows}
        def validate(value):
            errors = []
            assets = value.get("assets", [])
            if not isinstance(assets, list) or any(not isinstance(a, dict) for a in assets):
                return ["assets must be an array of objects"]
            if [a.get("id") for a in assets] != [a["id"] for a in batch_assets]:
                errors.append("Final prompts must retain exactly the used asset IDs and order: expected " +
                              " ".join(a["id"] for a in batch_assets) + ", got " + " ".join(str(a.get("id")) for a in assets))
            for a in assets:
                if classify_review:
                    from .educational_review import PROFILES
                    if a.get('review_profile') not in PROFILES:
                        errors.append(f"{a.get('id')}: needs a supported review_profile")
                if isinstance(a.get("prompt"), str) and RASTER_LABEL.search(a["prompt"]):
                    errors.append(f"{a.get('id')}: the prompt asks the image generator to write a label or caption "
                                  f"({RASTER_LABEL.search(a['prompt']).group(0)!r}); the compositor adds banners, describe only the scene")
                if (not isinstance(a.get("prompt"), str) or not a["prompt"].strip() or len(a["prompt"]) > 3500
                        or not isinstance(a.get("acceptance"), list) or not a["acceptance"]
                        or not isinstance(a.get("fact_ids"), list)
                        or any(not isinstance(f, str) or f not in allowed_facts for f in a.get("fact_ids", []))):
                    errors.append(f"{a.get('id')}: needs a prompt under 3500 characters, acceptance criteria and fact_ids from this batch's evidence")
            if message_recovery:
                for asset in assets:
                    for field, minimum, maximum in [("visual_message", 20, 1600), ("setting", 10, 1000), ("visual_style", 10, 800)]:
                        text = asset.get(field)
                        if not isinstance(text, str) or not minimum <= len(text.strip()) <= maximum:
                            errors.append(f"{asset.get('id')}: needs a {field} of {minimum}-{maximum} characters")
            if person_references:
                from .person_references import person_errors
                cast_names = {p["name"].casefold() for p in film_cast or []}
                for asset in assets:
                    errors.extend(f"{asset.get('id')}: {error}" for error in person_errors(asset.get('people_in_frame')))
                    if cast_names and isinstance(asset.get('people_in_frame'), list):
                        errors.extend(f"{asset.get('id')}: {person.get('name')!r} is not in film_cast; use a cast name or depict them generically"
                                      for person in asset['people_in_frame']
                                      if isinstance(person, dict) and str(person.get('name', '')).casefold() not in cast_names)
                    if len(asset.get('prompt', '')) > 2600:
                        errors.append(f"{asset.get('id')}: leave prompt space for reference conditioning")
            banners = value.get("banners", [])
            if not isinstance(banners, list) or any(not isinstance(b, dict) for b in banners):
                errors.append("banners must be an array of objects")
            else:
                keys = [b.get("scene_key") for b in banners]
                if len(banners) != len(batch_scene_ids) or set(keys) != batch_scene_ids:
                    missing, extra = sorted(batch_scene_ids - set(keys)), sorted(set(keys) - batch_scene_ids)
                    errors.append("Banner entries must exactly cover image scenes (one each)" +
                                  (f"; missing {missing}" if missing else "") + (f"; not in this batch {extra}" if extra else "") +
                                  ("; duplicated scene keys" if len(keys) != len(set(keys)) else ""))
                for b in banners:
                    if not isinstance(b.get("text"), str):
                        errors.append(f"{b.get('scene_key')}: banner text must be a string")
                    elif word_count(b["text"]) > 8:
                        errors.append(f"{b.get('scene_key')}: banner {b['text']!r} counts {word_count(b['text'])} words; at most 8 "
                                      "(times like 10:30, abbreviations like p.m. and dates count each part); no source tags in banners")
            return errors
        return validate

    base = {"subject": evidence["title"], "evidence": evidence, "style": library["style"],
            "used_assets": used, "scene_contexts_by_asset": contexts, "orientations": orientations,
            "composition_by_asset": composition, "creative_brief": library.get("creative_brief")}
    if person_references and film_cast:
        base["film_cast"] = [{k: p.get(k) for k in ("name", "birth_year", "reference_kind", "appearance_context")} for p in film_cast]
        if required_people:
            base["required_people"] = list(required_people)
    if not message_recovery:
        result = models.loop("scene-image-prompts", task, base,
                             validator(used, contexts, {f["id"] for f in evidence["facts"]}), experimental=experimental,
                             units=IMAGE_PROMPT_UNITS)
    else:
        if type(batch_size) is not int or not 1 <= batch_size <= 8:
            raise ValueError("Image prompt batch_size must be between 1 and 8")
        # The adapter fields made a 28–32 image chapter exceed a 7k output budget.
        # Bound each output independently; keep all contexts of a shared asset
        # together and preserve neighbours, source facts and film-wide direction.
        batches = [used[i:i+batch_size] for i in range(0, len(used), batch_size)]
        result = {"style": library["style"], "assets": [], "banners": []}
        for position, batch in enumerate(batches, 1):
            ids = {a["id"] for a in batch}
            batch_contexts = {key: rows for key, rows in contexts.items() if key in ids}
            fact_ids = {f for a in batch for f in a.get("fact_ids", [])}
            fact_ids.update(f for rows in batch_contexts.values() for row in rows for f in row.get("fact_ids", []))
            packet = {**base, "used_assets": batch, "scene_contexts_by_asset": batch_contexts,
                      "composition_by_asset": {key: value for key, value in composition.items() if key in ids},
                      "evidence": image_prompt_evidence(evidence, fact_ids),
                      "film_asset_outline": [{"id": a["id"], "purpose": a["purpose"]} for a in used],
                      "batch": {"position": position, "total": len(batches), "planned_images": len(used)}}
            detail = {"batch": position, "total_batches": len(batches), "asset_ids": [a["id"] for a in batch],
                      "planned_images": len(used), "planned_so_far": len(result["assets"])}
            if hasattr(models, "store"):
                models.store.event("image-prompts", "batch_running", detail)
            partial = models.loop(f"scene-image-prompts-batch-{position:03d}",
                task + " Plan only this batch. Keep the shared treatment in style. Use concise concrete descriptions; "
                "visual_message should be one informative sentence, setting and visual_style short phrases. "
                "Return every requested asset and banner; compact JSON is welcome." + (
                " required_people lists the people this chapter must show somewhere: depict them in this batch where "
                "its narration and facts support it. It is not a requirement for this batch alone; code checks the "
                "whole chapter after the last batch and repairs any omission, so a person missing here is not an issue."
                if person_references and film_cast and required_people else ""), packet,
                validator(batch, batch_contexts, fact_ids), experimental=experimental, units=IMAGE_PROMPT_UNITS)
            result["assets"].extend(partial["assets"])
            result["banners"].extend(partial["banners"])
            if hasattr(models, "store"):
                models.store.event("image-prompts", "batch_succeeded", {**detail, "planned_so_far": len(result["assets"])})
        errors = validator(used, contexts, {f["id"] for f in evidence["facts"]})(result)
        if errors:
            raise ValueError(f"Combined image prompt batches failed validation: {errors}")
        result["planning_batches"] = len(batches)
    if person_references and film_cast and required_people:
        result["cast_coverage"] = cover_required_people(models, task, base, result, used, contexts, evidence,
                                                        validator, required_people, experimental)
    if message_recovery:
        for asset in result["assets"]:
            for context in contexts[asset["id"]]:
                context["visual_message"] = asset["visual_message"]
        result["image_recovery_contract"] = "infofilm.image-message-recovery.v1"
    result["scene_contexts_by_asset"] = contexts
    result['orientations'] = list(orientations)
    result['composition_by_asset'] = {a['id']: image_composition(contexts[a['id']], orientations, result['banners']) for a in used}
    return result
