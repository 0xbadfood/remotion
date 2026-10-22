"""Exact figures in curriculum films: chemistry equations, structures (with a functional group highlighted), organic
reactions, resonance structures, curly-arrow mechanisms, Newman/sawhorse projections, chairs and atom models drawn
by code, and diagrams from the figure library (Wikimedia Commons, public domain / CC0 / CC BY, credited on screen).

Only films whose curriculum record carries "figures" get the figure beat kind (Chemistry Studio sets it); every other
film plans and renders exactly as before. The record's figures object:
  {"code_root": "/home/nitin/manim",                     # where figure_library lives
   "library_root": "/home/nitin/manim/figure-library/commons",
   "library": [{"id", "name", "description", "labels"}]} # the Commons figures this concept may use
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

FIGURE_TASK = (
    " This film teaches exact chemistry, and exact things must be shown exactly. Use kind:'figure' for a beat whose "
    "passage states an equation, a formula, a molecule's structure, an electron configuration, an atom model, energy "
    "levels, an orbital shape, a spectrum, or an apparatus or experiment that figure_options.library offers. A figure "
    "beat has a heading and summary like other beats, empty labels/details/image_brief, and a 'figure' object, one of: "
    "{type:'equation',latex:'\\\\ce{2H2 + O2 -> 2H2O}' or maths}; {type:'molecule',smiles,formula:'molecular formula, "
    "checked against the SMILES',name,highlight:'optional functional group to light up: hydroxyl, carbonyl, aldehyde, "
    "ketone, carboxylic acid, carboxylate, ester, ether, amine, amide, halide, alkene, alkyne, benzene ring, nitrile "
    "or nitro'}; {type:'reaction',reactants:[{smiles,formula}],products:[{smiles,formula}],conditions:'reagent or "
    "condition over the arrow, at most 40 characters',below:'optional text under it',balanced:true when every atom "
    "is shown on both sides (code checks)} for an organic reaction drawn as structures; {type:'resonance',"
    "structures:['2-4 SMILES with the same atoms in the same order, only double bonds and charges moved']} for "
    "resonance structures (benzene, carboxylate, an inductive or resonance effect); {type:'mechanism',stages:[{species:"
    "['SMILES with every atom the arrows touch numbered, e.g. [OH-:1] and [CH3:2][Br:3]; write an H that moves as "
    "[H:4]'],arrows:[{from:[1] (a lone pair on atom 1) or [2,3] (the bond 2-3),to:[1,2] (a new or stronger bond) or "
    "[3] (a lone pair on atom 3),electrons:1 for a fish-hook arrow moving one electron in a radical step (a "
    "breaking bond needs two, one to each atom; write a radical as [Br:1] or [CH:3])}],label:'optional, over the "
    "arrow into this stage, e.g. slow'}]} with 2-4 stages "
    "for a curly-arrow mechanism; the last stage is the products, with no arrows; keep the same atom numbers from "
    "stage to stage and every species, spectator ions too (code pushes the electrons and checks each stage makes "
    "the next); {type:'newman' or 'sawhorse',front:['3 groups on the front carbon, e.g. CH3, H, H'],back:['3 "
    "groups'],dihedral:degrees between the first front and first back group,conformation:'optional: staggered, "
    "eclipsed, anti, gauche or fully eclipsed (code checks it against the angle and names it if left out)'}; "
    "{type:'chair',substituents:[{carbon:1-6,position:'axial' or 'equatorial',group:'CH3'}],flip:true to show the "
    "ring flip, which code draws}; {type:'bohr',element:'Na'}; {type:'orbital_boxes',element:'N',"
    "valence_only:false} or {type:'orbital_boxes',config:'1s2 2s2 2p3'}; {type:'energy_levels',transitions:[[3,2],"
    "[4,2]]} (hydrogen: code computes the energies and wavelengths); {type:'orbital',orbital:'s'|'px'|'py'|'pz'|'dxy'|"
    "'dxz'|'dyz'|'dx2-y2'|'dz2'}; {type:'spectrum',kind:'electromagnetic'|'hydrogen'|'continuous'}; "
    "{type:'library',id:'an id from figure_options.library'}. Code draws every figure and computes every number, so "
    "give only what the narration states. Use a library figure only when its description shows what the narration "
    "says. Keep image beats for people, places, history, everyday scenes and the feel of an experiment; image briefs "
    "still carry no formulas, symbols or labels.")


BIOLOGY_TASK = (
    " This film teaches biology, and its structures must be shown exactly. Use kind:'figure' for a beat whose passage "
    "names or explains a structure, organ, cell part or process that figure_options.library draws: a figure beat has "
    "a heading and summary like other beats, empty labels/details/image_brief, and a 'figure' object {type:'biology',"
    "id:'an id from figure_options.library',stage:'optional: one of that diagram\'s stages, the one the passage "
    "describes',highlight:'optional: the one part id the passage is about (the rest dims)',labels:['at most 5 part "
    "ids of that diagram that the narration names around this beat, in the order it names them']}. Use the same "
    "diagram again with another highlight or stage as the narration moves on, so the student keeps one picture in "
    "mind; code draws it at the film's class level and places the labels. An equation the passage states may be "
    "{type:'equation',latex:'\\\\ce{6CO2 + 6H2O -> C6H12O6 + 6O2}'}. Keep image beats for people, places, "
    "everyday scenes, organisms and the feel of an experiment; image briefs carry no labels, and never ask an image "
    "to draw a structure the library has.")


def task_for(figures):
    """The figure instructions of a film's beat planner: biology diagrams or chemistry's code-drawn figures."""
    return BIOLOGY_TASK if (figures or {}).get('subject') == 'biology' else FIGURE_TASK


def biology(figures):
    root = str(Path(figures.get('code_root') or '/home/nitin/manim'))
    if root not in sys.path:
        sys.path.insert(0, root)
    from figure_library import biology as module
    return module


def options(evidence):
    """The figure options of a film, or None (films without them never see the figure kind)."""
    return ((evidence or {}).get('curriculum') or {}).get('figures') or None


def chem(figures):
    root = str(Path(figures.get('code_root') or '/home/nitin/manim'))
    if root not in sys.path:
        sys.path.insert(0, root)
    from figure_library import chem as module
    return module


def planner_options(figures):
    """What the visual-beat planner sees: the library figures it may use (paths stay out of the prompt)."""
    keys = ('id', 'name', 'description', 'labels', 'parts', 'stages')
    return {'library': [{k: f[k] for k in keys if k in f} for f in figures.get('library', [])]}


def scene_errors(scene, figures):
    sid, spec = scene.get('id'), scene.get('figure')
    if not figures:
        return [f'{sid}: unsupported visual beat kind']
    if not isinstance(spec, dict):
        return [f'{sid}: figure beats need a "figure" object']
    if figures.get('subject') == 'biology':
        if spec.get('type') == 'biology':
            return [f'{sid}: {e}' for e in biology(figures).figure_errors(spec, figures.get('level', 3))]
        if spec.get('type') != 'equation':
            return [f'{sid}: a biology film\'s figure is type biology or equation']
    if spec.get('type') == 'library':
        known = {f['id'] for f in figures.get('library', [])}
        return [] if spec.get('id') in known else [f'{sid}: library id must be one of figure_options.library']
    return [f'{sid}: {e}' for e in chem(figures).figure_errors(spec)]


def key(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:20]


SCALE = {'equation': 5.5}           # dvisvgm draws 10 pt type; 5.5x reads at ~70 px in a 1080p frame
LIBRARY_BOX = (1100, 640)           # the figure area of a 1080p frame


def library_png(svg_path, out, box=LIBRARY_BOX, pad=36):
    """A Commons drawing on a light card: most are drawn black on transparent, for a white page."""
    import cairosvg
    from io import BytesIO
    from PIL import Image, ImageDraw
    raw = Image.open(BytesIO(cairosvg.svg2png(url=str(svg_path), output_width=box[0] * 2))).convert('RGBA')
    raw.thumbnail((box[0] - 2 * pad, box[1] - 2 * pad), Image.LANCZOS)
    card = Image.new('RGBA', (raw.width + 2 * pad, raw.height + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle((0, 0, card.width - 1, card.height - 1), radius=18, fill='#f7f3e9')
    card.alpha_composite(raw, (pad, pad))
    card.save(out)


def render(store, films, figures):
    """Draw every figure beat to a PNG under the project (cached by its spec) and note file, hash and credit on the
    scene. Raises with every bad figure at once."""
    folder = Path(store.root) / 'figures'
    folder.mkdir(parents=True, exist_ok=True)
    library = {f['id']: f for f in figures.get('library', [])}
    errors = []
    for film in films:
        for scene in film['storyboard']['scenes']:
            if scene.get('kind') != 'figure':
                continue
            spec = scene['figure']
            level = figures.get('level', 3)
            out = folder / f'{key({**spec, "level": level} if spec["type"] == "biology" else spec)}.png'
            try:
                if not out.exists():
                    if spec['type'] == 'biology':
                        biology(figures).png(spec, out, level)
                    elif spec['type'] == 'library':
                        library_png(Path(figures['library_root']) / f'{spec["id"]}.svg', out)
                    else:
                        module = chem(figures)
                        import cairosvg
                        cairosvg.svg2png(bytestring=module.draw(spec).encode(), write_to=str(out),
                                         scale=SCALE.get(spec['type'], 2.0))
            except Exception as exc:                                     # noqa: BLE001 - reported together
                errors.append(f'{scene.get("id")}: {type(exc).__name__}: {exc}'[:300])
                continue
            scene['figure_file'] = str(out)
            scene['figure_sha256'] = hashlib.sha256(out.read_bytes()).hexdigest()
            if spec['type'] == 'library':
                scene['credit'] = library[spec['id']].get('credit', '')
    if errors:
        raise ValueError(f'Figures could not be drawn: {errors}')
