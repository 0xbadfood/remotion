# infofilm pipeline: findings to fix

Started 2026-10-11 while testing the new endpoint layout (planner 10.8.0.5, reviewer Spark arbiter, voice Spark VibeVoice,
images amdserver image factory, music local ACE-Step; voice and images reached through `infofilm-media-adapter` on
127.0.0.1:18092). Record findings here first; fix them after the analysis is complete. Related, older notes:
`performance-todo.md` (scheduling design), `todo-next.md`.

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
