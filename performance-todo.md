# Performance work after pipeline hardening

Status: deferred until the pipeline is sufficiently hardened. This records future work; the running production pipeline remains sequential across chapters.

The audio-first production target now provides a manual re-entry boundary: all chapter narration/MFA → continuity → scores and joined listening delivery; **Continue to video** queues scene planning using those frozen checkpoints. It retains narration and native music caches. This does not enable overlapping worker groups or automatic scheduling.

## Basic scheduling rule

Use three worker groups: **LLM**, **voice**, and **image**.

- The LLM group completes the currently ready planning or review work for a chapter, then moves on. It must not wait there for audio or images to be generated.
- At the end of every chapter iteration, scan again from the first chapter for pending LLM work whose inputs are ready. Return to an earlier chapter if its generated assets or MFA results have become available; otherwise continue to the next chapter with ready work. Pending work with unavailable inputs does not block later chapters.
- The voice group produces a chapter's requested audio set in a generation pass. The image group produces a chapter's requested image set in a generation pass. Native provider batch limits still apply within each pass.
- After a generation pass finishes, the LLM group handles the completed set in one chapter review pass: assess assets, compare available candidates, decide acceptance and prepare prompt/text corrections for the next generation pass.
- Generate first, review the set together, then regenerate only the items needing repair. Preserve passing assets. Avoid alternating generation and LLM review after every individual asset.
- A review pass is a scheduling unit, not necessarily one giant LLM request. Use bounded requests where necessary to preserve context limits and individual asset scrutiny, while completing the set's review and repair decisions in that chapter visit.
- For audio, run Whisper checks across the set; only Whisper failures need LLM adjudication. Whisper passes retain the direct route to MFA without extra LLM latency. Approved audio and completed MFA unlock the chapter's next planning pass.
- Track each generation set explicitly, including requested items, completed outputs and terminal provider failures, so a missing item cannot be mistaken for a finished set or make a review wait forever.

Example: C01 script planning hands its audio set to the voice group. The LLM group plans C02 while that set is produced. At the next chapter boundary it scans from C01; if the audio set is ready, it reviews its failed audits and prepares any repairs. After C01 audio/MFA is approved, a later scan returns to C01 for visual planning, then hands its image set to the image group. Subsequent scans return for image review and repair decisions as sets become ready.

## Overlap work across long-film chapters

- While one chapter uses the shared GPU for narration or image generation, let available LLM capacity research and plan another chapter.
- Keep GPU work serialized through the existing lease, including model loading, unloading and provider escalation. Overlapping chapter planning must not load a competing GPU model.
- Queue completed-set audio and image reviews alongside chapter planning. The scan from the first chapter gives earlier ready reviews access at chapter boundaries; bound pending generation sets so planning does not create an excessive backlog.
- Preserve the two-model planner/reviewer roles and factual checks. Concurrency should change scheduling, not acceptance standards.

## Leave and re-enter the chapter pipeline

- Split chapter execution into independently resumable stages with explicit prerequisites and saved outputs, rather than requiring a worker to occupy the entire pipeline until its chapter finishes.
- Research, evidence, creative direction and narration-script planning can run ahead of media production once the parent plan and chapter scope are approved.
- Narration generation, failed-audit adjudication, silence cleanup and MFA must finish before planning visual beats, image counts, scene timing and image prompts. These depend on the approved audio's actual duration and aligned speech.
- Re-enter planning after audio/MFA, then queue images, asset reviews and repairs, music, rendering and delivery checks as their dependencies become ready. Respect each provider's resource requirements.
- Keep approved narration, MFA, plans and passing assets across waits or retries. Track input hashes and invalidate dependent outputs when an upstream artifact changes.
- Persist submitted provider jobs and stage ownership. Restarting or re-entering must not duplicate synthesis, image generation or LLM requests whose completed outputs are already cached.
- A failed chapter should release resources and allow ready work in other chapters to continue. Final assembly still requires checked chapter deliveries in the approved narrative order.

## Scheduling and visibility

- Let the LLM group advance through chapters while earlier chapters await generation, subject to bounded queue capacity. Revisit earlier ready work at every chapter boundary rather than imposing a fixed one-chapter look-ahead.
- Track chapter, stage and states such as ready, queued, running, waiting for GPU, waiting for LLM, failed and complete. Show meaningful progress and queue waits in logs and the web app.
- Keep cancellation, process restart and resume reliable across all in-flight chapters. Prevent two workers from owning the same stage.
- Retain chapter scope, coverage ownership and transition checks so planning ahead does not introduce duplicated content or inconsistent handoffs.

## Evaluate before adoption

- Compare the sequential pipeline with overlapped execution on the same approved parent plan and settings, distinguishing fresh production from cache reuse.
- Measure total wall time, GPU idle time and model switches, LLM utilization, review queue latency, CPU/MFA contention, retry counts and peak resource use.
- Exercise chapter failures, malformed model output, provider timeouts, cancellation and restart. Verify that approved artifacts survive and completed external jobs are not submitted twice.
- Confirm narrative quality, asset acceptance, caption timing and final assembly remain sound before enabling overlap by default.
