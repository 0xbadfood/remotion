"""Claims-and-sources record for a delivered film: every narration point, the facts it relies on, and the web
sources (and cited passages) behind each fact. Provenance is kept even when a model already knew the event."""
from __future__ import annotations

import re
import time
from pathlib import Path

from .core import read, write

SCHEMA = "infofilm.claims-sources.v1"


def _passages(source, ids):
    passages = {p.get("id"): p for p in (source.get("source_digest") or {}).get("passages", []) if isinstance(p, dict)}
    return [{"id": pid, "excerpt": passages[pid]["text"][:400]} for pid in ids if pid in passages]


def chapter_claims(project, duration):
    """Points → facts → sources for one film folder (a short film or one chapter of a long film)."""
    project = Path(project)
    script = read(project / f"film-{duration}" / "script.json")
    facts = {f["id"]: f for f in read(project / "evidence-final.json")["facts"]}
    sources = read(project / "sources.json")["results"] if (project / "sources.json").exists() else []
    points = []
    for point in script["points"]:
        used = []
        for fact_id in point.get("fact_ids", []):
            fact = facts.get(fact_id)
            if not fact:
                continue
            note = fact.get("evidence_note", "")
            cited = []
            for source_id in fact.get("source_ids", []):
                match = re.fullmatch(r"S(\d+)", str(source_id))
                source = sources[int(match.group(1)) - 1] if match and 0 < int(match.group(1)) <= len(sources) else None
                if source is None:
                    cited.append({"id": source_id, "url": None, "note": "source not in the archive"})
                    continue
                # Passage ids the evidence note cites for this source: "S1 (P24, P50)", "S1 P60 and S3 P23".
                passage_ids = []
                for window in re.finditer(r"\b" + re.escape(source_id) + r"\b(.*?)(?=\bS\d+\b|$)", note):
                    passage_ids += [pid for pid in re.findall(r"\bP\d+\b", window.group(1)) if pid not in passage_ids]
                cited.append({"id": source_id, "url": source.get("url"), "title": source.get("title") or source.get("url"),
                              "passages": _passages(source, passage_ids)})
            used.append({"id": fact_id, "claim": fact.get("claim"), "evidence_note": note or None, "sources": cited})
        points.append({"point_id": point.get("id"), "narration": point.get("narration"), "facts": used,
                       "unsourced": not used})
    return points


def write_claims(destination, subject, parts, llm_configuration=None):
    """parts: [{'id', 'title', 'project', 'duration'}] — one entry for a short film, one per chapter for a long one."""
    chapters = [{"id": part.get("id"), "title": part.get("title"),
                 "points": chapter_claims(part["project"], part["duration"])} for part in parts]
    points = [p for c in chapters for p in c["points"]]
    return write(destination, {"schema": SCHEMA, "subject": subject, "generated": time.time(),
                               "llm_configuration": llm_configuration, "chapters": chapters,
                               "summary": {"points": len(points), "unsourced_points": sum(p["unsourced"] for p in points),
                                           "sources": len({s["url"] for p in points for f in p["facts"] for s in f["sources"] if s.get("url")})}})
