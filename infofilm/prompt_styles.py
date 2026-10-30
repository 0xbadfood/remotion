"""How scene prompts are written for the image generator in use.

structured (default): labelled Setting / Visual treatment / Composition / Scene blocks, written for Qwen Image, whose
large vision-language encoder reads long structured prompts well.

descriptive_v1: one flowing paragraph, medium first, for FLUX.2-class generators with a small text encoder (klein 4B
reads prompts with Qwen3-4B). On Human Evolution (2026-10-18) klein passed 20 of 25 images on the first take with
the structured prompts, but its scenes came out flat and sparse: the planner now writes the concrete, visually rich
description the generator needs, since it knows each scene; a mechanical rewrite after the fact would not.
"""
from __future__ import annotations

import re

STYLES = ('structured', 'descriptive_v1')

PLANNER_GUIDE = (
    " PROMPT STYLE descriptive_v1 (the generator reads prompts with a small text encoder and follows concrete prose "
    "literally): write each prompt as one flowing paragraph of natural prose, about 70 to 140 words, with no field "
    "labels, headings or lists. Start with the main subject, then its action, then the setting with one or two "
    "supporting details in the foreground and background, then light, palette and camera viewpoint. Code puts "
    "visual_style in front of the prompt, so do not repeat the art medium inside it. Make visual_style one sentence "
    "that names the medium first, then technique, palette and light (for example 'An oil painting in a hand-painted "
    "editorial style with visible brushstrokes, muted earth tones and warm natural light'), and keep the same "
    "visual_style wording across all assets of the film so the stills match. Be concrete about what things look "
    "like and are made of: name material, shape, colour and size of era-specific objects ('a teardrop-shaped grey "
    "flint hand axe with chipped edges'), the anatomy of non-modern humans ('a heavy brow ridge, sloping forehead "
    "and no chin'), and the materials of clothing and buildings; the generator fills vague nouns with modern "
    "defaults. Give each still enough to look at: a clear hero subject plus a few supporting details that serve the "
    "message, not an empty landscape, unless emptiness is the point. Name each person's pose and what their hands "
    "hold. All the earlier rules still apply: positive descriptions only, plain unmarked surfaces, framing stated "
    "once at the start of the prompt.")

REPAIR_GUIDE = (
    " PROMPT STYLE descriptive_v1: write the replacement as one flowing paragraph of natural prose, about 70 to 140 "
    "words, with no labels or lists: main subject, action, setting with a few supporting details, light and "
    "viewpoint. Be concrete about materials, shapes, colours and the anatomy of non-modern humans, since the "
    "generator fills vague nouns with modern defaults. The adapter puts the visual style in front, so do not "
    "repeat the art medium.")


def prompt_style(config):
    style = config.get('image_prompt_style', 'structured')
    if style not in STYLES:
        raise ValueError(f'image_prompt_style must be one of {", ".join(STYLES)}')
    return style


# Films are for school students: a person in a scene is said to be clothed, because a pose described by body parts
# alone ("knees pulled to the chest") came back as a bare body, and the reviewer passed it (C13 of keph1-06, 2026-10-30).
PEOPLE = re.compile(r"\b(person|people|man|men|woman|women|boy|girl|child|children|kid|student|teacher|worker|farmer|"
                    r"athlete|acrobat|gymnast|dancer|skater|diver|swimmer|player|runner|cyclist|rider|driver|figure|"
                    r"human|body|someone|crowd|family|scientist|doctor|nurse|patient|baby|infant|parent|"
                    r"mother|father|soldier|king|queen)s?\b", re.I)
CLOTHED = ' Every person shown is fully and modestly clothed, in everyday, work or sports clothes suited to a school film.'


def compose(style, *, setting, visual_style, layout, scene, addendum=''):
    if PEOPLE.search(scene) and 'modestly clothed' not in scene:
        scene = scene.rstrip() + CLOTHED
    if style == 'descriptive_v1':
        parts = [visual_style.strip().rstrip('.') + '.', scene.strip(), setting.strip().rstrip('.') + '.', layout.strip()]
        return ' '.join(p for p in parts if p.strip('. ')) + addendum
    return ('Setting: ' + setting + '\nVisual treatment: ' + visual_style + '\nComposition: ' + layout +
            '\nScene: ' + scene + addendum)
