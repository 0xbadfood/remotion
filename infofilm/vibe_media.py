"""Point-level VibeVoice batches, with independent audits and CPU alignment."""
from __future__ import annotations

import subprocess
import time
import wave
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
from pathlib import Path

import httpx

from .audio_audit import AUDIT_POLICY
from .core import Store, digest, gpu_lease, read, write
from .media import AlignmentError, align_audio, audio_info, audit_audio, mfa_numeric_text, request, wait_job
from .planning import script_text, word_count
from .gpu_release import release_vibevoice


@contextmanager
def vibe_service(config, store, root):
    with gpu_lease(config):
        health = request(config, "tts", "GET", "/health")
        if health.get("active_job") or health.get("queued_or_running_jobs"):
            own_active = False
            for path in root.glob("batches/*/provider.json"):
                job = request(config, "tts", "GET", "/jobs/" + read(path)["job_id"])
                own_active |= job.get("status") in {"queued", "loading", "rendering"}
            if not own_active:
                raise RuntimeError("VibeVoice has other active work; leave it undisturbed")
        image = request(config, "image", "GET", "/api/status")
        if image.get("busy") or image.get("state") == "loading":
            raise RuntimeError("Image service is busy; cannot switch shared GPU")
        if image.get("state") == "loaded":
            request(config, "image", "POST", "/api/unload")
        try:
            yield
        finally:
            health = request(config, "tts", "GET", "/health")
            if health.get("active_job") or health.get("queued_or_running_jobs"):
                store.event("vibevoice", "unresolved", {"gpu_released": False,
                    "reason": "Recorded provider job is still active; do not force unload"})
            else:
                complete = all((path.parent / "result.json").exists() and all(
                    (root / "points" / point["point_id"] / ("take-" + path.parent.name.split("-")[1]) / "narration.wav").is_file()
                    for point in read(path)["points"])
                    for path in root.glob("batches/*/provider.json"))
                release_vibevoice(config, store, request_fn=request, allow_restart=complete)


def _batch(config, store, root, tasks, reference, attempt, index, name):
    directory = root / "batches" / f"attempt-{attempt}-batch-{index}"
    record_path = directory / "provider.json"
    job_id = "infofilm_batch_" + digest({"root": str(root), "attempt": attempt, "batch": index})[:24]
    if record_path.exists():
        record = read(record_path)
        body = record["body"]
        if [x["point_id"] for x in record["points"]] != [p["id"] for p in tasks]:
            raise RuntimeError("Saved VibeVoice batch is immutable; resume its original points")
    else:
        with reference.open("rb") as handle:
            staged = request(config, "tts", "POST", "/stage-speaker",
                data={"speaker_id": "infofilm_" + digest(reference.read_bytes())[:16]},
                files={"file": (reference.name, handle, "audio/wav")})
        body = {"job_id": job_id, "lang_key": config["language"], "cfg_scale": 1.3,
            "items": [{"script": "Speaker 1: " + mfa_numeric_text(p["narration"]),
                       "voice_samples": [staged["voice_ref"]],
                       "output_name": p["id"] + ".wav", "chunk_index": index*5+i}
                      for i, p in enumerate(tasks)]}
        record = {"state": "submission_started", "job_id": job_id, "body": body,
                  "points": [{"point_id": p["id"], "text_sha256": digest(p["narration"]),
                              "spoken_text": mfa_numeric_text(p["narration"])} for p in tasks],
                  "synthesis_text_policy": "explicit-spoken-numbers-v1"}
        write(record_path, record)
        response = request(config, "tts", "POST", "/render-batch", json=body)
        write(record_path, {**record, "state": "submitted", "response": response})
    # A saved submission intent is reconciled by its client-selected ID. A 404
    # is unresolved, never permission to submit a duplicate paid/native job.
    job = (read(directory / "result.json") if (directory / "result.json").exists()
           else wait_job(config, "tts", "/jobs/" + job_id, {"success", "failed"}, set(), store, name))
    if job.get("job_id") != job_id or job.get("status") not in {"success", "failed"}:
        raise RuntimeError("Saved VibeVoice result does not match its terminal provider job")
    write(directory / "result.json", job)
    if job["status"] == "failed":
        return job
    outputs = {item["output_name"]: item for item in job.get("outputs", [])}
    for point in tasks:
        output_name = point["id"] + ".wav"
        if output_name not in outputs:
            raise RuntimeError(f"VibeVoice batch omitted output {point['id']}")
        destination = root / "points" / point["id"] / f"take-{attempt}" / "narration.wav"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            continue
        with httpx.Client(timeout=180) as client:
            response = client.get(config["tts_url"] + f"/jobs/{job_id}/audio-file/{output_name}")
            response.raise_for_status()
            raw = destination.parent / "provider.wav"
            raw.write_bytes(response.content)
        temporary = destination.with_name("narration.pending.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(raw),
                        "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(temporary)], check=True)
        temporary.replace(destination)
        write(destination.parent / "provider.json", {"job_id": job_id, "batch_record": str(record_path),
                                                     "output": outputs[output_name]})
    return job


def _check_clip(config, project, point, clip, name):
    # CPU work uses its own SQLite connection; the GPU can render the next batch.
    store = Store(project)
    try:
        from .silence import cleanup
        try:
            clip, pauses = cleanup(clip, config, store, name + '-' + point['id'])
        except ValueError as exc:
            return {'audit': {'status': 'failed', 'score': 0, 'reasons': ['silence_gate_failed'], 'error': str(exc)}}
        audit = audit_audio(config, store, clip, point["narration"])
        if audit["status"] != "passed":
            return {"audit": audit}
        try:
            timing = align_audio(config, store, clip, {"points": [point]}, name + "-" + point["id"])
        except AlignmentError as exc:
            failure = {"audit": audit, "alignment_error": str(exc), "audio": str(clip)}
            write(clip.parent / "alignment-failure.json", failure)
            return failure
        path = write(clip.parent / "timing.json", timing)
        return {"audit": audit, "timing": str(path), "audio": str(clip), "silence_cleanup": pauses}
    finally:
        store.db.close()


def _join_clips(points, selected, destination, pause_seconds):
    temporary = destination.with_suffix(".pending.wav")
    clips, offset, rate = [], 0, 24000
    pause = round(pause_seconds * rate)
    with wave.open(str(temporary), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        for index, point in enumerate(points):
            value = selected[point["id"]]
            clip = Path(value["audio"])
            with wave.open(str(clip), "rb") as source:
                if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, rate):
                    raise ValueError("VibeVoice point clips must be mono PCM16 at 24 kHz")
                count = source.getnframes()
                output.writeframes(source.readframes(count))
            clips.append({**value, "point_id": point["id"], "offset_samples": offset,
                          "sample_count": count, "audio_sha256": digest(clip.read_bytes())})
            offset += count
            if index < len(points)-1:
                output.writeframes(b"\0\0" * pause)
                offset += pause
    temporary.replace(destination)
    return clips


def render_vibevoice(config, store, points, name):
    batch_size = config.get("vibe_batch_size", 4)
    attempts = config.get("vibe_max_attempts", 3)
    pause = config.get("vibe_pause_seconds", .18)
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 5:
        raise ValueError("vibe_batch_size must be an integer between 1 and 5")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 5:
        raise ValueError("vibe_max_attempts must be an integer between 1 and 5")
    if isinstance(pause, bool) or not isinstance(pause, (int, float)) or not 0 <= pause <= 1:
        raise ValueError("vibe_pause_seconds must be between 0 and 1")
    if not points or len({p["id"] for p in points}) != len(points) or any(
            not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", p["id"]) or not p["narration"].strip() for p in points):
        raise ValueError("VibeVoice needs unique safe point IDs and nonempty narration")
    reference = Path(config["reference_audio"]).expanduser().resolve()
    identity = digest({"points": points, "reference_sha256": digest(reference.read_bytes()),
                       "url": config["tts_url"], "language": config["language"], "cfg_scale": 1.3,
                       "variant": config.get("audio_variant", 0), "pause": pause, "policy": "point-batches-v1"})
    root = store.root / "audio" / "vibevoice" / identity[:20]
    # Preserve historical joined narration in the same native-job cache while
    # reusing its raw point takes under the new CPU cleanup policy.
    final = root / "narration-pause-gated.wav"
    root.mkdir(parents=True, exist_ok=True)
    selected, pending = {}, list(points)
    started = time.monotonic()
    # A completed result is rechecked from saved clips, not trusted solely on
    # an old aggregate score. CPU-only cache reuse never loads the GPU model.
    with ExitStack() as stack:
        cpu = stack.enter_context(ThreadPoolExecutor(max_workers=1))
        service = False
        for attempt in range(attempts):
            if not pending:
                break
            futures, jobs = {}, []
            tasks = [p for p in pending if not (root / "points" / p["id"] / f"take-{attempt}" / "narration.wav").exists()]
            groups = [tasks[i:i+batch_size] for i in range(0, len(tasks), batch_size)]
            # Recover saved batch grouping before issuing any new native work.
            saved = sorted(root.glob(f"batches/attempt-{attempt}-batch-*/provider.json"),
                           key=lambda p: int(p.parent.name.rsplit("-", 1)[1]))
            if saved:
                by_id = {p["id"]: p for p in points}
                groups = [[by_id[x["point_id"]] for x in read(path)["points"]] for path in saved]
                covered = {p["id"] for group in groups for p in group}
                extra = [p for p in tasks if p["id"] not in covered]
                groups += [extra[i:i+batch_size] for i in range(0, len(extra), batch_size)]
            for point in pending:
                clip = root / "points" / point["id"] / f"take-{attempt}" / "narration.wav"
                if clip.exists():
                    futures[point["id"]] = cpu.submit(_check_clip, config, store.root, point, clip, name)
            for index, group in enumerate(groups):
                if not group or all((root / "points" / p["id"] / f"take-{attempt}" / "narration.wav").exists() for p in group):
                    continue
                store.event(name, "point_rendering" if attempt == 0 else "point_retry", {
                    "attempt": attempt+1, "max_attempts": attempts, "batch_size": len(group),
                    "points": [p["id"] for p in group]})
                if not service:
                    stack.enter_context(vibe_service(config, store, root))
                    service = True
                job = _batch(config, store, root, group, reference, attempt, index, name)
                jobs.append(job)
                if job["status"] == "failed":
                    store.event(name, "batch_failed", {"job_id": job["job_id"], "attempt": attempt+1, "error": job.get("error")})
                    continue
                for point in group:
                    if point["id"] not in {p["id"] for p in pending}:
                        continue
                    clip = root / "points" / point["id"] / f"take-{attempt}" / "narration.wav"
                    futures[point["id"]] = cpu.submit(_check_clip, config, store.root, point, clip, name)
            failures = []
            for point in pending:
                checked = futures[point["id"]].result() if point["id"] in futures else None
                if checked and checked["audit"]["status"] == "passed" and checked.get("timing"):
                    selected[point["id"]] = {"audio": checked.get("audio", str(root / "points" / point["id"] / f"take-{attempt}" / "narration.wav")),
                        "timing": checked["timing"], "text_sha256": digest(point["narration"]), "attempt": attempt+1,
                        "silence_cleanup": checked.get("silence_cleanup")}
                    store.event(name, "point_accepted", {"point": point["id"], "attempt": attempt+1,
                        "score": checked["audit"]["score"], "alignment": "MFA",
                        "audit_method": checked["audit"].get("acceptance_method", "asr_thresholds"),
                        "silence_removed_seconds": (checked.get('silence_cleanup') or {}).get('removed_seconds', 0)})
                else:
                    failures.append(point)
                    if checked and checked.get("alignment_error"):
                        store.event(name, "point_alignment_failed", {"point": point["id"], "attempt": attempt+1,
                            "max_attempts": attempts, "score": checked["audit"]["score"],
                            "whisper_status": checked["audit"]["status"], "error": checked["alignment_error"],
                            "chunk_rebuild_pending": attempt+1 < attempts})
                    elif checked:
                        store.event(name, "point_audit_failed", {"point": point["id"], "attempt": attempt+1,
                            "max_attempts": attempts, "score": checked["audit"]["score"], "reasons": checked["audit"]["reasons"]})
            pending = failures
            if any("out of memory" in str(j.get("error", "")).lower() for j in jobs):
                batch_size = max(1, batch_size//2)
        if pending:
            raise RuntimeError(f"VibeVoice point narration needs review after {attempts} attempts: " + ", ".join(p["id"] for p in pending))
    clips = _join_clips(points, selected, final, pause)
    # Two individually acceptable edge pauses can combine into excessive dead
    # air at a join. Gate the assembled voice as well; retain the original join
    # and remap its already computed MFA words if this second gate edits it.
    from .silence import cleanup, settings as silence_settings
    delivered, joined_pauses = cleanup(final, config, store, name + '-joined')
    text = "\n\n".join(p["narration"] for p in points)
    info = {**audio_info(delivered), "text": text, "text_sha256": digest(text), "provider": "VibeVoice",
        "reference_audio": str(reference), "reference_audio_sha256": digest(reference.read_bytes()),
        "reference_transcript": config.get("reference_transcript"), "reference_transcript_used_for_conditioning": False,
        "language": config["language"], "point_clips": clips, "pause_seconds": pause,
        "audit_policy": AUDIT_POLICY, "batch_size": config.get("vibe_batch_size", 4),
        "point_attempts": {p["point_id"]: p["attempt"] for p in clips},
        "measured_latency_seconds": round(time.monotonic()-started, 3),
        "silence_cleanup_policy": silence_settings(config),
        "observed_words_per_minute": round(word_count(text)/audio_info(delivered)["duration_seconds"]*60, 3)}
    if delivered != final:
        info.update(source_join_audio=str(final), joined_silence_cleanup=joined_pauses)
    write(root / "result.json", info)
    store.event(name, "succeeded", {"duration_seconds": info["duration_seconds"], "point_clips": len(clips),
        "observed_words_per_minute": info["observed_words_per_minute"], "cpu_alignment_overlapped": True})
    return delivered, info


def align_vibevoice(config, store, audio, script, info, name):
    """Translate independent MFA spans using exact assembled sample offsets."""
    delivered_info = audio_info(audio)
    if delivered_info["audio_sha256"] != info["audio_sha256"]:
        raise ValueError("Joined narration changed after clip selection")
    source_audio = info.get('source_join_audio', audio)
    final_info = audio_info(source_audio)
    if [c["point_id"] for c in info["point_clips"]] != [p["id"] for p in script["points"]]:
        raise ValueError("Point clips no longer match the script order")
    words, boundaries, clips = [], [], []
    expected_offset = 0
    pause_samples = round(info["pause_seconds"]*final_info["sample_rate"])
    for point, clip in zip(script["points"], info["point_clips"]):
        if clip["text_sha256"] != digest(point["narration"]) or digest(Path(clip["audio"]).read_bytes()) != clip["audio_sha256"]:
            raise ValueError("Point clip or script changed after audit")
        local = read(clip["timing"])
        if local["audio_sha256"] != clip["audio_sha256"] or local["text_sha256"] != digest(point["narration"]):
            raise ValueError("Point MFA no longer matches the approved recording")
        if clip["offset_samples"] != expected_offset:
            raise ValueError("Point offsets do not match exact clip lengths and join pauses")
        expected_offset += clip["sample_count"] + pause_samples
        if [w["word"] for w in local["words"]] != point["narration"].split():
            raise ValueError("Point MFA does not cover the complete source text")
        offset = clip["offset_samples"]/final_info["sample_rate"]
        first = len(words)
        prior = 0.0
        for word in local["words"]:
            if word["start"] < prior-1e-6 or not 0 <= word["start"] <= word["end"] <= clip["sample_count"]/final_info["sample_rate"]+.001:
                raise ValueError("Point MFA has invalid or overlapping word spans")
            prior = word["end"]
            words.append({**word, "id": f"w{len(words)+1:04d}", "start": word["start"]+offset,
                          "end": word["end"]+offset, "point_id": point["id"],
                          "mfa_word_ids": [point["id"]+":"+w for w in word["mfa_word_ids"]]})
        boundaries.append({"point_id": point["id"], "start": words[first]["start"], "end": words[-1]["end"],
                           "word_ids": [w["id"] for w in words[first:]]})
        clips.append({"point_id": point["id"], "offset_samples": clip["offset_samples"], "timing": clip["timing"]})
    if expected_offset-pause_samples != final_info["sample_count"]:
        raise ValueError("Joined audio sample count does not match its point clips")
    text = script_text(script)
    value = {"schema_version": "infofilm.timing.v1", "audio": str(source_audio), "audio_sha256": final_info["audio_sha256"],
        "text_sha256": digest(text), "words": words, "points": boundaries, "audio_info": final_info,
        "provenance": {"engine": "MFA", "policy": "point-local-mfa-exact-sample-offsets-v1", "clips": clips,
                       "pause_seconds": info["pause_seconds"], "human_alignment_review": "pending"}}
    if info.get('joined_silence_cleanup'):
        from .silence import retime
        value = retime(value, info['joined_silence_cleanup'], audio)
    write(store.root / "alignment" / digest(value)[:20] / "timing.json", value)
    store.event(name, "succeeded", {"engine": "MFA", "words": len(words), "points": len(boundaries),
                                   "reused_point_alignment": True, "exact_sample_offsets": True})
    return value
