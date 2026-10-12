"""Word alignment with the MMS forced-alignment model, for hosts where MFA has no build (the Spark is aarch64).

The acoustic model is torchaudio's MMS_FA bundle (wav2vec2, 300M, character CTC); the alignment itself is a CTC Viterbi
pass written here in plain torch (torchaudio's forced_align op is deprecated). The result has the MFA runner's shape —
{start, end, tiers: {words: {entries: [[start, end, word], ...]}}} — with words from the same tokenizer, so
`media.align_audio` maps them onto the script exactly as it maps MFA output. Each word also gets a confidence (mean
character probability on the chosen path), which MFA never reported.
"""
from __future__ import annotations

import time
import unicodedata
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
_MODELS = {}


def _model(device):
    if device not in _MODELS:
        import torch
        import torchaudio
        bundle = torchaudio.pipelines.MMS_FA
        model = bundle.get_model(with_star=False).to(device).eval()
        _MODELS[device] = (model, bundle.get_dict(star=None), torch)
    return _MODELS[device]


def _audio(path):
    """PCM WAV → mono float32 at 16 kHz."""
    import torch
    import torchaudio.functional as F
    with wave.open(str(path)) as source:
        rate, channels, width = source.getframerate(), source.getnchannels(), source.getsampwidth()
        raw = source.readframes(source.getnframes())
    if width != 2:
        raise ValueError(f"Expected 16-bit PCM WAV, got {8 * width}-bit: {path}")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    waveform = torch.from_numpy(samples).unsqueeze(0)
    if rate != SAMPLE_RATE:
        waveform = F.resample(waveform, rate, SAMPLE_RATE)
    return waveform


def _characters(word, dictionary):
    """Lower-case Latin letters and apostrophes the model knows; accents are folded, other symbols dropped."""
    folded = "".join(c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c))
    return [dictionary[c] for c in folded if c in dictionary and c != "-"]


def _viterbi(log_probs, tokens, blank, torch):
    """Best CTC path through `tokens` (blanks allowed between and around them). Returns the state per frame
    (odd states 2k+1 are token k)."""
    frames = log_probs.shape[0]
    states = 2 * len(tokens) + 1
    extended = torch.full((states,), blank, dtype=torch.long, device=log_probs.device)
    extended[1::2] = torch.tensor(tokens, dtype=torch.long, device=log_probs.device)
    skip = torch.zeros(states, dtype=torch.bool, device=log_probs.device)       # may jump from s-2 (distinct tokens)
    skip[3::2] = extended[3::2] != extended[1:-2:2]
    negative = torch.tensor(float("-inf"), device=log_probs.device)
    alpha = torch.full((states,), float("-inf"), device=log_probs.device)
    alpha[0] = log_probs[0, blank]
    if states > 1:
        alpha[1] = log_probs[0, extended[1]]
    pointers = torch.zeros((frames, states), dtype=torch.int8, device=log_probs.device)
    emit = log_probs[:, extended]                                                  # frames × states
    for t in range(1, frames):
        stay = alpha
        step = torch.cat([negative.view(1), alpha[:-1]])
        jump = torch.where(skip, torch.cat([negative.expand(2), alpha[:-2]]), negative)
        best = torch.stack([stay, step, jump])
        value, choice = best.max(dim=0)
        alpha = value + emit[t]
        pointers[t] = choice.to(torch.int8)
    # End in the last token or the trailing blank.
    state = states - 1 if states == 1 or alpha[-1] >= alpha[-2] else states - 2
    if not torch.isfinite(alpha[state]):
        raise ValueError("The narration is too short in time for its text (no CTC path)")
    pointers = pointers.cpu().numpy()
    path = np.empty(frames, dtype=np.int64)
    for t in range(frames - 1, -1, -1):
        path[t] = state
        state -= int(pointers[t, state])
    return path


def align(audio_path, text, *, tokenize, device=None):
    """Return (payload in the MFA runner's shape, metadata)."""
    started = time.monotonic()
    model, dictionary, torch = _model(device or ("cuda" if __import__("torch").cuda.is_available() else "cpu"))
    words = tokenize(text)
    if not words:
        raise ValueError("Nothing to align: the transcript has no words")
    waveform = _audio(audio_path)
    duration = waveform.shape[1] / SAMPLE_RATE
    with torch.inference_mode():
        emission, _ = model(waveform.to(next(model.parameters()).device))
        log_probs = torch.log_softmax(emission[0], dim=-1)
    tokens, owner = [], []
    for index, word in enumerate(words):
        characters = _characters(word, dictionary)
        tokens += characters
        owner += [index] * len(characters)
    if not tokens:
        raise ValueError("No transcript characters are known to the alignment model")
    path = _viterbi(log_probs, tokens, dictionary.get("-", 0), torch)
    seconds_per_frame = duration / log_probs.shape[0]
    probabilities = log_probs.exp().cpu().numpy()
    spans, scores = {}, {}
    for frame, state in enumerate(path):
        if state % 2:
            token = state // 2
            word = owner[token]
            first, last = spans.get(word, (frame, frame))
            spans[word] = (min(first, frame), max(last, frame))
            scores.setdefault(word, []).append(probabilities[frame, tokens[token]])
    entries, confidences, previous_end = [], [], 0.0
    for index, word in enumerate(words):
        if index in spans:
            first, last = spans[index]
            start, end = first * seconds_per_frame, (last + 1) * seconds_per_frame
        else:
            # A word with no known characters (e.g. only symbols): a zero-length span after the previous word.
            start = end = previous_end
        start, end = max(start, previous_end), min(max(end, start), duration)
        entries.append([round(start, 3), round(end, 3), word])
        confidences.append(round(float(np.mean(scores[index])), 4) if index in scores else None)
        previous_end = end
    known = [c for c in confidences if c is not None]
    metadata = {"engine": "mms_fa", "model": "torchaudio MMS_FA (wav2vec2 300M, character CTC)", "device": str(next(model.parameters()).device),
                "frames": int(log_probs.shape[0]), "seconds_per_frame": round(seconds_per_frame, 5),
                "audio_seconds": round(duration, 3), "alignment_seconds": round(time.monotonic() - started, 3),
                "mean_confidence": round(float(np.mean(known)), 4) if known else None,
                "low_confidence_words": [w for w, c in zip(words, confidences) if c is not None and c < 0.2][:20],
                "word_confidences": confidences}
    payload = {"start": 0.0, "end": round(duration, 3), "tiers": {"words": {"type": "interval", "entries": entries}}}
    return payload, metadata
