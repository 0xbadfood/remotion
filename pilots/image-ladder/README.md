# Matched image-recovery experiments

The frozen corpus in `cases.json` contains 20 failed starting prompts: seven unresolved assets, nine recoveries with at least three reviewed takes, and four two-take recoveries. Cases cover the Bengal famine, Plassey and Mughal chapters C01–C03. Active C04 was excluded. `cases.md` indexes the cases; the JSON includes the original submitted prompt, initial scene wording, source facts, narrated scene contexts, original reviews, historical candidate locations and provenance hashes. Historical repairs are evidence for case selection, not inputs to the new repair loops.

This is a failure-enriched historical test set. Its success rate does not estimate acceptance across all subjects or ordinary first-pass images.

## Two scripts

From `/home/nitin/remotion`, prepare both experiment plans without provider calls:

```bash
python scripts/experiment-images-current.py
python scripts/experiment-images-ladder.py
```

The completed 3 October 2026 experiment ran **only the ladder**, in tmux session `remotion-image-ladder`, after the user-requested stop of the production parent/C04 workers. Their generated artifacts and checkpoints remain available for resumption. The launcher is `scripts/run-image-ladder-experiment.sh`; live output goes to `artifacts/image-ladder-experiment/ladder.log`.

To inspect the retained session/log:

```bash
tmux attach -t remotion-image-ladder
tail -f artifacts/image-ladder-experiment/ladder.log
```

The historical reference is generated without API calls:

```bash
python scripts/experiment-images-historical.py
```

For these frozen cases it records 13 historical recoveries, seven unresolved outcomes and 68 reviewed takes with 5,301.846 seconds of recorded generation time. This excludes review/repair/lifecycle time and is a ballpark reference using earlier review rules, not a matched performance experiment.

For a future full matched comparison, run both arms sequentially:

```bash
python -u scripts/experiment-images-current.py --execute
python -u scripts/experiment-images-ladder.py --execute
python -m infofilm.image_experiment --compare
```

Both scripts use the shared GPU lease and installed idle-service checks. They write only to `artifacts/image-ladder-experiment`; production projects and accepted assets are never edited. Omit `--execute` to prepare only. Repeat a command to reuse completed cases and resume a recoverable interrupted case. Backend errors are recorded separately and the runner proceeds to the next case. Unknown provider submissions are reconciled or stopped, never blindly submitted again.

For a smaller first run, give both scripts identical arguments such as `--cases T01 T02 --output artifacts/image-ladder-smoke`. Subsets and changed settings require a separate output directory. Frozen plans include source code hashes, corpus hash, selected cases, registry, endpoints and settings; mismatched arms cannot be compared. Recorded model IDs also expose endpoint/model changes between the arms.

## Compared behavior

The **current** arm calls the existing production image-generation, review, prompt-repair and provider-routing implementation. It uses full-resolution images throughout.

The **ladder** arm uses:

1. Qwen at 1024×576, eight steps, with semantic preview review. Passing skips to full resolution. Failure gets a replacement prompt and proceeds to normal-step preview.
2. Qwen at 1024×576, normal 25 steps. Review/repair continues within the tier budget. Tiny unresolved details are nonblocking at preview resolution.
3. Full resolution at 1536×864, normal steps, followed by full-quality review. Semantic/composition failures return to normal-step preview; detail-only failures repair directly at full resolution.
4. Flux.2 Dev as the stronger tier when code-owned rules identify provider weakness, or when meaningful normal-step preview repairs exhaust the experimental tier budget. Flux gets its own low-step preview (six steps), normal-step preview (20 steps), full-resolution generation and final review.

Every rejected take that has a recovery path gets a prompt replacement. Accepted elements and source facts remain binding. Low-step draft failures do not qualify as provider weakness. Blank/corrupt outputs and unknown submissions stop for backend diagnosis.

Both arms retain the configured maximum of five generated candidates per tier and ten overall. Ladder previews count as candidates and reserve one slot for final rendering; they cannot extend the generation budget. Each stronger tier starts its counter at zero.

The ladder's exhaustion fallback is an explicit **experimental policy difference**, enabled by `exhaustion_fallback` in `settings.json`. The current arm's production routing is unchanged. Disable it in a new experiment directory to isolate the resolution/step ladder from the extra fallback policy. A change in outcome with fallback enabled measures the combined strategy, not resolution savings alone.

Initial Qwen seeds match the baseline's production calculation for the same corpus asset. All normal inference steps, source facts, scene/composition contracts, review/repair models and provider registry match. Resolution and stage sequencing differ intentionally. The same seed across resolutions does not guarantee the same content.

## Results

Each case records stage/provider paths, actual prompts, settings, candidates, reviews, prompt corrections, escalation triggers, generation/review/repair durations, total pipeline wall time, model identities and provider-reported LLM usage. Pipeline wall time includes GPU-lock wait and model lifecycle. Generation time measures image submission/job wait or the synchronous ImageStudio request; it excludes unrelated LLM work. These isolated asset runs include more model reload overhead than a whole-film image batch, so use the generation and review breakdown as well as total time.

Every selected full-resolution result also gets a separate review with repair history and provider identity withheld. This audit uses the same local vision model, so it reduces history anchoring but is not an independent human ground truth. Its time is reported separately and an audit failure counts as unresolved in the verified outcome.

`summary.json` and `comparison.json` report:

- Solved through the Qwen ladder, with draft/normal-preview approval stage and complete repair path.
- Solved through Flux, including qualified-provider versus exhausted-budget escalation.
- Still unresolved, with backend and pending-audit cases separately identifiable.
- Acceptance before and after the separate audit, stage counts, escalation frequency, timing and LLM usage.

`comparison.html` displays the paired final images for human review. Comparison summaries use only matched completed case pairs and state when the experiment is incomplete. The ladder-only pilot is now complete: 14/20 passed its separate final audit, with six unresolved. See `artifacts/image-ladder-experiment/final-report.md` and `final-gallery.html` for findings and candidates. This is not a fresh matched comparison; no current-arm execution was requested.

To rebuild the corpus deliberately:

```bash
python -m infofilm.image_experiment --collect
```

Recollection changes the frozen inputs and requires a fresh experiment output.
