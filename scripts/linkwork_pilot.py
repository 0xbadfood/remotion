"""A Linkwork pilot film with no GPU work: LuxTTS narration on the CPU, Linkwork scenes drawn from data, Remotion
rendering, and each event placed on the word of the narration that names it.

  .venv/bin/python scripts/linkwork_pilot.py [--out DIR]

Topic: why hydrogen glows in coloured lines (NCERT Class 11 Chemistry, Ch 2: Bohr's model, the Balmer series; the
5 → 2 transition is the book's own worked example). Every number in the narration is the one Linkwork computes."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from infofilm.core import Store, configuration  # noqa: E402
from infofilm.lux_media import render_luxtts  # noqa: E402

LINKWORK = Path.home() / "linkwork"
NODE = Path.home() / ".local/node-v24.4.0/bin/node"
LEAD, GAP, TAIL = 1.2, 0.55, 2.5
# Whisper writes small numbers as digits; event words are written as words.
NUMBERS = {str(i): w for i, w in enumerate(["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"])}

# Each point may carry events: the photon event happens on the first `word` (after the previous event) in its clip.
POINTS = [
    {"id": "P01", "narration": "In Bohr's model, the electron in a hydrogen atom cannot have just any energy. It can only sit on "
     "certain fixed levels, called stationary states, like the rungs of a ladder. The lowest rung, n equals one, is at minus "
     "thirteen point six electron volts. And notice how the rungs crowd closer together as we go up."},
    {"id": "P02", "narration": "Now a photon arrives with exactly twelve point zero nine electron volts, the gap between rung one "
     "and rung three. The electron absorbs it and is suddenly on rung three. There is no in between. This is a quantum jump.",
     "events": [{"kind": "absorb", "from": 1, "to": 3, "word": "absorbs"}]},
    {"id": "P03", "narration": "It does not stay there for long. It falls to rung two, and gives out the difference, one point "
     "eight nine electron volts, as a photon of light. Its wavelength is six hundred and fifty six nanometres. That is red light.",
     "events": [{"kind": "emit", "from": 3, "to": 2, "word": "falls"}]},
    {"id": "P04", "narration": "From rung two, it drops back to the lowest rung. This photon carries ten point two electron volts, "
     "much more energy, so its wavelength is shorter, one hundred and twenty two nanometres. That is ultraviolet, which our eyes "
     "cannot see.",
     "events": [{"kind": "emit", "from": 2, "to": 1, "word": "drops"}]},
    {"id": "P05", "narration": "Give it a bigger photon, and it absorbs enough to reach rung four. When it falls from four to two, "
     "the photon carries two point five five electron volts. That is blue green light, at four hundred and eighty six nanometres.",
     "events": [{"kind": "absorb", "from": 1, "to": 4, "word": "absorbs"}, {"kind": "emit", "from": 4, "to": 2, "word": "falls"}]},
    {"id": "P06", "narration": "Lift it once more, from rung two to rung five, and let it fall back to two. This line is violet, at "
     "four hundred and thirty four nanometres. Your textbook works out this very transition, five to two.",
     "events": [{"kind": "absorb", "from": 2, "to": 5, "word": "five"}, {"kind": "emit", "from": 5, "to": 2, "word": "violet"}]},
    {"id": "P07", "narration": "Every fall that ends on rung two gives visible light. Together these lines are the Balmer series, the "
     "only lines of hydrogen in the visible region. Because the rungs never move, hydrogen always glows in these same colours. "
     "That is its line spectrum, its fingerprint."},
]


def clip_seconds(path):
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def word_times(clips):
    """Word start and end times in each point clip, from faster-whisper on the CPU."""
    from faster_whisper import WhisperModel
    model = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=8)
    out = {}
    for pid, clip in clips.items():
        segments, _ = model.transcribe(str(clip), language="en", word_timestamps=True, beam_size=5)
        out[pid] = [{"word": NUMBERS.get(re.sub(r"[^a-z0-9']", "", w.word.lower()), re.sub(r"[^a-z0-9']", "", w.word.lower())),
                     "start": w.start, "end": w.end}
                    for s in segments for w in s.words]
    return out


def captions(point, start, words, W, H):
    """One caption per sentence, timed by mapping the sentence's share of the narration's words onto the heard words."""
    sentences = []
    for sentence in re.findall(r"[^.!?]+[.!?]", point["narration"]):
        # A caption is one line: long sentences are split at commas into pieces of at most ~90 characters.
        line = ""
        for piece in re.findall(r"[^,]+,?", sentence):
            if line and len(line) + len(piece) > 90:
                sentences.append(line); line = piece
            else:
                line += piece
        sentences.append(line)
    total = sum(len(s.split()) for s in sentences)
    props, used = [], 0
    for i, sentence in enumerate(sentences):
        n = len(sentence.split())
        a = int(used / total * len(words)); b = max(a, int((used + n) / total * len(words)) - 1)
        used += n
        props.append({"id": f"cap-{point['id']}-{i}", "type": "text", "x": W / 2, "y": 0.955 * H, "size": 30 * H / 1080,
                      "align": "middle", "width": 0.9 * W, "text": sentence.strip(),
                      "show": [round(start + words[a]["start"], 3), round(start + words[b]["end"] + 0.15, 3)], "fade": 0.08,
                      "style": {"fill": "ink"}, "layer": 5})
    return props


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts" / "linkwork-pilot" / "hydrogen")
    args = parser.parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    config = {**configuration(), "tts_engine": "luxtts", "reference_audio": str(ROOT / "references" / "kane.wav"),
              "reference_transcript": json.loads((ROOT / "references" / "kane.json").read_text())["expected_text"]}
    store = Store(out)
    final, info = render_luxtts(config, store, [{"id": p["id"], "narration": p["narration"]} for p in POINTS], "pilot-narration")
    clips = {pid: Path(path) for pid, path in info["point_clips"].items()}
    words = word_times(clips)

    W, H = 1920, 1080
    t, events, caption_props, starts = LEAD, [], [], {}
    for p in POINTS:
        starts[p["id"]] = t
        cursor = 0
        for e in p.get("events", []):
            hit = next((i for i, w in enumerate(words[p["id"]]) if i >= cursor and w["word"] == e["word"]), None)
            if hit is None:
                raise RuntimeError(f"{p['id']}: the word {e['word']!r} was not heard in its clip")
            cursor = hit + 1
            events.append({"at": round(t + words[p["id"]][hit]["start"], 3), "kind": e["kind"], "from": e["from"], "to": e["to"]})
        caption_props += captions(p, t, words[p["id"]], W, H)
        t += clip_seconds(clips[p["id"]]) + GAP
    duration = t - GAP + TAIL
    spec = {"verb": "energy_ladder", "title": "Why hydrogen glows in coloured lines", "start": 1, "events": events,
            "spectrum": True, "duration": round(duration, 3), "width": W, "height": H}
    (out / "spec.json").write_text(json.dumps(spec, indent=1))

    node = [str(NODE), str(LINKWORK / "src" / "cli.ts")]
    scene = json.loads(subprocess.run([*node, "compile", str(out / "spec.json")], capture_output=True, text=True, check=True).stdout)
    scene["props"] = [p for p in scene["props"] if p["id"] != "axis-note"] + caption_props
    (out / "scene.json").write_text(json.dumps(scene))
    check = subprocess.run([*node, "check", str(out / "scene.json"), "--every", "2"], capture_output=True, text=True)
    if check.returncode:
        raise RuntimeError("Linkwork check failed:\n" + check.stdout)

    # Narration track: each point's clip at its start, 48 kHz mono.
    sr = 48000
    track = np.zeros(int(duration * sr) + sr, dtype=np.int16)
    for pid, clip in clips.items():
        with wave.open(str(clip)) as w:
            samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        i = int(starts[pid] * sr)
        track[i:i + len(samples)] = samples
    with wave.open(str(out / "narration.wav"), "wb") as w:
        w.setparams((1, 2, sr, 0, "NONE", "not compressed"))
        w.writeframes(track[:int(duration * sr)].tobytes())

    (out / "props.json").write_text(json.dumps({"scene": scene}))
    renderer = ROOT / "renderer"
    subprocess.run([str(renderer / "node_modules/.bin/remotion"), "render", "src/index.ts", "Linkwork", str(out / "picture.mp4"),
                    f"--props={out / 'props.json'}", "--concurrency=6", "--log=error"], cwd=renderer, check=True,
                   env={"PATH": f"{NODE.parent}:/usr/bin:/bin"})
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(out / "picture.mp4"), "-i", str(out / "narration.wav"),
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000",
                    "-c:a", "aac", "-b:a", "256k", "-shortest", str(out / "film.mp4")], check=True)
    print(json.dumps({"film": str(out / "film.mp4"), "seconds": round(duration, 1), "events": events,
                      "narration_wpm": info["observed_words_per_minute"], "point_attempts": info["point_attempts"]}, indent=1))


if __name__ == "__main__":
    main()
