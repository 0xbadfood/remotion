"""One thematic backdrop image per chapter, behind its native-graphic scenes (compare, flow, diagram cards), which
otherwise sit on a flat gradient (user design, 2026-10-13). A short film is one chapter: its context comes from the
subject and the creative brief (user, 2026-10-15: shorts had no backdrop).

The prompt is built by code from the chapter's lesson objective, visual direction and the film's shared style. The
image is generated once with no review loop; only code checks apply (no request for written words). Any failure leaves
the scenes on the gradient: a backdrop never stops a chapter.
"""
from __future__ import annotations

import re
from pathlib import Path

from .core import digest, read, write

PROTOCOL = "infofilm.chapter-backdrop.v1"
# Sentences about written or drawn-in-code elements: equations, tables and diagrams belong to native overlays, and
# naming them in an image prompt paints a chalkboard of garbled writing (2026-10-29, the Electricity films).
LABEL_WORDS = re.compile(r"[^.;]*\b(?:label\w*|caption\w*|overlay\w*|banner\w*|text|typography|words?|date|title\w*|"
                         r"names?|letter\w*|numbers?|quotes?|identified|equations?|formula\w*|derivations?|tables?|tab|"
                         r"figures?|fig|diagrams?|graphs?|charts?|vector|examples?|worksheets?|symbols?)\b[^.;]*[.;]?", re.I)


def clean(text, limit):
    """Drop sentences that talk about written elements (they belong to native overlays) and quoted phrases."""
    text = re.sub(r"[\"'“”‘’][^\"'“”‘’]{0,80}[\"'“”‘’]", "", str(text or ""))
    return re.sub(r"\s+", " ", LABEL_WORDS.sub(" ", text)).strip(" ,.;")[:limit]


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
    from .image_recovery import provider_session, render_take
    provider_id, current = unreviewed_provider(config)
    params = {"prompt": text, "width": str(current["image_width"]), "height": str(current["image_height"]),
              "steps": str(current["image_steps"]), "guidance": "0", "output_format": "png"}
    identity = digest({"protocol": PROTOCOL, "provider": provider_id, "url": current["image_url"],
                       "engine": current.get("image_engine"), "params": params})
    params["seed"] = str(int(identity[:7], 16))
    root = store.root / "backdrop" / identity[:16]
    selected = root / "selected.json"
    if selected.exists():
        value = read(selected)
        if Path(value["file"]).is_file() and digest(Path(value["file"]).read_bytes()) == value["sha256"]:
            return value
    try:
        store.event("backdrop", "rendering", {"provider": provider_id})
        with provider_session(current, store):
            candidate = render_take(current, store, root, params, "chapter backdrop")
        value = {"file": str(candidate), "sha256": digest(Path(candidate).read_bytes()), "prompt": text,
                 "protocol": PROTOCOL, "review_performed": False, "provider_id": provider_id}
        write(selected, value)
        write(store.root / "backdrop" / "current.json", value)
        store.event("backdrop", "generated", {"file": str(candidate)})
        return value
    except Exception as exc:          # advisory: the scenes keep the gradient
        store.event("backdrop", "failed", {"error": str(exc)[:300]})
        return None


def current(store):
    path = Path(store.root) / "backdrop" / "current.json"
    if not path.exists():
        return None
    value = read(path)
    return value if Path(value["file"]).is_file() and digest(Path(value["file"]).read_bytes()) == value["sha256"] else None
