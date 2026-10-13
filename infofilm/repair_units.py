"""Targeted repair: the smallest repairable unit of every planning output, with a stable id, and code between the
reviewer and the planner.

A stage declares its units in a spec:
    {"lists": {"facts": "id", "uncertainties": "@U"}, "fields": False, "anchors": {"scenes": ["start_word_id"]},
     "operations": ["points"]}
- an item list keyed by an id field ("id", "scene_key", ...);
- a string list ("@U"): code gives entry i the id U01, U02, ...;
- "fields": True makes every other top-level key of a single-object output a unit (e.g. a creative brief);
- "anchors": fields a replaced item keeps from the prior candidate (its place in the timeline);
- "operations": lists where a repair may insert or remove items (code assigns the new ids);
- "links": {subfield: {item id: [unit ids]}}: a pointer '<item>.<subfield>' naming something that lives in another
  unit is redirected to it (Cuba C04: 'A017.banner' means the banner unit of A017's scene, '160:S18').

Rules (pipeline-findings-todo.md, BUILD NEXT): ids are stable once a candidate exists; reviewer findings and validator
errors point at unit ids; code maps findings to units, asks the planner for only the flagged units, merges them back by
id and re-validates the whole candidate. Only stage-level findings may cause a full redraft.
"""
from __future__ import annotations

import json
import re

DEFAULT_SPEC = {"lists": {"assets": "id", "scenes": "id"}, "fields": False,
                "anchors": {"scenes": ["id", "point_id", "start_word_id"]}, "operations": []}
STAGE_WORDS = {"", "stage", "whole", "candidate", "all", "overall", "global"}


def normalized(spec):
    spec = dict(DEFAULT_SPEC if spec is None else spec)
    spec.setdefault("lists", {}); spec.setdefault("fields", False)
    spec.setdefault("anchors", {}); spec.setdefault("operations", []); spec.setdefault("assign", {})
    spec.setdefault("links", {})
    return spec


def assign_ids(candidate, spec=None):
    """Code gives every item of a list in spec["assign"] ({key: prefix}) a unique stable id when it lacks one (or
    repeats one), e.g. cast people R01, R02. Run after each full draft; repairs keep the ids."""
    spec = normalized(spec)
    if not isinstance(candidate, dict):
        return candidate
    for key, prefix in (spec.get("assign") or {}).items():
        ident, entries = spec["lists"].get(key, "id"), candidate.get(key)
        if not isinstance(entries, list):
            continue
        seen, number = set(), 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            value = entry.get(ident)
            if value in (None, "") or str(value) in seen:
                while f"{prefix}{number + 1:02d}" in seen or any(
                        isinstance(e, dict) and e.get(ident) == f"{prefix}{number + 1:02d}" for e in entries):
                    number += 1
                value = entry[ident] = f"{prefix}{number + 1:02d}"
            seen.add(str(value))
    return candidate


def string_id(prefix, index):
    return f"{prefix}{index + 1:02d}"


def units(candidate, spec=None):
    """{unit id: (kind, key, index)} in candidate order. kind: item, string or field."""
    spec, found = normalized(spec), {}
    if not isinstance(candidate, dict):
        return found
    for key, ident in spec["lists"].items():
        entries = candidate.get(key)
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            if ident.startswith("@"):
                found[string_id(ident[1:], index)] = ("string", key, index)
            elif isinstance(entry, dict) and entry.get(ident) not in (None, ""):
                found[str(entry[ident])] = ("item", key, index)
    if spec["fields"]:
        for key in candidate:
            if key not in spec["lists"]:
                found[key] = ("field", key, None)
    return found


def resolve(pointer, candidate, spec=None):
    """Unit ids a finding's field names: 'F03', 'F03.claim', 'points[P03]', 'assets[id=A013]', 'assets[3]',
    'S20 (P10, start_word_id w0339)', 'A001-A008', 'theme.accent'. An empty list means stage level or unresolved."""
    spec, text = normalized(spec), str(pointer or "")
    known = units(candidate, spec)
    if not known or text.strip().lower() in STAGE_WORDS:
        return []
    order = list(known)
    hits = []
    for key, index in re.findall(r"([A-Za-z_]+)\[(\d+)\]", text):        # positional index
        for ident, (kind, list_key, position) in known.items():
            if list_key == key and position == int(index) and kind != "field":
                hits.append(ident)
    for first, last in re.findall(r"([A-Za-z]*\d+)\s*(?:–|—|-|\.\.|to)\s*([A-Za-z]*\d+)", text):   # ranges
        if first in known and last in known and order.index(first) <= order.index(last):
            hits.extend(order[order.index(first):order.index(last) + 1])
    for ident in sorted(known, key=len, reverse=True):                      # ids anywhere in the pointer
        if known[ident][0] == "field":
            if re.match(r"\s*" + re.escape(ident) + r"(?![\w])", text):
                hits.append(ident)
        elif re.search(r"(?<![\w-])" + re.escape(ident) + r"(?![\w])", text):
            hits.append(ident)
    for ident, subfield in re.findall(r"([\w:-]+)\.([A-Za-z_]+)", text):     # linked subfields
        targets = spec["links"].get(subfield) or spec["links"].get(subfield.rstrip("s")) or {}
        if ident in targets and ident in hits and not (known[ident][0] == "item" and subfield in (
                candidate[known[ident][1]][known[ident][2]] or {})):
            hits.remove(ident)
            hits.extend(t for t in targets[ident] if t in known)
    return list(dict.fromkeys(h for h in order if h in set(hits)))


def blocking(review):
    return [issue for issue in (review or {}).get("issues", []) or []
            if isinstance(issue, dict) and issue.get("severity") in {"blocker", "major"}]


def pointer_errors(review, candidate, spec=None):
    """Blocking findings whose field names no unit and is not stage level: the reviewer must fix the pointer."""
    errors = []
    for issue in blocking(review):
        field = str(issue.get("field", ""))
        if field.strip().lower() not in STAGE_WORDS and not resolve(field, candidate, spec):
            errors.append(f"field {field!r} names no unit id; use one of the listed unit ids, '<id>.<subfield>' or 'stage'")
    return errors


def screen_unchanged(review, previous, candidate, raised, spec=None):
    """The review may not move the target (Cuba parent plan, Dandi C07 image prompts): a blocking finding may only name
    units that changed since the previous review or that an earlier round asked the planner to fix. Other units are
    dropped from its pointer; a finding left with none becomes minor. Stage-level findings are untouched."""
    if previous is None or not isinstance((review or {}).get("issues"), list):
        return review
    before, after = units(previous, spec), units(candidate, spec)
    for issue in review["issues"]:
        if not isinstance(issue, dict) or issue.get("severity") not in {"blocker", "major"}:
            continue
        ids = resolve(issue.get("field"), candidate, spec)
        if not ids:
            continue
        live = [i for i in ids if i in raised or i not in before or i not in after
                or unit_value(previous, i, spec) != unit_value(candidate, i, spec)]
        if not live:
            issue.update(severity="minor", downgraded="unchanged since the previous review, which did not ask for this fix")
        elif len(live) < len(ids):
            issue.update(field=", ".join(live), narrowed_from=issue.get("field"))
    if review.get("pass") is not True and not any(isinstance(i, dict) and i.get("severity") in {"blocker", "major"}
                                                  for i in review["issues"]):
        review["pass"] = isinstance(review.get("score"), (int, float)) and review["score"] >= 82
    return review


def error_units(error, candidate, spec=None):
    """Validator errors name their unit before the first ': ' (e.g. 'S21: diagram labels/details are too long')."""
    head = str(error).split(": ", 1)[0] if ": " in str(error) else ""
    return resolve(head, candidate, spec) if head else []


def flag(candidate, errors, review, spec=None):
    """(flagged {unit id: [findings]}, stage-level findings). Code decides; the planner never sees the raw review."""
    flagged, stage = {}, []
    for issue in blocking(review):
        finding = {k: issue.get(k) for k in ("field", "problem", "correction")}
        ids = resolve(issue.get("field"), candidate, spec)
        for ident in ids:
            flagged.setdefault(ident, []).append(finding)
        if not ids:
            stage.append(finding)
    for error in errors:
        ids = error_units(error, candidate, spec)
        for ident in ids:
            flagged.setdefault(ident, []).append({"validation_error": error})
        if not ids:
            stage.append({"validation_error": error})
    return flagged, stage


def unit_value(candidate, ident, spec=None):
    kind, key, index = units(candidate, spec)[ident]
    if kind == "field":
        return candidate[key]
    if kind == "string":
        return {"id": ident, "text": candidate[key][index]}
    return candidate[key][index]


def merge(prior, response, flagged, spec=None):
    """The prior candidate with only the flagged units taken from the planner's response, matched by id. Anything else
    the response changed is ignored; a replaced item keeps its anchors; insert/remove operations are applied by code,
    which assigns the new ids. `response` may be the partial form ({units, fields, operations}) or a full candidate."""
    spec = normalized(spec)
    if not flagged or not isinstance(prior, dict) or not isinstance(response, dict):
        return response
    result = json.loads(json.dumps(prior))
    partial = response.get("units") if isinstance(response.get("units"), dict) else response
    fields = response.get("fields") if isinstance(response.get("fields"), dict) else response
    for key, ident in spec["lists"].items():
        replacements = partial.get(key)
        if not isinstance(replacements, list) or not isinstance(result.get(key), list):
            continue
        if ident.startswith("@"):
            by_id = {str(r.get("id")): r.get("text") for r in replacements if isinstance(r, dict) and isinstance(r.get("text"), str)}
            for index in range(len(result[key])):
                unit = string_id(ident[1:], index)
                if unit in flagged and unit in by_id:
                    result[key][index] = by_id[unit]
            continue
        by_id = {str(r.get(ident)): r for r in replacements if isinstance(r, dict) and r.get(ident) not in (None, "")}
        for index, item in enumerate(result[key]):
            unit = str(item.get(ident)) if isinstance(item, dict) else None
            if unit in flagged and unit in by_id:
                replacement = dict(by_id[unit])
                for anchor in spec["anchors"].get(key, []):
                    if anchor in item:
                        replacement[anchor] = item[anchor]
                replacement[ident] = item[ident]
                result[key][index] = replacement
    if spec["fields"]:
        for unit in flagged:
            if unit in fields and unit in result and unit not in spec["lists"]:
                result[unit] = fields[unit]
    for operation in response.get("operations") or [] if isinstance(response.get("operations"), list) else []:
        apply_operation(result, operation, flagged, spec)
    return result


def apply_operation(result, operation, flagged, spec):
    """Insert after / remove a flagged item, in a list that allows structural edits. New ids: <target>b, <target>c ..."""
    if not isinstance(operation, dict):
        return
    target, kind = str(operation.get("target", "")), operation.get("op")
    for key in spec["operations"]:
        ident = spec["lists"].get(key)
        entries = result.get(key)
        if not ident or ident.startswith("@") or not isinstance(entries, list) or target not in flagged:
            continue
        positions = [i for i, item in enumerate(entries) if isinstance(item, dict) and str(item.get(ident)) == target]
        if not positions:
            continue
        position = positions[0]
        if kind == "remove":
            del entries[position]
        elif kind == "insert_after":
            existing = {str(item.get(ident)) for item in entries if isinstance(item, dict)}
            new = []
            for item in operation.get("items") or []:
                if not isinstance(item, dict):
                    continue
                suffix = next(c for c in "bcdefghijklmnopqrstuvwxyz" if target + c not in existing)
                existing.add(target + suffix)
                new.append({**item, ident: target + suffix})
            entries[position + 1:position + 1] = new
        return


def pointer_guide(candidate, spec=None):
    """Reviewer instruction: the unit ids it may point at."""
    spec = normalized(spec)
    known = list(units(candidate, spec))
    shown = ", ".join(known[:80]) + (f" ... ({len(known)} units)" if len(known) > 80 else "")
    return ("POINTERS: every blocker/major issue's field must start with the id of the smallest unit it concerns "
            "(one of: " + shown + "), optionally '<id>.<subfield>' (e.g. '" + (known[0] if known else "ID") + ".prompt'), "
            "or several ids separated by commas. Use 'stage' only for problems of the whole candidate (counts, "
            "duplication across units, overall arc). Never point by list position."
            + "".join(f" A {name} is its own unit: point at its id ({', '.join(sorted({t for ts in links.values() for t in ts})[:12])}), "
                      f"not at the item it belongs to." for name, links in spec["links"].items() if links))


def repair_request(prior, flagged, spec=None, simplify=(), extra=""):
    """Planner feedback for a targeted repair: prior candidate read-only, flagged units with their findings, and a
    partial response holding only those units."""
    spec = normalized(spec)
    known = units(prior, spec)
    lists = sorted({known[u][1] for u in flagged if u in known and known[u][0] != "field"})
    fields = [u for u in flagged if u in known and known[u][0] == "field"]
    targeted = {u: {"current": unit_value(prior, u, spec), "findings": f} for u, f in flagged.items() if u in known and u not in simplify}
    hard = {u: {"current": unit_value(prior, u, spec), "findings": f} for u, f in flagged.items() if u in known and u in simplify}
    shape = []
    if lists:
        shape.append('"units": {' + ", ".join(f'"{key}": [complete replacement items with their unchanged ids]' for key in lists) + "}")
    if fields:
        shape.append('"fields": {' + ", ".join(f'"{key}": replacement value' for key in fields) + "}")
    ops = [key for key in lists if key in spec["operations"]]
    if ops:
        shape.append('"operations": [{"op": "insert_after"|"remove", "target": flagged id, "items": [new items, no id]}] '
                     "only when a finding requires adding or removing an item in " + ", ".join(ops))
    text = ("\nTARGETED REPAIR. The prior candidate stays as it is except the flagged units below. "
            "Return ONLY {" + ", ".join(shape) + "} with exactly the flagged units; do not return, renumber or change "
            "any other unit (code merges your units back by id and re-validates the whole candidate).\n"
            "PRIOR CANDIDATE (read-only context):\n" + json.dumps(prior, ensure_ascii=False))
    if targeted:
        text += "\nFLAGGED UNITS (fix exactly these):\n" + json.dumps(targeted, ensure_ascii=False)
    if hard:
        text += ("\nSIMPLIFY THESE UNITS (they failed review twice; stop re-arguing the same details): " +
                 json.dumps(hard, ensure_ascii=False) +
                 "\nRewrite each as a simpler, safer version with the same id and anchors. Use only facts stated in the "
                 "evidence and drop contested or unsupported specifics instead of guessing a replacement.")
    return text + extra
