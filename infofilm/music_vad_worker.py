"""Use the installed local Silero ONNX detector without asking ASR for words."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    audio = decode_audio(str(args.audio), sampling_rate=16000)
    intervals = get_speech_timestamps(audio, VadOptions())
    regions = [{"start": row["start"] / 16000, "end": row["end"] / 16000} for row in intervals]
    result = {"engine": "Silero ONNX", "threshold": .5, "regions": regions,
              "detected_seconds": sum(r["end"]-r["start"] for r in regions), "duration_seconds": len(audio)/16000}
    temporary = args.output.with_suffix(".pending.json")
    temporary.write_text(json.dumps(result, indent=2))
    temporary.replace(args.output)


if __name__ == "__main__":
    main()
