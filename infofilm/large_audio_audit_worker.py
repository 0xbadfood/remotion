"""Offline Whisper large-v3-turbo transcription; CPU only, no script conditioning."""
from __future__ import annotations

import argparse
import json
import os
import sys

# A directly launched copy must not shadow stdlib queue with infofilm/queue.py.
# Also support workers already in memory that still use the script-file command.
if __package__ in {None, ""}:
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--cache-dir", required=True)
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import av
    import torch
    from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

    torch.set_num_threads(4)
    cache = os.path.join(args.cache_dir, "hub")
    model = AutoModelForSpeechSeq2Seq.from_pretrained(
        args.model, torch_dtype=torch.float32, low_cpu_mem_usage=True,
        local_files_only=True, cache_dir=cache,
    )
    processor = AutoProcessor.from_pretrained(args.model, local_files_only=True, cache_dir=cache)
    transcribe = pipeline("automatic-speech-recognition", model=model,
                         tokenizer=processor.tokenizer, feature_extractor=processor.feature_extractor,
                         device=-1)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("command") == "close":
                return
            path = request["audio_path"]
            with av.open(path) as container:
                duration = float(container.duration or 0) / float(av.time_base)
            result = transcribe(
                path, return_timestamps=True,
                generate_kwargs={"task": "transcribe", "language": request.get("language") or "en",
                                 "num_beams": 5},
            )
            bounds = [(float(c["timestamp"][0] or 0),
                       min(duration, float(c["timestamp"][1] if c["timestamp"][1] is not None else duration)))
                      for c in result.get("chunks", []) if c.get("text", "").strip()]
            span = max(0, max(e for s, e in bounds) - min(s for s, e in bounds)) if bounds else 0
            response = {"text": result["text"].strip(), "speech_span_seconds": span,
                        "audio_duration_seconds": duration}
        except Exception as exc:
            response = {"error": str(exc)}
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
