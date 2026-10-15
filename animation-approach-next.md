# Research to image-backed animation: proposed approach

Status: design proposal, based on the four-stroke engine pilot. This document
does not mean the proposed automation has been implemented.

Pilot report:
https://remotion.photovault.live/experiments/four-stroke-image-mggen-manim/report.html

Keep the existing research and audio pipeline. Introduce image-backed animation
as another way to deliver a chapter. The planner decides what to teach and
demonstrate; reusable motion controllers calculate how the parts move.

## 1. Classify the subject and interpret the request

Use the subject graph to establish relevant attributes and possible ways to
organize the topic. Respect explicit scope: "all rulers" requires different
coverage from "an overview of the dynasty."

Classification guides planning without dictating the chapter outline.

## 2. Research the overall subject

Produce a bounded, sourced orientation brief containing:

- Important facts and relationships.
- Terminology and qualifications.
- Uncertainties and conflicting evidence.
- Coverage requirements derived from the user's request.

For scientific subjects, collect credible visual references where shape or
arrangement matters. A reference diagram helps establish correctness; the
generated image supplies the film's visual treatment.

## 3. Create the global chapter plan

The external planner chooses the teaching curve and assigns each chapter:

- Objective and scope.
- Entry context and exit handoff.
- Research questions.
- Delivery route: illustrated, animated, or mixed.

Introduction and conclusion chapters can remain ordinary illustrated chapters.
The global planner does not need to plan component coordinates or keyframes.

## 4. Research each chapter in detail

Resolve the chapter's specific questions and build a compact evidence package.
Record what has already been assigned to earlier chapters to avoid repetition.

Scientific chapters should explicitly identify the relationships that must
remain true and the simplifications acceptable for this audience.

## 5. Plan the teaching sequence and write narration

The animation director describes demonstrations before specifying geometry.
For the engine, the sequence is:

1. Introduce the parts.
2. Explain each stroke.
3. Compare the four stages.
4. Replay the complete cycle.

Each demonstration carries its learning message, required components and
intended viewer takeaway. Write narration around this sequence.

## 6. Render speech, audit it and run MFA

Keep the existing point-based audio production and recovery. The actual
narration determines the available demonstration windows.

This remains the audio-preview checkpoint: users can listen before incurring
the cost of visual production.

## 7. Generate the visual canvases

After audio approval, the image planner requests a suitable composition for
each demonstration group. One canvas can support several scenes, as the engine
pilot showed.

Prompts should account for:

- Which parts will move.
- Which attachment points need to be visible.
- Which surfaces will become exposed during motion.

The learning message stays attached to the canvas and its downstream artifacts.

## 8. Turn each canvas into a usable scene model

A vision-capable grounding step identifies:

- Components and their roles.
- Fixed and moving parts.
- Attachment landmarks and motion paths.
- Layer order and visibility constraints.

The default Qwen image provider also supports alpha-channel output and image
editing, as noted by the user. Use these capabilities in asset preparation:

- Edit the assembled canvas to produce a clean static background.
- Request complete moving components as transparent assets, using the canvas
  as the appearance and geometry reference.
- Complete surfaces hidden behind other parts before those surfaces become
  visible during motion.
- Use MG-Gen/SAM2 to extract or refine components when editing does not provide
  suitable isolated assets.

This may reduce segmentation and background-reconstruction work. It does not
remove the need to identify attachment landmarks, fit the mechanism and verify
the generated geometry. An edited component can still change shape or scale.

The provider adapter must preserve actual alpha data. Asset checks should catch
opaque backgrounds, baked-in checkerboards, missing surfaces and alignment
changes before accepting a component.

Check completeness before animation. The crankshaft patch in the pilot shows
why: segmentation cannot recover a surface hidden behind another part. Request
an isolated completed component when necessary.

Cache the results. Pause for unresolved assets or calibration, then resume from
this stage after the cache is filled.

## 9. Bind components to supported motion controllers

The planner selects a behavior and supplies its component relationships. The
executor fits the geometry and checks the constraints.

Initial controllers can come from the three pilots:

- Following a circular or other defined path.
- Comparing a dropped and horizontally launched projectile.
- Coordinating a slider-crank mechanism and four-stroke valve states.

Highlights, arrows, label cards and colour changes are separate teaching
overlays. Unsupported behavior becomes an explicit controller request.

## 10. Compile the animation against MFA timings

Instructions should express actions such as:

- Demonstrate compression during this phrase.
- Highlight the closed valves.
- Hold this state.
- Repeat the complete cycle here.

The compiler resolves these cues into times and frames. A shared mechanism
state controls coupled parts, so separate narration cues cannot accidentally
drive contradictory motions.

Each scene declares whether it continues, holds or resets the preceding state.

## 11. Render and review the demonstrations

Check controller invariants in code, then inspect rendered evidence for
teaching correctness:

- The right part moves.
- Its direction matches the narration.
- Labels identify the correct objects and remain readable.
- Moving parts do not reveal holes or incomplete surfaces.

Selected frames support visual review; they do not establish continuous-motion
correctness on their own. Controller checks and suitable temporal samples are
also needed.

Repairs return to the responsible stage: research, grounding, asset completion,
controller binding or timing. A small defect should not trigger another complete
chapter plan.

## 12. Assemble and deliver the film

Manim supplies the animation clips. The existing pipeline handles illustrated
scenes, narration, subtitles, music, section cards, volume and final stitching.

Cache each stage separately so an audio change, asset replacement or timing
repair rebuilds only its dependants.

## Model roles

Spark can continue handling ordinary research and planning. Initially retain
Luna for the animation teaching sequence and behavioral plan.

Image grounding needs vision. The main global planner remains text-only.

Reviewers assess scoped artifacts and rendered evidence rather than repeatedly
reviewing a complete chapter's geometry and frame-by-frame animation plan.

## What the pilot established

The prepared engine canvas supported a 134-second narrated film with seven
timed scenes. The controller kept the piston, rod, crankshaft and valves
coordinated. Existing narration and MFA timings were preserved.

Preparation still involved manual component grounding, calibrated segmentation,
attachment landmarks, substrate reconstruction and a hand-authored controller.
The handover prototype was assembled manually. The pilot does not establish
autonomous component discovery or general mechanism understanding.

## Next experiment

Automate the handover using the existing engine controller and narration:

1. Start with the saved canvas and teaching objective.
2. Have the model identify components and attachment landmarks without receiving
   the manually entered anchor values.
3. Produce controller bindings and narration-cue-based actions.
4. Extract and fit assets; detect the incomplete crankshaft before acceptance.
5. Render and review a short sequence.
6. Repeat on a different engine image.

Measure grounding accuracy, manual corrections, asset-completion failures and
visible agreement with narration. This tests the missing automation without
simultaneously inventing another mechanism.

The pilot established a convincing rendering method. The next task is to make
the preparation that we performed manually reproducible.

## Parked: on-screen historical narrator (user idea, 2026-10-15; "expensive, keep it for later")

A historical figure who is not in the cast narrates on screen as a talking portrait, e.g. a witness of the Dandi March.
Tool: `~/portrail` (LivePortrait motion from a driving clip -> LatentSync lip-sync -> CodeFormer face restore).
- Test (Mona Lisa, 5.5 s): good quality, but LatentSync took 4 min 54 s at 20 steps (~50x real time on the Spark).
  Whole-film presence is not viable (~3.5 h for 4 min); short windows only (intro, 2-3 bridges, outro, ~30 s total).
  10 steps untested. The 3 s driving clip loops; longer windows need a longer, calm driving clip. Its caption
  step fails on an `av` version mismatch (we would not use it; Remotion composes).
- Fit: lip-sync slices of the already-approved narration (word timings), rendered on the Spark GPU while the image
  factory renders scenes; narrator excluded from the cast; voice matched from the Indian narrators by gender/age.
- Content rules: the narrator must have lived through the events and been present where the narration says
  (Lala Lajpat Rai died 17 Nov 1928, before Dandi 1930; Sarojini Naidu or Mahadev Desai fit); first person only for
  documented facts; an on-screen note that the narration is imagined in their voice.
- First step when resumed: a 20 s Salt March intro from existing audio, a public-domain photo, 10 steps.

## Built: 2.5D depth camera (2026-10-15)

Depth Anything V2 Small (Apache-2.0) depth map per accepted image (`infofilm/depth.py`, ~0.05 s/image on the Spark,
31 images in 3.4 s), softened (blur 0.4% of width) so sharp depth edges warp instead of tearing; WebGL shader in
`renderer/src/DepthImage.tsx` (landscape scenes): parallax follows the pan (0.45x), near layers grow on a push.
Render cost: 32 s vs 30 s for 47 s of film. On by default (`image_depth`); advisory. Retrofit of a finished film:
resume from scenes (images, prompts, audio reused) and render again; Unification of Italy retrofitted, 31/31 scenes,
layout 70/70. Portrait delivery still uses the flat camera.

## LTX 2.5 (ComfyUI, aiserver) on the amdserver: findings (2026-10-15)

aiserver stack: `storyvault-local-ltx.target` = ComfyUI (`~/story_generator/tmp/ComfyUI-ltx25-int8`, :8197) + API
(`backend/ltx25_comfy_api_server.py`, :8011; modes conditioning_image_to_video at frame index 1,9,17.. with strength,
and first_last_frame_to_video). Smoke on the RTX 3090 with Italy images: cold 155 s (model load); warm 2 s at
768x448 = 20 s, 5 s at 768x448 = 40 s, 2 s at 1280x704 = 44 s. 2 s clips stay faithful and painterly; the 5 s clip
moves more and drifts from the still (new faces).
Why it is fast: distilled LTX-2.5 22B (8 steps, cfg 1.0: one pass per step); INT8 ConvRot weights
(`int8_tensorwise`, convrot group 256) for the transformer (21 GB) and the Gemma-4 12B text encoder (15 GB), run by
comfy-kitchen 0.2.35 native int8 kernels; ComfyUI dynamic VRAM loading (weights staged, text encoder only resident
while encoding); plain PyTorch attention.
AMD: comfy-kitchen ships a `hip` backend whose validated targets include gfx1200/gfx1201 (RDNA4 WMMA; the R9700s are
gfx1201) with int8_linear and the convrot ops; its module links libamdhip64.so.7 (amdserver has ROCm 7.x). The
aiserver wheel is cp311; the amdserver default Python is 3.14, so it needs a 3.11 venv with torch ROCm 7.2.
License: LTX-2.x Community License (to be read before product use).

### On the amdserver: video mode built and tested (2026-10-15)

- Setup: `~/projects/ltx25` (ComfyUI at the aiserver commit + ComfyUI-LTXVideo + ComfyUI-MultiGPU; Python 3.11 venv
  via uv; torch 2.14.1+rocm7.2; models copied, sha256 identical). User units `ltx-comfy`, `ltx-api` (:8011, our copy
  of the API with `--text-encoder-device cuda:1`), `ltxvideo.target`. `/opt/gpu-mode/gpu_mode_server.py` has a
  `video` mode (backup `.bak-20261015-before-video`); starting image mode now drops the file cache first.
- comfy-kitchen registers its hip backend (gfx1201, WMMA). ComfyUI's dynamic VRAM loading is not active on ROCm,
  so the 14.6 GB text encoder and the 20.5 GB transformer swapped on every new prompt; the first try ran out of
  memory at VAE decode (fixed with `--reserve-vram 4`).
- Times (2 s = 49 frames at 24 fps; RTX 3090 / AMD 250 W / AMD 300 W / AMD 300 W with the text encoder on GPU 1):
  2 s 768x448 new prompt 20 / 60 / 60 / 28 s; 5 s 768x448 40 / 86 / 84 / 56 s; 2 s 1280x704 44 / 56 / 54 / 56 s;
  2 s reference (frame 25, strength 0.65) repeated prompt - / 20 / 20 / 20 s; 2 s 1280x704 reference new prompt
  - / 92 / 92 / 64 s. 300 W barely helps (swap-bound); the encoder on GPU 1 is the real gain. With a repeated
  prompt the AMD card matches the 3090 at 768x448; at larger sizes it is about 1.2-1.4x slower.
- Reference mode (still mid-clip, strength 0.65-0.7) keeps the composition and moves on both sides of the still;
  identity drifts by the clip's end (Cavour lost his glasses), so keep clips short.
- Switching back to image mode stalled GPU 1's worker for 15+ min (file cache full of LTX weights; the worker pins
  its text encoder in RAM); dropping the cache fixed it, and the mode switch now does that itself.


### Pilot: LTX clips for a whole film (Unification of Italy, 2026-10-16)

- Motion prompts: `infofilm/motion.py`, one planner call for all 31 accepted images (final approved prompts), code
  checks only, passed first time.
- Clips: 5 s (121 frames), 768x448, approved still at frame 57 (middle) strength 0.7, RIFE x2 (ComfyUI-Frame-
  Interpolation in the amdserver ComfyUI; our API copy takes `interpolate`), so 241 frames. About 68 s per clip warm
  on one R9700 with the text encoder on GPU 1; 31 clips ~35 min.
- Renderer: a scene with `clip` plays it (OffthreadVideo) slowed to span the scene, landscape only; others keep depth.
  Full 304 s film rendered in 181 s. Output: `output/italy-clips-pilot.mp4` (pilot only; the run's delivery unchanged).
- Seen: identity holds near the middle; details drift at the ends (Cavour's glasses gone at both ends).
- Not yet in the pipeline: clip stage (after images, image -> video mode switch), clip manifest in video.py, a cheap
  vision check against the still with the depth still as fallback.
