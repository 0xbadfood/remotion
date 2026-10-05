# Digestive anatomy reference experiment

Standalone experiment, separate from production image planning and reference sourcing.
Six manual prompts each generate one image on Qwen Image 2.1 (25 steps) and the
configured premium Flux.2 Dev tier (20 steps), at 1536 × 864. Prompts and seeds match
across providers; native step settings differ deliberately. There is no image
repair loop or automatic provider selection.

```bash
python scripts/experiment-anatomy-images.py --prepare-only
python -u scripts/experiment-anatomy-images.py
```

Results: `artifacts/experiments/anatomy-digestive-v1/report.html`, `summary.json`,
and per-provider/per-case requests, candidates, provider checkpoints and reviews.
Progress logs explicitly count completed results out of 12. The report refreshes
after every case. Rerunning reuses completed images and verifies their checksums;
interrupted submissions reconcile through the existing adapters. Failed cases do
not stop the remaining cases. Shared GPU leasing and idle-provider parking apply.

To run a subset:

```bash
python -u scripts/experiment-anatomy-images.py --providers qwen_local --cases D01 D04
```

To test whether conditioning improves results, make a separate matched control:

```bash
python -u scripts/experiment-anatomy-images.py --conditioning none \
  --output artifacts/experiments/anatomy-digestive-unconditioned-v1
```

The control retains the same scene wording and seeds, omits the reference upload
and reference-specific instruction, and still supplies the reference to the
reviewer. A single conditioned run alone does not establish causal improvement.

Source: [NIH/NIDDK digestive-system diagram](https://www.niddk.nih.gov/news/media-library/18162).
The experiment archives the original image, source page, attribution policy,
and normalized full-resolution reference. Credit: National Institute of Diabetes
and Digestive and Kidney Diseases, National Institutes of Health.
Basic physiology checklist context is available at [NIDDK: Your Digestive System & How It Works](https://www.niddk.nih.gov/health-information/digestive-diseases/digestive-system-how-it-works).

The reviewer receives the generated image and the source separately, with
provider identity omitted. It reports visible checks, scores, blocking issues,
limitations and which details are absent from the source. **Automated screening
is not medical certification.** The overview diagram supplies organ arrangement;
duct details, wall cutaways, villi and muscular motion are deliberately tested as
inferred detail. A good-looking inferred cross-section is not source validation.
The HTML report supports side-by-side human assessment of these limits.

## Strict review and scene-specific reference tests

```bash
# Re-screen existing images without generating anything or switching GPU services.
python -u scripts/review-anatomy-images.py

# New generation set, using the archived per-scene reference manifests.
python -u scripts/experiment-anatomy-images.py \
  --case-file experiments/anatomy-images/cases-v2.json \
  --output artifacts/experiments/anatomy-digestive-v2 \
  --review-policy strict --publish
```

The stricter policy checks mandatory visible features and derives its verdict in
code: any contradiction fails; any essential uncertainty prevents a pass. It
requires concrete image locations, rejects substituted whole-system views and
copied labels, and asks the critic to trace inlet/outlet and duct paths instead
of assuming expected anatomy. The JSON uses an object keyed by gate IDs and has
three bounded structural attempts, preserving raw outputs. Cosmetic differences
are not blocking defects. Production image review is not changed by this pilot.

Calibration results and previous reviews are in
`artifacts/experiments/anatomy-digestive-review-v2/report.html`. The new experiment
uses the original whole-system source for D01/D02, plus independently sourced
textbook diagrams for D03/D04/D05/D06. Each manifest records provenance, scope,
attribution and usage. The retrieved 2026 OpenStax textbook states CC BY-NC-SA;
these references are used for this noncommercial educational experiment and are
not approved as commercial production sources. Intestinal micrographs have an
additional stated University of Michigan credit. No reference pixels are
invented, repainted or silently replaced.

Published reports use the existing login at `/experiments/<directory>/report.html`.
The explicit publication manifest exposes report files and image assets only.
