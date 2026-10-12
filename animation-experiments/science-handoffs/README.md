# External planner science handoffs

Run the planning-only smoke with the configured local models:

```bash
. /home/nitin/.profile
python scripts/smoke-animation-handoffs.py \
  --cases animation-experiments/science-handoffs/cases.json \
  --output artifacts/experiments/science-animation-handoff-new
```

The three briefs are manually curated from fetched OpenStax and NASA sources, with archived excerpts, source IDs and claim ownership. They are orientation test inputs, not full chapter research. This command never renders speech, SVGs, images or videos. Plans, explicit per-chapter handoffs, review histories and a table report are saved. Use a new output folder for changed inputs or prompts.

The October 6 smoke produced:

| Subject | Chapters | Routes | Model review |
|---|---:|---|---|
| Horizontal launch and gravity | 3 | 3 animation | First attempt accepted, 96.75 |
| Seasons | 3 | 2 animation, 1 illustrated | First attempt accepted, 93.5 |
| Series and parallel circuits | 3 | 3 animation | First attempt accepted, 95.5; inspection found a misleading voltage criterion |

The initial gravity draft had five small chapters. The routing prompt was tuned to keep related demonstrations in a shared workspace and choose a smaller useful chapter set. It leaves detailed teaching, narration, assets, placement and motion to Luna.

Inspection found that the circuit plan's statement about voltage not dropping across parallel branches could imply no voltage across their loads. The saved plan was retained, then revised with source-backed feedback to say that parallel branch loads have the same source voltage across them and ideal connecting wires have negligible voltage change. The revision passed on its first attempt at 95.5. The general planner/reviewer instructions now explicitly assess demonstrations and success criteria as factual instructions, including qualifiers and the location of measured quantities.

The configured `10.8.0.4:8001` endpoint refused connections during this test. The completed smoke used separate planning and review requests to the same dense 27B model at `10.8.0.5:8001`, through an experiment-only configuration. Production routing was not changed. These scores are model judgments, not proof of scientific correctness; the caught voltage error illustrates that limit. No complete model-authored science animation was produced by this smoke.
