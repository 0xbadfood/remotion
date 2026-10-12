# Image-backed animation CLI

[scripts/create-image-animation.py](../scripts/create-image-animation.py) runs an isolated, resumable educational-film workflow. It supports a new subject, a multi-chapter film, or one chapter from an existing parent plan. It writes its own output directory and uses the existing speech, image, music, and CPU rendering adapters.

The sequence is:

```text
optional subject classification
→ source research/extraction → source-grounded evidence → parent chapter plan
→ chapter teaching sequence → narration → audited speech → MFA word alignment
→ reusable canvas plan → original canvas assets → measured image grounding
→ clean background and complete transparent parts → layer review
→ narration-word motion cues → deterministic controller compilation
→ CPU Cairo/Manim rendering and rendered-frame review
→ optional music/mix → chapter delivery → final chapter stitching
```

Runtime targets guide planning, with a default advisory duration of 180 seconds. They do not force narration padding or truncation. A chapter can reuse one canvas across several adjacent teaching beats, with holds, guided actions, loops, or replays selected for those spoken words. Optional subject-graph classification is enabled by default.

## Start and stop at useful phases

Run commands from the repository root. A route/configuration preview makes no external calls:

```bash
python scripts/create-image-animation.py \
  --subject "How a four-stroke engine works" \
  --output artifacts/image-animation-cli/engine-demo \
  --dry-run
```

A normal run contacts its configured research/LLM and speech services. Image generation is an explicit separate choice: without `--generate-assets`, it writes image requests and pauses for cache fill. To inspect the accepted narration and actual MFA timing first:

```bash
python scripts/create-image-animation.py \
  --subject "How a four-stroke engine works" \
  --output artifacts/image-animation-cli/engine-demo \
  --duration-seconds 180 \
  --until audio
```

To continue through the reusable canvas plan:

```bash
python scripts/create-image-animation.py \
  --output artifacts/image-animation-cli/engine-demo \
  --resume --until canvas
```

Resume through the remaining phases and create manual image requests:

```bash
python scripts/create-image-animation.py \
  --output artifacts/image-animation-cli/engine-demo \
  --resume
```

Enable real Qwen image submissions explicitly:

```bash
python scripts/create-image-animation.py \
  --output artifacts/image-animation-cli/engine-demo \
  --resume --generate-assets
```

`--generate-assets` authorizes the image-generation stage; speech and optional music use their existing native paths. Busy external work produces a waiting state. Image generation preserves an already loaded Qwen model and does not automatically park or restart services.

`--until` accepts `classification`, `research`, `evidence`, `parent`, `teaching`, `narration`, `audio`, `canvas`, `assets`, `grounding`, `motion`, `render`, or `complete`. Stops occur at the workflow's phase boundaries; phases inside the canvas loop may stop on the first canvas that reaches that boundary. The default is `complete`.

## Route each LLM activity

[image-animation-providers.json](../image-animation-providers.json) defines three routes:

| Route | Current adapter/configuration |
| --- | --- |
| `luna` | `gpt-6-luna` through the official Responses endpoint; reasoning effort and output ceiling come from the registry. |
| `local` | Existing local chat-completions planner at the root config's `planner_url`. |
| `local-reviewer` | Existing local chat-completions reviewer at the root config's `reviewer_url`. |

The Luna adapter reads `OPENAI_API_KEY` or `OPENAI_TOKEN` from the environment. Credentials are excluded from registry snapshots and request metadata. Local model identities are discovered from their endpoints unless the registry supplies a model name.

| Activity | Default route |
| --- | --- |
| `research`, `classification`, `parent`, `evidence` | `local` |
| `teaching`, `narration`, `canvas`, `motion` | `luna` |
| `grounding`, `review` | `local-reviewer` |

Set any activity with `--<activity>-model ROUTE`, or repeat `--llm ACTIVITY=ROUTE`:

```bash
python scripts/create-image-animation.py \
  --subject "Why two released balls reach the ground together" \
  --output artifacts/image-animation-cli/gravity-demo \
  --teaching-model local \
  --narration-model luna \
  --canvas-model luna \
  --grounding-model local-reviewer \
  --motion-model luna \
  --review-model local-reviewer \
  --until canvas
```

All supported activity names are `research`, `classification`, `parent`, `evidence`, `teaching`, `narration`, `canvas`, `grounding`, `motion`, and `review`. A dedicated `--<activity>-model` value takes precedence over a `--llm` value for that activity. `--providers PATH` supplies another validated registry with the same route names.

`--config PATH` selects the root engine configuration, default `config.json`. Fresh runs use `image-animation-providers.json` when no registry is supplied; resumed runs use their saved registry by default. `--audience TEXT` sets the intended viewer group.

Grounding, layer review, and rendered-frame review need an image-capable model. The current main `local` GLM endpoint is a text planner; choose `local-reviewer` or `luna` for these vision activities. The CLI permits route experimentation, and incompatible image requests fail visibly.

The `review` route handles semantic reviewer calls, including reviews of the other planning activities and the dedicated layer/frame reviews. `--review on` is the default. `--review off` disables this workflow's planning and visual semantic checks. Code/schema, geometry, file-hash, alpha, timing, Whisper, and MFA gates still run.

The existing speech audit remains independent of these new LLM routes. Whisper audits run on the CPU, use the configured fallback recognizers when needed, and invoke the existing local `reviewer_url` transcript adjudicator only for eligible recognition disagreements. `--review-model` and `--review off` do not reroute or disable those audio checks.

## Resume accepted work

```bash
python scripts/create-image-animation.py \
  --output artifacts/image-animation-cli/engine-demo \
  --resume
```

The run restores its saved subject, input files, chapter selection, mode, advisory duration, audience, orientation, frame rate, output/source-canvas dimensions, graph/review policy, and music choice. It restores the saved provider registry by default. Completed stages retain their original accepted results and route provenance.

An explicit route override applies to pending work:

```bash
python scripts/create-image-animation.py \
  --output artifacts/image-animation-cli/engine-demo \
  --resume --grounding-model luna --review-model luna
```

`--providers` can explicitly replace the registry used for pending calls. Saved speech/image engine settings remain pinned; current root planner/reviewer endpoints can be used by pending local calls. Changing the subject, source content, accepted script, or other frozen content/settings requires a new output directory. `--until` and `--generate-assets` are execution controls and can be selected again on resume; image execution stays off unless that command supplies `--generate-assets`.

Model calls save request/result metadata and bounded format/phase repair history. `--max-attempts` controls phase attempts from 1 to 5, default 2. Output-budget exhaustion preserves the incomplete response and pauses for a smaller packet or an adjusted provider output ceiling instead of repeating an identical truncated call.

Transient network failures and HTTP 408/429/5xx receive at most three transport attempts with short delays. Safe diagnostic files record the failure category and status. Authentication, configuration and other permanent request failures remain visible. Source research archives each usable source progressively; one temporarily unavailable source digest can be deferred while other readable sources continue. Resume reuses full pages and accepted digests, including across explicit pending model-route changes. Digest structure repairs use the parsed candidate and bounded cited excerpts. Every passage referenced by an accepted claim is retained in the downstream evidence packet.

## Reuse research, narration, or a chapter handoff

`--sources PATH` supplies saved extracted sources with a nonempty `results` array containing `url` and `raw_content`. Source text, rather than a subject graph or planning digest alone, supports the evidence packet. `--no-subject-graph` skips optional taxonomy classification.

`--narration-input PATH` attaches a verified `infofilm.animation-narration.v1` bundle to one chapter. Its transcript must exactly match the accepted script; audio/timing hashes, word order, and MFA spans are checked. Select one chapter when attaching an existing bundle.

The mutually exclusive starting inputs are `--subject` and `--chapter-input`. Supported chapter inputs are:

- An existing `{film, chapter, owned_topics, orientation_anchors, ...}` chapter-context envelope.
- An existing `infofilm.chapter-plan.v0` parent plan; select one chapter with `--chapter-id` when it contains several.
- A new `infofilm.image-animation-film.v1` parent plan produced by this CLI; select its chapter the same way.

```bash
python scripts/create-image-animation.py \
  --chapter-input artifacts/existing-film/parent.json \
  --chapter-id C02 \
  --sources artifacts/existing-film/extracted.json \
  --output artifacts/image-animation-cli/chapter-C02 \
  --until canvas
```

The handoff retains the film subject and the chapter's objective, owned scope, exclusions, entry context, and exit handoff. Optional historical dates remain optional for conceptual subjects. A chapter still runs its source-grounded teaching, narration, audio, visual, and rendering phases.

For a new multi-chapter film, use `--mode long`. The parent plans 1–12 coherent chapters according to the subject; `--mode single` requests exactly one. Saved accepted narration contributes to the next chapter's continuity even when an earlier chapter is waiting for image assets.

## Fill assets and revise measured grounding

See [the asset guide](image-animation-assets.md) for import JSON, alpha validation, reference binding, and measured anchors.

Each canvas writes an original-image package beneath:

```text
chapters/<chapter>/canvases/<canvas>/canvas-assets/
```

After inspecting that image, the workflow writes the background/part package beneath:

```text
chapters/<chapter>/canvases/<canvas>/layers-<groundhash>/
```

Read `state.json` for the active package path. Copy its `asset-import-template.json` to `asset-import.json` alongside it, supply the requested relative PNG paths and landmarks, and resume. A partial import is valid. The layer package includes an exact hashed copy of the approved original canvas as its reference, so it does not regenerate that canvas.

All moving layers currently use full source-canvas RGBA at their original position and initial pose. The normal canvas is 1536 × 864. Complete parts, genuine transparency, exact dimensions, and source-canvas anchor measurements are required; centered cropped sprites need a separate explicit placement contract. Imported measured landmarks are applied to the final scene.

Unclear geometry or an incomplete silhouette pauses before layer production. For a scoped correction, save a complete corrected `grounding-import.json` in that canvas's directory, then resume. Keep the controller and component identities consistent; update the measured boxes, anchors, prompts, parameters, and uncertainty flags as needed. The corrected grounding receives a `grounding-manual-<hash>.json` artifact and its own checkpoint, followed by a new `layers-<groundhash>/` package. Earlier proposals and PNGs remain available for comparison.

A technically valid PNG that fails layer review cannot be overwritten inside its ready immutable request. Correct its grounding/edit request through `grounding-import.json` to create a revised layer package, then generate or fill that package. Pure landmark corrections supplied through an asset import are retained; controller-parameter changes belong in the grounding revision.

To repair a static or original canvas, save `canvas-prompt-import.json` in its canvas directory with exactly `schema_version: "infofilm.image-animation-canvas-repair.v1"`, `canvas_id`, `prompt`, and `reason`. Resume creates a separate `canvas-assets-manual-<hash>/` package. The prompt may change; the teaching message, beat ownership, controller and component roles stay frozen. Prior images remain intact and the replacement still passes actual image review.

Explicit grounding revisions can register a native isolated sprite with `normalised_size_px` and `normalised_centre_px` in its scene asset. These are renderer transforms, not claims that the provider placed the pixels precisely. Preserve the original PNG and record the source alpha bounds, target registration and reason in provenance. This does not resolve an unclear silhouette: deliberately simplifying a marker or obtaining a complete part remains necessary.

Layer reviews use one labeled contact sheet showing the source, background, isolated alpha support over checkerboard, and the assembled initial state. Hidden RGB beneath transparent pixels is never shown as an opaque scene. The accepted learning message, limitations and explicit registration reach the reviewer.

## Controllers and current limits

The code compiles an allowlisted controller and measured parameters; model responses cannot supply executable Manim/Python or invent runtime expressions.

| Controller | Supported behavior and limits |
| --- | --- |
| `static_canvas` | Static illustration with code label cards. |
| `orbit_path` | Measured circular/elliptical orbit; optional radius and relation guides. |
| `shared_projectile_drop` | Horizontal projectile and dropped object released at equal height under uniform gravity, without air resistance. Display timing does not assert physical metre/second units. |
| `ideal_slider_crank_four_stroke` | Ideal in-line slider crank, piston/valves/crankshaft, optional complete connecting-rod raster. No thermodynamics, fluid simulation, or valve advance/overlap. |

An unsupported behavior produces `waiting_controller` and a `controller-request.json`. Add and verify a scoped code controller before resuming. Required engine anchors include the piston wrist pin, crankshaft rotation anchor, and connecting-rod endpoints when that raster is supplied.

MFA word IDs determine cue times. Controllers compute positions and states from progress/angle; the planner chooses actions and explanatory labels. Holds retain the last state, loop/replay semantics are explicit, and geometry/timeline checks run before delivery. Unknown geometry and incomplete-preview flags cannot silently become a final film.

The compiler allocates cumulative chapter frames to canvases so separate clip rounding cannot add or lose frames. It retains the original audio/MFA timestamps and records any subframe presentation adjustment in `frame_allocation`. The final chapter join verifies the complete narration frame count. Subtitles come directly from the verified word map and are included as an MP4 subtitle track, with an SRT sidecar.

A rendered-frame defect pauses with its verdict. Correct a canvas's grounded layers through `grounding-import.json`, or supply a corrected `motion-import-<beat_id>.json` cue packet in that canvas directory. Motion imports still use the accepted beat ID and actual MFA word IDs and pass the same controller/timing checks. Repairs keep the original proposal and create a scoped revision checkpoint.

## Output, orientation, and music

Default delivery is landscape 1280 × 720 at 30 fps. `--orientation portrait` swaps the declared landscape width/height; `--orientation both` produces both layouts from the same source canvas and timing. `--width`, `--height`, and `--fps` set output quality. `--canvas-width` and `--canvas-height` set source-image dimensions and must be multiples of 32; video dimensions must be even. The renderer fits the complete source diagram and keeps code label cards in a separate area.

`--manim-python PATH` selects the CPU Cairo/Manim environment, default `.venv-manim-pilot/bin/python`. Geometry, alpha placement, video frame count, narration clocks, and final delivery checks produce artifacts alongside each canvas render. With semantic review enabled, the `review` route inspects a labeled contact sheet with initial/final frames, interior motion samples and visible narration-timed label/highlight cues, accompanied by expected controller states. Sampling is bounded to seven frames and includes intermediate states of closed turns. Static scenes with timed cards sample their visible intervals; scenes without such cues use three frames. Scene-wide titles and explanatory cards occupy the separate label region; they do not need to attach to an individual object.

`--music none` is the default. `--music acestep` uses the existing ACE-Step generation/mix adapter for an instrumental educational underscore. The selected music setting is frozen with the run. Chapter delivery and final stitching reuse rendered video streams and attach the chapter audio/mix.

Useful root artifacts are `state.json`, `inputs.json`, `engine-config.json`, `provider-registry.json`, `model-routes.json`, and `continuity-digest.json`. Chapter/canvas directories retain accepted phase JSON, source/audio/image hashes, planning evaluations, motion timing, render results, and deliveries. Completed films are listed in the root `delivery.json` and written beneath `delivery/<orientation>/film.mp4`.

Successful completion and intentional phase stops return exit code 0. Waiting for assets, geometry, a controller, or review returns 2; a top-level failure returns 1. Read `state.json` and its details for the concrete resume step.

Validation covers mocked model/image providers, short real CPU compositor smoke runs, and the completed live `artifacts/experiments/engine-cli` pilot on Earth rotation and revolution. That pilot used real research, local and Luna planning, VibeVoice speech, Whisper/MFA, Qwen canvas/background/native-alpha generation, CPU Manim rendering and final audio/subtitle delivery. Its four canvases cover the full accepted 84.14-second narration; the advisory runtime did not force padding.

The live pilot needed explicit grounding/registration repairs for two moving scenes and a replacement prompt to correct the seasonal image's globe count. Its rotation view is a schematic marker moving on a static globe, not a 3D globe simulation. The run demonstrates the full path and resumable repairs; it does not establish autonomous success across arbitrary scientific subjects. The saved run uses landscape delivery and no background music.

The repeatable CPU smoke is `python scripts/smoke-image-animation-cli-cpu.py`. Its [report](../artifacts/experiments/image-animation-cli-cpu-smoke/smoke-report.json) records a two-second orbit fixture through the real CLI, native RGBA imports, audio integrity checks, Manim rendering, subtitle/audio mux and cached resume. The LLM decisions and source pixels/audio are explicitly declared test fixtures; network and GPU calls are blocked throughout that smoke.
