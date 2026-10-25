"""Narration room: the narration waits for the picture (user, 2026-10-25).

A Linkwork scene that needs longer than its beat used to be played faster (speed_to_fit) or sent back to the planner,
and both made concept films feel rushed. The narration is recorded one clip per point and aligned as a whole, so
silence inserted after a word only shifts the words after it: no new recording, no new alignment. Once the scenes are
compiled, code knows how long each one really runs and inserts what it needs after the beat's last word:

- finish: the seconds a Linkwork scene's motion runs past its beat (always, for every film);
- hold: a pause on a core beat (an equation or figure to take in), asked for in the beat sheet;
- look / notice: a fixed spoken phrase ("Look carefully.") while the scene keeps playing, then its finish;
- replay: the scene plays to its end, "Watch it once more.", and it plays again from the start.

The phrases are recorded once per voice (LuxTTS) and aligned once, then reused by every film; their words join the
timing, so captions show them. Caps keep a film from filling with dead air: seconds per beat and a share of the
film. The extended narration replaces the film's audio and timing before images, music and the video.
"""
from __future__ import annotations

import os
import wave
from pathlib import Path

from .core import Store, digest, read, write

POLICY = "narration-room-v1"
PHRASES = {"look": "Look carefully.", "notice": "Notice what changes.", "replay": "Watch it once more."}
ROOMS = ("hold", "look", "notice", "replay")
GAP = 0.5           # seconds of quiet between the narration and a phrase, and between a phrase and the replay


def settings(config=None):
    config = config or {}
    value = {"enabled": config.get("narration_room", True),
             "max_beat_seconds": config.get("room_max_beat_seconds", 12.0),
             "max_share": config.get("room_max_share", 0.25),
             "hold_seconds": config.get("room_hold_seconds", 2.5),
             "replay_max_scene_seconds": config.get("room_replay_max_scene_seconds", 15.0),
             "max_replays": config.get("room_max_replays", 3)}
    if not isinstance(value["enabled"], bool):
        raise ValueError("narration_room must be boolean")
    return value


def phrase_root(config):
    return Path(config.get("phrase_root") or Path.home() / "remotion" / "artifacts" / "phrases").expanduser()


def phrase_clips(config):
    """{key: {path, seconds, words:[{word, start, end}]}} for the fixed phrases in this film's voice (LuxTTS only),
    recorded, checked and aligned once and cached; {} for other voices (their rooms fall back to silence)."""
    if config.get("tts_engine", "luxtts") != "luxtts":
        return {}
    from .lux_media import render_luxtts
    from .media import align_audio
    store = Store(phrase_root(config))
    points = [{"id": key, "narration": text} for key, text in PHRASES.items()]
    final, info = render_luxtts(config, store, points, "phrases")
    script = {"points": [{**p, "fact_ids": []} for p in points]}
    timing = align_audio(config, store, final, script, "phrases-alignment", spoken_points=info.get("spoken_points"))
    words = {w["id"]: w for w in timing["words"]}
    out, offset = {}, 0.0
    for point in timing["points"]:
        path = Path(info["point_clips"][point["point_id"]])
        with wave.open(str(path)) as clip:
            seconds = clip.getnframes() / clip.getframerate()
        out[point["point_id"]] = {"path": str(path), "seconds": seconds, "words": [
            {"word": words[i]["word"], "start": round(words[i]["start"] - offset, 3), "end": round(words[i]["end"] - offset, 3)}
            for i in point["word_ids"]]}
        offset += seconds
    return out


def _scene_seconds(scene):
    if scene.get("kind") != "linkwork" or not scene.get("linkwork_file"):
        return None
    return float(read(scene["linkwork_file"])["duration"])


def plan(scenes, words, base_seconds, room, phrases):
    """The inserts for resolved scenes (start/end in the narration's seconds): [{scene, at, seconds, phrase?,
    phrase_at?, restart?}], `at` in the narration's own seconds, after the beat's last word. Core beats first, then
    in order, until the film's share runs out."""
    from .linkwork_beats import TAIL
    by_start = sorted(words, key=lambda w: w["start"])
    wanted = []
    for index, scene in enumerate(scenes):
        length = scene["end"] - scene["start"]
        inside = [w for w in by_start if scene["start"] - 1e-6 <= w["start"] < scene["end"] - 1e-6]
        if not inside:
            continue
        last = inside[-1]
        following = next((w for w in by_start if w["start"] >= scene["end"] - 1e-6), None)
        at = last["end"] + min(0.15, max(0.0, ((following["start"] if following else last["end"]) - last["end"]) / 2))
        heard = at - scene["start"]                         # the beat's narration, in seconds from its start
        motion = _scene_seconds(scene)
        finish = max(0.0, motion - TAIL - length) if motion else 0.0
        kind = scene.get("room") if scene.get("room") in ROOMS else None
        if kind in ("look", "notice", "replay") and (kind not in phrases or kind == "replay" and not motion):
            kind = "hold"                           # no phrase in this voice, or nothing to replay: a plain pause
        insert = {"scene": scene["id"], "at": round(at, 3), "after_word": last["id"], "seconds": finish,
                  "core": scene.get("importance") == "core"}
        if kind == "hold":
            insert["seconds"] = finish + room["hold_seconds"]
        elif kind in ("look", "notice"):
            phrase = phrases[kind]["seconds"]
            insert.update(phrase=kind, phrase_at=GAP, seconds=max(finish, GAP + phrase + 0.5))
        elif kind == "replay" and motion <= room["replay_max_scene_seconds"]:
            first = max(0.0, motion - TAIL - heard)                         # the first play's end, after the words
            restart = heard + first + GAP + phrases["replay"]["seconds"] + GAP
            insert.update(phrase="replay", phrase_at=first + GAP, restart=round(restart, 3),
                          seconds=first + 2 * GAP + phrases["replay"]["seconds"] + max(0.0, motion - TAIL))
        insert["seconds"] = round(min(insert["seconds"], room["max_beat_seconds"]), 3)
        if insert.get("restart") and insert["seconds"] < insert["restart"] - heard + max(0.0, motion - TAIL) - 1e-6:
            insert.pop("restart"), insert.pop("phrase"), insert.pop("phrase_at")
            insert["seconds"] = round(min(finish + room["hold_seconds"], room["max_beat_seconds"]), 3)
        if insert["seconds"] >= 0.3:
            wanted.append((not insert["core"], index, insert))
    budget, replays, out = room["max_share"] * base_seconds, 0, []
    for _, _, insert in sorted(wanted, key=lambda x: x[:2]):
        if insert.get("restart"):
            if replays >= room["max_replays"]:
                continue
            replays += 1
        if insert["seconds"] > budget:
            continue
        budget -= insert["seconds"]
        out.append(insert)
    return sorted(out, key=lambda i: i["at"])


def _frames(path):
    with wave.open(str(path)) as audio:
        return (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), audio.readframes(audio.getnframes())


def extend(store, film, inserts, phrases):
    """Write the extended narration and return (audio path, audio_info, timing), cached by narration and inserts."""
    from .media import audio_info
    timing = film["timing"]
    identity = digest({"policy": POLICY, "audio": timing["audio_sha256"], "inserts": inserts,
                       "phrases": {k: digest(Path(v["path"]).read_bytes()) for k, v in phrases.items()
                                   if any(i.get("phrase") == k for i in inserts)}})
    root = store.root / "audio" / "room" / identity[:20]
    final = root / "narration.wav"
    if (root / "timing.json").exists() and final.exists():
        saved = read(root / "timing.json")
        if digest(final.read_bytes()) == saved["audio_sha256"]:
            return final, saved["audio_info"], saved
    params, pcm = _frames(timing["audio"])
    channels, width, rate = params
    step = channels * width
    pieces, cursor, extra_words = [], 0, []
    shift = 0.0
    for n, insert in enumerate(inserts, 1):
        cut = min(len(pcm) // step, round(insert["at"] * rate)) * step
        pieces.append(pcm[cursor:cut])
        cursor = cut
        total = round(insert["seconds"] * rate)
        filler = bytearray(total * step)
        if insert.get("phrase"):
            clip_params, clip = _frames(phrases[insert["phrase"]]["path"])
            if clip_params != params:
                raise ValueError("Room phrases and the narration have different audio formats")
            begin = round(insert["phrase_at"] * rate) * step
            clip = clip[:max(0, len(filler) - begin)]
            filler[begin:begin + len(clip)] = clip
            for k, w in enumerate(phrases[insert["phrase"]]["words"], 1):
                base = insert["at"] + shift + insert["phrase_at"]
                extra_words.append({"id": f"room{n:02d}w{k}", "word": w["word"], "start": round(base + w["start"], 3),
                                    "end": round(base + w["end"], 3), "room": insert["scene"], "after": insert["at"]})
        pieces.append(bytes(filler))
        shift += total / rate
    pieces.append(pcm[cursor:])
    root.mkdir(parents=True, exist_ok=True)
    with wave.open(str(final), "wb") as out:
        out.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        out.writeframes(b"".join(pieces))

    def moved(t):
        return round(t + sum(round(i["seconds"] * rate) / rate for i in inserts if i["at"] <= t + 1e-9), 4)

    words = [{**w, "start": moved(w["start"]), "end": moved(w["end"])} for w in timing["words"]]
    points = []
    for point in timing["points"]:
        # An insert belongs to the point whose word it follows: the point runs on through its room and owns its phrase.
        mine = [i for i in inserts if i["after_word"] in point["word_ids"]]
        late = sum(round(i["seconds"] * rate) / rate for i in mine if i["at"] > point["end"])
        points.append({**point, "start": moved(point["start"]), "end": round(moved(point["end"]) + late, 4),
                       "word_ids": list(point["word_ids"]) + [w["id"] for w in extra_words
                                                              if w["room"] in {i["scene"] for i in mine}]})
    words = sorted(words + [{k: v for k, v in w.items() if k != "after"} for w in extra_words], key=lambda w: w["start"])
    info = audio_info(final)
    extended = {**timing, "audio": str(final), "audio_sha256": info["audio_sha256"], "words": words, "points": points,
                "audio_info": {**timing["audio_info"], **info},
                "room": {"policy": POLICY, "base_audio": timing["audio"], "base_audio_sha256": timing["audio_sha256"],
                         "inserts": inserts, "added_seconds": round(shift, 3)}}
    write(root / "timing.json", extended)
    return final, extended["audio_info"], extended


def apply(config, store, films):
    """Give each film's beats the room they need (after its Linkwork scenes are compiled); notes each scene's insert
    and replay on the storyboard. A film whose timing already has room is left alone."""
    room = settings(config)
    if not room["enabled"]:
        return
    from .creative import resolved_scenes
    for film in films:
        timing = film["timing"]
        if timing.get("room"):
            continue
        scenes = resolved_scenes(film["storyboard"], film["script"], timing)
        wants_phrase = any(s.get("room") in ("look", "notice", "replay") for s in scenes)
        phrases = {}
        if wants_phrase:
            try:
                phrases = phrase_clips(config)
            except Exception as exc:                  # noqa: BLE001 - silence still gives the room
                store.event("narration-room", "phrases_unavailable", {"error": str(exc)[:300]})
        inserts = plan(scenes, timing["words"], timing["audio_info"]["duration_seconds"], room, phrases)
        if not inserts:
            continue
        audio, info, extended = extend(store, film, inserts, phrases)
        by_scene = {i["scene"]: i for i in inserts}
        for scene in film["storyboard"]["scenes"]:
            insert = by_scene.get(scene.get("id"))
            if insert:
                scene["room_seconds"] = insert["seconds"]
                if insert.get("restart"):
                    scene["linkwork_restart"] = insert["restart"]
        film.update(audio=str(audio), timing=extended, actual_seconds=info["duration_seconds"],
                    audio_info={**film["audio_info"], **info})
        write(store.root / f"film-{film['requested_seconds']}" / "timing.json", extended)
        store.event("narration-room", "extended", {"film": film["requested_seconds"], "inserts": len(inserts),
                                                   "added_seconds": extended["room"]["added_seconds"],
                                                   "replays": sum(1 for i in inserts if i.get("restart"))})
