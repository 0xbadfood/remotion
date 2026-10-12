# Multi-queue pipeline design (draft for review)

Status: design only, 2026-10-11. Nothing is built. It implements item 3 of the agreed plan in
`pipeline-findings-todo.md`, and supersedes the scheduling sketch in `performance-todo.md`.

## Decision 2026-10-12: cloud LLMs, two phases (supersedes local-mode scheduling)

The user moved infofilm to cloud LLMs directly (as Lesson Studio did). With LLM capacity no longer scarce, the one rule
and lane scheduling below become the **local-mode fallback**; the main design is:

- **Phase A, planning (all parallel):** external planner + LLM pair for film research, evidence, chapter plan and shared
  direction, with a **thorough film-level digest** so chapters search only real gaps. Then one worker unit (subprocess)
  per chapter, a controller plus its planner/reviewer pair, for chapter evidence and script.
- **Gate:** script-level continuity across every adjacent pair, plus shared person references for the film. Nothing
  reaches the media layer before the gate.
- **Phase B, media:** each chapter controller keeps its own LLM pair (visual beats, image prompts, image review and
  repair, audit checks). Voice, images, Flux, music and render are provider queues fed by all chapters at once; voice is
  the first thing each chapter queues, since visual beats need timing.
- **Models:** a cheap tier while proving it out (Haiku planner; a cheaper non-Anthropic reviewer, so no self-review).
  Sonnet/Opus only in production, once the pipeline is proven. Chosen in an **LLM settings page** like Lesson Studio's:
  providers/connections can be added; the planner and reviewer endpoints are assigned from them; named configurations.
- **Rate limits are the throttle:** one shared limiter per model with 429 backoff, plus outage retries; this also keeps
  chapters from all hitting the voice queue at once.
- **Provenance is a product feature:** every claim keeps its web source (fact → `source_ids` → URL/passage) in the logs,
  even when a model already knows the event. Later the video ships with a claims-and-sources JSON (scene → point →
  fact → source).

### Implementation status (2026-10-12, built, not yet run end to end)

- **LLM settings page** (web app → "LLM settings"): connections (local OpenAI-compatible, OpenRouter, OpenAI Responses
  e.g. Luna), planner/reviewer roles, named configurations, per-run choice on the create form. `infofilm/llm_settings.py`;
  keys in `artifacts/interface/llm-secrets.json` (mode 600), never in snapshots. A run freezes `<url>#<connection id>`
  per role plus `llm_endpoints`.
- **Rate limits / outages:** `Models.call` holds a cross-process slot per connection (`max_concurrency`; cloud default
  8, local 1) and retries connection errors, 429 (Retry-After) and 5xx for up to 30 min without counting attempts.
  Media providers (`media.request`) wait on 429/503 the same way (factory queue full).
- **Phase A:** film voice calibration, then every chapter to `until='script'` (all at once with cloud LLMs, else one).
  Film research gets a wider budget (`film_research_budget`, default 6 searches / 16 fetches).
- **Gate:** `script_gate` reviews adjacent scripts touching this run's chapters; a failing pair writes
  `continuity-notes.json` for the later chapter and redrafts only its script, once; `continuity-report.json`
  (stage "scripts"). `cast_film` then builds `film-cast.json` (names must appear in scripts/facts) and researches its
  portraits once into the film cache.
- **Phase B:** every chapter to video in parallel; image prompts must use cast names. `shared_gpu_services`
  (config: music, flux) limits the local GPU lease to services really on the 3090; renders limited to 2
  (`render_slots`).
- **Claims and sources:** `claims-sources.json` (point → facts → sources → cited passages) written at delivery and
  linked from the review page.
- Not done: chapter research seeded from the film's fetched pages (beyond the wider film budget); continue a chapter
  without assets that still fail after simplification.

## Goal

Every resource pulls ready work from any chapter, so no resource idles while some chapter has work it could do.
A chapter still passes through its stages in order; the queues only change *when* each step runs, never what it
checks or accepts.

## Scheduling model (user's design, 2026-10-11)

A chapter moves through four blocks, always in this order:

1. **Script block (LLM):** research → evidence → mood/direction → script → continuity with the previous chapter.
2. **Audio block:** narration (Spark VibeVoice) → audit → alignment. Entry: the chapter's script block is done.
3. **Visual planning block (LLM):** visual beats → image prompts (per batch) → person references.
   Entry: the chapter's timing exists.
4. **Image block:** render → review → repair per image, then music and video. Entry: an accepted prompt batch
   (images start per batch, not after all prompts).

**Every block is a sink.** A block receives chapters when their entry condition is met, holds their pending work, and
queues requests for the resource it needs. Visual planning is pure LLM work but is modelled the same way as audio and
images: it queues its beats and prompt tasks as requests to the LLM pair instead of being a special case inside the
LLM loop.

**The LLM pair serves requests.** Sinks that need the planner or reviewer (script, audio-audit adjudication, visual
planning, image review/repair) put requests on the LLM queues. The LLM loop's one rule:

- serve the pending request of the **earliest chapter, from any sink**, first;
- only when no sink is waiting, start or advance the **next chapter's script block**.

So early in a film the pair mostly writes new chapters; once every script block is done it only serves requests from
the audio, visual-planning and image sinks. Planner and reviewer remain separate queues (no self-review).

**Music and video are short, mostly sequential steps** at the end of each chapter's image block. Video renders run two
at a time on this machine; if rendering becomes the bottleneck, add a render server on 10.8.0.5 (CPU-rich; needs Node,
Chrome and the Remotion renderer there; its GPU stays with the fixed LLM).

The lanes, task table and failure rules below describe what each block contains and how it recovers; they are the
reference for implementing this loop, not a separate scheduler.

### Cloud mode (later, when the LLM pair moves to cloud models)

With cloud models the LLM is no longer one planner slot and one reviewer slot. LLM lane capacity becomes a setting:

- **Local mode:** capacity 1 per role; the one rule decides who is served first.
- **Cloud mode:** capacity N per role, limited by rate limits and budget. Each sink makes its own LLM calls
  (audit adjudication, image review, prompt repair, visual planning) in parallel, while a dedicated pair keeps
  advancing the script blocks. The one rule still applies whenever capacity is exhausted (e.g. rate-limited).
- **Audio and images become pure provider queues.** With LLM capacity no longer the bottleneck, every chapter runs its
  script and visual-planning blocks at once, and all chapters queue to the voice provider (Spark VibeVoice batches,
  up to 40 items, mixed across chapters) and the image providers (factory 2 GPUs + 4 waiting, Flux 1 slot)
  simultaneously. Throughput is then set by those providers and the budget. Keep the provider queues independent of
  how the LLM side is scheduled, so this switch is a capacity change rather than a redesign. The user expects this
  soon, given how capable the open models are becoming.
- **Plan checkpoint before media (cloud mode).** With fast cloud LLMs, finish the whole planning layer first: external
  planner, internal film and chapter plans, every chapter script, script-level continuity across all adjacent
  chapters, and the shared person-reference images. Only then release chapters to the media layer (voice, images,
  music, video). Valkyrie showed why: the C02→C03 handoff problem was found only after all six chapters were rendered.
  In local mode the one rule approximates this (scripts run ahead); in cloud mode it becomes a hard gate.
- Planner and reviewer remain different models (no self-review).
- A per-film token/cost budget replaces hardware as the limit, because parallel calls can spend quickly.
- Calls that resend the same chapter context use a stable prompt prefix so parallel requests hit the prompt cache
  (ties into "trim repeated prompt bulk" in the findings).

## Principles

0. **The one rule (non-negotiable).** The LLM pair always serves the pending request of the earliest chapter, from
   any sink, first; only when no sink is waiting does it start or advance the next chapter's script block. Every
   other scheduling detail must preserve this rule; it is what keeps audio and image blocks fed while new chapters
   are still being written.
1. **Work is a task; a task is ready when its inputs exist and are accepted.** Readiness comes from checkpointed
   artifacts, never from timing assumptions.
2. **One queue (lane) per resource**, with that resource's real concurrency.
3. **Every task has an entry rule, a lane, and an exit destination** for each outcome (pass, repairable fail,
   exhausted, transient error).
4. **Planner and reviewer stay separate lanes.** A task's draft and its review never run on the same model.
5. **Transient errors pause a lane, not a chapter.** Connection refused, 5xx, 429 and timeouts retry with backoff and
   do not count as attempts. (Today a 12-second restart failed five chapters.)
6. **Everything is checkpointed and idempotent.** Task results are files keyed by input digest (infofilm already caches
   LLM calls and provider jobs this way); a restart puts running tasks back to ready.
7. **Earlier chapters first.** Within a lane, lower chapter number wins, so the film completes front to back and
   continuity checks become available early.

## Lanes

| Lane | Resource | Concurrency | Notes |
|---|---|---|---|
| `plan` | planner LLM, 10.8.0.5:8001 | 1 | slot shared with the FSSAI portal |
| `review` | reviewer LLM with vision, Spark arbiter 10.8.0.4:8001 | 1 | also runs the research agent and image review |
| `web` | Tavily search + Obscura fetch | rate-limited | search credits; fetch budget per research task |
| `image` | amdserver image factory :8075 | 2 running + up to 4 waiting | back off on 429 |
| `flux` | Flux.2 Dev, local 3090 (ImageStudio :8000) | 1 | premium tier; stays loaded (plan item 4) |
| `voice` | Spark VibeVoice :8092 | 1 batch (up to 40 items) | batches may mix chapters |
| `asr` | Whisper audit of narration clips | 1-2 | local; only failures go to `review` |
| `align` | MFA word alignment, local CPU | cores − 2 | per narration point, in parallel |
| `music` | ACE-Step (local 3090 now, Spark later) | 1 | plan item 5 |
| `render` | Remotion video render, local CPU | 2 | chapter videos and the final join; 10.8.0.5 render server if it becomes the bottleneck |

## Task graph

Notation: **entry** = inputs that must be accepted; **exit** = where each outcome goes.

### Film level (once per film)

| Task | Lane | Entry | Exit |
|---|---|---|---|
| F1 research | `web` + `review` (agent turns) | job submitted | sources + digests → shared digest cache; fails clearly when search is unavailable |
| F2 orientation evidence | `plan` draft → `review` | F1 | accepted → F3; rejected → redraft (≤ N) |
| F3 chapter plan | `plan` draft → `review` → compare | F2 | accepted → F4 and creates one chapter context per chapter |
| F4 shared direction (creative) | `plan` → `review` | F3 | accepted → spawns every chapter graph at once |
| F5 voice calibration | `voice` | job submitted (independent of F1-F4) | words-per-minute → every script task |
| F6 final assembly | `render` | every chapter video accepted (continuity already passed) | delivery |

### Chapter level (every chapter in parallel once F4 is accepted)

| Task | Lane | Entry | Exit |
|---|---|---|---|
| C1 gap research | `web` + `review` | F4 + chapter context; reads the shared digest cache first | new sources → shared cache; skipped when the film evidence already covers the chapter |
| C2 chapter evidence | `plan` draft → `review` | C1 (or F2 if C1 skipped) | accepted → C3 |
| C3 script | `plan` draft → `review` | C2 + F5 | accepted → C4; rejected → redraft |
| C4 continuity with the previous chapter | `review` | scripts of chapter n−1 and n accepted (C1 has none) | pass → C5 for chapter n; fail → repair **chapter n's** script only (n−1 may already be voiced) → C3 |
| C5 narration | `voice` (batched) | C4 passed | clips → C6 |
| C6 narration audit | `asr`, failures → `review` | each clip | pass → C7; fail → re-voice only the failed points (C5) |
| C7 alignment | `align` (per point) | each audited clip | all points aligned → timing → C8 and C11 |
| C8 visual beats (scenes) | `plan` draft → `review` | timing | accepted storyboard → C9 |
| C9 image prompts | `plan` draft → `review`, per batch of 8 | C8 | each accepted batch → C10 for its assets immediately (not after all batches) |
| C9b person references | `web` + `plan`/`review` | C9 batch naming people | reference cards → that asset's C10; unavailable → proceed without |
| C10 image, per asset | `image` render → `review` (vision) | accepted prompt (+ references) | pass → accepted; fail → repair prompt (`plan`) → back to `image`; escalation rule met → `flux` → `review`; attempts exhausted → asset needs attention |
| C11 music | `music` | timing + shared direction (needs no images) | accepted score → C12 |
| C12 chapter render | `render` | all assets accepted + C11 + storyboard | chapter video + QA → F6 |

What moves earlier than today:

- **Continuity (C4)** runs on scripts, before voice and images, instead of after every chapter has been rendered.
- **Music (C11)** starts as soon as timing exists, in parallel with scenes, prompts and images.
- **Images (C10)** start per prompt batch, and repairs loop per asset, so both factory GPUs stay fed.
- **Voice (C5)** for chapter 2 can be batched with chapter 1's re-voices.
- **Voice calibration (F5)** runs while film research is still going.

## Signals

The scheduler keeps one SQLite table, `tasks(id, film, chapter, kind, lane, deps, state, attempts, priority, payload,
result, error, created, updated)`. States: `blocked → ready → running → done | needs_repair | exhausted`.

- A task becomes `ready` when every dependency is `done`. When a task finishes, the scheduler re-checks only its
  dependents (the signal is "artifact accepted").
- A repair loop creates a new attempt of the same task (`attempts + 1`) and marks its downstream tasks `blocked`
  again if they had already used the rejected output (only possible for C4 repairs).
- Lane workers claim the highest-priority `ready` task in a transaction and write the result before marking it done.
- Priority: chapter number first, then repairs before first attempts (a waiting repair holds up a whole chapter),
  then FIFO.

## Failure handling

| Failure | Handling |
|---|---|
| Transient (connection refused, 5xx, 429, timeout) | retry in the same lane with backoff (5 s → 5 min); not an attempt; after ~30 min the lane is marked degraded and shown in the web app |
| Semantic rejection (review fails) | exit destination of that task (redraft, repair, escalate) |
| Attempts exhausted | the task and its downstream tasks become `needs attention`; other chapters continue |
| Missing capability (e.g. reviewer cannot see images) | detected by a readiness check before the film starts, not mid-run |

## Bounds

- **Chapters in flight:** all, by default. Optionally cap image work at two chapters ahead of the oldest unfinished
  chapter to limit factory backlog and disk (images are kept 4 h on the factory and copied locally at once).
- **Factory:** never more than 4 queued jobs per film; 429 → back off and retry.
- **Search credits:** per-film budget, shared by F1 and all C1 tasks.

## Migration path

1. **Coarse tasks around existing functions.** Make each current stage function (chapter research, evidence, script,
   narration+timing, scenes, image prompts, image set, music, video) a task with the entry rules above. The existing
   per-stage checkpoints make this mostly scheduling code. Gives cross-chapter overlap immediately.
2. **Split the image stage per asset** (render → review → repair loop) and submit a chapter's images as a set.
3. **Move continuity to scripts (C4)** and gate narration on it.
4. **Batch voice across chapters** and parallelise alignment.
5. **Lane occupancy in the web app** (queue depth and busy time per lane), reusing the measurement approach from the
   Valkyrie run.

## Open questions for the user

1. Gate narration on continuity with the previous chapter (proposed), or voice early and accept occasional
   re-voicing?
2. Cap on chapters in flight (all / 2-3 ahead)?
3. Allow voice batches that mix chapters?
4. ~~One or two concurrent video renders?~~ Decided: two on this machine; 10.8.0.5 as a render server only if needed.
