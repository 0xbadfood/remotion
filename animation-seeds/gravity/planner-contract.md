# Gravity animation seed

This is a hand-authored scene demonstrating the proposed planner contract. It does not enable automatic animation routing in production.

The main planner supplies the visual message, source-backed facts, assumptions, narration and MFA checkpoints, orientation, and creative brief. The specialized planner selects a supported archetype and requests objects by asset identity and required capabilities. It returns the object instances, interaction model, animation checkpoints, text cues and acceptance criteria. The seed currently uses manual cue times and has no speech track.

Use `scene.json` as an executable example. Preserve its `visual_message` in object requests, compiled manifests, render results and review artifacts. For future narration, bind named events to MFA checkpoints and reserve readable holds; the physical clock and presentation clock remain separate.

## Shared assets and instance changes

`animation-library/registry.json` holds versioned, content-hashed SVG objects. Asset files remain immutable during a scene run. Scene instances own transforms, visibility and timing. The launched and dropped balls share the same asset. Creating a new geometry or changing its capabilities creates a new asset version with provenance; it does not overwrite the shared version. SVGs are renderer assets, not arbitrary executable scene code.

## Missing objects

An unavailable asset or an unmet capability is an `object_page_fault`. Write a structured request containing the visual message, object role, required capabilities, instance requirements and proposed asset identity. Halt compilation before rendering the incomplete scene. An external asset creator can consume that request, create a new candidate SVG, and submit it for structural and semantic review before publication to the registry. The seed emits requests; an automatic external creation worker is not implemented yet.

Example exercise: `python scripts/experiment-gravity-animation.py --probe-missing notation.stopwatch.v1`. This writes an unresolved request and exits with code 3 without rendering. Normal runs resolve all seed objects.

## Motion and review

The renderer supports the `projectile-comparison.v1` archetype. Positions are calculated from uniform gravity: `x = x0 + vx*t`, `y = h - g*t*t/2`, ending at `sqrt(2*h/g)`. Presentation time slows the demonstration. It never changes the physical relationship.

Review the initial frame, midflight, first landing, comparison midflight and final landing, plus the video for motion and readability. Frame boundaries and mathematical invariants are checked mechanically. A successful seed demonstrates execution and object reuse; it is not evidence of autonomous planning quality or a substitute for future educational review.

## Running

`python scripts/experiment-gravity-animation.py --orientation both`

Uses the installed renderer and browser from `config.json`; requires no image-generation or LLM GPU. Results go to `artifacts/experiments/gravity-animation-seed/`. The report embeds the MP4 previews so it can use the existing authenticated experiment-report route without restarting the webapp. Standalone MP4 files are retained locally.
