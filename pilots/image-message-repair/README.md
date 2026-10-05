# Message-preserving image repair experiment

This is a new, isolated experiment on the same 20 difficult starting cases as the completed ladder pilot. The first run completed on 3 October 2026: 19 automated acceptances, one repair-blocked case, 28 candidates and about 46 minutes. See `artifacts/image-message-repair-experiment/final-report.md` and `assessment-gallery.html` for findings and visual observations. The baseline remains prepared, not executed. Production behavior and earlier results are preserved.

`messages.json` contains manually written annotations, grounded in each frozen narration, purpose and supplied source facts. Each entry has:

- `visual_message`: the informative meaning that must survive a change in depiction.
- `setting`: the period/place needed to ground the generated scene.
- `essential_constraints`: factual distinctions for the repair planner, including which details are optional or metaphorical.
- `initial_scene`: a manually cleaned version of the original initial scene, retaining its underlying visual idea and complexity while removing title/banner and negative-prompt wording. Ambiguities and difficult visual ideas remain available to test recovery.

`cases.json` is the extended frozen corpus. It preserves original prompts, history, source facts and provenance; adds the same `visual_message` to each case, asset and associated scene; and records parent/annotation hashes. `cases.md` makes the 20 messages easy to review. The original ladder corpus is not edited.

## Workflow

1. Generate at full resolution: Qwen Image 2.1, 1536×864, 25 steps.
2. Review the current submitted prompt, immutable message and actual image. The reviewer gets no provider identity, previous images, prior reviews, old asset acceptance checklist or failure history.
3. Acceptance ends the loop. There is no preview, provider escalation or automatic second acceptance audit.
4. A failure recommends `targeted` or `simplify`, with concrete visible evidence and correction guidance. A useful depiction with a small identifiable defect can get targeted repair; an overloaded, unclear or incidental depiction can be simplified immediately.
5. The planner receives the current prompt/scene, latest review, message, factual constraints and relevant source facts. It can choose a simpler alternative even after a targeted recommendation. When simplification is requested, it must identify optional complexity removed or replaced and explain preservation of the message. Historical repairs are not planner inputs.
6. The default limit is an initial image plus two repaired images. Unchanged scenes, including punctuation/case-only changes, are rejected before another image call. JSON repair attempts are separately bounded. Technical corruption stops for diagnosis.

The provider prompt contains concrete setting, visual treatment, composition and scene. It omits literal film titles, banner/typography instructions, abstract message wording and lists of unwanted objects. The message is carried in the case, scene, asset, request-contract sidecar, reviewer/planner input and output, checkpoint, selected manifest entry, logs and result. The native provider API receives only its supported fields; it has no `visual_message` form field.

The image supports the narration. It does not need to independently prove causation or encode every date/number. Simplification must still communicate the useful idea without contradicting essential facts; it must not collapse every difficult subject into an unrelated generic symbol.

## Prepare and run

Preparation makes no provider or LLM calls:

```bash
python scripts/experiment-images-message-repair.py
```

The complete run, when deliberately started:

```bash
python -u scripts/experiment-images-message-repair.py --execute
```

For a subset, use a separate directory so its frozen selection cannot be confused with the complete run:

```bash
python -u scripts/experiment-images-message-repair.py --cases T04 T08 T09 T17 T19 --output artifacts/image-message-repair-subset --execute
```

Settings are in `settings.json`. Only one provider is used for the entire experiment; changing provider, settings, annotations or code after execution requires a new output directory. Original initial seeds are retained despite addition of message metadata. Shared GPU leases and idle-service checks come from the existing benchmark transport. Unknown submissions cannot be blindly repeated.

Results go to `artifacts/image-message-repair-experiment/`: a frozen plan/corpus, per-case request contracts, provider submissions/candidates, current-image reviews, planner replacements, checkpoints, request/usage records, metrics, `summary.json` and `gallery.html`. Repeating the execution command reuses accepted/exhausted cases and resumes recoverable interrupted work. Planner interruption after rejection resumes the repair without repeating the rejected image. Failed/blocked cases remain in the denominator and do not stop the next case.

## Baseline and interpretation

The existing baseline script remains available and accepts the extended corpus:

```bash
python scripts/experiment-images-current.py --corpus pilots/image-message-repair/cases.json --output artifacts/image-message-repair-baseline
```

That command only prepares the baseline. Adding `--execute` would run the existing production loop, including its original wrapper, history-aware review, repair/escalation budgets and separate final audit. It would be a comparison of whole strategies, not an isolated simplification effect. Do not silently treat its audit outcome or candidate budget as identical to the new pass-ends-loop protocol.

The completed ladder pilot and historical metadata are unmatched references: starting wrapper, scene wording, review contract, routing and budgets differ. New loop acceptance is not equivalent to the ladder's separate-audit acceptance. Compare accepted images by human inspection for message support and factual correctness, along with actual candidate count, generation/review/repair time, simplification frequency and unresolved cases. Do not claim speed or quality improvement from preparation or automated acceptance alone.
