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
