"""Bound source excerpts and retain their offsets into the archived full page."""
from __future__ import annotations

import json
import re

MODEL_MESSAGE_LIMIT = 90_000  # JSON bytes, below either configured model context.


def choose_passages(content: str, subject: str, budget: int = 32_000) -> list[dict]:
    if budget < 1000:
        raise ValueError("Source excerpt budget is too small")
    start = content.find("From Wikipedia, the free encyclopedia")
    if start < 0:
        start = 0
    else:
        start += len("From Wikipedia, the free encyclopedia")
    # Raw offsets let an editor find every retained passage in the full archive.
    candidates = []
    for match in re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", content[start:]):
        absolute = start + match.start()
        segment = match.group().strip()
        if len(segment) < 90:
            continue
        # Source-site navigation and reference lists have many Markdown links.
        if segment.count("](") > max(5, len(segment) // 110):
            continue
        for position in range(0, len(segment), 2800):
            piece = segment[position:position + 2800]
            if len(piece) >= 90:
                candidates.append((absolute + position, piece))
    if not candidates:
        candidate = content[start:start + budget]
        return [{"id": "P01", "offset_start": start, "offset_end": start + len(candidate), "text": candidate}]
    terms = {t.lower() for t in re.findall(r"[A-Za-z]{4,}", subject)} - {
        "what", "why", "when", "where", "does", "works", "work", "with", "from", "that", "this", "about", "civilization"}
    ranked = []
    for index, (offset, piece) in enumerate(candidates):
        hits = sum(min(3, len(re.findall(r"\b" + re.escape(term) + r"\b", piece, flags=re.I))) for term in terms)
        ranked.append((hits * 5 + (4 if index < 5 else 0), index, offset, piece))
    # Reserve room for the article lead and then take the most relevant distinct
    # body passages. Sort back into source order for chronology and traceability.
    selected = []
    spent = 0
    for _, index, offset, piece in ranked[:3] + sorted(ranked[3:], key=lambda row: (-row[0], row[1])):
        if spent >= budget:
            break
        if any(old_index == index for old_index, *_ in selected):
            continue
        allowed = min(len(piece), budget - spent)
        if allowed < 90:
            break
        selected.append((index, offset, piece[:allowed]))
        spent += allowed
    selected.sort(key=lambda row: row[1])
    return [{"id": f"P{i+1:02d}", "offset_start": offset,
             "offset_end": offset + len(piece), "text": piece}
            for i, (_, offset, piece) in enumerate(selected)]


def bound_messages(messages: list[dict], limit: int = MODEL_MESSAGE_LIMIT) -> tuple[list[dict], dict]:
    """Bound accumulated tool results; preserve all tool-call IDs and the latest answer context."""
    bounded = json.loads(json.dumps(messages, ensure_ascii=False))
    original = len(json.dumps(messages, ensure_ascii=True).encode())
    current = original
    if current > limit:
        for index, item in enumerate(bounded):
            if current <= limit:
                break
            if item.get("role") != "tool":
                continue
            try:
                result = json.loads(item.get("content", "{}"))
            except (TypeError, ValueError):
                continue
            if isinstance(result, dict) and isinstance(result.get("content"), str) and len(result["content"]) > 1200:
                # Summaries are already persisted. A past tool result can be
                # shortened without changing the actual fetched source archive.
                result["content"] = result["content"][:1200] + "\n[Earlier source detail archived; request this URL again if needed.]"
                bounded[index]["content"] = json.dumps(result, ensure_ascii=False)
                current = len(json.dumps(bounded, ensure_ascii=True).encode())
    if current > limit:
        raise ValueError(f"Research request exceeds the {limit}-byte safety bound ({current} bytes) after compaction")
    return bounded, {"original_message_bytes": original, "sent_message_bytes": current,
                     "compacted": current < original, "limit_bytes": limit}


def digest_errors(value: dict, passage_ids: set[str]) -> list[str]:
    errors = []
    if not isinstance(value, dict):
        return ["Source digest must be a JSON object"]
    facts = value.get("claims", [])
    if value.get("status", "usable") not in ("usable", "unusable"):
        return ["Source status must be usable or unusable"]
    if not isinstance(value.get("reason", ""), str):
        return ["Source reason must be a string"]
    cautions = value.get("cautions", [])
    if not isinstance(cautions, list) or any(not isinstance(c, str) for c in cautions):
        return ["Source cautions must be a list of strings"]
    # Refusing to invent claims from an irrelevant or unreadable page is valid.
    # An explicit rejection must include a reason and never carry accepted facts.
    if value.get("status") == "unusable" or (facts == [] and value.get("status") is None and cautions):
        if facts != [] or not (str(value.get("reason") or "").strip() or any(c.strip() for c in cautions)):
            return ["An unusable source needs no claims and a clear reason"]
        return []
    if not isinstance(facts, list) or not 1 <= len(facts) <= 12:
        return ["Need 1–12 concise supported claims, or mark this source unusable with a reason"]
    for i, item in enumerate(facts):
        if not isinstance(item, dict) or not isinstance(item.get("claim"), str) or not 15 <= len(item["claim"]) <= 500:
            errors.append(f"Claim {i+1} needs a clear supported statement")
        if (not isinstance(item, dict) or not isinstance(item.get("passage_ids"), list)
                or not item["passage_ids"] or any(not isinstance(p, str) for p in item["passage_ids"])
                or not set(item["passage_ids"]) <= passage_ids):
            errors.append(f"Claim {i+1} needs IDs of provided passages")
    return errors


def source_summary_prompt(subject: str, url: str, passages: list[dict]) -> str:
    return ("Make a concise first-pass research digest of this single fetched source. "
            "Only assert facts supported by supplied passages; every claim must name one or more passage IDs. "
            "Label debated interpretations as interpretations rather than settled facts. "
            "Preserve qualifications, causal sequence and uncertainty; do not flatten a rich subject to fit a video runtime. "
            "Avoid the website's menus, link lists, unsourced speculation, and claims outside these passages. "
            "Return JSON {status:'usable'|'unusable', claims:[{claim,passage_ids:['P01']}], cautions:[string], reason:string}. "
            "For usable sources, write 1–12 useful claims. If there are no relevant supported claims, return status:'unusable', claims:[], and a clear reason. "
            "Do not invent claims to satisfy a count. "
            f"Subject: {subject}\nURL: {url}\nPassages with offsets into the archived full page:\n" +
            json.dumps(passages, ensure_ascii=False))
