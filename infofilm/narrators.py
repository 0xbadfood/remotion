from __future__ import annotations

import re
from pathlib import Path


def narrator_profiles(config):
    """Configured voices carry their own audio and exact reference transcript."""
    profiles = {"default": {
        "label": "Default reference · " + Path(config["reference_audio"]).name,
        "reference_audio": config["reference_audio"],
        "reference_transcript": config.get("reference_transcript"),
    }}
    for name, profile in config.get("narrators", {}).items():
        if name == "default" or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", name):
            raise ValueError(f"Invalid configured narrator ID: {name}")
        profiles[name] = {**profile, "label": profile.get("label", name)}
    return profiles


def select_reference(config, *, narrator=None, reference_audio=None, reference_transcript=None):
    config = dict(config)
    if narrator and reference_audio:
        raise ValueError("Choose --narrator or --reference-audio for the reference recording")
    if narrator:
        profile = narrator_profiles(config).get(narrator)
        if profile is None:
            raise ValueError(f"Unknown narrator: {narrator}")
        config.update({key: profile.get(key) for key in ["reference_audio", "reference_transcript"]})
        config["narrator_id"] = narrator
        config["narrator_label"] = profile["label"]
    if reference_audio:
        config["reference_audio"] = str(Path(reference_audio).expanduser().resolve())
        # An unrelated upload must never inherit the default voice's transcript.
        config["reference_transcript"] = None
    if reference_transcript is not None:
        config["reference_transcript"] = " ".join(reference_transcript.split()) or None
    if config.get("tts_engine") == "omnivoice" and not config.get("reference_transcript"):
        raise ValueError("OmniVoice requires the exact transcript of the selected reference recording")
    return config
