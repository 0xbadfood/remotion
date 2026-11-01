"""School renditions: a finished film carried as a bundle, then re-voiced and re-rendered with no planning (user, 2026-10-31).

A bundle holds one concept film's plans and pictures with relative paths and a hash for every file: script, storyboard
with its word anchors and Linkwork specs, compiled Linkwork scenes, images, depth maps, clips, figures, evidence and
credits. A rendition reads the bundle and a teacher's recording: voice, audit, alignment, narration room, timeline,
render, QA. Compiled scenes are rendered as they were approved; a Linkwork change reaches a bundle only by re-export.

Usage: python -m infofilm.rendition export <run> <bundles>/<chapter>/<concept>
       python -m infofilm.rendition voice <recording> <out-dir>
       python -m infofilm.rendition render <bundle> <out-dir> --voice <out-dir-of-voice> --template teal [--logo x.png] [--name "Asha Rao"]
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .core import Store, configuration as load_config, digest, read, resource_slot, write

FORMAT = "infofilm.bundle.v1"
# The passage every narrator reads. The consent sentence stays out of the voice reference: it is checked and kept apart.
PASSAGE = ("Mumbai is the financial, commercial and the entertainment capital of India. It is also one of the world's "
           "top ten centres of commerce in terms of global financial flow.")
ARBITER = "audio-semantic-"       # the one LLM a rendition may use: reliable audio needs it
PER_VOICE = ("room_seconds", "linkwork_restart")     # the narration room's notes on one voice's timing

# Colours for the film's chrome and cards, and Linkwork's tokens. Linkwork draws on the film's background. Dark only:
# book figures are drawn in white and image headings are white, so a light template loses them (stills, 2026-11-01).
TEMPLATES = {
    "teal": {"label": "Deep teal", "background": "#082d34", "accent": "#f3b85e", "text": "#f7f3e9", "chrome": "#bfd5d5", "chrome_dim": "#68898d",
             "track": "#183c42", "card": "#103e46", "card_alt": "#483927", "line": "#4a7c82", "backdrop": True,
             "linkwork": {"paper": "#082d34", "ink": "#f4efe4", "muted": "#8fb3b8", "faint": "#2f5960", "accent": "#f3b85e", "figure": "#f4efe4", "good": "#8fd18f", "bad": "#ff8a7a", "cool": "#7cc4ff"}},
    "midnight": {"label": "Midnight blue", "background": "#101a33", "accent": "#ffc35c", "text": "#f3f5fb", "chrome": "#c3cde6", "chrome_dim": "#7584a8",
                 "track": "#222e50", "card": "#1b2850", "card_alt": "#47391f", "line": "#55679a", "backdrop": False,
                 "linkwork": {"paper": "#101a33", "ink": "#f3f5fb", "muted": "#9fadcf", "faint": "#34426b", "accent": "#ffc35c", "figure": "#f3f5fb", "good": "#8fd8a0", "bad": "#ff9384", "cool": "#86c8ff"}},
    "blackboard": {"label": "Blackboard", "background": "#17322a", "accent": "#f6d365", "text": "#f5f3e7", "chrome": "#c6d8cd", "chrome_dim": "#7d9a8b",
                   "track": "#264a3e", "card": "#21463a", "card_alt": "#4c4122", "line": "#5f8a79", "backdrop": False,
                   "linkwork": {"paper": "#17322a", "ink": "#f5f3e7", "muted": "#a9c4b6", "faint": "#3c6152", "accent": "#f6d365", "figure": "#f5f3e7", "good": "#9fe0a4", "bad": "#ff9f8f", "cool": "#8fd0f5"}},
}


def luminance(colour):
    channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4 for c in channels]
    return .2126 * r + .7152 * g + .0722 * b


def contrast(a, b):
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + .05) / (low + .05)


def template_errors(template):
    """A template must be readable: words at 7:1, Linkwork's marks and the chrome at 3:1 and over."""
    errors, paper = [], template["background"]
    for name, colour, floor in [("text", template["text"], 7), ("chrome", template["chrome"], 4.5), ("accent", template["accent"], 3)]:
        if contrast(colour, paper) < floor:
            errors.append(f"{name} on the background is below {floor}:1")
    for card in ("card", "card_alt"):
        if contrast(template["text"], template[card]) < 4.5:
            errors.append(f"text on {card} is below 4.5:1")
    tokens = template["linkwork"]
    for token, floor in [("ink", 7), ("figure", 7), ("muted", 4.5), ("accent", 3), ("good", 3), ("bad", 3), ("cool", 3)]:
        if contrast(tokens[token], tokens["paper"]) < floor:
            errors.append(f"Linkwork {token} on paper is below {floor}:1")
    return errors


def _file_sha(path):
    return digest(Path(path).read_bytes())


def _carry(value, bundle, assets, run):
    """A copy of `value` with every file inside this run moved into the bundle and named by its contents."""
    if isinstance(value, dict):
        return {k: _carry(v, bundle, assets, run) for k, v in value.items()}
    if isinstance(value, list):
        return [_carry(v, bundle, assets, run) for v in value]
    if isinstance(value, str) and value.startswith("/") and Path(value).is_file():
        source = Path(value)
        name = f"assets/{_file_sha(source)[:24]}{source.suffix}"
        if name not in assets:
            (bundle / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, bundle / name)
            assets[name] = True
        return name
    return value


def _resolve(value, bundle):
    """The bundle's relative paths as absolute ones, for the engine."""
    if isinstance(value, dict):
        return {k: _resolve(v, bundle) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, bundle) for v in value]
    if isinstance(value, str) and value.startswith("assets/"):
        return str(bundle / value)
    return value


def film_dir(run):
    films = sorted(Path(run).glob("film-*/storyboard.json"))
    if len(films) != 1:
        raise ValueError(f"expected one film in {run}, found {len(films)}")
    return films[0].parent


def export(run, bundle, *, chapter=None, concept=None):
    run, bundle = Path(run).resolve(), Path(bundle)
    if (bundle / "bundle.json").exists():
        raise ValueError(f"{bundle} already holds a bundle: export to a new folder")
    delivery = read(run / "delivery.json")
    if delivery.get("music_engine", "none") != "none":
        raise ValueError("films with music are not carried yet: music would have to be mixed under each new voice")
    film = film_dir(run)
    script, storyboard = read(film / "script.json"), read(film / "storyboard.json")
    used = {s.get("asset_id") for s in storyboard["scenes"] if s.get("asset_id")}
    images = {k: v for k, v in read(run / "image_manifest.json").items() if k in used}
    missing = used - set(images)
    if missing:
        raise ValueError(f"storyboard names images the manifest lacks: {sorted(missing)}")
    bundle.mkdir(parents=True)
    assets = {}
    carried = _carry({k: v for k, v in storyboard.items()}, bundle, assets, run)
    for scene in carried["scenes"]:
        for key in PER_VOICE:
            scene.pop(key, None)
    write(bundle / "script.json", script)
    write(bundle / "storyboard.json", carried)
    write(bundle / "images.json", {k: _carry({"file": v["file"], "sha256": v["sha256"], **({"needs_review": True, "review": {"score": v["review"]["score"]}} if v.get("needs_review") else {})}, bundle, assets, run)
                                   for k, v in images.items()})
    extras = {}
    for name, path, key in [("depth", run / "depth" / "manifest.json", "maps"), ("clips", run / "clips" / "manifest.json", "clips")]:
        if path.exists():
            kept = {k: v for k, v in read(path).get(key, {}).items() if k in images and v.get("source_sha256") == images[k]["sha256"]}
            extras[name] = {key: _carry(kept, bundle, assets, run)}
    if (run / "backdrop" / "current.json").exists():
        extras["backdrop"] = _carry(read(run / "backdrop" / "current.json"), bundle, assets, run)
    write(bundle / "extras.json", extras)
    # The evidence travels as what the film needs: facts and their sources, no paths back into our digest or machines.
    write(bundle / "evidence-final.json", {"facts": [{k: f[k] for k in ("id", "text", "statement", "source_ids") if k in f} for f in read(run / "evidence-final.json")["facts"]]})
    shutil.copy2(run / "creative-brief.json", bundle / "creative-brief.json")
    sources = read(run / "sources.json")
    write(bundle / "sources.json", {"results": [{k: r[k] for k in ("url", "title", "credit") if k in r} for r in sources["results"]]})
    output = delivery["outputs"][0]
    timeline = read(output["timeline"])["data"]
    write(bundle / "master.json", {"seconds": output["actual_seconds"], "width": timeline["width"], "height": timeline["height"], "fps": timeline["fps"]})
    files = {str(p.relative_to(bundle)): _file_sha(p) for p in sorted(bundle.rglob("*")) if p.is_file()}
    commit = subprocess.run(["git", "-C", str(Path.home() / "linkwork"), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    write(bundle / "bundle.json", {"format": FORMAT, "chapter": chapter, "concept": concept, "title": script["title"], "subject": delivery.get("subject"),
                                   "source_run": run.name, "requested_seconds": script.get("requested_seconds"), "linkwork_commit": commit,
                                   "exported": time.strftime("%Y-%m-%d %H:%M"), "files": files})
    return bundle


def write_chapter(root, title):
    """The chapter's manifest: its films in order, each named by the hash of its own bundle.json."""
    root = Path(root)
    films = sorted((p for p in root.iterdir() if re.fullmatch(r"C\d+", p.name)), key=lambda p: int(p.name[1:]))
    return write(root / "chapter.json", {"format": FORMAT, "title": title, "films": [{"concept": p.name, "title": read(p / "bundle.json")["title"], "bundle_sha256": _file_sha(p / "bundle.json")} for p in films]})


def verify(bundle):
    bundle = Path(bundle)
    manifest = read(bundle / "bundle.json")
    if manifest.get("format") != FORMAT:
        raise ValueError(f"unknown bundle format {manifest.get('format')}")
    bad = [name for name, sha in manifest["files"].items() if not (bundle / name).is_file() or _file_sha(bundle / name) != sha]
    if bad:
        raise ValueError(f"bundle files changed or missing: {bad[:5]}")
    return manifest


# --- the recording

SECONDS = (8.0, 45.0)        # a shorter take gives the voice too little; a longer one is not this passage
MIN_SNR_DB = 25.0            # speech above the room. Our own recordings stand well over this
MAX_CLIPPED = 0.001
MIN_LEVEL_DB = -40.0         # a take too quiet for the room to matter is still too quiet
MIN_MATCH = 0.7              # share of the passage's words heard (a correct clean take scored .86 on Whisper small)


def _words(text):
    return re.findall(r"[a-z']+", text.lower())


def voice_measures(path):
    """Decode to mono floats and measure: speech level (loud frames) against the room (quiet frames), clipping, length."""
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "48000", "-f", "f32le", "-"], capture_output=True, check=True).stdout
    samples = np.frombuffer(raw, dtype=np.float32)
    frame = 2400                # 50 ms
    count = len(samples) // frame
    if not count:
        return {"seconds": len(samples) / 48000, "speech_db": -120.0, "room_db": -120.0, "snr_db": 0.0, "clipped": 0.0}
    rms = np.sqrt(np.mean(samples[:count * frame].reshape(count, frame) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-9)
    speech, room = float(np.percentile(db, 90)), float(np.percentile(db, 10))
    return {"seconds": round(len(samples) / 48000, 2), "speech_db": round(speech, 1), "room_db": round(room, 1), "snr_db": round(speech - room, 1),
            "clipped": round(float(np.mean(np.abs(samples) >= .999)), 5)}


def passage_match(heard, passage=PASSAGE):
    """Share of the passage's words heard, in order."""
    from difflib import SequenceMatcher
    want = _words(passage)
    return sum(b.size for b in SequenceMatcher(None, want, _words(heard), autojunk=False).get_matching_blocks()) / len(want)


def voice_problems(measures, match=None):
    """What the teacher must fix, in their words; empty when the take is usable. Noise means record again, not repair."""
    problems = []
    if measures["seconds"] < SECONDS[0]:
        problems.append("The recording is too short. Please read the whole passage.")
    elif measures["seconds"] > SECONDS[1]:
        problems.append("The recording is too long. Please read only the passage, then stop.")
    if measures["speech_db"] < MIN_LEVEL_DB:
        problems.append("Your voice is too quiet. Please move closer to the microphone.")
    elif measures["snr_db"] < MIN_SNR_DB:
        problems.append("There is too much background noise. Please find a quieter room and record again.")
    if measures["clipped"] > MAX_CLIPPED:
        problems.append("The recording is distorted. Please move a little further from the microphone.")
    if match is not None and match < MIN_MATCH:
        problems.append("We could not hear the passage clearly. Please read it exactly as written.")
    return problems


def hear(path, config):
    """What Whisper hears in a take, on this machine's CPU."""
    from faster_whisper import WhisperModel
    model = WhisperModel(config.get("rendition_whisper", "Systran/faster-whisper-small"), device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(path), language="en", vad_filter=True)
    return " ".join(s.text for s in segments)


def clean_voice(path, destination):
    """The cleaning our own voices had: 80 Hz high-pass, light denoise, level. Only a take that passed reaches this."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "48000", "-af",
                    "highpass=f=80,afftdn=nr=10:nf=-50,loudnorm=I=-20:TP=-1.5:LRA=11,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1",
                    "-c:a", "pcm_s16le", str(destination)], check=True)
    return destination


def prepare_voice(recording, out, config=None, *, transcript=PASSAGE):
    """Check a recording and, if usable, clean it into out/voice.wav. Returns {ok, problems, measures, match}."""
    config = config or load_config()
    out = Path(out)
    measures = voice_measures(recording)
    problems = voice_problems(measures)
    match = None
    if not problems:
        match = passage_match(hear(recording, config), transcript)
        problems = voice_problems(measures, match)
    report = {"ok": not problems, "problems": problems, "measures": measures, "match": None if match is None else round(match, 3)}
    out.mkdir(parents=True, exist_ok=True)
    write(out / "voice-check.json", report)
    if not problems:
        clean_voice(recording, out / "voice.wav")
        write(out / "voice.json", {"reference_audio": str((out / "voice.wav").resolve()), "reference_transcript": transcript})
    return report


# --- the rendition

def logo_uri(path):
    """A logo re-encoded as a small PNG inside the timeline: no script from an SVG, nothing large in the props."""
    from PIL import Image
    image = Image.open(path).convert("RGBA")
    image.thumbnail((256, 256))
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


def disclosure(name):
    who = f"{' '.join(name.split())}'s voice" if name and name.strip() else "the teacher's voice"
    return f"Narration by AI, in {who}, used with consent."


def dress(data, template, *, logo=None, name=None):
    """The school's look on a compiled timeline's data: colours, Linkwork tokens, logo, the disclosure line."""
    errors = template_errors(template)
    if errors:
        raise ValueError(f"template fails its contrast check: {errors}")
    data = {**data, "creativeBrief": {**(data.get("creativeBrief") or {}), "theme": {k: template[k] for k in ("background", "accent", "text")}}}
    scenes = []
    for scene in data["scenes"]:
        scene = dict(scene)
        if scene.get("linkwork"):
            scene["linkwork"] = {**scene["linkwork"], "theme": template["linkwork"]}
        if not template["backdrop"]:
            scene.pop("backdrop", None)
        scenes.append(scene)
    data["scenes"] = scenes
    data["school"] = {"chrome": template["chrome"], "chromeDim": template["chrome_dim"], "track": template["track"], "card": template["card"],
                      "cardAlt": template["card_alt"], "line": template["line"], "disclosure": disclosure(name),
                      **({"logo": logo_uri(logo)} if logo else {})}
    return data


FORBIDDEN_PATHS = ("textbook-digest", "kb_research", "/planning/")
_sealed = False
_watching = False


def _watch(event, args):
    if _watching and event == "open" and isinstance(args[0], (str, Path)) and any(part in str(args[0]) for part in FORBIDDEN_PATHS):
        raise PermissionError(f"a rendition must not read {args[0]}: the bundle provides everything")


@contextlib.contextmanager
def sealed():
    """A rendition has only its bundle (user, 2026-11-01): no digest, no planning data, and no LLM but the audio arbiter."""
    global _sealed, _watching
    from .llm import Models
    original = Models.call

    def only_arbiter(self, role, name, *a, **k):
        if not str(name).startswith(ARBITER):
            raise PermissionError(f"a rendition may call an LLM only for the audio arbiter, not {name}")
        return original(self, role, name, *a, **k)

    if not _sealed:
        sys.addaudithook(_watch)
        _sealed = True
    Models.call = only_arbiter
    _watching = True
    try:
        yield
    finally:
        _watching = False
        Models.call = original


def anchor_problems(storyboard, timing):
    known = {w["id"] for w in timing["words"]}
    return [s.get("id") for s in storyboard["scenes"] if s.get("start_word_id") and s["start_word_id"] not in known]


def render_film(bundle, out, *, voice, template="teal", logo=None, name=None, config=None):
    bundle, out = Path(bundle).resolve(), Path(out).resolve()
    manifest = verify(bundle)
    chosen = TEMPLATES[template]
    profile = read(Path(voice) / "voice.json")
    master = read(bundle / "master.json")
    config = {**(config or load_config()), **profile, "tts_engine": "luxtts", "tts_fallback": "none", "width": master["width"], "height": master["height"],
              "fps": master["fps"], "phrase_root": str(Path(voice).resolve() / "phrases")}
    store = Store(out)
    try:
        with sealed():
            return _render_film(bundle, out, store, manifest, chosen, config, voice, template, logo, name, master)
    finally:
        store.db.close()


def _render_film(bundle, out, store, manifest, chosen, config, voice, template, logo, name, master):
    from .lux_media import render_luxtts
    from .media import align_audio, audit_audio
    from .music import prepare_music
    from .narration_room import apply as give_room
    from .pipeline import point_audited, script_text
    from .video import compile_timeline, render
    script = read(bundle / "script.json")
    duration = script.get("requested_seconds") or round(master["seconds"])
    storyboard = _resolve(read(bundle / "storyboard.json"), bundle)
    images = _resolve(read(bundle / "images.json"), bundle)
    extras = _resolve(read(bundle / "extras.json"), bundle)
    if extras.get("depth"):
        write(out / "depth" / "manifest.json", extras["depth"])
    if extras.get("clips"):
        write(out / "clips" / "manifest.json", extras["clips"])
    if extras.get("backdrop"):
        write(out / "backdrop" / "current.json", extras["backdrop"])
    store.event("rendition", "voice", {"title": manifest["title"], "template": template})
    audio, info = render_luxtts(config, store, script["points"], "rendition")
    audit = point_audited(audit_audio(config, store, audio, script_text(script), points=script["points"]), info)
    write(out / "audit.json", audit)
    if audit["status"] != "passed":
        store.event("rendition", "audit_failed", {"reasons": audit.get("reasons"), "points": audit.get("failed_points")})
        raise RuntimeError("the narration in this voice did not pass its audit; no film was made")
    timing = align_audio(config, store, audio, script, "rendition-alignment", spoken_points=info.get("spoken_points"))
    lost = anchor_problems(storyboard, timing)
    if lost:
        raise ValueError(f"scene anchors missing from this voice's words: {lost}")
    film = {"script": script, "storyboard": storyboard, "timing": timing, "audio": str(audio), "audio_info": info, "actual_seconds": info["duration_seconds"],
            "requested_seconds": duration}
    (out / f"film-{duration}").mkdir(parents=True, exist_ok=True)
    give_room(config, store, [film])
    film["music"] = prepare_music(config, store, film, read(bundle / "creative-brief.json"), "none")
    root, timeline = compile_timeline(config, store, script, film["timing"], storyboard, images, read(bundle / "evidence-final.json"), read(bundle / "sources.json"),
                                      duration, creative_brief=read(bundle / "creative-brief.json"), music=film["music"])
    value = read(timeline)
    value["data"] = dress(value["data"], chosen, logo=logo, name=name)
    write(timeline, value)
    with resource_slot("remotion-render", config.get("render_slots", 2)):    # shared with the concept films
        video = render(config, store, root, timeline)
    result = {"video": str(video), "seconds": film["actual_seconds"], "qa": read(root / "qa.json")["passed"], "template": template, "bundle": manifest["concept"], "voice": str(voice)}
    write(out / "rendition.json", result)
    store.event("rendition", "succeeded", result)
    return result


def main():
    parser = argparse.ArgumentParser(prog="infofilm.rendition")
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("export")
    a.add_argument("run")
    a.add_argument("bundle")
    a.add_argument("--chapter")
    a.add_argument("--concept")
    a = sub.add_parser("voice")
    a.add_argument("recording")
    a.add_argument("out")
    a = sub.add_parser("render")
    a.add_argument("bundle")
    a.add_argument("out")
    a.add_argument("--voice", required=True)
    a.add_argument("--template", default="teal", choices=sorted(TEMPLATES))
    a.add_argument("--logo")
    a.add_argument("--name")
    args = parser.parse_args()
    if args.command == "export":
        print(export(args.run, args.bundle, chapter=args.chapter, concept=args.concept))
    elif args.command == "voice":
        print(json.dumps(prepare_voice(args.recording, args.out)))
    else:
        print(json.dumps(render_film(args.bundle, args.out, voice=args.voice, template=args.template, logo=args.logo, name=args.name)))


if __name__ == "__main__":
    main()
