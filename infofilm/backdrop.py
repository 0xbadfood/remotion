"""One thematic backdrop image per chapter, behind its native-graphic scenes (compare, flow, diagram cards), which
otherwise sit on a flat gradient (user design, 2026-10-13). A short film is one chapter: its context comes from the
subject and the creative brief (user, 2026-10-15: shorts had no backdrop).

The prompt is built by code from the chapter's lesson objective, visual direction and the film's shared style. The
image has no review loop; only code checks apply: no request for written words, and no painted words found by OCR in
the image (a take with them is drawn again with another seed). Any failure leaves the scenes on the gradient: a
backdrop never stops a chapter.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from .core import digest, read, write

PROTOCOL = "infofilm.chapter-backdrop.v1"
# Sentences about written or drawn-in-code elements: equations, tables and diagrams belong to native overlays, and
# naming them in an image prompt paints a chalkboard of garbled writing (2026-10-29, the Electricity films).
LABEL_WORDS = re.compile(r"[^.;]*\b(?:label\w*|caption\w*|overlay\w*|banner\w*|text|typography|words?|date|title\w*|"
                         r"names?|letter\w*|numbers?|quotes?|identified|equations?|formula\w*|derivations?|tables?|tab|"
                         r"figures?|fig|diagrams?|graphs?|charts?|vector|examples?|worksheets?|symbols?)\b[^.;]*[.;]?", re.I)


# A sentence with a figure in it (the 10 g steel bob) asks for written numbers.
DIGITS = re.compile(r"[^.;]*\d[^.;]*[.;]?")
# 2026-10-29: 18 of 70 backdrops had painted titles and fake diagrams ("Dimeinnal Aayiss Retiontions", "Merror Fumule")
# though the prompt asked for a wordless image; OCR finds two or more such words in every one of them and at most one
# stray in the clean ones.
TEXT_CHECK = 1
MOST_WORDS = 1
TAKES = 3


def clean(text, limit):
    """Drop sentences that talk about written elements (they belong to native overlays), quoted phrases and numbers."""
    text = re.sub(r"[\"'“”‘’][^\"'“”‘’]{0,80}[\"'“”‘’]", "", str(text or ""))
    return re.sub(r"\s+", " ", DIGITS.sub(" ", LABEL_WORDS.sub(" ", text))).strip(" ,.;")[:limit]


def painted_words(path):
    """Words of four or more letters that OCR reads in an image (None when tesseract is missing)."""
    try:
        out = subprocess.run(["tesseract", str(path), "-"], capture_output=True, text=True, timeout=120).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return re.findall(r"[A-Za-z]{4,}", out)


def wordless(store, value):
    """True when a backdrop has no painted words (a backdrop chosen before the check is read now)."""
    if value.get("text_check") == TEXT_CHECK:
        return True
    words = painted_words(value["file"])
    if words is not None and len(words) > MOST_WORDS:
        store.event("backdrop", "rejected", {"file": value["file"], "painted_words": words[:12]})
        return False
    return True


def prompt(context):
    chapter, film = context.get("chapter") or {}, context.get("film") or {}
    lesson = clean(chapter.get("objective"), 300)
    direction = clean(chapter.get("visual_direction"), 400)
    style = clean(film.get("shared_visual_style") or (context.get("shared_creative") or {}).get("visual_style"), 300)
    return ("Composition: a wide atmospheric establishing image, calm and uncluttered, with an open low-detail centre "
            "and soft depth; the main forms sit towards the edges and fade into shadow. "
            + (f"Shared visual style: {style}. " if style else "")
            + f"Scene: an evocative, unpeopled backdrop that sets the mood for this lesson: {lesson}. "
            + (f"Visual world: {direction}. " if direction else "")
            + "Quiet, cinematic, richly textured surfaces and gentle light. "
            + "A wordless image: no writing, letters, numbers or equations anywhere, and no blackboards, whiteboards, "
              "books, papers or screens.")


def backdrop_context(store):
    """A long film's chapter context, or for a short film the same shape built from its subject and creative brief."""
    root = Path(store.root)
    if (root / "chapter-context.json").exists():
        return read(root / "chapter-context.json")
    if not (root / "creative-brief.json").exists():
        return None
    brief = read(root / "creative-brief.json")
    subject = read(root / "project.json").get("subject", "") if (root / "project.json").exists() else ""
    subject = re.sub(r"\s*\([^)]*\)", "", subject)       # "(NCERT Class 10 History, ...)" is not imagery
    return {"chapter": {"objective": subject}, "film": {"shared_visual_style": brief.get("visual_style", "")}}


def chapter_backdrop(config, store):
    """The chapter's (or short film's) backdrop {file, sha256, prompt}, generated once and cached, or None."""
    from .planning import RASTER_LABEL
    context = backdrop_context(store)
    if context is None or not config.get("chapter_backdrop", True):
        return None
    text = prompt(context)
    if RASTER_LABEL.search(text):
        store.event("backdrop", "skipped", {"reason": "prompt asks for written words"})
        return None
    from .image_direct import unreviewed_provider
    provider_id, current = unreviewed_provider(config)
    params = {"prompt": text, "width": str(current["image_width"]), "height": str(current["image_height"]),
              "steps": str(current["image_steps"]), "guidance": "0", "output_format": "png"}
    identity = digest({"protocol": PROTOCOL, "provider": provider_id, "url": current["image_url"],
                       "engine": current.get("image_engine"), "params": params})
    seed = int(identity[:7], 16)
    root = store.root / "backdrop" / identity[:16]
    selected = root / "selected.json"
    if selected.exists():
        value = read(selected)
        if Path(value["file"]).is_file() and digest(Path(value["file"]).read_bytes()) == value["sha256"]:
            if wordless(store, value):
                return value
            set_aside(store)
            return None if value.get("text_check") == TEXT_CHECK else draw(store, current, provider_id, text, params, seed, root)
    return draw(store, current, provider_id, text, params, seed, root)


def draw(store, current, provider_id, text, params, seed, root):
    """Up to TAKES seeds until one has no painted words; else None, and the scenes keep the gradient."""
    from .image_recovery import provider_session, render_take
    try:
        store.event("backdrop", "rendering", {"provider": provider_id})
        with provider_session(current, store):
            for take in range(TAKES):
                folder = root if take == 0 else root / f"take-{take}"
                candidate = render_take(current, store, folder, {**params, "seed": str(seed + take)}, "chapter backdrop")
                words = painted_words(candidate)
                if words is not None and len(words) > MOST_WORDS:
                    store.event("backdrop", "rejected", {"file": str(candidate), "painted_words": words[:12]})
                    continue
                value = {"file": str(candidate), "sha256": digest(Path(candidate).read_bytes()), "prompt": text,
                         "protocol": PROTOCOL, "review_performed": False, "provider_id": provider_id,
                         "text_check": TEXT_CHECK, "painted_words": words or []}
                write(root / "selected.json", value)
                write(store.root / "backdrop" / "current.json", value)
                store.event("backdrop", "generated", {"file": str(candidate)})
                return value
        write(root / "selected.json", {"file": "", "sha256": "", "text_check": TEXT_CHECK, "painted_words": ["every take"]})
        set_aside(store)
        return None
    except Exception as exc:          # advisory: the scenes keep the gradient
        store.event("backdrop", "failed", {"error": str(exc)[:300]})
        return None


def set_aside(store):
    """The film's current backdrop moved aside (never deleted), so its scenes fall back to the gradient."""
    path = Path(store.root) / "backdrop" / "current.json"
    if path.exists():
        path.rename(path.with_name(f"current.json.old-{time.strftime('%Y%m%d-%H%M')}"))


def current(store):
    path = Path(store.root) / "backdrop" / "current.json"
    if not path.exists():
        return None
    value = read(path)
    return value if Path(value["file"]).is_file() and digest(Path(value["file"]).read_bytes()) == value["sha256"] else None
