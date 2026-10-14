# infofilm pipeline: findings to fix

Started 2026-10-11 while testing the new endpoint layout (planner 10.8.0.5, reviewer Spark arbiter, voice Spark VibeVoice,
images amdserver image factory, music local ACE-Step; voice and images reached through `infofilm-media-adapter` on
127.0.0.1:18092). Record findings here first; fix them after the analysis is complete. Related, older notes:
`performance-todo.md` (scheduling design), `todo-next.md`.

## Long-term roadmap (user order, 2026-10-13)

1. **Animations.** Motion for stills, layered on the image checkpoint, so it also works on films already produced,
   and optional per film (user, 2026-10-12). Ladder, cheapest first, as in "Low priority: livelier visuals" below:
   - renderer only: a focus point per beat, transitions and a mood look;
   - 2.5D depth parallax and animated route maps;
   - image-to-video for a few hero shots per chapter.
2. **Famous people database.** About 1000 people cover roughly 80% of a history film's cast. Details are under the
   Dandi March cast item:
   - the list: Wikidata humans ranked by sitelinks or pageviews;
   - the portraits: Commons public domain or CC only, several per person, tagged by era;
   - the names: aliases as name variants.

   Live search stays for the long tail.
3. **Local Wikipedia with a suitable index.** An offline dump (e.g. Kiwix ZIM or the XML dump) with a full-text and
   entity index, as the research starting point:
   - seed facts, dates, people and outbound sources before any web search;
   - fewer web searches and fetches;
   - faster, steadier research;
   - a shared backbone for the cast database's people and aliases.

   Web search then goes to primary and scholarly sources the dump points to.

## Run and rescue rules (user decision 2026-10-13: 2-3 clean runs; the user fires the runs)

Duration is a target, not a limit: dense subjects take the time they need (Manhattan Project ran 17:56 for 15:00
and the user found it balanced). Do not trim treatment to hit the clock.

When something breaks:
1. **Let the run finish.** A long film keeps processing the other chapters; the job ends with
   `Long film is incomplete; chapters needing attention: Cxx`. Do not cancel to fix one chapter.
2. **Read the failure first.** Real content problem, or a checker/rule bug? (Manhattan: the C04 banner was a word-count
   bug; C06 was conflicting rules.) Fix the cause in code, with a test, never by loosening a check blindly.
3. **Only code and prompt fixes between run and resume.** Settings that feed the long-film identity (duration,
   planner settings in `external-planner.json`) are fingerprinted, and changing them breaks resume. Change them for
   the next film, or move `longform-project.json` aside knowingly (the plan gets redone).
4. **Restart the film service** (`systemctl --user restart remotion-video.service`) so the worker loads the fix, then **Resume saved run** in the portal.
   Research, evidence, beat sheet, cast and finished chapters are reused, and images resume from the chapter snapshots.
5. **Images never stop a film.** After 4 local takes the best-scoring take is used and tagged for review. Premium rescue
   is per-run opt-in (off by default).
6. **Audio:** a repeated identical Whisper failure goes to the arbiter (the reviewer). A real misread is re-voiced.
7. **Record every failure** in this file under the run's section, fixed or not.

A run is **clean** when it finishes with no resume and no code fix. **Production ready = 5 consecutive clean runs**
(user, 2026-10-13). Production accepts no planning round below score 82.

## Production: premium arbiter (user design, 2026-10-13; not built)

Opus, not Sonnet. Code calls it when a repair stalls: the same finding in consecutive rounds, a flagged unit returned
unchanged, a score plateau, or rounds exhausted.
- **It writes the fixed unit itself.** It doesn't advise, and its output isn't reviewed again, so no new failure modes
  open up. Code still runs its deterministic checks: schema, ids, word counts, raster-text rules.
- Code checks always apply. A failed check gets a few retries **between code and the arbiter only**: the code errors go
  back to the arbiter, and no reviewer or other model joins the loop.
- **The context is unit-level and complete enough to write the unit alone.** For a failed scene plan: the evidence
  digest, the scene's notes (narration, beat, neighbours, composition, banner rules), the failed candidate and the
  findings. Then it makes the scene, and that's all.
- Every stalled stage in the 50-100 dev runs is recorded with its signature; that set defines when the arbiter is called.

## BUILD NEXT, in this order (user decision 2026-10-13, after the Manhattan Project run)

Rules every planning stage must follow (from the audit of all 12 production planning stages, 2026-10-13):
1. Smallest repairable unit per stage, with a **stable id assigned by code** (never invented, renumbered or reused by
   the planner); a changed id list outside an explicit structural operation is a validation error.
2. **Strict pointers checked by code**: reviewer findings and validator errors name `"<unit id>"`,
   `"<unit id>.<subfield>"` or `"stage"`. A review with unparseable pointers gets a schema repair; the raw review is
   never forwarded to the planner.
3. **Code always sits between reviewer and planner**: code maps findings to units, builds a repair request with only
   the flagged units (their findings, read-only neighbours as context); the planner returns only those units; code
   merges by id, keeps timeline anchors and re-validates the whole candidate.
4. **Structural edits are explicit operations** (insert, split, remove, reorder) requested by a finding and applied by
   code, which assigns the new ids.
5. Whole-candidate issues (counts, cross-unit duplication, arc) are stage-level: the only full redraft, still via code.

### 1. Structure compliant with targeted repair (done 2026-10-13)
- [x] Strict pointer format in every reviewer prompt and every validator message; code validation of reviews
  (schema repair on bad pointers). Found in Manhattan C03: `scenes[S17]`, `S20 (P10, ...)` and
  `assets[].people_in_frame (A001-A008)` matched no unit, so the planner got the whole raw review; script errors put
  the id last ("Invalid point mapping: P03"); one evidence error prints the whole fact instead of its id.
- [x] Code-assigned stable ids for every unit: facts, uncertainties, points, scenes, assets, banners (today keyed by
  scene_key), cast people (today keyed by name), orientation cautions and coverage rules, creative-brief fields.

  Done: `infofilm/repair_units.py` (unit specs, pointer resolution incl. `assets[A013]`, `assets[id=A013]`,
  `scenes[S17]`, `S20 (...)`, ranges and lists; pointer errors go back to the reviewer once); every stage declares
  its units; validator errors start with `<unit id>: `; cast people get code ids R01.. and fact refs `C01:F03`;
  string lists (uncertainties, cautions, coverage rules) get code ids; creative-brief fields are units.

### 2. Logic for targeted repair (done 2026-10-13)
- [x] Generic engine in `llm.loop`: repair request with only the flagged units, partial planner response, merge by id,
  anchors kept, structural operations, stage-level fallback. (Today `keep_unflagged` enforces only assets and
  scenes, and the planner still returns the whole candidate.)
- [x] Wire every stage, by impact: script points and the continuity gate (both rewrite a whole chapter script today),
  evidence facts, film cast, longform orientation, image-prompt banners, creative-brief fields; parent plan returns
  only the flagged chapters.

  Done: the planner gets only the flagged units and answers `{units, fields, operations}`; code merges by id
  (anchors kept; insert/remove applied by code with new ids such as S17b); stage-level findings are the only full
  redraft, and they get structured findings, never the raw review. The continuity gate now repairs only the points
  its findings name (`C02 P11`), starting from the accepted script; a finding about the other chapter stays stage
  level. Parent plan: structured findings only and a by-id merge; it still returns the whole plan (one document
  with code enrichment, once per film) and code keeps every unflagged chapter.
  Tests: `tests/test_repair_units.py`, `tests/test_repair_loop.py`.

Audit table before the build (2026-10-13):

| Stage | Unit | Stable id today | Code-enforced repair today |
|---|---|---|---|
| Parent plan | chapter | yes (`C01`) | yes (flagged chapters, merge, repair_scope); returns whole plan |
| Longform orientation | anchor; caution/coverage rule | anchors yes; others no | no (full redraft) |
| Chapter evidence | fact; uncertainty | facts model-assigned; others no | no |
| Chapter script | point | model-assigned (`P01`) | no |
| Creative brief | field | field names | no |
| Visual beats | scene | model-assigned (`S01`) | yes since 2026-10-13 (merge by id, anchors kept) |
| Image prompts (batches) | asset; banner | assets yes; banners by scene_key | assets yes; banner errors force a full batch redraft |
| Cast coverage repair | asset | yes | yes (planner returns only chosen assets): the target pattern |
| Film cast | person | no (name) | no |
| Continuity gate | point within a chapter | finding names `C02 P11` | no (redrafts the whole chapter script) |
| Per-image take repair | one image's scene | yes | yes |
| Person reference choice | one decision | n/a | n/a |

### 3. Hybrid planning: central beat sheet for long films (option C, user decision 2026-10-13; built 2026-10-13)

Today chapters write their scripts alone from a central outline, and a pairwise continuity gate patches conflicts
afterwards (redrafting only the later chapter, before any voice work). Its blind spots: it compares only neighbours,
so duplication between non-adjacent chapters (e.g. C02 and C05) is never seen; it can change only the later chapter;
and nothing controls pacing or length across the film. Apollo 13: C01 revealed the splashdown and verdict before the
rescue chapters; the film ran 46% long.

- [x] **Built** (`infofilm/beat_sheet.py`, `external-planner.json` `beat_sheet: true`, `script_length_tolerance: 0.15`):
  phase A runs every chapter's research and evidence, then one central beat sheet (`beat-sheet.json`: chapter frames
  with opening/closing/people, beats B01.. with chapter, intent, own fact ids and word allocation), then the chapter
  scripts in parallel from their slice. Word budget per chapter = advisory seconds x the film calibration's measured
  words per minute, owned by code; scripts outside the tolerance are a validation error. The cast receives the
  planned people per chapter. Units: chapter frames and beats (targeted repair per beat). Older frozen runs keep
  the previous flow (`beat_sheet` absent = off). Tests: `tests/test_beat_sheet.py`, the phase-order test in
  `tests/test_longform.py`.
- [x] **Central beat sheet** between chapter evidence and chapter scripts:
  1. Central outline (as today): chapters, owned topics, research angles.
  2. Per-chapter research and evidence (as today, parallel).
  3. **New:** one central call sees every chapter's evidence summary and fixes, per chapter: the beats (points with
     fact ids), what is revealed where (no spoilers ahead of their chapter, no repeats anywhere in the film), the
     handoffs (how each chapter opens from the last and closes into the next), which named people appear where
     (feeds the cast's `show_in` directly), and a **word budget** from the narrator's measured words per minute.
     Reviewed once by the reviewer for duplication across all chapters, arc and budget; targeted repair per chapter.
  4. Chapter scripts in parallel: each writes the prose for its own beats; reviews stay chapter-sized.
  5. Continuity becomes a light confirmation against the beat sheet (should rarely redraft).
- Absorbs: script-stage continuity after multi-queue (Valkyrie decision above), the Valkyrie C02/C03 duplicate as
  a test case, the Apollo length overrun (word budget), and makes chapter-only plan repair a natural fit.
- Rejected alternatives: fully central script writing (one ~2,500-word output and review degrade with film length;
  loses parallel writing and chapter-sized repair); keeping solo chapters with a wider gate (still patches after
  the fact, cannot manage length).

## Mechanical Turk (20-min target, 2026-10-13): first exit-gate plan, 6 chapters failed on run 1

- [x] Parent plan: rounds 83.4 / 85.2 / 81.25, accepted with findings (round 2); the C03 finding (split 1774 / 1783 /
  1804) was carried to C03's evidence and script. First real use of the exit gate.
- [x] C05, C06, C09: "Person reference identity or source status changed". The pre-1850 change rewrote
  `reference_kind` inside the worker (Babbage, born 1791, planned as photograph), so the returned person no longer
  matched the planned one. Fix: `person_references.planned_kind` is applied to the plan before keys are computed; the
  worker applies the same function.
- [x] C01, C07: whole-chapter audit `numeric_mismatch` on "firsthand"/"first hand" and "secondhand"/"second hand".
  Fix: the number check ignores a word split and a same-value rewrite (`same_number`).
- [x] C03 P06: "Joseph II" failed three takes. The audit form used the regnal reading ("the second") but Whisper writes
  "II"; `same_number` did not read ordinals; the arbiter never ran because foreign-name misspellings differed
  between takes. Fix: the audit keeps Roman numerals as written; `same_number` reads "the second" = II; a repeated
  failure means a shared difference, not an identical list.
- [ ] Sources were popular (BBC, History.com, Slate, Wikipedia, plus Poe's essay): source reputation preference.

## Indus Valley (15-min, 2026-10-14) and Great Stink power cut (2026-10-13)

- [x] Great Stink: a UPS failure rebooted the Spark four times (23:44-00:03) and left 157 zero-byte files (152 JSON,
  4 PNG, 1 WAV); the resume failed six chapters on empty JSON and changed image checkpoints. Fix: `core.write` /
  `write_bytes` fsync before the rename; the queue sweeps zero-byte data files on every launch; an emptied image
  take restarts that asset (`intact_checkpoint`). The run is parked; resume it later (user, 2026-10-14).
- [x] Indus C05 ("What People Wore"): all five fetches failed or had no claims (harappa.com empty, Kenoyer PDF 403,
  textileschool timeout); chapter research has no floor, so the agent's answer from nothing failed the chapter with
  budget and leads left. Fix: every research answer needs at least one usable fetched source (nudged up to 3 times).
- [ ] The beat sheet then ran with C05 lacking evidence and repeated "C05: needs at least two beats" for nine rounds:
  it should stop at once when a chapter has no evidence (or leave that chapter for the resume).
- [ ] Unrelated: mage-edit, ltx25-api, ltx-web user services crash-loop thousands of times on the Spark.

## Listening audio arbiter: Whisper + Gemma 4 "two ears" (eval 2026-10-13)

- [x] Gemma 4 12B (llama.cpp, `~/llm/rungemma.sh`, PORT=8001; Qwen service stopped, not disabled) hears audio.
  Given the script it agrees with it (accepted 1781 -> 1791), so it is only ever asked for a blind transcript.
- [x] Eval: `scripts/audio_arbiter_eval.py` (64 cases from saved takes: 14 Whisper failures, 10 text-arbiter accepts,
  20 controls, 20 code-made fakes) and `scripts/audio_consensus_eval.py` (decision rule over saved transcripts).
  The production Whisper scorer alone passes 13/20 fakes (one swapped name or inserted "not" stays inside WER).
- [x] Two-ears rule: fail only where both transcripts depart from the same script word and heard the same thing,
  after code removes formatting (joined words, possessive, same number) and sound-alike name spellings.
  Result: fakes 0/19 accepted, controls 19/19, text-arbiter accepts 10/10, Whisper failures 11/14; the 3 rejected
  are genuinely bad (Cuba P03 truncated after two words, Tiger P07 invented phrase, Tiger P04 "2020" for "2022").
- [x] Gemma refused once ("Please provide the audio") in 64: detect and retry, else fall back.
- [x] Holdout from the aiserver's 12 old runs (copied to artifacts/audio-arbiter-eval/aiserver; 192 cases: 14 Whisper
  failures, 14 text-arbiter accepts, 40 controls, 124 fakes incl. 17 number fakes). Frozen rule: controls 40/40,
  fakes 5/124 accepted. All 5 misses and 2 Tamil-name rejections were comparison flaws (joined-name rule swallowing
  "not", word-level alignment drift, accents), so the rule became word-level: fail when both ears missed the same
  script word or heard the same extra word. After fixing: Spark fakes 0/19, holdout fakes 1/124 (a fake-maker
  artifact, "this" -> "that"), controls 59/59. Rejected real takes are bad audio (truncations, skipped
  "Kulottunga", invented phrases, "2020" for "2022", garbled dates) or close name calls (Stulpnagel, Vijayalaya,
  echec), which go to a retake or the fallback.
- [ ] The holdout was used to fix the rule, so it is no longer clean: the next runs' takes are the true holdout
  (run the eval on them before wiring the rule in as the deciding check).
- [ ] Wire into the audit: Gemma transcript on every point (about 10 s) or only on Whisper failure; decide the
  fallback when Gemma is unusable (text arbiter over all transcripts, user idea) and the llm-gemma4 service unit.

## Pre-photography likeness (user question, 2026-10-13)

- [x] People born before 1850 were marked "unavailable" and never searched, which also cut off the
  `historical_portrait` path (paintings, engravings, busts) built for them. They were still drawn (cast coverage),
  but each scene invented the face. Now they are searched as documented portraits, never photographs; "unavailable"
  only when nothing documented exists.

## How a Jet Engine Works (20-min target, 2026-10-13): first "how it works" film

- Parent plan passed in round 1 at 94.1 (the highest yet). Hybrid structure: principle, then intake to nozzle, then
  history, then the Brayton cycle; 6 chapters, 16:30 planned. 5 sources from 3 sites (NASA Glenn).
- [x] C02 evidence failed 7 rounds on one finding pointed `F16.narrative_priorities`: code repaired fact F16, and the
  priority ("squeezes air into smaller spaces", phrasing taken from the parent plan's chapter objective) never
  changed. Pointers `<id>.<another list>` now resolve to that list's units.
- [ ] Underlying conflict: the evidence reviewer rejected the parent plan's own phrasing. Consider passing plan-wording
  findings back as plan_findings, or letting the evidence stage flag the objective wording.
- [x] Native-graphic scenes (compare, flow, diagram cards; 12 of 137 shots on the tiger film) sat on a flat gradient.
  **Chapter backdrop** (user design): one thematic image per chapter, prompt built by code from the lesson objective,
  visual direction and shared style (written-element sentences stripped), generated once with no review, and shown
  dimmed and blurred behind those scenes. Any failure leaves the gradient. Live test on jet C02: a compressor cutaway
  with airflow, no text, about 45 s. Follow-up: ladder rung 3 could reuse the chapter backdrop.
- Run stopped by the user to fix and restart fresh.
- **Fresh run: the first clean run** (no stop, resume or fix): 16:38 film, 7 chapters, 1 h 22 min. 101 images:
  58 first take (57%), 21 on 2, 10 on 3, 12 on 4; 9 tagged best-available (scores 53-67), 5 of them in C02 and C03
  (compressor, combustion and turbine cutaways). 6 chapter backdrops generated.
- [x] Cast (castless film) exhausted 4 rounds while the job went on: the reviewer echoed the candidate
  ({"people": []}) instead of a review. Reviews are now shape-checked; a malformed one is retried once with the format
  spelled out, and if it is still malformed the round is accepted on code checks (`code_checks_only`).

## Royal Bengal Tiger of the Sundarbans (20-min long film, 2026-10-13): no cast, nature subject

- Parent plan passed in round 1 (92.8), the first first-round pass. Thematic structure, 8 chapters, conservation
  framing (3 chapters on people); the user judged it fair for the wording given.
- [ ] Source quality: the floor counts sources and sites, not reputation; a pet magazine got in, and there was no
  primary census report. Consider a reputation preference (government, UNESCO, journals, universities first) in the
  research prompt or code ranking. Local Wikipedia (roadmap 3) would also seed better sources.
- Stopped at 15:48 on OpenRouter 402 (credits; topped up, resumed). [ ] A billing error should stop the whole job at
  once with a plain message (build with the Anthropic connection).
- [x] Cast deadlock, 9 rounds: the reviewer demanded quoted researchers and a private widow ("principal people must be
  seen"), code demanded integer birth years nobody knows, the planner dropped them, and so on. Non-fatal (the film
  went on castless). Fix: the cast task leaves out private individuals used as examples and experts quoted only as
  sources (a named private person's likeness is never generated); birth_year may be null when unknown.
- [x] C03 narration P06 failed 3 takes on "NTCA Phase IV" spoken as "four": the arbiter accepted (number format),
  but code's number check did not know Roman numerals and overruled it. `same_number` now reads Roman numerals.
- [x] **Pre-voice conditioning** (user design): `speech.py` builds a per-chapter lexicon {written token: spoken} before
  VibeVoice renders. Code rules come first: Roman numerals after a counter or a regnal name, and letter acronyms. One
  LLM call (about $0.0005 per chapter) corrects the acronym guesses (DISHA -> Disha, CITES as a word) and adds
  abbreviations and symbols (km -> kilometres). The voice and aligner get the spoken form, the audit gets it except
  for capitalised tokens, and captions keep the written words. Live on tiger C03: "N T C A Phase Four". Names are not
  respelled (the arbiter still handles name variants); a pronunciation lexicon for names could come later.
- Scene plans on the tiger run: the ladder fired for the first time (C05 A015, widow Shobita's account: four rounds of
  contradictory findings, then rung 2, then rung 3; accepted after screening at 76). C07 was accepted after screening
  at 79; C04 and C08 evidence after screening at 76 and 80. No scene-plan failure.
- [x] Rung 3 failed the raster-text check: it embedded the film style ("reconstructed scenes are identified as
  illustration"). It now uses the asset's own short style phrase with label words stripped.
- [x] Image task "show the real people involved" now applies to public and historical figures only: a private
  individual in an example story is never a named likeness (the place, an unnamed figure or an object instead).

## Dandi March (20-min long film, 2026-10-13)

Result: 19:31 for 20:00 (-2%), 8 chapters, 1 h 49 min with two fix-and-resume stops. C07 passed clean at 88 on
resume. YouTube upload from the portal works (Cuba uploaded).

- [x] Beat sheet (8 chapters) hit the 7000-token output cap twice; retries kept the same cap. Loop drafts now raise
  the cap x1.6 up to 12000, and the beat sheet starts at about 1000 tokens per chapter. Every other model call retries
  a cut-off once with 30% more room (user rule). Resume passed.
- Audio: 4 arbiter rulings in 8 chapters (2 accept name variants such as Sabarmati and Dandi; 2 retry real misreads:
  "Philip Miller" for Webb Miller, 322 for 320 injured). 3 cross-checks passed without the arbiter.
- [ ] Cast: only 4 of 9 people got photo references (Gandhi, Naidu and Azad are well photographed), and cast took 13
  minutes (Cuba: 17). **Plan (user, later): an offline database of famous people**: vetted, license-checked
  portrait references plus name variants, built ahead of time, so the pipeline looks well-known people up instantly
  and searches online only for the long tail. Should fix coverage and cut the cast stage's time.
  Sizing (user): about 1000 people cover 80% of history films' cast needs. Candidate list: Wikidata humans ranked by
  sitelink count or Wikipedia pageviews; images: Wikimedia Commons, public domain or CC only, with several era
  portraits per person and Wikidata name aliases as variants.
- [x] C07 image prompts batch 2: 6 rounds at score 78, each blocking on units nobody changed, and the reviewer
  removed Ghaffar Khan, demanded him back, then removed him again. Fixes in the shared repair loop:
  `screen_unchanged` (blocking findings may only name changed or asked-about units), `coverage_screen` (a batch
  finding demanding a required person is advisory; code's whole-chapter coverage repair owns omissions), and
  `review_decision`: a round passes clean at 82, or at 75 or more when code screened away every blocking finding
  (development stand-in for the production premium arbiter). Floor lowered to 65 for development (user): this
  failure is known and recorded, and runs should keep going to surface others. **Production accepts nothing below
  82.**
- [x] Downward ladder for a unit that keeps failing (user design, development only): strike 2 simplify (existing),
  strike 3 rung 2 (image prompts: an abstract explanatory visual, no people, no specific machines or scenes; banners:
  a neutral label from the narration heading), strike 4 rung 3, built by code (an abstract backdrop in the shared
  style, or the narration heading as the banner), which review cannot block. **Production removes the ladder:** a
  paid film must show complex subjects, so the premium arbiter and the premium renderer take over.

## Cuban Missile Crisis (20-min long film, 2026-10-13): stopped at the parent plan, restarted fresh

- [x] Research fetched 2 of 16 allowed sources (13 facts for 20 minutes; Manhattan had 5 and 20): the agent decides
  when evidence is "sufficient" and nothing in code set a floor. Film research now has a code-owned floor
  (`min_sources` 5 from `min_domains` 3): an early answer is sent back with the shortfall and unread leads, up to 3
  times, while budget remains. `result.json` and the research event record `source_floor`.
- [x] Parent plan: 3 rounds, 84.1 / 85.3 / 81.2, and each review blocked on something new. Round 1's minor "RFK the only
  channel" was never sent to the planner (majors only), so round 2 promoted it to major. Round 3 blocked on C04's title,
  unchanged since round 1, plus "needs Soviet sources beyond the excerpts". The parent plan never got the repair-unit
  rules. Now:
  - every film field, top-level list and chapter field is a unit (`film.<key>`, `chapters[Cxx].<key>`, `coverage`,
    `research_gaps`); code merges only the flagged units back;
  - minor findings on the chapters being repaired travel with the repair;
  - a field returned unchanged is called out (`unchanged_last_round`);
  - a new blocking finding on text that did not change, and that no round asked to fix, becomes advisory;
  - a finding that needs sources beyond the brief becomes a `research_gaps` entry (code and review prompt);
  - **exit gate** (`accept_with_findings`, default true): after the last round, the best round with no code errors, score
    at or above the minimum, no blocker, and only narrative-field majors is accepted. Its open findings land on the
    chapter as `plan_findings`, which chapter research, the beat sheet and the script must honour. The planning
    event shows `accepted_with_findings`.
  - Replayed on the real Cuba rounds: round 2 would have been accepted, carrying one C04 handoff finding.
- Fresh run: research 6 sources / 5 sites after two floor nudges (20 facts); parent plan passed normally in round 2
  (91.8 -> 93.8). Cast 12 people, 8 referenced.
- [x] C04 scene-image-prompts batch 3 exhausted 6 rounds (76/78/76/76/76/76) on one finding: the banner "Two Soviet
  messages, October 26 and 27, 1962" dates an undated letter. The reviewer pointed at `A017.banner`, but banners are
  their own units (`160:S18`), so code repaired asset A017 every round and the banner never changed. Fix: unit specs
  take `links` (`<item>.<subfield>` -> the units it lives in); image prompts link each asset's banner to its scene's
  banner unit, and the pointer guide tells the reviewer to point at banner ids. Not a case for a score-based exit:
  the scores were below 82, and the finding was right.

## Manhattan Project redo (first run after the build, 2026-10-13)

- [x] Beat sheet budget error named only the chapter frame (C06), so beat changes were discarded six times: aggregate
  errors now name every unit that can fix them (`C06 (B28, ..., B34): ...`).
- [x] C05 script: 303/299/304 words for a 259 budget (15% band) got three full redrafts, then the one-point review
  fix never ran (rounds exhausted). Length errors now name the points to trim; code-only rounds get up to three
  extra rounds; default band 25% (user: not too harsh on length).
- [x] Reference selector rejected Oppenheimer's c.1944 official portrait and Hahn's 1944 Nobel Foundation photo
  because the metadata did not state the medium. Rule added: a dated photographic-era portrait from a reputable
  archive counts as a photograph unless it says painting/drawing/sculpture. (This run keeps the cached rejections.)
- [x] C04 image prompts: banner "Oak Ridge plants: Y-12, K-25, S-50" counted 9 words by code (6 by the planner);
  targeted repair asked nine times and got the same text back. Designations (Y-12, B-29, U-235) now count as one
  word, banner errors show code's token breakdown, and a flagged unit returned unchanged is called out in the next
  request ("UNCHANGED: ... change it").
- [x] C06 image prompts: the parent plan's visual direction asked for split-screen panels for the debate chapter;
  the image rules forbid panels. Haiku followed the rule, Luna enforced the direction, six reviews failed. The
  image-prompt task now states that code-owned image rules override visual direction (panels/labels become native
  overlays), and the parent plan's visual_direction must not ask the generator for panels or text.
- [x] Narration still ~15% over plan with scripts on budget: delivered chapters run 113-134 wpm (1221 words in
  608 s = 120.5) against the 129.7 calibration (one continuous passage), and scripts land +4..+12% over budget.
  Beat-sheet budgets now use calibration x `narration_rate_factor` (0.92). Re-measure on the next film.
- Images: 46 of 52 first-take passes, no tagged image in five finished chapters; every required person shown (the
  coverage repair added 4 people in C02 and Oppenheimer in C03).
- Cast survived with code ids R01-R12 and C01:F03 refs; 10 of 12 referenced. C02 must show 8 people.

## Manhattan Project trial (15-min long film, 2026-10-13): stopped for the build, to be redone

4 of 8 chapter videos made. No rescue (premium off on purpose); the run fed the build above.
- Film cast exhausted silently (no cast: no required people, no face references in the whole film): fact refs
  `C01-F01` called fabricated (format now defined and code-checked), mechanical length errors cost whole rounds
  (now targeted per person), the reviewer kept asking for more people than the budget (task now states the budget).
- C03 visual beats: repairs inserted scenes and renumbered 16 later ones (merge by id since tonight).
- C07/C08 image prompts: unparsed pointers (`assets[A009]`, `assets[id=A013]`, `assets[0].prompt; assets[3]...`)
  and prompts asking for "labelled as an illustration" (now a planner rule and a validator error per asset).
- C06 narration: "P9" false positive (scoring fix, arbiter on repeated identical failures).

## Agreed optimization plan (2026-10-11)

After watching the Valkyrie test run end to end, the user settled on five changes, in this order. Details for each
are in the findings below.

1. **Research and search.** Reliable search (Tavily fallback key in place; stop research with a clear error when search
   is unavailable), stop the agent calling tools after its budget is spent, and size the budget to the need.
2. **Share digests.** Film-level fetches and digests are reused by chapters (cache by URL across parent and chapters);
   chapters research only the gaps their brief leaves.
3. **Multi-queue architecture, all chapters in parallel.** One queue per resource (LLM planning, LLM review, image
   generation, voice + alignment, music, video rendering), each pulling ready work from any chapter; images submitted as
   a set so both factory GPUs work; planner and reviewer stay separate; every task checkpointed. Expected ~50-60% less
   total latency.
4. **Premium image model always loaded.** Flux.2 Dev stays resident on the local 3090 instead of reloading per job.
5. **Music to the Spark.** ACE-Step turbo as a small service on the Spark (34 GB free), which frees the 3090 for Flux and
   gives music its own queue.

Smaller fixes found on the way (below): retry short server outages instead of failing chapters, keep service addresses
out of the long-film identity, one place for image endpoints, Wikimedia 403 on portrait references, parallel MFA.

## Blockers

- [ ] **Web search is down: the Tavily key is over its plan limit.** Every `web_search` returns
  `Tavily search failed: This request exceeds your plan's set usage limit`. Upgrade or replace the key
  (`.service.env`), or add a fallback search provider. Found in the Valkyrie run (job d2cf0c0f…, stopped).
  Checked 2026-10-11 via `api.tavily.com/usage`: "Researcher" plan, 999/1000 credits used (975 searches, 24 extracts).
  A plain 1-credit search still went through; the research agent's searches were refused from the first call
  (probably costlier search settings). The "research-digest … succeeded" lines are LLM summaries of fetched pages,
  not searches.
- [ ] **Research continues silently without search.** With every search failing, the run went on to plan a film from
  one usable source. Stop the research stage (clear error in the web app) when search is unavailable.

## Research stage

- [ ] **Agent keeps calling tools after its budget is spent.** After 8/8 fetches it asked for the same URL again and
  again ("fetch budget exhausted"), one full reviewer turn (~10k prompt tokens) each, up to `max_steps` 60.
  Once search and fetch budgets are exhausted, withdraw the tools and ask for the final answer
  (`research_harness.run_agent`).
- [ ] **Without search results the agent guesses URLs**, mostly homepages, bot-challenge or JavaScript pages that
  Obscura cannot read (Britannica challenge page, BBC/history.com/USHMM navigation only, Bundesarchiv 404,
  historyplace empty). When search fails, say so plainly in the tool result so the model stops guessing, and
  consider a list of reliably readable reference domains.
- [ ] **A resumed job reuses its saved research**, even when that research was degraded (e.g. one usable source).
  Mark research with too few usable sources as failed instead of passing it on.

## Throughput (from the Mughal token audit and the user's plan)

- [ ] Images are the bottleneck (~60% of tokens; most wall time). Generate each chapter's single-pass image set in
  parallel against the two-GPU image factory (input queue 4, 429 + Retry-After when full; infofilm needs to back off on 429).
- [ ] Interleave the LLM pair with image generation across chapters through checkpoints, so planner/reviewer work on
  one chapter while images for another are generating (see `performance-todo.md` scheduling rule).
- [ ] **The LLM pair idles while a chapter's audio is produced.** Valkyrie C01: last LLM call (script review) 16:48:48;
  narration started 16:47; at 16:51 both LLM slots and Spark VibeVoice were idle while this machine ran MFA word
  alignment point by point (P07, retry beam 320). That window could have run C02's research, evidence and script.
  Hand the chapter's audio set to the voice/alignment group and move the LLM pair to the next chapter (scheduling
  rule in `performance-todo.md`); come back for the audio review when the set is ready.
- [ ] **Resource queues shared by all chapters** (user's design, 2026-10-11; replaces the "two chapters at once" idea).
  Three work queues that accept tasks from every chapter:
  1. image generation: the two-GPU factory pulls jobs from any chapter;
  2. LLM review: generated images (and drafts) from any chapter wait for the reviewer; a failed image goes back to
     the generation queue with its repair, a passed one is done;
  3. LLM planning/drafting: scripts, visual beats, image prompts from any chapter wait for the planner.
  4. voice + alignment: Spark VibeVoice batches (up to 40) and local MFA;
  5. music: local ACE-Step on the 3090;
  6. video rendering: Remotion on this machine. C01's render took ~10 min while images and the LLM pair sat idle.
  Each chapter still advances through its stages in order (a task is queued only when its inputs exist); within that,
  work from all chapters interleaves, so no resource idles while any chapter has ready work. Planner and reviewer keep
  separate queues, so no model reviews its own output. Checkpoint every task so the queues survive restarts.
  Expected gain: stage times per chapter now sum to ~40 min (Valkyrie C01: research+evidence 16, images 12, video ~10,
  narration 4.4, scenes 2, music 0.7); with every resource on its own queue a 6-chapter film approaches the slowest
  lane per chapter (~20 min) instead of the sum, about a 50-60% cut in total latency (user's estimate; consistent with
  these numbers). For reference, Mughal spent 129 min per chapter on images (74% of stage time) before the factory.
- [ ] **Move music to the Spark; keep Flux hot on the 3090** (user, 2026-10-11). Today ACE-Step runs as a local
  process (`acestep_python -m infofilm.music_worker`) under the shared 3090 lease, so music and the premium Flux
  tier contend, and ImageStudio unloads Flux after each job (~60 s reload per premium image). ACE-Step turbo is
  ~10 GB of weights (model 4.5 + LM 1.7B 3.5 + embedding 1.2 + VAE); the Spark has 34 GB free next to VibeVoice
  (31 GB) and the arbiter (41 GB). Needs: an ARM (aarch64) ACE-Step install on the Spark, a small music API there,
  and an infofilm client for it. Music QA (Whisper large-v3 via stable-ts, VAD) can stay local at first. Then the
  3090 is Flux-only and stays loaded, and music gets its own queue.
- [ ] **Images are rendered and reviewed one at a time.** C01: ~25 s generation + ~8 s vision review per image, strictly
  serial, so the factory's second GPU is idle. Submit a chapter's image set together (factory queue 4) and review as
  each completes.
- [ ] **The reviewer must accept images.** Image review sends pictures to the reviewer; the Spark arbiter had no
  `--mmproj`, so C01's first review got HTTP 500 "image input is not supported". Fixed: `~/llm/run38.sh` on the
  Spark now loads `mmproj-Qwen3.8-27B-Q8_0.gguf` (copied from 10.8.0.5). Check vision support in readiness.
- [ ] **A server outage of a few seconds fails every remaining chapter.** Restarting the arbiter (12 s) made C02's
  research fail ("Connection refused") and C03-C06 fail within 3 s each (503 while loading): no wait or retry.
  Retry connection errors/503 with backoff before failing a chapter. (Restart was done by the assistant mid-run.)
- [~] **Image-prompt review loops do not converge, and exhaustion fails the whole chapter.** Valkyrie C02
  `scene-image-prompts-batch-002`: 4 planner drafts, 4 rejections. Each redraft rewrote the whole batch, fixed one
  complaint and introduced another (Stauffenberg "missing hand and arm" → wheelchair → Olbricht placed in a scene
  whose narration names Stülpnagel, Wagner and Nebe). This is a prompt-writing failure, so the Flux premium tier
  (a rendering fallback) cannot help here. Fix, borrowing from Lesson Studio (user, 2026-10-11):
  1. repair only the flagged assets by id and keep the accepted ones (`scivid/patch.py` approach);
  2. **simplify immediately after 2 failed rounds** on an asset: drop the contested specifics and rewrite it as a
     simpler, safer prompt (generic or symbolic) instead of re-arguing the same details;
  3. allow more attempts in total before failing (simplification gets its own rounds);
  4. if still unresolved, mark only those assets as needing attention and let the chapter continue.
  **Done 2026-10-11 (1-3)** in `infofilm/llm.py` `Models.loop`, after the run ended with C02, C04, C06 exhausted
  (C02 prompt batch 2, C06 prompt batch 1, C04 visual beats): flagged items are named by id and only they are
  repaired; unflagged assets are restored from the prior draft; an item flagged in 2 rounds gets a SIMPLIFY
  instruction and the stage gets `simplify_rounds` (default 2) extra rounds; the reviewer receives its prior findings
  and must re-check them against the current draft. C06 showed why: the planner fixed the fact links in round 1, and
  the reviewer kept reporting the old links for three more rounds. Visual-beat validation errors now name their
  scene (C04 lost two rounds to an anonymous "Invalid summary"). Item 4 (continue without the failed assets) is open.
  **Result on resume:** C02 batch 2 passed in 3 rounds (targeted repair alone); C04 visual beats passed in 4 rounds
  (score 92) where it had failed before. In C04, S18's 14-word summary failed the 12-word limit twice even with the
  scene named, so simplification fired for a plain length error (it then shortened it). Validation errors should state
  the measured value ("14 words, limit 12") and probably not count as simplification strikes.
  C06 batch 1 passed in 2 rounds (score 95): with its prior findings to re-check, the reviewer accepted the fact
  links the planner had fixed in round 1, instead of repeating the stale complaint for three more rounds.
- [ ] **Both models improvise physical details the evidence lacks.** The evidence said only "severely wounded".
  The planner wrote "missing hand and arm", then a wheelchair; the reviewer "corrected" it to "three fingers on his
  right hand". (Stauffenberg lost his right hand, two fingers of his left hand and his left eye, Tunisia 1943.) Put
  key appearance facts of named people into the evidence or person cards; when a detail is missing, prompts should
  stay generic (e.g. "bandaged hand") instead of inventing specifics, and the reviewer should flag only actual
  contradictions.
- [ ] (deferred 2026-10-11: accept for now, expect stronger models to fix it) **Accepted prompts can still carry invented names and details.** After the repair fix, C02 batch 2 passed in
  3 rounds (score 92, no simplification needed), but the accepted A015 names "Hans von Beck" (evidence: Ludwig Beck)
  and A009 keeps the wheelchair (not in evidence). The reviewer checks what it is told to fix, not every name. A
  deterministic check that each `people_in_frame` name appears in the chapter evidence would catch this.
  Wrong names also cost reference research: C02 searched for "Claus-Heinrich Stülpnagel" (really Carl-Heinrich
  von Stülpnagel) and spent 3 selection rounds before giving up. The same check, run before person research, avoids it.
- [ ] **The film fails at the very end on one over-long continuity review.** Valkyrie resume, 2026-10-12 01:22: all six
  chapter videos were done, then `continuity-C02-C03` (Spark arbiter) hit its 3500-token cap and the job failed. The
  reviewer started thinking aloud inside the JSON ("Let's double check if 'immediate consequences' are covered...").
  `longform.py:378` uses `models.call`, not `call_json`, so there is no compact-format retry, and the cap is low for a
  two-chapter review. The review was still useful: C03's handoff ("What happened on July 20?") doesn't lead into a
  consequences chapter. Fix: `call_json` with a compact retry, a higher cap, and (multi-queue C4) run continuity on
  scripts, before voice and images, not after every chapter is rendered.
  **Decision (user, 2026-10-12):** script-stage continuity waits until the multi-queue conversion. Chapters are written
  solo today; with the LLM pair running ahead on scripts, adjacent scripts exist early and continuity can move there.
  Revisit as one of the opportunities to hunt after multi-queue lands (see `multiqueue-design.md`, C4 and open question 1).
- [ ] **People leak between scenes.** Olbricht (C01's person reference) appeared in a C02 scene about other
  conspirators. Bind `people_in_frame` to the people named in that scene's narration.
- [x] **The same person is researched again in every chapter.** Person caches were keyed by the whole
  `people_in_frame` entry, including the per-scene `appearance_context`, so Valkyrie made 20 searches for 11 people
  (Stauffenberg and Olbricht 4 each). **Fixed 2026-10-11:** the worker shares the portrait (when ready) and the
  Commons/Tavily searches per film under `person-references/people-by-name/<name + reference_kind>`; a reused result
  takes the scene's own person entry. Unavailable results are not shared, so the selection can retry.
- [x] **Portrait references blocked by Wikimedia (HTTP 403)** on both the Commons API and upload.wikimedia.org
  (Friedrich Olbricht), so people are drawn without reference cards. Check the User-Agent / request policy.
  **Fixed 2026-10-11:** Wikimedia's robot policy (https://w.wiki/4wJS) rejects user agents without a contact URL or email.
  `person_reference_worker.USER_AGENT` now includes `WIKIMEDIA_CONTACT` (set in `.service.env`); verified 200 on the
  Commons API and thumbnails. Takes effect after `remotion-video` restarts.
- [ ] MFA alignment runs one narration point at a time on the CPU, with retries at wider beams; it outlasts the
  VibeVoice batch itself. Align points in parallel (cores are free) and measure how often the wide-beam retry is needed.
- [ ] One `gpu_lease` (`/tmp/infofilm-rtx3090.lock`) still serialises voice, images and music even though they now run on
  three different machines. Use per-service leases; only local GPU work needs the local lock.
- [x] **Visual-beat prompts are mostly word timings.** Valkyrie C04 draft: 24.7k prompt tokens, ~60% of the characters
  (more of the tokens, at ~2 chars/token) are `aligned_words` (312 × {id, word, start, end}) plus `aligned_points`
  repeating every word id. The planner only picks a `start_word_id` per beat; timestamps are computed by code. Send
  each point's text with inline word numbers (or let the planner quote the anchor phrase and map it in code) plus each
  point's duration; drop per-word timestamps and the repeated id lists. The reviewer gets the same context, so the
  saving doubles. Image-prompt calls are not bloated (~7.5k tokens each); their cost is call count (batches of 8 ×
  rounds × planner+reviewer), which the repair fix and fewer rounds address.
  **Done 2026-10-12 (user: "sentence timing and duration, no MFA dump"):** `make_visual_beats` sends
  `narration_points[{point_id, seconds, suggested_shots, sentences:[{start_word_id, start, seconds, text}]}]` and the
  script without its narration text; `aligned_words`/`aligned_points` are gone. Beats anchor at sentence starts; the
  output schema, validator and downstream code are unchanged. On Valkyrie C04: 46.9k → 18.8k characters, about
  24.7k → ~5-6k tokens per call, for planner and reviewer. Cuts are sentence starts only: clause cuts inside long
  sentences were tried and dropped (user): Valkyrie had only 5 sentences over 150 characters, and comma cuts risk
  splitting lists. Resuming an older job re-plans its visual beats (the context is part of the stage identity).
- [ ] **Advisory continuity report on the final film** (2026-10-12): C03→C04 score 45 (chronological contradiction, C03
  outside its scope, duplicated coup-failure narrative); C05→C06 score 65 (Fromm's execution placed as immediate,
  duplicated Bendlerblock executions). Three other pairs passed. Use these as test cases for script-stage continuity.
- [ ] Trim repeated prompt bulk for the LLM pair: visual beats send ~40k-token prompts; every picture check resends
  3-4k of context.
- [ ] Image creation is one generation pass followed by a separate review pass; overlap them where possible.
- [ ] Then move the LLM pair to cloud models.

## Configuration notes

- **Image tiers read `image-providers.json`, not `image_url`.** `provider_config` overwrites `image_url` with each
  provider's `url`, and every job freezes the registry into its `config-snapshot.json`. Fixed 2026-10-11: `qwen_local`
  → `http://127.0.0.1:18092/image` (amdserver factory via the media adapter); `flux_dev_local` stays on the local 3090
  (ImageStudio :8000, tested: 768×512, 12 steps, 66 s incl. load) with `park_idle_services: false`, because parking
  only accepts the old local 8092/8075 addresses and would abort the premium tier. Backups `image-providers.json.bak-*`.
- [ ] Endpoint settings live in two places (config.json and the provider registry); make the registry follow
  `image_url` or validate that they agree.
- [ ] **A long film's frozen identity hashes the whole config**, so changing only a service address (no effect on
  content) makes the folder "different frozen settings; choose a new project". Exclude endpoint/infrastructure keys
  from the identity. Workaround used for the Valkyrie run: moved `longform-project.json` aside
  (`.before-registry-fix`) and resumed; research, evidence, plan and creative were reused from cache.
- [ ] **Chapter research repeats the film's research.** Valkyrie C01 ("The Contingency Plan", ~2 min, 8 final facts):
  3 searches, 6 fetches, 8 agent turns, 6 digests + 2 evidence calls ≈ 123k input / 10k output tokens and 17 min.
  2 of its 6 sources (Wikipedia "Operation Valkyrie", USHMM) were already fetched and digested at film level and
  were paid for again; 2 more (Wolf's Lair, Bendlerblock) belong to later chapters. ~15k input tokens per final
  fact; ×6 chapters ≈ 0.7M tokens and ~1.7 h of research. Share film-level fetches/digests with chapters (cache by
  URL across parent and chapters), let chapters research only the gaps the film brief leaves for that chapter, and
  scale the search/fetch budget to chapter length.
- [ ] Orientation research ended with only 3 usable sources for a well-documented subject (budget 3 searches /
  8 fetches); check whether that is enough or the budget/loop needs changing.

- 10.8.0.5 llama.cpp `--n-predict` raised 4096 → 8000 (infofilm asks for up to 7000 tokens).
- The planner (10.8.0.5) and reviewer (Spark) each serve one request at a time (`--parallel 1`); the 10.8.0.5 slot is
  shared with the FSSAI portal, the Spark slot with Lesson Studio narration audits and the night digest.

## Cast must be seen (Neptune Spear review, 2026-10-12)

- [x] **No person appeared in the whole film.** bin Laden was cast for 5 chapters (references fetched), yet every scene was
  an empty room, a corridor or a distant figure; C04 left his death "undepicted". No rule caused it: Haiku's caution
  on a killing spread to the whole film. User: a historical documentary must show the people involved; restraint
  applies to violence, not to depiction. Fix: the cast gives each person `show_in` chapters (fallback: their first
  chapter); the image-prompt task carries a documentary rule and `required_people`; if a chapter's prompts still leave
  someone out, one targeted repair (`scene-image-prompts-cast`) rewrites only the assets the planner picks (advisory on
  failure, recorded as `cast_coverage` in `scene-image-prompts.json`).

## Apollo 13 trial (15-min long film, 2026-10-12)

Result: 21:51 film, 7 chapters, 126 images; 2 h 39 min wall clock including three resumes. Image report:
`/experiments/apollo13-image-takes/index.html` on the portal.

- [x] Named LLM configuration saved before the premium-image role dropped the premium model ("No premium image
  model is set" in five chapters). `freeze()` now keeps the current premium choice when a configuration names none.
- [x] Batched image prompts were each told to show the chapter's required people; Luna rejected batch 1 four times
  because the Mission Control people belonged in a later batch (C05). Batches now get them as guidance; the
  chapter-wide check runs once in code after the last batch.
- [x] Continuity gate redrafted C02 a second time on resume (the repaired script was reviewed again), which would
  have discarded its narration and images. A chapter with notes is now never redrafted again.
- [x] Terminal scoring crashed C03 when Luna passed an image whose own inventory said 4 rings for 2. Code now rules
  such a pass a fail (marks kept, `code_ruling` recorded).
- [x] Premium rescue made optional per run (web checkbox; off = best local take, tagged) and up to 3 premium tries.
- [ ] **Film 46% over length** (21:51 for 15 min): chapters asked for 100-160 s came out 165-251 s; narration runs
  ~125-140 wpm. Calibrate the planner's word budget to the measured voice rate.
- [ ] **MMS out-of-memory** on the Spark (5 workers × MMS + VibeVoice + arbiter): release the model's GPU memory
  after each alignment and fall back to CPU on CUDA OOM; audit events still label alignment "MFA".
- [ ] Rescues cluster on images with named people (10 of 18 vs 6 of 105); Gemini won all 19 rescues. Consider sending
  people scenes to the premium tier earlier instead of three local takes first (re-measure after the face references).
- [x] **Text failures had two causes.** (1) Qwen copied the labeled reference card (names + biography) into the image:
  15 first-take and 19 later failures in Apollo 13; only 5 of 15 such images ever lost the text, 10 went to rescue.
  Qwen now gets text-free head-and-shoulders face crops (OpenCV YuNet detector, `models/`; separate images for up to
  three people, one strip above that); the labeled card stays with the reviewer. A/B on C05 A003/A005/A006 (same
  prompt and seed): all three old takes reproduced the card, all three new takes are one full-frame scene, no text.
  (2) Text drawn in the scene (18 images) was fixed by repairs in 17 cases; the planner now describes such surfaces
  positively from the first prompt ("plain unlabeled tick marks") instead of ending with "no text" lists.
- [ ] Targeted negative prompt (guidance on, ~2x render time) for unwanted_text retakes only: needs factory support;
  only if text failures persist after the two fixes above.
- [ ] Per-take scores: normal reviews are pass/fail only; add the three marks so take-to-take improvement is visible.
- [ ] Research gap: Gene Kranz never reached the scripts (named once in sources); cast "Lousma" lacks a first name.

## Low priority: livelier visuals (user, 2026-10-11)

- [ ] **Camera motion is very mild** (`renderer/src/Video.tsx`: zoom 1→5.5%, 24 px drift, hard cuts). Options, cheapest first:
  1. Renderer only: a focus point (x, y) per visual beat so push/pan aims at a face or detail (the image reviewer can
     supply it); transitions (crossfade, dip to black, flash at chapter turns); a look tied to the creative brief's mood
     (grain, vignette, flicker, slight jitter, sepia for wartime material); typewriter date/place stamps and name captions;
     the fetched Commons portraits shown as archival "photo on a desk" inserts.
  2. 2.5D parallax from a depth map (Depth Anything on the image factory, WebGL in Remotion); animated route maps.
  3. Image-to-video (Wan-class) for a few hero shots per chapter only; GPU-heavy.
  Start with 1, then depth parallax. Each adds render time (fits the 2-render lane in `multiqueue-design.md`).

## Haiku + Luna trial (Operation Neptune Spear, 2026-10-12)

- [x] Luna (Responses API) needs "json" in the input messages for JSON output; the instructions alone don't count. Fixed in `Models.call`.
- [x] Haiku through OpenRouter counts hidden reasoning in `max_tokens`: reasoning effort low + 8k room on top
  (`REASONING_ROOM`, also in the research agent); chapter plan budget 9.5k → 14k.
- [x] Parent plan: requested subject/language/orientation/narrator/length copied by code (Haiku tidied
  "Neptune spear :" and a 95-score plan went into repair); targeted chapter repair; Luna asked not to demand facts the
  sources lack; new majors on locked chapters count as minor.
- [x] Chapter evidence: Haiku writes 21-23 facts against the 8-20 limit and the error never said the count (C05
  failed 4 rounds). The error now states the count and asks to merge or drop.
- [x] Phase B: the Spark VibeVoice server runs one render job at a time and answers 409 "A render job is already
  active" to the rest; five of six parallel chapters failed. `media.request` now waits on that 409 (up to 2 h) like a
  full queue, and a refused, never-accepted batch is resubmitted under the same id on resume (`vibe_media.job_known`).
  Phase A, the gate (4 pairs, 95-96, 30 s) and the cast (5 people, 4 portraits, <4 min) worked first time.
- [x] Anonymous validation errors keep costing chapters (C04 "Invalid summary", C05 fact count, C04 banners). The banner
  counter counts "10:30" and "p.m." as two words each, so "Jalalabad departure, May 1, 10:30 p.m. (S3)" is 9 words and
  Haiku could not see why. Banner, coverage, beat-id, anchor-order and asset-id errors now name the item, the measured
  value and the rule; banners also say no source tags.
- [x] MFA is a queue: `resource_slot("mfa", mfa_slots)` around each alignment (default one per 8 cores).
- [ ] "Save configuration" in LLM settings saves the last saved roles, not the selected ones (same as the Lesson Studio
  bug); worked around by clicking "Save roles" first.

## First film on the Spark (GPS short, 2026-10-12)

- [x] Whole chain works on the Spark: research (ARM Obscura), Haiku/Luna, local VibeVoice (159 s, 11 points), MMS
  alignment, scenes, 13 images on the amdserver factory (12 accepted, 1 via Gemini terminal rescue, tagged for review),
  ACE-Step music, ARM Remotion render; claims file 11/11 points sourced.
- [x] Fixed on the way: shared-GPU housekeeping called the remote image service before narration (502 while the factory
  was starting); Whisper audit worker script and its offline models were missing from the Spark's quiz replica;
  `image_terminal_recovery` still named the local Flux (127.0.0.1:8000 = unrelated ImageStudio on the Spark).
- [ ] Research found only 2 sources for "How does GPS know where you are?" (thin for an evergreen topic); check the
  agent's searches/fetches on the Spark (Tavily fallback key, Obscura fetch failures, budget).
- [ ] The factory must be up before images: `~/startamd.sh` on amdserver after a power cut; a readiness check could
  start or wait for it (gpu-mode API :8099).

## After the cloud migration (user, 2026-10-12)

- [ ] **Plan repair asks only for the flagged chapters.** Today the targeted repair (`external_planner.py`,
  `flagged_chapters` / `keep_unflagged_chapters`) still has the planner rewrite the whole plan (~9k output tokens) and
  code restores the unflagged chapters. Change: when every major finding is chapter-scoped, request JSON for those
  chapters only (`{chapters:[...]}`) and let code merge them into the previous plan, then run the usual checks. When a
  finding touches an overlapping, plan-level object (`coverage`, `research_gaps`, topic ownership across chapters, film
  section), ask for the whole plan back as now. Expected: repairs drop to ~1.5-3k output tokens and run faster, and
  other chapters cannot drift.
