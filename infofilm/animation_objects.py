"""Immutable SVG catalog and explicit object faults for animation experiments.

No object is substituted or created by resolution.  A caller may retain the
registry snapshot and pass it back to pin an accepted scene to exact assets.
"""
from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
import xml.etree.ElementTree as ET

from .core import ROOT

DEFAULT_LIBRARY = ROOT / "animation-library"
LIBRARY_SCHEMA = "infofilm.animation-library.v1"
_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_LOCAL_REFERENCE = re.compile(r"url\(\s*(['\"]?)#([A-Za-z_][A-Za-z0-9_.:-]*)\1\s*\)")
_SVG_ELEMENTS = {
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline",
    "polygon", "defs", "linearGradient", "radialGradient", "stop", "title",
    "desc", "clipPath", "mask", "marker", "symbol", "use", "pattern", "text", "tspan",
}


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value) and len(value) == len(set(value))


def _pivot(value):
    return (isinstance(value, list) and len(value) == 2 and
            all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and 0 <= x <= 1 for x in value))


def _inside(library, name):
    path = (library / name).resolve()
    if not path.is_relative_to(library.resolve()):
        raise ValueError("Animation object file escapes its library")
    return path


def validate_svg(data):
    """Return intrinsic geometry; reject executable or externally linked SVGs."""
    text = data.decode("utf-8")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
        raise ValueError("SVG DTDs and entities are forbidden")
    if re.search(r"<\?(?!xml(?:\s|\?>))", text, re.I):
        raise ValueError("SVG processing instructions are forbidden")
    try:
        svg = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid SVG: {exc}") from exc
    ids, references = set(), []
    for element in svg.iter():
        tag = element.tag
        namespace, local = (tag[1:].split("}", 1) if tag.startswith("{") else (None, tag))
        if namespace not in {None, _SVG_NS} or local not in _SVG_ELEMENTS:
            raise ValueError("Unsupported SVG element: " + local)
        if element is svg and local != "svg":
            raise ValueError("Object root must be svg")
        for key, value in element.attrib.items():
            attr_namespace, attr = (key[1:].split("}", 1) if key.startswith("{") else (None, key))
            if attr.lower().startswith("on") or attr.lower() in {"style", "base", "externalresourcesrequired"}:
                raise ValueError("Executable or external SVG attribute: " + attr)
            if attr_namespace not in {None, _XLINK_NS, "http://www.w3.org/XML/1998/namespace"}:
                raise ValueError("Unsupported SVG attribute namespace")
            if attr_namespace == "http://www.w3.org/XML/1998/namespace" and attr != "space":
                raise ValueError("Unsupported XML attribute in SVG")
            if attr == "id":
                if value in ids:
                    raise ValueError("Duplicate SVG definition ID")
                ids.add(value)
            if attr == "href":
                if not re.fullmatch(r"#[A-Za-z_][A-Za-z0-9_.:-]*", value):
                    raise ValueError("SVG references must be local fragments")
                references.append(value[1:])
            if "url(" in value.lower():
                matches = list(_LOCAL_REFERENCE.finditer(value))
                remainder = _LOCAL_REFERENCE.sub("", value)
                if not matches or "url(" in remainder.lower():
                    raise ValueError("SVG URL references must be local fragments")
                references.extend(match.group(2) for match in matches)
    if set(references) - ids:
        raise ValueError("SVG has unresolved local references")
    view_box = None
    if "viewBox" in svg.attrib:
        try:
            view_box = [float(x) for x in re.split(r"[\s,]+", svg.attrib["viewBox"].strip())]
        except ValueError as exc:
            raise ValueError("Invalid SVG viewBox") from exc
        if len(view_box) != 4 or not all(math.isfinite(x) for x in view_box) or min(view_box[2:]) <= 0:
            raise ValueError("SVG viewBox needs finite positive dimensions")
    dimensions = []
    for key in ("width", "height"):
        value = svg.attrib.get(key, "")
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(?:px)?", value.strip())
        dimensions.append(float(match.group(1)) if match else None)
    if any(x is None or not math.isfinite(x) or x <= 0 for x in dimensions):
        if not view_box:
            raise ValueError("SVG requires a viewBox or positive intrinsic dimensions")
        dimensions = view_box[2:]
    return {"viewbox": view_box, "intrinsic_dimensions": {"width": dimensions[0], "height": dimensions[1]}}


def _validate_registry(registry):
    if not isinstance(registry, dict) or registry.get("schema_version") != LIBRARY_SCHEMA or not isinstance(registry.get("objects"), dict):
        raise ValueError("Unsupported animation library registry")
    if not isinstance(registry.get("revision"), str) or not registry["revision"].strip():
        raise ValueError("Animation registry needs a revision")
    for asset_id, asset in registry["objects"].items():
        if not isinstance(asset_id, str) or not _SAFE_ID.fullmatch(asset_id) or not isinstance(asset, dict):
            raise ValueError("Invalid animation asset identity")
        if not isinstance(asset.get("file"), str) or not asset["file"] or not _HASH.fullmatch(str(asset.get("sha256", ""))):
            raise ValueError("Animation asset needs a file and SHA256: " + asset_id)
        if not _pivot(asset.get("pivot")) or not _strings(asset.get("capabilities")):
            raise ValueError("Invalid animation asset capabilities or pivot: " + asset_id)
        if not isinstance(asset.get("provenance"), str) or not asset["provenance"].strip():
            raise ValueError("Animation asset needs provenance: " + asset_id)
    return registry


def registry_snapshot(library=DEFAULT_LIBRARY):
    return copy.deepcopy(_validate_registry(json.loads((Path(library) / "registry.json").read_text())))


def _asset_data(library, asset_id, asset):
    data = _inside(Path(library), asset["file"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != asset["sha256"]:
        raise ValueError("Object content changed without a new identity: " + asset_id)
    geometry = validate_svg(data)
    return data, geometry


def catalog(library=DEFAULT_LIBRARY, registry=None):
    """Describe verified objects without SVG bytes or baseline scene details."""
    snapshot = copy.deepcopy(_validate_registry(registry)) if registry is not None else registry_snapshot(library)
    objects = {}
    for asset_id, asset in snapshot["objects"].items():
        _, geometry = _asset_data(library, asset_id, asset)
        objects[asset_id] = {"asset_id": asset_id, **{key: copy.deepcopy(asset[key]) for key in ("sha256", "pivot", "capabilities", "provenance")}, **geometry}
    return {"schema_version": "infofilm.animation-catalog.v1", "library_revision": snapshot["revision"],
            "registry_sha256": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(), "objects": objects}


def validate_component_plan(plan):
    errors = []
    if not isinstance(plan, dict):
        return ["Component plan must be one JSON object"]
    objects, requests = plan.get("objects"), plan.get("component_requests", [])
    if not isinstance(objects, list) or not objects:
        return ["Component plan needs nonempty objects"]
    if not isinstance(requests, list):
        return ["component_requests must be a list"]
    if not isinstance(plan.get("visual_message"), str) or not plan["visual_message"].strip():
        errors.append("Component plan needs its visual_message")
    request_ids = set()
    for request in requests:
        if not isinstance(request, dict):
            errors.append("Invalid component request"); continue
        request_id = request.get("id")
        if not isinstance(request_id, str) or not request_id.strip() or request_id in request_ids:
            errors.append("Component request IDs must be unique nonempty strings")
        else:
            request_ids.add(request_id)
        if not isinstance(request.get("requested_asset_id"), str) or not _SAFE_ID.fullmatch(request["requested_asset_id"]):
            errors.append("Component request needs a safe requested_asset_id")
        if not isinstance(request.get("description"), str) or not request["description"].strip():
            errors.append("Component request needs a description")
        if not _strings(request.get("required_capabilities")) or not isinstance(request.get("svg_requirements"), dict):
            errors.append("Component request needs capabilities and svg_requirements")
    ids, referenced = set(), set()
    for obj in objects:
        if not isinstance(obj, dict):
            errors.append("Invalid component object"); continue
        object_id = obj.get("id")
        if not isinstance(object_id, str) or not object_id.strip() or object_id in ids:
            errors.append("Object IDs must be unique nonempty strings")
        else:
            ids.add(object_id)
        if not isinstance(obj.get("kind"), str) or obj["kind"] not in {"svg", "text", "rect", "line"}:
            errors.append("Unsupported component kind")
        for key in ("role", "conveys"):
            if not isinstance(obj.get(key), str) or not obj[key].strip():
                errors.append("Object needs " + key)
        if not _strings(obj.get("required_capabilities")):
            errors.append("Object needs unique required_capabilities")
        asset_id, request_id = obj.get("asset_id"), obj.get("request_id")
        if obj.get("kind") == "svg":
            if (asset_id is None) == (request_id is None):
                errors.append("SVG object needs exactly one asset_id or request_id")
            if asset_id is not None and (not isinstance(asset_id, str) or not _SAFE_ID.fullmatch(asset_id)):
                errors.append("Invalid object asset_id")
            if request_id is not None:
                if not isinstance(request_id, str) or request_id not in request_ids:
                    errors.append("Unknown object request_id")
                else:
                    referenced.add(request_id)
        elif asset_id is not None or request_id is not None:
            errors.append("Native primitives cannot refer to SVG assets or requests")
    if request_ids - referenced:
        errors.append("Unreferenced component requests: " + ", ".join(sorted(request_ids - referenced)))
    return errors


def resolve_components(plan, library=DEFAULT_LIBRARY, registry=None):
    """Resolve the accepted plan or return explicit faults without substitutions."""
    errors = validate_component_plan(plan)
    if errors:
        raise ValueError("Invalid component plan: " + "; ".join(errors))
    snapshot = copy.deepcopy(_validate_registry(registry)) if registry is not None else registry_snapshot(library)
    requests = {request["id"]: request for request in plan.get("component_requests", [])}
    assets, faults = {}, []
    for obj in plan["objects"]:
        if obj["kind"] != "svg":
            continue
        request = requests.get(obj.get("request_id"), {})
        asset_id = obj.get("asset_id") or request["requested_asset_id"]
        asset = snapshot["objects"].get(asset_id)
        required = sorted(set(obj["required_capabilities"]) | set(request.get("required_capabilities", [])))
        missing = sorted(set(required) - set(asset.get("capabilities", []) if asset else []))
        if not asset or missing:
            faults.append({"schema_version": "infofilm.animation-object-request.v1", "state": "pending_creation",
                "reason": "missing_asset" if not asset else "missing_capabilities", "request_id": obj.get("request_id") or "request-" + obj["id"],
                "scene_id": plan.get("scene_id", plan.get("id")), "visual_message": plan["visual_message"],
                "object_id": obj["id"], "object_ids": [obj["id"]], "requested_asset_id": asset_id,
                "proposed_asset_id": asset_id, "role": obj["role"], "conveys": obj["conveys"],
                "description": request.get("description", obj["conveys"]), "required_capabilities": required,
                "missing_capabilities": missing, "svg_requirements": copy.deepcopy(request.get("svg_requirements", {})),
                "creation_contract": {"format": "svg", "immutable_identity": True, "external_resources": False,
                                      "review_before_registration": True, "review_bound_to_sha256": True}})
            continue
        if asset_id not in assets:
            data, geometry = _asset_data(library, asset_id, asset)
            assets[asset_id] = {**copy.deepcopy(asset), **geometry,
                               "data_uri": "data:image/svg+xml;base64," + base64.b64encode(data).decode()}
    return assets, faults


def _atomic_json(path, value):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(value, handle, indent=2, ensure_ascii=False); handle.write("\n")
            handle.flush(); os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True); raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def register_object(requests, request_id, svg_path, metadata, review, *, library=DEFAULT_LIBRARY, asset_id=None):
    """Publish a hash-bound, reviewed SVG without overwriting an asset identity."""
    if isinstance(requests, dict):
        requests = requests.get("requests")
    if not isinstance(requests, list):
        raise ValueError("Object requests must be a list or a requests envelope")
    matches = [row for row in requests if isinstance(row, dict) and (row.get("request_id") or row.get("id")) == request_id]
    if not matches:
        raise ValueError("Unknown object request_id")
    identities = [row.get("requested_asset_id") or row.get("proposed_asset_id") for row in matches]
    if any(not isinstance(identity, str) or not _SAFE_ID.fullmatch(identity) for identity in identities):
        raise ValueError("Request needs a safe immutable asset identity")
    requested_ids = set(identities)
    if len(requested_ids) != 1:
        raise ValueError("Request identity maps to inconsistent assets")
    requested = next(iter(requested_ids))
    asset_id = requested if asset_id is None else asset_id
    if not isinstance(asset_id, str) or not _SAFE_ID.fullmatch(asset_id):
        raise ValueError("Registered asset_id must be a safe immutable identity")
    variant = asset_id != requested
    if variant and any(row.get("reason") != "missing_capabilities" for row in matches):
        raise ValueError("Registered asset_id must match the requested identity except for an explicit missing_capabilities variant")
    object_ids = set()
    for row in matches:
        values = row.get("object_ids", [row["object_id"]] if row.get("object_id") else [])
        if not _strings(values):
            raise ValueError("Request object_ids must be unique strings")
        object_ids.update(values)
    if variant and not object_ids:
        raise ValueError("A capability variant needs explicit object IDs for its binding")
    receipt_context = {"request_id": request_id, "requested_asset_id": requested,
                       "binding_required": variant, "object_ids": sorted(object_ids)}
    if any(not _strings(row.get("required_capabilities")) for row in matches):
        raise ValueError("Request capabilities must be a list of unique strings")
    required = set().union(*(set(row["required_capabilities"]) for row in matches))
    data = Path(svg_path).read_bytes()
    geometry = validate_svg(data)
    checksum = hashlib.sha256(data).hexdigest()
    if not isinstance(metadata, dict) or not _pivot(metadata.get("pivot")) or not _strings(metadata.get("capabilities")):
        raise ValueError("Metadata needs a normalized pivot and unique capabilities")
    if not isinstance(metadata.get("provenance"), str) or not metadata["provenance"].strip():
        raise ValueError("Metadata needs nonempty provenance")
    if not required <= set(metadata["capabilities"]):
        raise ValueError("Candidate does not declare all requested capabilities")
    if not isinstance(review, dict) or review.get("pass") is not True or review.get("asset_sha256") != checksum:
        raise ValueError("Registration requires an approving review bound to the exact SVG SHA256")
    if not _strings(review.get("assessed_capabilities")) or not set(metadata["capabilities"]) <= set(review["assessed_capabilities"]):
        raise ValueError("Review must assess every capability declared by the candidate")
    library = Path(library).resolve(); library.mkdir(parents=True, exist_ok=True)
    with (library / ".registry.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry_path = library / "registry.json"
        registry = registry_snapshot(library) if registry_path.exists() else {"schema_version": LIBRARY_SCHEMA, "revision": "initial", "objects": {}}
        if variant:
            requested_asset = registry["objects"].get(requested)
            if not requested_asset:
                raise ValueError("A capability variant requires an existing requested asset")
            _asset_data(library, requested, requested_asset)
            if required <= set(requested_asset["capabilities"]):
                raise ValueError("A capability variant requires genuinely missing capabilities")
            if asset_id in registry["objects"]:
                raise ValueError("A capability variant requires a different unused immutable identity")
        existing = registry["objects"].get(asset_id)
        if existing:
            _asset_data(library, asset_id, existing)
            if existing["sha256"] != checksum or any(existing[key] != metadata[key] for key in ("pivot", "capabilities", "provenance")):
                raise ValueError("Immutable asset identity already exists; request a new version")
            return {"state": "reused", "asset_id": asset_id, "sha256": checksum, "library_revision": registry["revision"], **receipt_context}
        objects_dir = _inside(library, "objects"); objects_dir.mkdir(parents=True, exist_ok=True)
        relative = "objects/" + asset_id + "-" + checksum[:16] + ".svg"
        destination = _inside(library, relative)
        if destination.exists():
            if destination.read_bytes() != data:
                raise ValueError("Immutable SVG file already exists with different bytes")
        else:
            # A same-directory temporary file makes publication atomic too.
            with tempfile.NamedTemporaryFile("wb", dir=objects_dir, prefix=".object-", delete=False) as handle:
                temporary = Path(handle.name); handle.write(data); handle.flush(); os.fsync(handle.fileno())
            try:
                # A hard link publishes the complete file without an overwrite,
                # including if another writer published it since the check.
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    if destination.read_bytes() != data:
                        raise ValueError("Immutable SVG file appeared with different bytes")
            finally:
                temporary.unlink(missing_ok=True)
        registry["objects"][asset_id] = {"file": relative, "sha256": checksum, "pivot": metadata["pivot"],
            "capabilities": metadata["capabilities"], "provenance": metadata["provenance"], **geometry,
            "registration_review": {"pass": True, "asset_sha256": checksum, "assessed_capabilities": review["assessed_capabilities"]}}
        identity = hashlib.sha256(json.dumps(registry["objects"], sort_keys=True).encode()).hexdigest()
        registry["revision"] = "objects-" + identity[:16]
        _atomic_json(registry_path, registry)
    return {"state": "registered", "asset_id": asset_id, "sha256": checksum,
            "library_revision": registry["revision"], "file": relative, **receipt_context}
