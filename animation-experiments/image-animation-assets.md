# Image animation assets and manual cache fill

The asset helper is [infofilm/image_animation_assets.py](../infofilm/image_animation_assets.py). It prepares an immutable image request package, generates or resumes explicitly executed Qwen jobs, and imports supplied PNGs. Preparing requests, inspecting files, and importing assets use the CPU. Generation requires `execute=True`; the film CLI enables it only with explicit `--generate-assets`. The default CLI run writes requests and pauses for manual cache fill.

See [the CLI guide](image-animation-cli.md) for phase controls, saved routes, chapter handoffs, and complete film execution.

## Files in an asset request directory

| File | Purpose |
| --- | --- |
| `asset-requests.json` | Original asset prompts, dimensions, dependencies, metadata, and request hashes. |
| `image-provider-capabilities.json` | Read-only API schema/status inspection and evidence for reference edits and native RGBA. |
| `asset-manifest.json` | Ready assets, exact file hashes, alpha statistics, and unresolved requests. |
| `missing-asset-requests.json` | Complete remaining requests, including available reference paths/hashes. |
| `asset-import-template.json` | Starting JSON for filling missing assets manually. |
| `assets/<id>-<request hash>/` | Per-asset PNGs and submission, provider-result, validation, and selection checkpoints. |

The helper's `directory` and `cache_dir` arguments name this directory, rather than its `assets/` subdirectory. Different canvas/layer request batches may have their own directories. Use the directory belonging to the package being filled.

The CLI puts the original canvas package at `chapters/<chapter>/canvases/<canvas>/canvas-assets/` and its background/part package at `chapters/<chapter>/canvases/<canvas>/layers-<groundhash>/`. Read `state.json` for the active versioned package. The layer package includes an exact hashed copy of the approved original canvas as its `canvas` reference, so creating the layers does not generate another original scene.

## Canvas, background, and positioned parts

The standard pipeline uses a single source-canvas coordinate system: origin at the upper left, x increasing right, and y increasing down. For a 1536 × 864 canvas, the original scene, clean background, and each moving layer are all 1536 × 864 PNGs.

A moving part stays at its measured scene position and initial pose inside its full-canvas RGBA image. Pixels outside the complete part have native transparent alpha. The wrist pin, crank rotation center, valve placement, or other named anchors remain in the source-canvas coordinates used by the controller. A centered isolated sprite would require additional placement measurements and does not fulfill this positioned-layer contract.

The background is a reference-conditioned edit of the canvas. Its prompt should describe the clean static scene and the surfaces revealed after removing the moving components. Each part request describes the complete component, including portions that were originally hidden by another component. Shared reference images preserve appearance; they do not prove that an edit retained exact geometry. Inspect the resulting layers and measure their anchors before accepting the final rig.

Generated labels, arrows, and explanatory text are separate code overlays. Keep them out of the raster requests unless the writing belongs to an actual depicted object.

Example flat requests before the helper adds hashes and defaults:

```json
[
  {
    "id": "canvas",
    "kind": "canvas",
    "prompt": "A clear side cutaway of a four-stroke engine at its specified initial crank angle.",
    "width": 1536,
    "height": 864
  },
  {
    "id": "background",
    "kind": "background",
    "prompt": "Edit the attached engine into its clean static housing, with the moving components removed and the exposed surfaces completed.",
    "width": 1536,
    "height": 864,
    "reference_ids": ["canvas"]
  },
  {
    "id": "piston",
    "kind": "part",
    "prompt": "From the attached engine, render the complete piston at its original position and initial pose on a 1536 by 864 transparent RGBA canvas. Keep the wrist pin at its measured source-canvas coordinate.",
    "width": 1536,
    "height": 864,
    "reference_ids": ["canvas"],
    "transparent_required": true,
    "required_landmarks": ["wrist_pin_px"],
    "landmarks": {},
    "grounding": {"coordinate_system": "source_canvas_pixels"},
    "controller": {"type": "ideal_slider_crank_four_stroke", "role": "piston"}
  }
]
```

IDs must be unique safe strings. References name other requests in the same package; the helper orders dependencies and rejects cycles. It binds every uploaded reference to the exact ready PNG hash, including when a confirmed missing Qwen job is resubmitted.

## Native alpha and reference-edit capability

The inspected Qwen Image 2.1 API has `references` uploads on `/api/generate`. It passes those images into the image-conditioned pipeline. The installed `QwenImage21Pipeline` uses a four-channel VAE, and the API saves its PNG output without flattening RGBA. Existing returned PNGs confirm a genuine alpha channel with values spanning 0–255.

This API has no separate `transparent_background` flag. Native transparent generation is requested in the prompt. The helper recognizes this route using the installed model, pipeline, and server evidence, and records their file hashes. For a different service, an explicit API alpha control or a verified native-RGBA declaration/evidence is required; otherwise parts receive `unsupported_native_alpha` and remain available for manual fill.

The installed pipeline rounds dimensions to multiples of 32 even though its HTTP endpoint admits multiples of 16. Use supported dimensions such as 1536 × 864. A successful HTTP request alone cannot establish that the output dimensions or transparency are usable.

Every asset is decoded and checked against its exact requested dimensions. Moving parts additionally need actual transparency and visible content: at least 0.5% of pixels must have alpha 0–8, and at least 0.1% must have alpha 200–255. Nearly opaque RGBA and empty or faint transparent outputs fail. The raw returned/imported PNG bytes are retained; the helper performs no chroma key, RGB-to-alpha conversion, or background-removal approximation.

These are technical checks. They do not certify component identity, complete hidden geometry, alignment, historical/scientific correctness, or a successful visual review. Asset records explicitly report `visual_review: "not_performed"`.

## Fill missing assets and resume

1. Open `asset-manifest.json` and `missing-asset-requests.json` in the relevant request directory. Read the exact prompt, dimensions, alpha requirement, references, and required landmark names for each missing item.
2. Copy `asset-import-template.json` to `asset-import.json` in the same request directory. Keep its `schema_version`, `requests_sha256`, each asset ID, and each `request_sha256` unchanged. Remove entries that you are not filling in this import.
3. Put the PNGs beside `asset-import.json`, or in a subdirectory beneath it. Set each `file` to a relative path such as `manual/piston.png`. Absolute paths, `..`, and symlinks resolving outside the import JSON's directory are rejected.
4. Check the actual alpha against a contrasting background. Confirm that the complete part occupies the intended source-canvas location and that the clean background contains no remaining copy of the moving object.
5. Measure every required landmark in the source PNG. Supply finite pixel `[x, y]` coordinates within its width and height. Update `grounding` and `controller` metadata when the manual inspection provides better evidence.
6. Resume the same film run with `--resume`; the CLI imports `asset-import.json` automatically from each request directory. Alternatively, call the helper below before resuming. Ready assets are verified by their hashes and reused without new GPU work.

Example import entry; copy the actual hashes from the template:

```json
{
  "schema_version": "infofilm.image-animation-import.v1",
  "requests_sha256": "COPY_FROM_TEMPLATE",
  "assets": [
    {
      "id": "piston",
      "file": "manual/piston.png",
      "request_sha256": "COPY_FROM_TEMPLATE",
      "landmarks": {"wrist_pin_px": [768, 420]},
      "grounding": {
        "coordinate_system": "source_canvas_pixels",
        "measurement": "Wrist-pin center inspected in the supplied full-canvas PNG"
      },
      "controller": {
        "type": "ideal_slider_crank_four_stroke",
        "role": "piston"
      }
    }
  ]
}
```

```python
from pathlib import Path
from infofilm.image_animation_assets import import_assets

package_directory = Path("animation-experiments/my-film/chapters/C01/canvases/K01/layers-0123456789ab")
result = import_assets(
    package_directory / "asset-import.json",
    package_directory / "asset-requests.json",
    package_directory,
)
print(result["status"], result["unresolved"])
```

Replace the example directory's hash with the active package path from `state.json`.

A partial import is valid. Referenced assets must already be ready or supplied in the same import. The package is validated before files are copied, so an invalid entry does not partly fill the cache. A supplied optional `sha256` is checked against the PNG bytes. Existing ready assets are immutable: replace an asset by creating a new request/package, rather than overwriting a ready cached PNG or changing a saved prompt.

Generated assets can carry `missing_landmarks:<name>` in `incomplete_flags` until grounding is complete. Manual imports enforce their request's required landmarks. Numeric validation establishes that a point is in bounds; it does not establish that the point marks the correct physical feature.

An import preserves its supplied grounding/controller metadata, and imported measured landmarks are applied to the final scene. Changing other metadata does not by itself rewrite the film's accepted controller parameters; inspect the final `scene.json` before rendering.

To correct uncertain geometry, controller parameters, or a technically ready layer that failed visual review, save a complete corrected `grounding-import.json` in the canvas directory and resume. The workflow preserves the earlier grounding, writes a `grounding-manual-<hash>.json` revision, and creates a new `layers-<groundhash>/` package. Change the scoped edit prompt and measured geometry as needed, then fill or explicitly generate the revised package. Ready PNGs in the previous package remain immutable.

## Measured anchors and cropped parts

Measure anchors on the actual layer that will be rendered, at the rig's initial pose. For an engine, use the piston wrist-pin center, crankshaft rotation anchor, and both connecting-rod endpoints when a rod raster is supplied. Keep these distinct from the PNG center or alpha bounding-box center. A valid rectangular bound cannot substitute for a mechanical attachment point.

The [renderer](../infofilm/image_animation_renderer.py) can internally crop a full-canvas layer's alpha support while retaining its source placement and source-canvas landmarks. This reduces the raster being transformed without shifting its measured anchors. The original cached full-canvas PNG and its provenance hash remain intact.

The renderer also accepts an explicitly authored cropped-raster scene. For that contract, `bbox_xyxy_px` describes where the **entire cropped input raster**, including any transparent margin, fits in the source canvas. Alternatively, supply both `size_px` and `centre_px`. Required anchor coordinates in the renderer scene remain source-canvas coordinates, rather than coordinates local to the crop.

For example, a 128 × 256 cropped piston covering source-canvas `[660, 330, 788, 586]` with a crop-local wrist pin `[64, 90]` has source-canvas wrist pin `[724, 420]`. If the crop is scaled, map the point proportionally through the declared rectangle:

```text
canvas_x = bbox_left + local_x / crop_width  * bbox_width
canvas_y = bbox_top  + local_y / crop_height * bbox_height
```

The current full-canvas request package does not silently accept smaller cropped PNGs. Its importer enforces the original requested dimensions, and its landmark validation uses that source PNG's coordinates. To use separately supplied cropped parts, author an explicit renderer scene with the placement and canvas-space anchors above, or add a reviewed crop-aware request contract. Do not change a saved full-canvas request's dimensions or reuse crop-local anchors as canvas-space anchors.

## Waiting states and checkpoints

| State | Next step |
| --- | --- |
| `waiting_execution` | Run the explicit generation stage or fill the cache manually. |
| `waiting_provider_busy` | Resume when the shared provider is idle; the helper does not park/restart services. |
| `unsupported_native_alpha` | Import genuine transparent PNGs or configure a verified native-alpha route. |
| `unsupported_referenced_edit` | Supply manually edited layers or a provider with actual reference-image support. |
| `unsupported_generation_dimensions` | Supply exact-size assets manually or make a new request with supported dimensions. |
| `waiting_reference_assets` | Fill the referenced canvas/layers before the dependent asset. |
| `invalid_native_alpha` | Inspect the saved candidate and supply a valid PNG through the import package. |
| `assets_ready` | Continue geometry grounding and controller/render checks. |

The overall `waiting_manual_assets` state packages unsupported or invalid remaining assets. It does not exhaust automatic artistic retries. Each generated asset keeps its submission settings, seed, references, and returned job record. A confirmed `GET /api/jobs/<id>` 404 uses the existing bounded missing-job recovery with the same settings/reference bytes. An unknown POST outcome remains blocked for reconciliation and is never blindly resubmitted.

The helper acquires the root configuration's existing GPU lease only for useful, explicitly executed generation. It preserves an already loaded Qwen model, refuses busy Qwen/audio work, and does not automatically switch or recycle services.
