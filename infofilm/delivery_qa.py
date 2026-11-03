"""Delivery verification: the finished file decodes, has the right shape, and carries the audited audio in sync.

Taken over from Filmora's backend/qa.py. The sync check now listens where someone is speaking: a window in the pauses
the narration keeps for the picture compares silence with silence, and the loud speech at its edge wins the correlation
at a false lag (leph1-07 C3 failed with a perfect file, lag 0.08 s, correlation 0.38)."""
from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

import numpy as np

from .core import digest, read, write

SYNC_FRACTIONS = (.03, .36, .72)
SYNC_SEARCH_SECONDS = 60
SPOKEN_RMS_SHARE = .1
SPOKEN_BLOCKS = 5


def spoken_window(samples, rate, seconds, window):
    """The first start at or after `seconds` whose window is spoken from end to end, else the most evenly spoken one."""
    length = int(window*rate)
    block = max(1, length//SPOKEN_BLOCKS)
    last = len(samples)-length
    floor = SPOKEN_RMS_SHARE*float(np.sqrt(np.mean(samples**2))) if len(samples) else 0.
    best, best_score = seconds, -1.
    step = max(1, block)
    for start in range(int(seconds*rate), min(last, int((seconds+SYNC_SEARCH_SECONDS)*rate))+1, step):
        score = min(float(np.sqrt(np.mean(samples[start+i*block:start+(i+1)*block]**2))) for i in range(SPOKEN_BLOCKS))
        if score > best_score:
            best, best_score = start/rate, score
        if floor and score >= floor:
            return start/rate
    return best


def run_qa(video, package_path, timeline_path, project, output):
    video = Path(video).resolve()
    package, timeline = read(package_path), read(timeline_path)
    source = package["source"]
    audio = Path(project)/source["audio_file"]
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(video)],
                                      capture_output=True, text=True, check=True).stdout)
    v = next(s for s in probe["streams"] if s["codec_type"] == "video")
    a = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    decode = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"],
                            capture_output=True, text=True)
    decoded = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-map", "0:a:0", "-ar", str(source["sample_rate"]),
                              "-ac", "1", "-f", "s16le", "-"], capture_output=True, check=True).stdout
    with wave.open(str(audio)) as f:
        raw = f.readframes(f.getnframes())
    source_decoded = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(audio), "-ac", "1", "-f", "s16le", "-"],
                                    capture_output=True, check=True).stdout
    original = np.frombuffer(source_decoded, dtype="<i2").astype(np.float64)
    rendered = np.frombuffer(decoded, dtype="<i2").astype(np.float64)
    rate = source["sample_rate"]
    comparisons = []
    total_seconds = source["duration_seconds"]
    window = min(5, total_seconds*.12)
    margin_seconds = min(.25, total_seconds*.01)
    for fraction in SYNC_FRACTIONS:
        sec = spoken_window(original, rate, total_seconds*fraction, window)
        start, end = int(sec*rate), int((sec+window)*rate)
        margin = int(rate*margin_seconds)
        x = original[start:end]
        y = rendered[start-margin:end+margin]
        nfft = 1 << (len(x)+len(y)-1).bit_length()
        corr = np.fft.irfft(np.fft.rfft(y, nfft)*np.fft.rfft(x[::-1], nfft), nfft)
        valid = corr[len(x)-1:len(y)]
        peak = int(np.argmax(valid))
        offset = peak-margin
        matched = y[peak:peak+len(x)]
        coefficient = float(np.dot(x, matched)/(np.linalg.norm(x)*np.linalg.norm(matched)))
        comparisons.append({"window_start_seconds": sec, "lag_samples": offset, "lag_seconds": offset/rate,
                            "normalized_correlation": round(coefficient, 8)})
    frame_count = int(v.get("nb_read_frames", v.get("nb_frames", "0")))
    tolerance = 1/timeline["fps"]
    checks = {"full_decode": decode.returncode == 0 and not decode.stderr.strip(),
              "frame_count": frame_count == timeline["durationInFrames"],
              "dimensions": (v["width"], v["height"]) == (timeline["width"], timeline["height"]),
              "frame_rate": v["r_frame_rate"] == f"{timeline['fps']}/1",
              "video_codec": v["codec_name"] == "h264",
              "audio_present": a["codec_name"] == "aac",
              "video_start_zero": abs(float(v.get("start_time", 0))) < tolerance,
              "audio_start_zero": abs(float(a.get("start_time", 0))) < tolerance,
              "duration": abs(float(v["duration"])-source["duration_seconds"]) <= tolerance,
              "source_file_unchanged": digest(audio.read_bytes()) == source["audio_sha256"],
              "source_pcm_unchanged": digest(raw) == source["pcm_sha256"],
              "audio_sync": all(abs(c["lag_seconds"]) < tolerance and c["normalized_correlation"] > .97 for c in comparisons)}
    report = {"schema_version": "filmora.delivery_qa.v1", "video": str(video), "video_sha256": digest(video.read_bytes()), "checks": checks,
              "passed": all(checks.values()), "video_frames": frame_count, "video_duration_seconds": float(v["duration"]),
              "audio_duration_seconds": float(a["duration"]), "source_audio_duration_seconds": source["duration_seconds"],
              "audio_comparison": {"method": "Decoded AAC resampled to source rate, FFT cross-correlation in three spoken five-second windows; not PCM equality",
                                   "windows": comparisons},
              "decode_errors": decode.stderr,
              "editorial_review": {"actor_type": "model", "status": "see asset reviews and render review notes", "human_acceptance": "pending"},
              "probe": probe}
    output = Path(output)
    if output.exists() and read(output).get("passed") and read(output).get("video_sha256") != report["video_sha256"]:
        raise FileExistsError(f"Passed report exists for another video: {output}")
    write(output, report)
    print(json.dumps({"passed": report["passed"], "checks": checks, "audio_windows": comparisons, "report": str(output)}, indent=2))
    if not report["passed"]:
        raise SystemExit(1)
    return report
