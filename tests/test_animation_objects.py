"""Asset identity, pause/resume, and safe external creation contracts."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import tempfile
import unittest

from infofilm.animation_objects import (
    DEFAULT_LIBRARY, catalog, registry_snapshot, register_object,
    resolve_components, validate_component_plan, validate_svg,
)


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><circle cx="10" cy="10" r="9"/></svg>'


def plan(asset_id="geometry.ball.v1", capabilities=None):
    return {"requirement_id": "gravity", "scene_id": "gravity-exp", "visual_message": "Gravity pulls a moving ball down.",
            "teaching_strategy": "Observe one ball", "assumptions": [],
            "objects": [{"id": "ball", "kind": "svg", "role": "moving ball", "conveys": "A falling object",
                         "asset_id": asset_id, "request_id": None, "required_capabilities": capabilities or ["circular"],
                         "physics_role": "projectile"}], "component_requests": []}


class AnimationObjectTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.work = Path(self.folder.name)
        self.library = self.work / "library"
        (self.library / "objects").mkdir(parents=True)
        (self.library / "objects" / "ball.svg").write_bytes(SVG)
        (self.library / "registry.json").write_text(json.dumps({"schema_version": "infofilm.animation-library.v1", "revision": "one",
            "objects": {"geometry.ball.v1": {"file": "objects/ball.svg", "sha256": hashlib.sha256(SVG).hexdigest(),
            "pivot": [.5, .5], "capabilities": ["circular"], "provenance": "Test original"}}}))

    def test_existing_catalog_and_resolution_do_not_change_seed_registry(self):
        before = (DEFAULT_LIBRARY / "registry.json").read_bytes()
        snapshot = registry_snapshot()
        description = catalog(registry=snapshot)
        assets, faults = resolve_components(plan(), registry=snapshot)
        assert not faults
        assert set(assets) == {"geometry.ball.v1"}
        assert description["objects"]["geometry.ball.v1"]["intrinsic_dimensions"]["width"] > 0
        assert "data_uri" not in description["objects"]["geometry.ball.v1"]
        assert "objects/ball.svg" not in json.dumps(description)
        assert (DEFAULT_LIBRARY / "registry.json").read_bytes() == before


    def test_missing_identity_and_capability_are_explicit_faults(self):
        assets, faults = resolve_components(plan("geometry.missing.v1"), self.library)
        assert assets == {}
        assert faults[0]["reason"] == "missing_asset"
        assert faults[0]["object_ids"] == ["ball"]
        assert faults[0]["visual_message"] == plan()["visual_message"]
        assets, faults = resolve_components(plan(capabilities=["side-view"]), self.library)
        assert assets == {}
        assert faults[0]["reason"] == "missing_capabilities"
        assert faults[0]["missing_capabilities"] == ["side-view"]


    def test_requested_asset_resolution_and_native_primitives(self):
        candidate = plan()
        candidate["objects"][0].update(asset_id=None, request_id="R01")
        candidate["component_requests"] = [{"id": "R01", "requested_asset_id": "geometry.ball.v1", "description": "Round ball",
                                           "required_capabilities": ["circular"], "svg_requirements": {"view": "front"}}]
        candidate["objects"].append({"id": "caption", "kind": "text", "role": "caption", "conveys": "Gravity", "asset_id": None,
                                      "request_id": None, "required_capabilities": []})
        assert validate_component_plan(candidate) == []
        assets, faults = resolve_components(candidate, self.library)
        assert not faults and set(assets) == {"geometry.ball.v1"}
        candidate["objects"][0]["asset_id"] = "geometry.ball.v1"
        assert "SVG object needs exactly one asset_id or request_id" in validate_component_plan(candidate)
        candidate["objects"][0].update(asset_id=None, request_id="wrong")
        assert "Unknown object request_id" in validate_component_plan(candidate)


    def test_registry_snapshot_keeps_missing_assets_pinned_after_replenishment(self):
        frozen = registry_snapshot(self.library)
        candidate = plan("geometry.new-ball.v1")
        _, faults = resolve_components(candidate, self.library, frozen)
        svg = self.work / "new.svg"; svg.write_bytes(SVG)
        checksum = hashlib.sha256(SVG).hexdigest()
        metadata = {"pivot": [.5, .5], "capabilities": ["circular"], "provenance": "Reviewed test original"}
        review = {"pass": True, "asset_sha256": checksum, "assessed_capabilities": ["circular"]}
        register_object(faults, faults[0]["request_id"], svg, metadata, review, library=self.library)
        # Accepted component plan can resume against a new snapshot, but an already
        # compiled snapshot must not acquire assets from the changing live registry.
        assert resolve_components(candidate, self.library)[1] == []
        assert resolve_components(candidate, self.library, frozen)[1][0]["reason"] == "missing_asset"
        (self.library / "objects" / "ball.svg").write_bytes(SVG.replace(b'r="9"', b'r="8"'))
        with self.assertRaisesRegex(ValueError, "changed without a new identity"):
            resolve_components(plan(), self.library, frozen)


    def test_unsafe_svg_rejected(self):
        samples = [
            b'<svg viewBox="0 0 10 10"><script>alert(1)</script></svg>',
            b'<svg viewBox="0 0 10 10"><foreignObject/></svg>',
            b'<svg viewBox="0 0 10 10" onload="run()"/>',
            b'<svg viewBox="0 0 10 10"><use href="https://example.com/a.svg"/></svg>',
            b'<svg viewBox="0 0 10 10"><rect fill="url(https://example.com/a.svg)"/></svg>',
            b'<!DOCTYPE svg [<!ENTITY x "x">]><svg viewBox="0 0 10 10"/>',
            b'<?xml-stylesheet href="https://example.com/a.css"?><svg viewBox="0 0 10 10"/>',
            b'<svg viewBox="0 0 10 10"><rect fill="url(#missing)"/></svg>',
        ]
        for svg in samples:
            with self.subTest(svg=svg), self.assertRaises(ValueError):
                validate_svg(svg)


    def test_harmless_local_gradients_and_defs_are_allowed(self):
        svg = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 30">
        <defs><linearGradient id="paint"><stop stop-color="#fff"/><stop offset="1" stop-color="#000"/></linearGradient>
        <circle id="shape" cx="10" cy="10" r="5"/></defs>
        <use href="#shape" fill="url(#paint)"/></svg>'''
        assert validate_svg(svg) == {"viewbox": [0, 0, 20, 30], "intrinsic_dimensions": {"width": 20, "height": 30}}


    def test_registration_requires_hash_bound_review_and_never_overwrites(self):
        candidate = plan("geometry.test-ball.v1")
        _, faults = resolve_components(candidate, self.library)
        svg = self.work / "input.svg"; svg.write_bytes(SVG)
        metadata = {"pivot": [.5, .5], "capabilities": ["circular"], "provenance": "Reviewed original"}
        review = {"pass": True, "asset_sha256": "0" * 64, "assessed_capabilities": ["circular"]}
        original = (self.library / "registry.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "exact SVG SHA256"):
            register_object(faults, faults[0]["request_id"], svg, metadata, review, library=self.library)
        assert (self.library / "registry.json").read_bytes() == original
        review["asset_sha256"] = hashlib.sha256(SVG).hexdigest()
        with self.assertRaisesRegex(ValueError, "match the requested identity"):
            register_object(faults, faults[0]["request_id"], svg, metadata, review, library=self.library, asset_id="other.v1")
        with self.assertRaisesRegex(ValueError, "assess every capability"):
            register_object(faults, faults[0]["request_id"], svg, {**metadata, "capabilities": ["circular", "unreviewed"]}, review, library=self.library)
        receipt = register_object({"requests": faults}, faults[0]["request_id"], svg, metadata, review, library=self.library)
        assert receipt["state"] == "registered"
        assert hashlib.sha256((self.library / receipt["file"]).read_bytes()).hexdigest() == review["asset_sha256"]
        frozen = (self.library / "registry.json").read_bytes()
        assert register_object(faults, faults[0]["request_id"], svg, metadata, review, library=self.library)["state"] == "reused"
        assert (self.library / "registry.json").read_bytes() == frozen
        svg.write_bytes(SVG.replace(b'r="9"', b'r="8"'))
        review["asset_sha256"] = hashlib.sha256(svg.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "Immutable asset identity"):
            register_object(faults, faults[0]["request_id"], svg, metadata, review, library=self.library)
        assert (self.library / "registry.json").read_bytes() == frozen


    def test_library_path_escape_rejected(self):
        (self.work / "outside.svg").write_bytes(SVG)
        snapshot = registry_snapshot(self.library)
        snapshot["objects"]["geometry.ball.v1"]["file"] = "../outside.svg"
        with self.assertRaisesRegex(ValueError, "escapes its library"):
            resolve_components(plan(), self.library, snapshot)

    def test_registration_cli_accepts_envelope_and_publishes_resumable_asset(self):
        _, faults = resolve_components(plan("geometry.cli-ball.v1"), self.library)
        inputs = {"requests": {"requests": faults}, "metadata": {"pivot": [.5, .5], "capabilities": ["circular"], "provenance": "CLI original"},
                  "review": {"pass": True, "asset_sha256": hashlib.sha256(SVG).hexdigest(), "assessed_capabilities": ["circular"]}}
        for name, data in inputs.items():
            (self.work / (name + ".json")).write_text(json.dumps(data))
        svg = self.work / "input.svg"; svg.write_bytes(SVG)
        script = Path(__file__).resolve().parents[1] / "scripts/register-animation-object.py"
        result = subprocess.run([sys.executable, str(script), "--library", str(self.library), "--requests", str(self.work / "requests.json"),
            "--request-id", faults[0]["request_id"], "--svg", str(svg), "--metadata", str(self.work / "metadata.json"),
            "--review", str(self.work / "review.json")], check=True, capture_output=True, text=True)
        assert json.loads(result.stdout)["state"] == "registered"
        assert resolve_components(plan("geometry.cli-ball.v1"), self.library)[1] == []

    def test_capability_variant_requires_explicit_binding_and_preserves_original_asset(self):
        original = registry_snapshot(self.library)
        accepted = plan(capabilities=["circular", "center-pivot"])
        _, faults = resolve_components(accepted, self.library)
        svg = self.work / "variant.svg"; svg.write_bytes(SVG)
        metadata = {"pivot": [.5, .5], "capabilities": ["circular", "center-pivot"], "provenance": "Reviewed center pivot variant"}
        review = {"pass": True, "asset_sha256": hashlib.sha256(SVG).hexdigest(), "assessed_capabilities": metadata["capabilities"]}
        receipt = register_object(faults, faults[0]["request_id"], svg, metadata, review,
                                  library=self.library, asset_id="geometry.ball.v2")
        assert receipt["requested_asset_id"] == "geometry.ball.v1"
        assert receipt["asset_id"] == "geometry.ball.v2"
        assert receipt["binding_required"] is True and receipt["object_ids"] == ["ball"]
        assert registry_snapshot(self.library)["objects"]["geometry.ball.v1"] == original["objects"]["geometry.ball.v1"]
        # Registration does not alias the old ID or silently repair the accepted plan.
        assert resolve_components(accepted, self.library)[1][0]["reason"] == "missing_capabilities"
        binding = {receipt["object_ids"][0]: receipt["asset_id"]}
        effective = copy.deepcopy(accepted)
        effective["objects"][0]["asset_id"] = binding[effective["objects"][0]["id"]]
        assets, remaining = resolve_components(effective, self.library)
        assert not remaining and set(assets) == {"geometry.ball.v2"}
        assert accepted["objects"][0]["asset_id"] == "geometry.ball.v1"
        frozen = (self.library / "registry.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "unused immutable identity"):
            register_object(faults, faults[0]["request_id"], svg, metadata, review, library=self.library, asset_id="geometry.ball.v2")
        assert (self.library / "registry.json").read_bytes() == frozen

    def test_variant_registration_rejects_nonexistent_requested_identity(self):
        _, faults = resolve_components(plan("geometry.missing.v1"), self.library)
        faults[0]["reason"] = "missing_capabilities"
        svg = self.work / "input.svg"; svg.write_bytes(SVG)
        metadata = {"pivot": [.5, .5], "capabilities": ["circular"], "provenance": "Reviewed original"}
        review = {"pass": True, "asset_sha256": hashlib.sha256(SVG).hexdigest(), "assessed_capabilities": ["circular"]}
        with self.assertRaisesRegex(ValueError, "existing requested asset"):
            register_object(faults, faults[0]["request_id"], svg, metadata, review, library=self.library, asset_id="geometry.missing.v2")
        faults[0]["requested_asset_id"] = "geometry.ball.v1"
        with self.assertRaisesRegex(ValueError, "genuinely missing capabilities"):
            register_object(faults, faults[0]["request_id"], svg, metadata, review, library=self.library, asset_id="geometry.ball.v2")


if __name__ == "__main__":
    unittest.main()
