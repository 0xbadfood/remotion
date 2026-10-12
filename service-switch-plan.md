# GPU service relocation plan

Status: measured and proposed; production routing has not changed.

## Intended layout

| Role | Endpoint | GPU | Plan |
|---|---|---|---|
| Planner | `127.0.0.1:7001/v1` | Local RTX 3090, 24 GB | Local `qwen3.8-27b`, 64K text planning; pilot still required |
| Primary reviewer | `10.8.0.4:8001/v1` | Spark LLM host | Flash reviewer, 64K advertised context |
| Optional fast reviewer | `127.0.0.1:7001/v1` | Local RTX 3090, 24 GB | Local `qwen3.8-27b`, validated workloads only |
| Standard images | `10.8.0.5:8075` | Remote Radeon R9700, 32 GB | Qwen Image |
| Narration | `10.8.0.5:8091` | Remote Radeon R9700, 32 GB | VibeVoice, configurable batches of eight |
| Premium images | `127.0.0.1:8000` | Local RTX 3090 | Keep Flux.2 Dev |
| Background score | Local subprocess | Local RTX 3090 | Keep ACE-Step |

Qwen Image and VibeVoice must alternate on the remote GPU. The local planner or optional reviewer must release its GPU before Flux, ACE-Step, or local OmniVoice work. Using Spark for primary review leaves it available during premium generation. Audio is a small part of the total job; prioritize image generation and review performance. The proposed local-planner/Spark-reviewer split preserves cross-model review; Spark can remain an explicit planner fallback. The 465 saved production planner calls audited had no attached images, so vision can be disabled on the local planner to reclaim memory. Its quality on large planning tasks still needs a pilot.

## Follow-up vision and context tests

Vision weights and the projector are present. A temporary two-image server configuration passed all three attachment-order/content controls. On an isolated GPU, two historical person-card reviews completed in 3.94 and 3.52 seconds; both correctly agreed with prior rejection of copied reference-card labels.

The first full anatomy review timed out after 120 seconds both with and without native JSON enforcement. Parking Voice and Image did not fix it. A further isolated attempt with a 3 GiB KV cache and 32K context also timed out with about 3 GiB free VRAM. CUDA allocator OOM warnings occur during weight initialization, but there is no established causal explanation for the later review stall. The MTP experiment was stopped before its server became ready; it produced no review-quality result. These observations do not justify treating local two-image anatomy review as production-ready.

An audit of 1,183 saved production reviewer calls found 24 whose prompt plus configured output allowance exceeded 32K. Visual-plan review/comparison reached 53,810 tokens; research digests reached 43,895. Historical requests include older prompt policies and do not guarantee bounds for future subjects. Therefore 32K is an experimental memory setting, not a universal production limit. Spark currently advertises a 65,536-token context in `/v1/models`; keep code-based input/output budgets and explicit overflow handling even there.

The revised proposal uses Spark as the primary reviewer and keeps local 27B optional for workloads that have been validated. Prior anatomy comparisons showed matching overall verdicts, but both reviewers missed some visual defects; model agreement is not expert validation. Production routing has not changed.

Planner context audit: 667 saved planner calls, including 465 production calls, were measured separately. The largest production visual-beat repair had 46,367 input tokens and a 7,000-token output allowance: 53,367 tokens reserved. Its actual completed total was 51,549 tokens. Other observed prompt-plus-output maxima were 33,044 for image-prompt planning, 27,928 for evidence planning, 22,598 for external chapter planning, and 13,398 for scripts. The separately logged bounded research harness had 134 measured attempts, peaking at 11,353 input plus a 4,000-token output allowance. A 64K route covers these observed requests; future subjects still require budget checks. Full audit: `artifacts/experiments/service-switch/planner-context-audit.json`.

Evidence: `artifacts/experiments/service-switch/vision-two-images-clean/summary.json`, `vision-anatomy-headroom/summary.json`, and `reviewer-context-audit.json`. Reusable test runner: `scripts/test-local-qwen-vision.py`.

Spark normal image-review benchmark: nine unmodified saved production reviews across three subjects, excluding one warm-up, averaged 12.022 seconds (median 12.647, range 7.910–14.050). Six single-image scenes averaged 11.840 seconds; three person-card comparisons averaged 12.387 seconds. All returned valid JSON; seven verdicts agreed with prior reviews. One disagreement identified an explicitly requested but absent rose. The other incorrectly passed a candidate with visible generated labels copied from a reference card. Review latency is practical, but this sample does not establish error-free review quality; candidate-only text-overlay checks need attention before adopting the routing. Results: `artifacts/experiments/service-switch/spark-normal-image-reviews/summary.json`; runner: `scripts/benchmark-spark-image-reviews.py`.

Historical comparison with the existing reviewer at `10.8.0.5:8001`: 811 saved production image-bearing review responses averaged 13.071 seconds, with a 10.573-second median and a 17.255-second 90th percentile. Of these, 808 took 6.713–26.467 seconds; three longer calls took 60.216, 338.971, and 798.631 seconds. Their recorded wall time does not distinguish queueing from inference. Excluding those three explicitly, the mean is 11.637 seconds. For the exact nine cases replayed on Spark, the historical reviewer averaged 11.584 seconds versus Spark's 12.022 seconds. Thus ordinary review latency is comparable; these measurements do not support a speedup over the old remote reviewer. The intended gain is reallocating GPU capacity and retaining a reviewer able to handle the larger multimodal context. Audit: `artifacts/experiments/service-switch/historical-image-review-latency.json`.

Spark combined context/vision stress test passed: 59,989 tokenizer-counted text tokens became 62,084 API input tokens including two attached images and chat overhead, plus 103 output tokens. It correctly retrieved distinct values near the beginning, middle, and end, and described both images in order. The recorded API call took 183.621 seconds; this timing is separate from normal image review. The 64K window includes text, image tokens, and output together. This validates capacity/attachment reading, not broad review accuracy. Results: `artifacts/experiments/service-switch/spark-context-vision/summary.json`; runner: `scripts/test-spark-context-vision.py`.

## Measurements

Recorded on October 6, 2026 (India time). Detailed requests, responses, audio, and results are in `artifacts/experiments/service-switch/`.

| Test | Existing local/old endpoint | Proposed endpoint | Observation |
|---|---:|---:|---|
| Qwen Image, 1536×864, 25 steps | 76.04 s | 29.95 s | Remote image generation is 2.54× faster; two measured samples per host after one warmup |
| Saved single-image review | 20.97 s, old remote GGUF | 5.27 s, local AutoRound | Same prompt and image; output lengths differ |
| Saved evidence review | 27.05 s, old remote GGUF | 10.21 s, local AutoRound | Same prompt; both returned JSON |
| VibeVoice warm two-item batch | 29.56 s | 32.90 s | Two batches per host; all four clips per host passed CPU Whisper |
| Initial VibeVoice load | 12.58 s | 68.73 s | Keep the model loaded throughout an audio pass |
| Remote eight-item voice batch | Not compared against four | 116.63 s | No memory failure; seven Whisper passes, one severe mismatch |

The eight-item batch establishes feasibility, not a throughput advantage over four. Its failed clip was 41.6 seconds long for a paragraph normally taking about twelve seconds; Whisper heard only “You You.” Keep independent point audits and retries. The benchmark did not run MFA or retry that clip. A download connection error was recovered by retrieving saved outputs, without regenerating the batch.

These are deployment comparisons, not isolated GPU benchmarks. Image implementations have identical source hashes, but memory/offload settings differ. The old and new reviewers use different runtimes and quantization. Two saved requests do not establish general review accuracy.

## Required work before activation

1. **Support multiple images in reference reviews on the local LLM.** Vision is already enabled (`VISION=1`), and both the base and fast checkpoints retain all 333 vision/merger tensors, verified in their physical safetensors headers. The merger is the integrated vision projector; a separate GGUF `mmproj` file is not required by this vLLM deployment. Vision-tower CPU offload is enabled by default for the 24 GB memory budget. Its current server permits one image per request; a candidate-plus-reference request returned HTTP 400. Increase the multimodal image limit to cover real pipeline requests, then profile memory and validate person cards and educational references. Preserve the reference image rather than dropping it to satisfy the limit.
2. **Enable batches of eight in the pipeline.** The remote API accepts up to ten items, but our pipeline caps batches at five and uses `chunk_index = batch_index * 5 + item_index`. Make the limit configurable and use collision-free indices. Preserve saved immutable batches and point output names; retry failed points only. CPU Whisper/MFA should continue while the next batch renders.
3. **Separate routing from GPU ownership.** Add explicit GPU groups and service adapters/settings. Change both `config.json` and the image provider registry when activating; use a new remote image provider identity so old job IDs cannot be queried on the wrong host.
4. **Implement remote GPU handover.** Drain active work, unload the sibling service, and verify actual remote VRAM release. Local locks and API health alone cannot coordinate other remote clients. Establish a host-side lease or equivalent shared ownership mechanism.
5. **Handle the local reviewer around premium and music work.** Current premium parking assumes CUDA is empty and will see the reviewer as a blocker. Drain LLM requests, park only the owned service, render the asset, and restore it before scoring. Measure startup overhead; preserve active external jobs.
6. **Keep each service loaded for its pass.** Avoid unloading VibeVoice between batches or reloading Qwen for every image. GPU work on one machine must not unload unrelated services on the other.
7. **Run a complete pilot.** Exercise narration, point retries, MFA, reference-conditioned Qwen images, a premium replacement, candidate scoring, BGM, and final composition. Compare decisions as well as elapsed time.
8. **Activate for new runs after the pilot.** Archive configuration and registry first. Existing job snapshots keep their original routes; interrupted jobs need explicit migration of endpoints and provider records, not silent rebinding. Keep rollback settings.

## Lifecycle findings

The old reviewer at `10.8.0.5:8001` was not listening before testing. The remote voice API is on **8091**, not the local service's 8092. The local reviewer is on **7001**. Its API authentication matches the webapp's existing key configuration; no credentials are included in this plan.

The local reviewer and remote voice service were started temporarily for these tests. Restore their original inactive states, and reload both initially loaded image models after testing. No webapp job needs a restart for this plan.

Machine-readable plan: `artifacts/experiments/service-switch/switch-plan.json`.
