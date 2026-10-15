from __future__ import annotations

import math
import re
import shutil
import subprocess
from pathlib import Path

from .core import ROOT, digest, read, write
from .creative import resolved_scenes


def video_stream_hash(path):
    """Hash encoded video packets, excluding audio and container metadata."""
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
        "-map", "0:v:0", "-c:v", "copy", "-f", "streamhash", "-hash", "sha256", "-"],
        capture_output=True, text=True, check=True)
    return result.stdout.strip()


def replace_audio(config, store, output, music):
    """Stream-copy checked picture and replace its audio; no Remotion invocation."""
    import sys
    import time
    original = Path(output["video"])
    old_root = original.parent
    old_qa = read(old_root / "qa.json")
    master_info = music["master_audio_info"]
    master = Path(music["master_audio"])
    if not old_qa["passed"] or digest(original.read_bytes()) != old_qa["video_sha256"]:
        raise ValueError("The source video changed or has not passed delivery checks")
    if digest(master.read_bytes()) != master_info["audio_sha256"]:
        raise ValueError("Delivery master changed after its speech audit")
    compile_report = read(old_root / "compile-report.json")
    if abs(master_info["duration_seconds"] - compile_report["actual_seconds"]) > 1/48000 + 1e-6:
        raise ValueError("Replacement audio changed narration duration")
    identity = digest({"video": old_qa["video_sha256"], "master": master_info["audio_sha256"],
                       "policy": "copy-picture-replace-audio-v1"})
    root = old_root.parent / identity[:16]
    root.mkdir(parents=True, exist_ok=True)
    final = root / "video.mp4"
    if final.exists() and (root / "qa.json").exists():
        qa = read(root / "qa.json")
        if qa["passed"] and digest(final.read_bytes()) == qa["video_sha256"]:
            store.event("remux", "succeeded", {"video": str(final), "cached": True, "picture_reused": True})
            return final
        raise ValueError("Saved remux changed after delivery checks")
    start = time.monotonic()
    shutil.copyfile(master, root / "master.wav")
    shutil.copyfile(old_root / "narration.wav", root / "narration.wav")
    if digest((root / "narration.wav").read_bytes()) != compile_report["source_audio_sha256"]:
        raise ValueError("Narration snapshot changed")
    # The timeline and browser measurements remain valid because picture is copied.
    shutil.copyfile(old_root / "timeline.json", root / "timeline.json")
    shutil.copytree(old_root / "layout", root / "layout", dirs_exist_ok=True)
    write(root / "music.json", music)
    write(root / "source.json", {**master_info, "audio_file": "master.wav"})
    write(root / "compile-report.json", {**compile_report, "identity": identity,
        "delivery_master_sha256": master_info["audio_sha256"], "music_engine": music["engine"],
        "picture_reused": True, "source_video": str(original), "source_video_sha256": old_qa["video_sha256"]})
    sys.path.insert(0, config["filmora_root"])
    from backend.mux import continuous_audio_command
    from backend.qa import run_qa
    temporary = root / "video.pending.mp4"
    temporary.unlink(missing_ok=True)
    command = continuous_audio_command(original, root / "master.wav", temporary)
    write(root / "render-request.json", {"command": command, "method": "ffmpeg stream copy", "remotion_rendered": False})
    store.event("remux", "running", {"source": str(original), "output": str(final), "picture_reused": True})
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    temporary.replace(final)
    source_hash, result_hash = video_stream_hash(original), video_stream_hash(final)
    reuse = {"source_video": str(original), "source_video_sha256": old_qa["video_sha256"],
        "source_encoded_video_hash": source_hash, "output_encoded_video_hash": result_hash,
        "passed": source_hash == result_hash, "timeline_sha256": digest((root / "timeline.json").read_bytes()),
        "layout_reused_from": str(old_root / "layout"), "remotion_rendered": False}
    write(root / "picture-reuse.json", reuse)
    if not reuse["passed"]:
        raise RuntimeError("Remux changed the encoded video stream")
    data = read(root / "timeline.json")["data"]
    package = write(root / "qa-package.json", {"source": read(root / "source.json")})
    timeline = write(root / "qa-timeline.json", {k: data[k] for k in ["width", "height", "fps", "durationInFrames"]})
    pending_qa = root / "qa.pending.json"
    pending_qa.unlink(missing_ok=True)
    try:
        report = run_qa(final, package, timeline, root, pending_qa)
    except SystemExit as exc:
        raise RuntimeError(f"Replacement audio failed delivery checks; inspect {pending_qa}") from exc
    report["checks"]["encoded_video_unchanged"] = True
    write(root / "qa.json", report)
    pending_qa.unlink()
    write(root / "render-result.json", {"elapsed_seconds": round(time.monotonic()-start, 3),
        "video": str(final), "qa_passed": True, "picture_reused": True, "remotion_rendered": False})
    store.event("remux", "succeeded", {"video": str(final), "qa": True, "picture_reused": True})
    return final


def point_captions(timing):
    captions = []
    words = {w["id"]:w for w in timing["words"]}
    for point in timing["points"]:
        group = []
        for word_id in point["word_ids"]:
            word = words[word_id]
            group.append(word)
            if len(group) >= 10 or word["end"]-group[0]["start"] > 3 or word["word"].endswith((".","?","!",";")):
                captions.append({"start":group[0]["start"], "end":group[-1]["end"], "text":" ".join(w["word"] for w in group)})
                group = []
        if group:
            captions.append({"start":group[0]["start"], "end":group[-1]["end"], "text":" ".join(w["word"] for w in group)})
    return captions


STOP_WORDS = {"the", "and", "for", "with", "from", "into", "that", "this", "was", "were", "are", "its", "their", "his",
              "her", "they", "them", "who", "which", "what", "when", "where", "over", "under", "than", "then", "also",
              "not", "but", "all", "any", "one", "two", "new", "more", "most", "very", "been", "has", "had", "have"}


def _stems(text):
    return {w[:6] for w in re.findall(r"[a-z0-9]+", str(text).casefold())
            if (len(w) >= 3 or w.isdigit()) and w not in STOP_WORDS}


def _same(a, b):
    return a.startswith(b) or b.startswith(a)      # "tax" / "taxes", "refusi" / "refusa" do not match; "boycot" does


def label_reveals(labels, details, words, start, end, fps):
    """Frame offsets (from the scene start) at which each diagram card appears. The first card is there at the cut;
    each later one appears when the narration first names it (a moment early), in card order, matching first on the
    words only that card has ("11 March deadline" vs "12 March start"). Cards the narration never names (or names out
    of order) are spread evenly between their neighbours. Everything is visible by 85% of the scene."""
    n, length = len(labels), end - start
    if not n:
        return []
    latest, lead, gap = max(0, int(length * .85)), round(.25 * fps), round(.4 * fps)
    spoken = [(round(w["start"] * fps) - start, _stems(w["word"])) for w in words if start <= round(w["start"] * fps) < end]
    cards = [_stems(label) or _stems(details[i] if i < len(details) else "") for i, label in enumerate(labels)]
    found, previous = [0], 0
    for i in range(1, n):
        own = cards[i] - set().union(*(c for k, c in enumerate(cards) if k != i))
        hit = None
        for stems in (own, cards[i]):
            hit = next((f for f, said in spoken if f > previous and any(_same(a, b) for a in said for b in stems)), None)
            if hit is not None:
                break
        value = None if hit is None else min(latest, max(0, hit - lead))
        found.append(value)
        previous = value if value is not None else previous
    i = 0
    while i < n:                                   # fill each run of unnamed cards between its named neighbours
        if found[i] is not None:
            i += 1
            continue
        j = i
        while j < n and found[j] is None:
            j += 1
        left = found[i - 1] if i else None
        right = found[j] if j < n else latest
        for k in range(i, j):
            if left is None:                       # the scene opens on these cards: the first shows at once
                found[k] = round(right * (k - i) / (j - i)) if j < n else round(latest * (k - i) / max(1, j - i - 1))
            else:
                found[k] = round(left + (right - left) * (k - i + 1) / (j - i + 1))
        i = j
    reveals, previous = [], None
    for value in found:
        value = value if previous is None else max(value, previous + gap)
        reveals.append(min(latest, value))
        previous = reveals[-1]
    return reveals


def compile_timeline(config, store, script, timing, storyboard, images, evidence, sources, target, *, creative_brief=None, music=None):
    from .backdrop import current as current_backdrop
    info = timing["audio_info"]
    fps = config["fps"]
    frames = math.ceil(info["sample_count"] * fps / info["sample_rate"])
    backdrop = current_backdrop(store)
    from .depth import current as current_depth
    depth = current_depth(store, images) if config.get("image_depth", True) else {}
    identity = digest({"script": script, "timing": timing, "storyboard": storyboard, "images": images,
                       **({"backdrop": backdrop["sha256"]} if backdrop else {}),
                       **({"depth": {k: v["sha256"] for k, v in sorted(depth.items())}} if depth else {}),
                       "size": [config["width"], config["height"]], "fps": fps, "caption_policy":"point-and-sentence-v2", "reveal_policy": "spoken-words-v1", "transition_seconds": config.get("transition_seconds", .5), "depth_strength": config.get("depth_strength", 1),
                       "creative_brief": creative_brief, "music": music,
                       "renderer_revision": {str(p.relative_to(ROOT / 'renderer')): digest(p.read_bytes())
                           for folder in ['src','public/fonts'] for p in sorted((ROOT / 'renderer' / folder).glob('*')) if p.is_file()}})
    orientation = "portrait" if config["height"] > config["width"] else "landscape"
    root = store.root / "videos" / f"{target}s-{orientation}" / identity[:16]
    root.mkdir(parents=True, exist_ok=True)
    public = ROOT / "renderer/public"
    media = public / identity[:16]
    media.mkdir(parents=True, exist_ok=True)
    audio = root / "narration.wav"
    shutil.copyfile(timing["audio"], audio)
    if digest(audio.read_bytes()) != timing["audio_sha256"]:
        raise ValueError("Audio snapshot hash mismatch")
    master = root / "master.wav"
    master_info = info
    if music and music.get("engine") != "none":
        shutil.copyfile(music["master_audio"], master)
        master_info = music["master_audio_info"]
        if digest(master.read_bytes()) != master_info["audio_sha256"]:
            raise ValueError("Delivery master hash mismatch")
        write(root / "music.json", music)
    else:
        shutil.copyfile(audio, master)
    fact_sources = {f["id"]: f["source_ids"] for f in evidence["facts"]}
    planned = resolved_scenes(storyboard, script, timing)
    starts = [0] + [math.floor(s["start"] * fps + .5) for s in planned[1:]]
    ends = starts[1:] + [frames]
    scenes = []
    point_by_id = {p["id"]: p for p in script["points"]}
    for i, (scene, start, end) in enumerate(zip(planned, starts, ends)):
        point = point_by_id[scene["point_id"]]
        if end <= start:
            raise ValueError("Point intervals collapse at the chosen frame rate")
        value = {**scene, "id": f"scene-{i+1:02d}", "index": i, "from": start, "duration": end-start,
                 "source_ids": sorted({s for fact in point["fact_ids"] for s in fact_sources[fact]})}
        if scene["kind"] != "image" and scene.get("labels"):
            value["reveals"] = label_reveals(scene["labels"], scene.get("details") or [], timing["words"], start, end, fps)
        if scene["kind"] == "image":
            asset = images[scene["asset_id"]]
            if asset.get('needs_review'):
                from .image_terminal import REVIEW_TAG
                value.update(needs_review=True, review_tag=REVIEW_TAG,
                             image_review_score=asset['review']['score'])
            target_path = media / (asset["sha256"][:20] + ".png")
            if digest(Path(asset["file"]).read_bytes()) != asset["sha256"]:
                raise ValueError("Image changed after selection")
            shutil.copyfile(asset["file"], target_path)
            value["src"] = str(target_path.relative_to(public))
            if scene["asset_id"] in depth:
                depth_path = media / (depth[scene["asset_id"]]["sha256"][:20] + "-depth.png")
                shutil.copyfile(depth[scene["asset_id"]]["file"], depth_path)
                value["depth"] = str(depth_path.relative_to(public))
        elif backdrop:
            target_path = media / (backdrop["sha256"][:20] + ".png")
            if not target_path.exists():
                shutil.copyfile(backdrop["file"], target_path)
            value["backdrop"] = str(target_path.relative_to(public))
        scenes.append(value)
    captions = point_captions(timing)
    data = {"title": script["title"], "subject": script.get("subject", script["title"]), "width": config["width"], "height": config["height"],
            "fps": fps, "durationInFrames": frames, "transitionFrames": round(config.get("transition_seconds", .5) * fps), "depthStrength": config.get("depth_strength", 1), "scenes": scenes, "captions": captions,
            "creativeBrief": creative_brief,
            "sources": [{"id": f"S{i+1}", "url": r["url"], "title": r.get("title") or r["url"]} for i, r in enumerate(sources["results"])]}
    timeline = write(root / "timeline.json", {"data": data})
    write(root / "source.json", {**master_info, "audio_file": str(master.relative_to(root))})
    write(root / "compile-report.json", {"identity": identity, "target_seconds": target, "actual_seconds": info["duration_seconds"],
        "frames": frames, "rounding": "nearest absolute boundary; ceil final sample duration", "full_coverage": True,
        "source_audio_sha256": info["audio_sha256"], "delivery_master_sha256": master_info["audio_sha256"],
        "music_engine": music.get("engine", "none") if music else "none", "scene_count": len(scenes), "point_count": len(script["points"]),
        "needs_expert_review_assets": sorted({s['asset_id'] for s in scenes if s.get('needs_review')})})
    return root, timeline


def render(config, store, root, timeline):
    renderer = ROOT / "renderer"
    snapshot = root / "renderer-snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    for filename in ['package.json','package-lock.json','tsconfig.json']:
        shutil.copyfile(renderer / filename, snapshot / filename)
    for folder in ['src','public/fonts']:
        shutil.copytree(renderer / folder, snapshot / folder, dirs_exist_ok=True)
    data = read(timeline)["data"]
    for scene in data["scenes"]:
        for key in ("src", "depth", "backdrop"):
            if scene.get(key):
                target = snapshot / "public" / scene[key]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(renderer / "public" / scene[key], target)
    modules = snapshot / "node_modules"
    if not modules.exists():
        modules.symlink_to((renderer / "node_modules").resolve(), target_is_directory=True)
    executable = modules / ".bin/remotion"
    final = root / "video.mp4"
    layout = root / "layout"
    if not (layout / "layout-results.json").exists():
        with (root / "layout.log").open("w") as log:
            subprocess.run(["node", str(renderer / "measure.cjs"), str(snapshot), str(timeline),
                            config["browser"], str(layout)], cwd=snapshot, stdout=log, stderr=subprocess.STDOUT, check=True)
    if not read(layout / "layout-results.json")["passed"]:
        raise RuntimeError(f"Browser measurements found layout defects; inspect {layout}")
    if final.exists() and (root / "qa.json").exists() and read(root / "qa.json")["passed"]:
        return final
    picture = root / "picture.mp4"
    start = __import__("time").monotonic()
    command = [str(executable), "render", "src/index.ts", "InfoFilm", str(picture), "--props="+str(timeline),
               "--browser-executable="+config["browser"], "--codec=h264", "--crf=19", "--muted", "--pixel-format=yuv420p", "--concurrency=3"]
    write(root / "render-request.json", {"command": command, "renderer_sources": {
        str(p.relative_to(snapshot)): digest(p.read_bytes()) for p in sorted((snapshot / "src").glob("*")) if p.is_file()},
        "executed_snapshot": str(snapshot)})
    store.event("render", "running", {"output": str(final)})
    with (root / "render.log").open("w") as log:
        subprocess.run(command, cwd=snapshot, stdout=log, stderr=subprocess.STDOUT, check=True)
    # Reuse Filmora's direct-source audio mux and waveform synchronization QA.
    import sys
    sys.path.insert(0, config["filmora_root"])
    from backend.mux import continuous_audio_command
    subprocess.run(continuous_audio_command(picture, root / "master.wav", final), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    from backend.qa import run_qa
    source = read(root / "source.json")
    package = write(root / "qa-package.json", {"source": source})
    data = read(timeline)["data"]
    qa_timeline = write(root / "qa-timeline.json", {k: data[k] for k in ["width", "height", "fps", "durationInFrames"]})
    report = run_qa(final, package, qa_timeline, root, root / "qa.json")
    write(root / "render-result.json", {"elapsed_seconds": round(__import__("time").monotonic()-start, 3), "video": str(final), "qa_passed": report["passed"]})
    store.event("render", "succeeded", {"video": str(final), "qa": report["passed"]})
    return final
