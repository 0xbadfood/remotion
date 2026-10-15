"""Motion prompts for short video clips made from accepted scene images (animation pass 3, user design, 2026-10-16).

One planner call for the whole film, after the images are accepted, with no review loop: code checks only, with a
corrected answer asked for at most twice (user, 2026-10-16). It is a separate call, not a field of the image prompts:
a new asset field would change every image's cache identity and pull motion into the image review loop, and the clip
must animate the accepted image's final prompt (18 of Italy's 31 scenes were rewritten during image review).
"""
from __future__ import annotations

import json
import re

TASK = (
    "Each scene below is an accepted still illustration from a narrated history film. A video model will animate each "
    "still into a short clip, with the still fixed at the middle of the clip, and the clip is played slowed down under "
    "the narration. Write one motion prompt per scene: what moves in the picture, gently and plausibly, in one or two "
    "plain sentences (at most 45 words). Animate only what the scene already shows: people breathe, glance, turn a head, "
    "gesture slightly or walk on; flags, smoke, water, foliage, candle flames and dust move. The camera stays almost "
    "still (at most a slow, slight push or drift). Never add people, objects, text, captions or new events; never "
    "change the setting, era or anyone's appearance; no fast action, violence or cuts. Keep the main subject where it "
    "is (the right half of the frame for most scenes). End every prompt with the art style: 'Painterly editorial "
    "illustration.' Return {clips:[{id, motion}]} with every scene id exactly once."
)
BANNED = re.compile(r"\b(text|captions?|subtitles?|title card|lettering|logo|watermark|whip pan|cut to|explod\w*|"
                    r"gunfire|shoot\w*|blood\w*|morph\w*|transform\w* into)\b", re.I)


def motion_errors(value, ids):
    clips = value.get("clips") if isinstance(value, dict) else None
    if not isinstance(clips, list):
        return ["clips must be a list of {id, motion}"]
    errors, seen = [], set()
    for clip in clips:
        cid, text = (clip or {}).get("id"), (clip or {}).get("motion")
        if cid not in ids:
            errors.append(f"unknown scene id {cid!r}")
            continue
        if cid in seen:
            errors.append(f"{cid}: listed twice")
        seen.add(cid)
        if not isinstance(text, str) or not 8 <= len(text.split()) <= 60:
            errors.append(f"{cid}: motion must be one or two sentences of 8-60 words")
        elif BANNED.search(text):
            errors.append(f"{cid}: motion asks for something a clip must not add ({BANNED.search(text).group(0)!r})")
    if ids - seen:
        errors.append("missing scenes: " + ", ".join(sorted(ids - seen)))
    return errors


def make_motion_prompts(models, scenes, attempts=3):
    """{asset_id: motion} for every accepted scene image. scenes: [{id, visual_message, scene, people}]."""
    ids = {s["id"] for s in scenes}
    prompt, errors = TASK + "\n\nSCENES:\n" + json.dumps(scenes, ensure_ascii=False), []
    for attempt in range(attempts):
        note = ("\n\nYOUR LAST ANSWER HAD THESE PROBLEMS; answer again in full with them fixed:\n- " + "\n- ".join(errors)
                if errors else "")
        value = models.call("planner", f"clip-motion-{attempt}", prompt + note, max_tokens=6000)
        errors = motion_errors(value, ids)
        if not errors:
            return {c["id"]: c["motion"].strip() for c in value["clips"]}
    raise ValueError("Motion prompts failed code checks: " + "; ".join(errors[:5]))
