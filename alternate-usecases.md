# Alternate use cases

## Current-events explainers ("what we know so far")

User idea, 2026-10-14. Not built; the short (up to 3 minutes) format is to be adapted for it.

### Why it fits

Old subjects already have decades of documentaries, archive footage and far better photography. A story that is a
day or two old has not been through the media machinery yet: few careful, sourced explainers exist. The pipeline's
edge there is speed with rigour (research floors, claims tied to sources, an explicit "what the evidence cannot say"
section), which most quick news content lacks.

Positioning: not breaking news, but a clear, sourced roundup a day or two behind the wire services, and more careful
than social media.

### What changes for current events

- **Facts move.** Early reports are often wrong. Every film carries an "as of <date, time>" stamp, attributes claims
  in the narration ("according to Reuters ..."), and has a "known / still unconfirmed" section. A corrected version
  must be easy to publish.
- **Stricter sourcing.** Two independent outlets per key claim, preferring wire services and official statements.
  Many sites repeating the same early report is one source, not several.
- **Images are the main risk.** A generated "photo" of a real event from last week, or of a living person in the
  news, can mislead or read as a deepfake, and YouTube requires synthetic-content disclosure. Use maps, timelines,
  data cards, documents and clearly illustrative scenes; no photorealistic recreation of the event and no real
  faces. This suits the existing native graphics (banners, diagrams, chapter backdrops).
- **Speed.** A 20-minute film takes 1-2 hours today; a 3-minute bulletin should be ready in about 15-20 minutes to
  be timely: one chapter, fewer review rounds, a faster research step.
- **Legal care.** Ongoing cases, allegations against named people and casualty figures need conservative,
  attributed wording.

### Open questions (to discuss)

- How the short format's structure maps to a bulletin (hook, what happened, why it matters, known / unconfirmed,
  what to watch next).
- Research tuned for recency (date-filtered search, wire services first) and how source dates are checked.
- Versioning: how an updated bulletin replaces or links to the earlier one.

## NCERT humanities films (Classes 8-12)

User idea, 2026-10-14. Not built. The maths and science lessons are built from a chapter **digest**
(`~/manim/kb_research/DIGEST_FORMAT.md`: what the book contains, what it teaches, gaps, checked additions). History,
geography, political science, economics and sociology fit the infofilm stream better than the lesson stream: they
are narrative, with people, places, dates, maps and causes, which is what infofilm already does.

### The rules

- **Rooted in the book.** The book chapter, not open research, is the film's subject and its main source. The plan
  follows the book's sections and its framing.
- **Coverage.** Every section and key idea of the chapter (or of the chosen topic) is in the film. As in the digest,
  code lists the book's numbered things (sections, key terms, dates, maps, source boxes) and checks that each is
  covered, rather than leaving it to the model.
- **Fill gaps only where the book is visibly incomplete**: a term used but not explained, a missing date or place,
  a jump in a cause-and-effect chain, a map the text assumes. Each addition is marked as an addition, cited, and
  kept within the class level.
- **Never contradict the book.** If an outside source disagrees with the book, the claim is left out, not
  "corrected". A review step checks every narrated claim against the digest: from the book, a marked addition that
  agrees with it, or rejected.

### How infofilm changes

- **Research becomes the digest.** The research phase is replaced or bounded by the chapter digest. Claims are
  tied to book pages and quotes instead of web sources; web research only serves marked gaps.
- **The digest needs humanities block types**: event (date, place, actors, cause, effect), person, place or map,
  primary-source box (NCERT history quotes many sources), timeline, table or data, case study, key term, and
  exercise. `scivid/curriculum.py` indexes science and maths only; the social science books need adding.
- **Editions.** NCERT is replacing books with new editions (Class 8 among the first). The digest records the
  edition, and a film is tied to it.
- **Maps are central** to geography and history. Native map graphics (regions, routes, rivers, borders of the
  period) are needed; generated images of maps are not reliable enough.
- **Images and people.** Historical figures use the portrait reference flow. Modern political figures and recent
  events follow the current-events image rules (no realistic invented scenes of real people).
- **Neutral tone.** History and politics topics can be contested. The book's framing is the line; the film does not
  add opinions or alternative narratives.
- **Length.** Duration comes from the chapter's coverage (a target, not a limit). One film per chapter, or per
  section for long chapters.

### Open questions (to discuss)

- One film per chapter or per section, and whether a short recap format is also wanted for revision.
- Language: English first; Hindi narration later?
- Tying in the book's exercises (an end card, or a quiz from the digest's exercise blocks).
- Board-exam focus for Classes 10 and 12 (emphasis on what the exam asks).
