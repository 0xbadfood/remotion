# Animation chapter runner

## Ordered Luna workflow (v2)

The [frontier runner](../../scripts/experiment-animation-frontier.py) now runs the complete chapter in this order:

1. Accept the global planner's chapter objective, scope, grounding and continuity context.
2. Luna plans the teaching sequence and visual demonstrations, including guided demonstrations, appropriate repetition and motion requirements. No detailed canvas or asset instances yet.
3. Luna writes narration around the accepted sequence. Each paragraph names its teaching beats.
4. Render speech through VibeVoice, audit each paragraph, clean long silences and run MFA. The recording establishes the clock.
5. Map explanation/repeat anchors, then ask Luna for the detailed canvas, objects, placement and animation against the real MFA timings.
6. Compile and execute Remotion. Landscape and portrait plans both finish before rendering begins.

All planning phases receive an independent semantic review and bounded repair loop. Luna is the default reviewer as well as planner, using a separate request with the candidate and source context. The local model has a narrow optional task: mapping known teaching beats and repetition transitions to actual MFA word IDs. Its result also passes code validation and Luna semantic review. It never rewrites narration or designs mechanics.

The default model is `gpt-6-luna`, using the existing Responses API adapter and JSON mode described in the [official model documentation](https://developers.openai.com/api/docs/models/gpt-6-luna) and [structured-output guide](https://developers.openai.com/api/docs/guides/structured-outputs). Credentials are read from `OPENAI_API_KEY` or `OPENAI_TOKEN` in memory and are excluded from saved artifacts.

### One command

Run from `/home/nitin/remotion`:

```bash
. /home/nitin/.profile
python scripts/experiment-animation-frontier.py \
  --chapter-plan animation-experiments/four-stroke-engine/chapter-context.json \
  --output artifacts/experiments/four-stroke-engine-luna-v2
```

`render` is the default endpoint. Speech preparation uses the configured GPU lease and preserves active external work. Native voice/Whisper/MFA dependencies must be available. Runtime remains advisory.

For a standalone concept, replace `--chapter-plan` with `--situation "Explain the working of a four-stroke engine"`. The shortcut creates the same `{film, chapter, owned_topics, orientation_anchors, ...}` envelope. The [example chapter](../four-stroke-engine/chapter-context.json) is manually prepared, not researched or model-approved. A full existing `infofilm.chapter-plan.v0` parent JSON requires `--chapter-id C01`; a child context is preferable because it includes grounding claims. The webapp's long-film orchestrator now routes chapters through this runner when animation chapters are enabled.

### Webapp chapter routing

Choose **Long film**, then **Explanatory animation chapters → Auto**. This is opt-in and uses VibeVoice; the existing image review toggle applies to illustrated chapters. The local external planner chooses `production.kind` as `illustrated` or `animation` per chapter. Animation handoffs include an objective, demonstrations, assumptions and success criteria; Luna owns the subsequent teaching, narration and detailed visual plans. The chapter first uses the existing local research/evidence path, so orientation claims are supplemented with chapter evidence and qualifications before Luna begins.

`animation-provider.json` selects the animation model, reviewer, shared library and repair/render budgets. Its resolved settings are embedded in new queue snapshots. The service needs `OPENAI_API_KEY` or `OPENAI_TOKEN`; credentials are not embedded in those snapshots.

Missing drawings set the chapter and parent run to **Waiting for assets**, rather than failed. Remaining chapters continue, and the queue can process other requests after the parent pauses. The webapp shows each chapter's plan/preview and downloadable asset demands. Register the requested SVGs in the shared cache; the page checks identities, capabilities and hashes and enables **Resume after cache fill** once the drawing requests resolve. Accepted teaching, narration, MFA and plans are retained, and completed chapters are skipped. Missing essential motion support has a separate waiting state and requires a renderer capability update before resuming.

Animation outputs pass native frame layout checks, are scaled to the usual delivery dimensions, receive the existing score/master mix and pass delivery media QA. Final assembly handles illustrated and animation chapters together; animation section cards reuse an existing rendered checkpoint. **Audio first** stops animation chapters before canvas/object planning and continues through the same saved-audio path when video is requested.

CLI equivalent: `python -m infofilm create --generation-mode long --animation-chapters --subject "Why Earth has seasons" --durations 360`. Keep the same project and options when resuming a paused CLI run. The standalone animation runner's `--resume` applies to its own output directory.

### Missing assets and resume

Asset requests are made **after narration and MFA**, during object planning. Missing SVGs save `object-requests.json`, set `waiting_for_components`, and exit **3**. If existing assets satisfy every request, execution continues without a stop. Fill the shared library through [register-animation-object.py](../../scripts/register-animation-object.py), using its hash-bound review and capability checks, then run:

```bash
python scripts/experiment-animation-frontier.py \
  --output artifacts/experiments/four-stroke-engine-luna-v2 --resume
```

Accepted teaching, script, verified audio and plans are reused. Resume restores the saved objective, model settings, orientation, library and speech configuration. It needs an API key only for phases that still need LLM work. Changed inputs require a new output directory. An explicit `--bindings FILE` can bind a requested object to a new immutable asset version; repeat those bindings on later resumptions.

`--until teaching|narration|audio|timing|canvas|objects|placement|animation|render` permits deliberate phase stops. The default is the entire pipeline. `--max-attempts` defaults to three and can be raised to five to extend a previously exhausted repair budget. Rejected candidates and review findings remain in `planning/PHASE/HASH/`. A stop never discards accepted earlier work.

### Guided demonstrations and continuous motion

The animation plan declares `animation_phases`, `motion_requirements`, `capability_gaps` and shared `cycles`. Individual guided actions use MFA anchors. A repeat may start at a later explanation/recap anchor; coordinated parts then share a period and phase. A one-time event can replay with an explained reset or hold its final state instead.

Supported primitives include continuous angular interpolation, keyframed motion, shared loops, rigid pin links and a slider-crank solver, in addition to hold, linear and projectile tracks. Pin links use local SVG landmarks; geometry is calculated in pixel units. Code checks dependency cycles, rigid pin separation, visible phase-transition jumps, loop closure and bounds across every rendered frame. This verifies implementation of declared behavior, not universal scientific correctness. Semantic review checks whether the behavior serves the objective and matches the narration.

An essential unsupported behavior saves `motion-capability-requests.json`, sets `waiting_for_motion_capabilities`, and exits **4**. Add the missing primitive/capability and resume; the updated motion contract changes the motion-phase cache while preserving upstream teaching/audio. No essential behavior is silently downgraded to a schematic fallback.

Text labels use `label_card:true` in the placement plan. Remotion draws a consistent background, border, rounded corners, shadow and six-pixel internal padding. Luna sizes the text box for that padding. Headings and body text can use `label_card:false`. Card rendering requires no SVG or separate background object.

Remotion measures text overflow and element bounds across the rendered frames. If those measurements reject the layout, the runner sends the saved findings back to placement planning, then rebuilds the dependent animation plan. Teaching, narration, audio and assets are retained. This repair is limited to measured layout defects; unrelated rendering or service failures do not trigger replanning.

### Controls and artifacts

- `--orientation landscape|portrait|both`: defaults to the chapter orientation, otherwise landscape.
- `--continuity-model local|luna`: local is the default MFA anchor mapper; its endpoint comes from the speech config's `reviewer_url`.
- `--reviewer-model MODEL`: a separate OpenAI semantic reviewer model; defaults to the chosen Luna planner.
- `--render-concurrency 1..32`: defaults to six CPU rendering workers and does not change accepted plans.
- `--max-render-repairs 0..3`: defaults to two repairs from measured rendering layout failures. Findings and rejected plans remain archived.

Outputs include teaching, narration, timing cues, canvas, components, resolved assets, placement, animation, compiled plans, measured frames, video, semantic review histories and usage totals. `/tmp/<experiment-name>-narration.wav` contains the narration for parallel listening. The authenticated experiment route serves `report.html` and the preview, for example:

<https://remotion.photovault.live/experiments/four-stroke-engine-luna-v2/report.html>

Human viewing/listening review remains pending even after semantic review and successful rendering. The reviewer assesses the plan and source context; it does not watch the entire rendered video.

## Preserved earlier pilots

Existing v1 output directories automatically resume with their original contract. `--legacy` explicitly selects the old canvas-before-audio, code-only frontier workflow for a new comparison. Its phase boundaries remain `canvas|objects|placement|animation|render`, with supplied `--narration-input` required for motion. Existing gravity and four-stroke previews remain intact.

The separate `scripts/write-animation-narration.py` helper remains for the earlier staged pilot; the new ordered runner owns narration and speech directly and needs no second command.

## Earlier local-model gravity experiments

This standalone experiment gives the configured planner and reviewer a scene requirement, the verified component catalog, and a fixed narration. The models choose the teaching sequence, component instances, placement, motion and narration anchors. It does not change production film routing.

The hand-authored [gravity seed](../../animation-seeds/gravity/scene.json) remains the preserved baseline. Its scene, cue numbers, layout and timing are excluded from model inputs. The component library is shared, so the models can reuse its four SVG assets: ball, cannon, ground and arrow. Native text, rectangles and lines are also available.

Run commands below from the repository root, `/home/nitin/remotion`.

## Fixed narration input

[narration.txt](narration.txt) is a manually written 94-word explanation of a horizontal cannon launch and a simultaneous dropped-ball comparison. Both balls start at the same height with zero initial vertical velocity; air resistance is ignored.

```bash
python scripts/prepare-animation-narration.py
```

The helper uses the current VibeVoice configuration and the existing point-render path, including Whisper auditing, retries, silence cleanup and MFA recovery. It checks that the web queue and shared GPU APIs have no active external work. It saves:

```text
artifacts/experiments/gravity-animation-llm/input-audio/narration-input.json
```

The initial prepared audio is 39.47 seconds. Whisper matched all 94 words, and MFA aligned them on its first attempt. The text was written manually; the alignment was generated by MFA and has **human review pending**. It must not be described as manually corrected or visually approved.

The bundle includes the transcript, audio and timing paths, hashes, duration, word IDs and word spans. Repeating preparation verifies and reuses the bundle. `--narration`, `--reference-audio`, `--config` and `--output` permit separate inputs; use a new output directory when changing frozen text or files.

## Phases and resumption

The [scene requirement](requirement.json) states the teaching message, facts and constraints without prescribing the baseline scene. Model routes come from `config.json` unless `--config` is supplied.

First ask the models to select components and their teaching roles:

```bash
python scripts/experiment-animation-planner.py --until components
```

The component planner receives the catalog and fixed transcript. It does not plan positions or timestamps in this phase. A separate LLM reviewer checks the component plan, and code validates its structure. The accepted plan is saved as `components-plan.json`.

After all components resolve, plan motion from the exact MFA word anchors:

```bash
python scripts/experiment-animation-planner.py --resume --until motion --orientation both
```

Each orientation gets its own layout and motion plan. The model selects supported renderer primitives; the runner computes projectile positions deterministically and adds captions from the frozen transcript. Motion plans undergo semantic LLM review and code validation before compilation.

Render the accepted plans with the fixed narration:

```bash
python scripts/experiment-animation-planner.py --resume --until render --orientation both
```

Review the actual rendered checkpoint images together with the compiled motion plan:

```bash
python scripts/experiment-animation-planner.py --resume --until review --orientation both
```

`review` is the default final phase when `--until` is omitted. Checkpoint acceptance requires a reviewer score of at least 82, a passing verdict and no major defect. Findings are saved as `renders/ORIENTATION/frame-review.json`. A rejected checkpoint review preserves the renders, sets `state.json` to `rendered_needs_review`, and exits with code **2**. It does not automatically rewrite the accepted plan after reviewing frames.

`--orientation landscape|portrait|both` is available in every phase. The default is landscape. This experiment renders landscape at 1280 × 720 and portrait at 720 × 1280, both at 30 fps. `--max-attempts` bounds each planning/review loop to 1–5 attempts; the default is three.

`--resume` requires an existing experiment. Accepted phases are reused, and changed requirement, narration bundle or model routes require a new `--output` directory. Replenishing the library resumes object resolution without discarding the accepted component plan.

## Comparing planner and reviewer roles

`--swap-models` reverses the configured planner and reviewer URLs for this experiment only. It leaves production configuration unchanged. With the current routes, the swapped trial uses the dense model at `10.8.0.5:8001` as planner and Spark at `10.8.0.4:8001` as reviewer. It uses the same requirement, frozen narration and library inputs, with no baseline scene injected.

Use a **new output directory** for the different frozen model routes:

```bash
python scripts/experiment-animation-planner.py --swap-models \
  --output artifacts/experiments/gravity-animation-llm-swapped \
  --until review --max-attempts 5
```

When resuming this trial, repeat both `--swap-models` and its output directory:

```bash
python scripts/experiment-animation-planner.py --resume --swap-models \
  --output artifacts/experiments/gravity-animation-llm-swapped \
  --until review --max-attempts 5
```

The swapped trial is an experiment; its results must be assessed separately from the original role assignment. This command description makes no claim that the trial has passed.

## Previewing a model draft that was not accepted

`--preview-candidate PATH` can render a saved, model-written motion draft as an **explicitly unapproved preview**. The candidate must belong to the current output directory and chosen orientation, match a saved planner result from the frozen planner route, and still pass code validation. This permits inspecting a disputed draft without accepting or freezing the rejected plan.

Use exactly one orientation and `--until render` or `--until review`. For example, this previews the saved `candidate-3.json` from the swapped trial:

```bash
python scripts/experiment-animation-planner.py --resume --swap-models \
  --output artifacts/experiments/gravity-animation-llm-swapped \
  --orientation landscape --until review \
  --preview-candidate artifacts/experiments/gravity-animation-llm-swapped/planning/motion-landscape/983534b5c494d3be/candidate-3.json
```

The source is recorded in `preview-source.json`, including the matched model request and `approved: false`. Even a passing checkpoint review leaves the run in `preview_needs_review` and returns exit code **2**. A preview is evidence for evaluation; it is not visual approval or an automatic substitute for an accepted motion plan. Human viewing and listening review remains pending.

## Missing components: explicit page faults

If an asset is absent or lacks a requested capability, the runner saves `object-requests.json`, marks `state.json` as `waiting_for_components`, and exits with code **3**. This is a planned pause. It does not silently substitute an object or invoke an object creator.

Each request includes its request ID, immutable asset ID, affected instance IDs, visual message, teaching role, desired capabilities and SVG requirements. An operator or external object creator can produce the requested SVG, review it, and register it:

```bash
python scripts/register-animation-object.py \
  --requests artifacts/experiments/gravity-animation-llm/object-requests.json \
  --request-id REQUEST_ID \
  --svg /tmp/requested-object.svg \
  --metadata /tmp/requested-object-metadata.json \
  --review /tmp/requested-object-review.json
```

Replace `REQUEST_ID` with the request's actual value. Metadata must contain a normalized `pivot` pair, a unique `capabilities` list covering every required capability, and nonempty `provenance`. For example, the structure is:

```json
{
  "pivot": [0.5, 0.5],
  "capabilities": ["actual-requested-capability"],
  "provenance": "Who created this SVG and how it was checked"
}
```

Registration requires an actual approving review tied to the **exact SVG bytes**. Compute their checksum with `sha256sum /tmp/requested-object.svg`. The review JSON must contain `pass: true`, that checksum as `asset_sha256`, and `assessed_capabilities` covering **every capability declared by the metadata**, not only the required subset:

```json
{
  "pass": true,
  "asset_sha256": "REPLACE_WITH_THE_EXACT_64_CHARACTER_SVG_SHA256",
  "assessed_capabilities": ["actual-requested-capability"]
}
```

Editing the SVG after review invalidates that approval. Executable SVGs and external references are rejected. Registered identities are immutable; existing objects are never overwritten.

For a `missing_capabilities` fault on an existing asset, register a new unused version using `--asset-id`, then explicitly bind the affected instance IDs to that version. A bindings file has this shape:

```json
{
  "ACTUAL_COMPONENT_INSTANCE_ID": "namespace.object.v2"
}
```

Resume with the same bindings on subsequent commands:

```bash
python scripts/experiment-animation-planner.py --resume \
  --bindings /tmp/object-bindings.json --until review --orientation both
```

The binding changes the experiment's resolved instance while preserving the shared original asset. A newly requested missing asset normally uses its requested identity and needs no binding override. `--library` selects another registry for either planning or registration; pass the same library path to both.

## Results and evaluation

The experiment saves input snapshots, component requests and resolutions, model requests/reviews, motion drafts, compiled plans, render logs, previews and checkpoint frames beneath `artifacts/experiments/gravity-animation-llm/`.

After the render phase publishes its report, view it through the existing webapp login:

<https://remotion.photovault.live/experiments/gravity-animation-llm/report.html>

Semantic plan review, code validation and rendered checkpoint review are distinct checks. The checkpoint reviewer sees selected still frames and the compiled plan; it cannot establish every detail of continuous animation or listen to the narration. Human visual and listening evaluation remains pending even if the checkpoint reviewer passes. Inspect the previews for teaching clarity, label placement, visible interactions and synchronization. Rendering successfully does not itself mean the frames have been approved.
