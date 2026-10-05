# Subject-driven parent planning

The graph supplies a subject framework; the planner chooses an explanation.
Classification identifies the domain, requested lens, scope and useful secondary
facets. Graph attributes and seed questions are research scaffolding, not facts
or a mandatory chapter outline. Long-film production can enable this guidance
with `--subject-graph` or the web app's Subject graph planning switch. It defaults
to Off; existing runs preserve the generic planning path.

Enabled runs freeze the graph, use both models for category selection/review,
and archive the profile and source-backed prompt handoff. Research and orientation
use its questions as guidance; the external planner receives the selected node's
prompt shape alongside the subject-autonomous contract. An ambiguous or rejected
classification is logged and falls back to generic planning. Changing the setting
requires a new project; resumption retains the original graph and classification.

The revised parent contract declares `film.structure` and
`film.structure_rationale`. Available arrangements are chronological, thematic,
hybrid, comparison and procedure. Neither an explicit date range nor its absence
selects an arrangement. The exact user wording controls coverage: a broad entity
permits selective overview, named aspects must be retained, and an explicitly
exhaustive request requires coverage of the researched set. Exhaustive coverage
can group several entities into one coherent chapter; it does not require a
chapter per entity.

For example, “the Chola dynasty” permits periods, institutions, prominent
leaders or a hybrid explanation. “All Chola rulers” requires a source-backed
inventory and coverage of that inventory. Whole-body anatomy can use systems
and their connections; heart anatomy needs a focused explanation. A comparison
needs shared comparison criteria, and a procedure needs prerequisites before
dependent steps. These are suitable alternatives, not universal templates.

Code checks executable structure, known references, topic ownership, user-bound
violations and date types. Each chapter date endpoint is independently optional;
unknown dates must not be invented to satisfy a numeric field. Declared chronology
orders known starts forwards and keeps discrete dated events in their owning
chapter's stated range. Concurrent developments can overlap. Gaps, overlap and
unreached requested endpoints are surfaced to the independent reviewer, which
checks whether they cause a substantive omission or misleading sequence. Thematic
and hybrid plans can revisit time while explaining different owned questions.

The reviewer checks the chosen outline against the user request and source
evidence. It must distinguish a substantive defect from another reasonable
editorial choice. Source qualifications, explicit focuses, causal relationships
and a connected final answer matter more than matching duration or generating
equal-sized chapters. Missing evidence belongs in research gaps; changing
validation does not establish factual correctness.

Model roles are operational settings, not inferred from dates in the title.
`external-planner.json` controls the default assignment (the larger-context configured reviewer endpoint plans, while the other endpoint reviews); `longform_planner_role`
can override it. Chapter creative direction and research context now ask for
subject-relevant information rather than prescribing political, social or
economic context for every topic. Visual counts remain downstream of narration
and MFA.

`scripts/evaluate-planner-autonomy.py` tests archived source briefs without
producing media. `--validate-saved DIR` permits fresh review of an unchanged first
draft under the current validator. The original draft/reviews remain archived.
`scripts/evaluate-subject-set.py` separately groups the supplied subjects using
the frozen graph. Supplied type labels remain unused metadata. Neither experiment
establishes final film quality, and neither restarts a production job.

When evaluating graph planning, check classification on unrelated subjects,
preservation of explicit coverage intent, quality of chapter decomposition, and
whether classifications with uncertainty are surfaced instead of silently
becoming a forced outline. The existing title year-range parser remains a scope
hint; it is not a semantic classifier. Replacing that hint with reviewed scope
extraction is a separate integration step.

The archived autonomy checks are in `artifacts/external-planner/subject-autonomy-summary/`. Chola, monsoon and Mughal were model-approved under subject-appropriate outlines. The flash endpoint repeatedly produced malformed Mughal JSON; the alternate endpoint produced valid JSON and passed independent review on the first attempt. This limited comparison supports the configurable default, not a general model-quality claim.

## Proposed upstream continuity digest

The Chola continuity check caught C04 retelling the Srivijaya campaign already narrated in C03. Parent topic ownership and chapter exclusions did not prevent this factual-level duplication. The current child context includes the previous chapter's title, but not its approved narration points.

After script approval and before TTS, save a per-chapter coverage record containing the script hash, point IDs and narration, linked claims/source IDs, dates and qualifications, and outgoing handoff. Construct this from the approved artifacts; an additional model summary is not required initially. Feed a bounded view of earlier records to the next chapter's planner and reviewer. Review must distinguish repeated explanation from a short reminder, new perspective, consequence or substantive qualification. C04 can discuss cultural aftermath without retelling C03's military operation.

Coverage records are editorial memory, not factual evidence: new sources can expose a prior error or conflicting chronology. Reconcile those differences explicitly instead of treating the earliest chapter as authoritative. Preserve intended versus actually narrated status when a chapter fails, consistent with continuing later chapters. Store records by chapter and script hash, then rebuild the cumulative view after revisions; a blind append-only log would retain removed points. The digest hash must participate in planning/review cache identities. Keep the final continuity review as a backstop. This upstream digest is a proposed change, not yet integrated.
