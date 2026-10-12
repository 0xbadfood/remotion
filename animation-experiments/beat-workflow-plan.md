# Migration plan: animation execution by teaching beat

Status: implemented as the default ordered animation execution mode on 2026-10-07. Existing production chapter artifacts remain retained. The original whole-chapter path is available with `--execution-mode chapter`.

## Implemented and verified

The beat manifest, scoped selection/asset resolution/placement/motion phases, cycle carry, dependency receipts, resumable asset pauses, web chapter progress and independent Remotion beat wrapper are implemented. The same standalone command runs the full sequence and resumes with `--resume`. A separate execution namespace preserves the original chapter inputs, speech, MFA and catalogue.

Python fixtures cover failure and cache-fill recovery, reuse without new model calls, placement continuity, scoped layout repair and audio-before-visual planning. A real two-beat CPU render verified separate sizes for a reused SVG, one caption layer, one audio stream, both boundary checkpoints and one global layout measurement per frame. The four existing circular-motion chapters pass read-only manifest checks, with every original MFA word assigned once and all silent intervals covered.

The first implementation retains the chapter catalogue and does not automatically subdivide teaching beats or add catalogue objects. Actual checkpoint stills are produced with the chapter render, alongside every-frame layout checks; separate semantic image review of stills before assembly remains future work. Saved narration paragraphs shared by several beats require a reviewed partition instead of silently changing speech. Fresh beat runs require single-owner paragraphs.

The first live Luna pilot used C02's 26-second opening beat, with its original narration and cached SVGs. It selected 10 active objects from the 66-instance chapter catalogue. Selection and placement passed on their first attempts; motion passed after two scoped repairs, with a semantic score of 86. The repairs addressed a pause/speed discontinuity at release and the resulting viewport bounds. Every model response completed within the normal 24,000-token output budget. The actual Remotion render measured all 780 frames, with zero layout failures and one audio stream. Original checkpoint and narration WAV hashes remain unchanged for all four chapters. The preview is served at `/experiments/c02-beat-refactor-smoke/report.html`; formal receipts are under `artifacts/experiments/beat-refactor-validation/`.

## Decision

Keep the chapter's accepted teaching sequence, narration, MFA timing map, canvas plans and object catalogue. Replace chapter-wide placement and motion requests with independently checkpointed requests for each teaching beat. A beat owns its active objects, placement, animation and transition. The chapter owns the shared visual conventions and narration clock.

Use a versioned execution mode (`execution_mode: "chapter" | "beats"`) in the animation provider settings and a corresponding standalone `--execution-mode` switch. Existing execution remains available for comparison. The migration creates a separate execution branch inside the existing animation project; it does not rewrite the original frozen v2 inputs.

## What the existing circular-motion run can reuse

Read-only inspection of the current saved plans and live object registry produced this inventory:

| Chapter | Teaching beats | Planned object instances | Distinct resolved SVG assets | Missing SVG assets | Approved chapter-wide placement |
| --- | ---: | ---: | ---: | ---: | --- |
| C01 — The Force That Turns | 6 | 55 | 10 | 0 | Yes |
| C02 — The Rotating Perspective | 6 | 66 | 5 | 0 | Yes |
| C03 — Turning on the Road | 10 | 90 | 7 | 0 | No |
| C04 — Beyond the Circle | 10 | 47 | 10 | 0 | No |

SVG counts are distinct within each chapter; assets can be shared across chapters. Instance counts include native labels, rectangles and lines, as well as alternate demonstration states. They are not simultaneous on-screen objects.

- Reuse `teaching-plan.json`, accepted narration script, `attached-narration.json`, the checked audio and MFA files, and `timing-cues-plan.json` without new synthesis or alignment.
- Reuse each `canvas-<orientation>-plan.json`: viewport, palette, caption reserve, zones and teaching intent remain authoritative. Zones can be reused at different times; their overlap in the chapter plan does not mean simultaneous objects.
- Reuse `components-plan.json`, `resolved-components.json` and immutable registry assets. Asset identity, SVG bytes, pivots, landmarks and capabilities stay hash-bound.
- Treat approved C01/C02 placements as reusable candidate geometry. Extract only the objects used by a particular beat, then check its geometry and entry-state compatibility and conduct scoped semantic review. Approval of the whole placement is not automatic approval of every new subset or transition.
- C03/C04 placement candidates are diagnostic material, not accepted geometry. Their canvases and assets are still usable.
- Imagegen canvas previews remain review illustrations. They are not production assets or exact placement specifications.

An SVG asset and an on-screen instance are different things. One cached car SVG can appear in several beats at different sizes or positions. Those sizes belong to each beat's placement; they do not require regenerating the SVG.

## Execution sequence

1. **Create an execution manifest.** Record source checkpoint hashes, accepted teaching order, orientation and exact MFA anchors. Keep original word IDs. Record each beat's narration window, repeat cue and a separate visual ownership window.
2. **Select objects for a beat.** Luna receives that beat, a concise neighbouring-beat synopsis, the shared canvas conventions and the existing object catalogue. It returns active instance IDs, dependency closure and any genuinely missing capability or asset. It selects from the catalogue rather than re-requesting available drawings. Native text remains code-drawn, including label cards.
3. **Resolve assets.** Verify only the selected assets and dependencies. A missing asset creates a scoped request and pause. Filling the cache resumes that beat; accepted earlier work is retained. Any added object is a versioned delta, not a rewrite of the original catalogue.
4. **Plan placement.** Send only the selected components, relevant SVG geometry, the shared canvas and preceding end state. Luna returns initial placement, labels and relationships for this demonstration. Run code checks, failure-only local schema repair where appropriate, then the scoped semantic review.
5. **Plan motion.** Send only the accepted beat placement, its narration/MFA slice, repeat cue and required capabilities. Luna returns guided motion, loops/replays/holds and the outgoing transition. It does not regenerate the chapter-wide teaching plan or tracks for inactive objects. Validate and review the beat.
6. **Compile and inspect.** Compile that beat using the existing motion primitives. Render small checkpoint stills at entry, important demonstration moments and exit. Review actual geometry and teaching meaning; repair only the affected beat. Preserve the full approved narration.
7. **Assemble the chapter.** Sequence beat plans on one chapter timeline, with one narration track, one caption layer and the existing downstream music/delivery path.

The first migration retains the approved chapter catalogue. For future runs, the same catalogue interface can accumulate objects through per-beat selection and requests, so object discovery need not become another oversized chapter call.

The default execution unit is an accepted teaching beat. A beat with several independent demonstrations can be subdivided at its existing narration clauses or repeat cue, using MFA anchors. Subdivision requires a reviewed mapping back to the same teaching objective and speech; it never cuts or rewrites narration just to meet a runtime target.

## Timing, visibility and continuity

The saved timing cues describe spoken explanations, not continuous video coverage. This run has roughly one-second pauses between beats, plus leading and trailing silence. Simply cutting at speech starts and ends would leave visual gaps.

- Code derives contiguous, frame-aligned visual windows covering the whole audio. By default, hold the preceding beat's final picture through its pause and let the next beat own its entrance at the next spoken cue. Give the first beat the leading interval and the last beat the trailing interval.
- Retain original global MFA IDs and seconds for provenance. Code converts only the selected window to a local rendering clock, recording the offset and frame rounding. Models do not renumber words or calculate chapter offsets.
- The compiler computes the previous beat's actual terminal state: instance/asset identity, position, size, rotation, visibility, styles and relevant mechanical links. A planner's claimed end state is not sufficient.
- Every transition declares `retain`, `move`, `hide`, `introduce` or `explained_reset`. A retained visible object must enter at the carried state. Changes of size, text or scene treatment require an explicit transition or a new local instance.
- Carry shared cycle phase and timing when a loop genuinely continues across a boundary. Add explicit compiler support and tests for this; restarting a loop at phase zero must not masquerade as continuity. Supported closing-loop/hold or narrated replay transitions can be used when appropriate.
- Linked rods, vectors and reference grids travel with their dependency group. A link may not reference an object absent from the beat.
- Code owns temporal coverage, legal references, geometry bounds and measurable continuity. Semantic review owns whether the transition, mechanics and demonstration explain the narration. Scope reviewers to the beat plus its entry/exit context.

## Renderer and compiler changes

The current renderer gives each object one fixed width, height, style and asset for the entire compiled chapter. Appending all beat tracks into that flat plan would prevent legitimate resizing or relabeling between demonstrations.

Use a chapter composition containing independently compiled beat plans inside Remotion `Sequence` boundaries. Each beat keeps its own fixed placements and local clock. Reuse `AnimationExperiment`, SVG rendering, label cards and the existing motion primitives inside those sequences. The chapter wrapper renders audio and captions once; nested beats render neither a second audio track nor duplicate captions.

Namespace rendered instance, marker, cycle and checkpoint IDs by beat while retaining original catalogue IDs in provenance. Centralize layout measurement at the chapter level, or explicitly map nested measurement frames to global frames, so QA still verifies every final video frame. Sample both sides of every beat boundary.

Start with one composed chapter video, not separate encoded clips joined with ffmpeg. Beat-level planning and still checkpoints provide resumability without adding audio seams or repeated compression. Beat-video caching can be a later performance improvement.

## Checkpoints and invalidation

Create `executions/beat-v1/<source-hash>/manifest.json` plus per-orientation, per-beat directories for selection, placement, motion, review, compiled output and outgoing-state receipts. The manifest references existing upstream checkpoints and verified asset hashes.

- A motion repair invalidates that beat's compiled output and render receipts, not its narration, objects or approved placement.
- A placement repair invalidates dependent motion for that beat.
- If a beat's outgoing state changes, revalidate the next beat against it. Reuse later checkpoints whenever their declared incoming dependencies are unchanged; otherwise propagate invalidation only along affected continuity dependencies.
- A missing asset pauses the dependent beat with the existing cache-fill/resume model. Independent planning may continue, but final assembly requires every essential beat to be ready.
- Log chapter, beat position/total, beat ID, phase, selected-object count, review result and repair count. Expose the same progress inside the existing chapter row in the webapp.
- Transport failures remain distinct from schema, semantic and output-budget failures. No partial JSON becomes a checkpoint and no unconfirmed timed-out request is blindly retried.

## Implementation boundaries

1. Add a beat manifest/context builder and compiler adapter, including timeline ownership and incoming/outgoing-state receipts.
2. Add the beat execution branch to `infofilm/animation_workflow.py`, reusing `phase`, scoped schema repair and asset resolution without weakening their acceptance rules.
3. Add versioned beat selection/placement/motion contracts. Validators use the selected subset and its complete dependency graph; catalogue role coverage is checked in the assembled chapter.
4. Extend the renderer with a chapter wrapper, single audio/caption ownership and global-frame QA measurements.
5. Add CLI/provider routing and compatible production pause/progress handling in `animation_frontier.py` and `animation_production.py`. Existing chapter-mode runs retain their old checkpoints.
6. Validate with meaningful fixtures: an object reused at a different scale; a linked mechanism crossing a boundary; a carried cycle phase; silence between spoken beats; local repair and resume after an asset fault; and invalidation after a changed terminal state.

## Pilot and acceptance

Use C02 first, reusing its approved canvas, object cache, placement candidates, narration and MFA. Plan its six beats separately. Then repeat for C01, C03 and C04, which exercise reusable equations, vehicle force diagrams and broad changes of scene.

Record prompt/output/reasoning tokens, response time, truncations, repair count and accepted objects per beat. Inspect the actual checkpoint images and watch the stitched chapter with the original audio.

Acceptance requires unchanged upstream audio/MFA/source hashes, correct coverage of the accepted teaching sequence, all essential assets/capabilities resolved, readable label cards, no blank or unintended persistent objects at transitions, correct narrated demonstrations, and successful existing media/layout QA. Runtime remains advisory. Frontier calls remain bounded and independently resumable; a difficult beat must not force regeneration of the whole chapter.

## Answer to the reuse question

Yes: existing canvases, object definitions and verified drawings remain usable. The accepted narration and MFA remain usable too. What changes is how instances are selected, placed and animated over time. C01/C02 approved placements can seed that work, but the new beat boundaries and incoming states still require validation. No SVG regeneration or research/narration restart is required unless a beat exposes a genuinely missing capability or asset.
