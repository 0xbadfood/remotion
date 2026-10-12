# Four-stroke engine: narrated image-layer pilot

Artifacts: `artifacts/experiments/four-stroke-image-mggen-manim/`.
Completed report: `/experiments/four-stroke-image-mggen-manim/report.html` (normal webapp login).

This pilot reuses the earlier Luna teaching script, unchanged VibeVoice narration and MFA timings. The 134.28-second explanation has seven visual owners: engine overview, intake, compression, power, exhaust, cycle comparison and recap. The speech is not shortened or regenerated. Scene boundaries and semantic action cues are saved in `narration-scenes.json`; source hashes and timing checks are in `narration/provenance.json`.

Built-in imagegen supplies one polished engine cutaway. Official MG-Gen recognizes the complete engine as one object, so calibrated SAM2 boxes/points separate the five moving parts: piston, connecting rod, crankshaft and two valves. Valve silhouette corrections and source occlusions are disclosed. Raw masks and the source pixels remain available. LaMa removes moving parts; flat cavity masks explicitly reconstruct the hidden bore/crankcase substrates where inpainting is insufficient.

Manim's ideal slider-crank controller preserves the rod's length and its two attachments. The piston slides along the cylinder axis, the crankshaft rotates, and ideal valve states follow the four strokes. Stroke demonstrations, highlights, sparks and recap cycles use narration word cues; the introduction and closing recap retain their own scene ownership. A complete four-stroke cycle is 720 crankshaft degrees. Gas colours and arrows are explanatory overlays, not simulated fluid mechanics. Real-engine valve advance, delay and overlap are outside this simplified explanation.

The finished video is 134.3 seconds at 1280×720 and 30 fps, with 4,029 decoded frames and the preserved narration muxed as AAC. All seven scene checks, ideal mechanism checks and delivery checks passed. CPU rendering took 432 seconds; a cache-only intake rerun confirmed resumability. The decoded AAC waveform correlates with the source WAV at 0.99993. The report and its supporting assets were verified through the authenticated webapp routes without starting a worker.

The source connecting rod hides part of the crankshaft, and rotating the extracted crankshaft exposes that missing patch. This remains visible in the pilot. Production must request a complete isolated moving part or reconstruct its hidden region before rendering; segmentation alone cannot recover unseen pixels.

## Planner handover learned from the pilot

`llm-handover-prototype.json` is a hand-assembled instruction prototype derived from this experiment, not a new Luna response. It separates the planner's teaching decisions from the executor's constrained motion:

1. Supply the learning objective and ordered teaching actions.
2. Identify component roles, visible bounds and attachment landmarks.
3. Select a suitable motion controller, or request a missing one.
4. Specify the relationships it must preserve and source calibration.
5. Anchor demonstrations, holds, highlights and repeated cycles to MFA words.
6. Specify flow paths, layer order and occlusion/reconstruction needs.

The executor extracts the assets, derives the frame geometry, checks relationships, renders independently cached scenes and muxes the preserved narration. This is still a scoped mechanism pilot with human-inspected anchors and a hand-authored controller; it does not establish general machine understanding.

## Repeat with existing environments and saved source

Run from `/home/nitin/remotion`. The narration copies and source image are already prepared.

```bash
python scripts/experiment-four-stroke-image-pilot.py geometry \
  --output artifacts/experiments/four-stroke-image-mggen-manim

.venv-manim-pilot/bin/python scripts/experiment-four-stroke-image-manim.py \
  --scene artifacts/experiments/four-stroke-image-mggen-manim/scene.json \
  --timings artifacts/experiments/four-stroke-image-mggen-manim/narration-scenes.json \
  --output-dir artifacts/experiments/four-stroke-image-mggen-manim/manim

python scripts/experiment-four-stroke-image-pilot.py report \
  --output artifacts/experiments/four-stroke-image-mggen-manim
```

Extraction rerun commands are in the experiment's `scripts/EXTRACTION.md`; the previous circular pilot's CPU environment, official MG-Gen checkout and model weights are reused. Upstream MG-Gen is AGPL-3.0. The production job remains stopped, and no GPU or service configuration changes are required.
