# Photo Metadata Augmentation — Side-car Database (design note)

**Status:** design/scoping, nothing built. This note seeds a dedicated chat.
It captures (a) the decision to build a UUID-keyed side-car store that augments
Immich's data without writing back into it, and (b) the GPU-model candidates
for *producing* that augmentation data, carried over from the "Local GPU for
image labeling augmentation" discussion.

---

## Why this exists

The tool-calling search agent (see the main README) works well on data Immich
already provides — CLIP scene search, faces, EXIF city/state/country. Two kinds
of gap remain, and both point at the same solution:

1. **Data Immich has but doesn't populate reliably.** Photos with GPS
   coordinates but no reverse-geocoded `city` (manual location edits, and
   family-uploaded photos that never got clean geocoding) are invisible to
   place search, which keys off the `city` text field. Discovered concretely:
   4 Disney World photos with correct lat/long but null city returned nothing
   for "Florida."
2. **Structured facts CLIP can't give reliably.** "Is Kevin alone in frame"
   (person count), object/animal/vehicle counts, scene tags — CLIP is a
   holistic embedding and can't be trusted for counts or exclusivity. The SQL
   agent tool can *express* these queries, but only if the underlying facts
   exist somewhere queryable.

Both are the same shape: **per-photo facts that should be computed once and
stored somewhere the search agent can query.** That store is the side-car.

## Core design decisions (agreed in the search-agent chat)

- **Key everything on the Immich asset UUID.** Photos move, get re-organized,
  and enter uncontrolled from a shared Dropbox folder. The asset UUID is the
  one stable identifier that survives moves and Immich upgrades. Every
  augmentation row references it.
- **Side-car, not write-back.** Do NOT write augmentation data into Immich's
  own `asset_exif`/schema. Two reasons:
  - Immich's `lockedProperties` system deliberately protects manually-edited
    fields from being overwritten by re-extraction — so writing back is both
    fragile and can be silently blocked (this is exactly why manual coordinate
    edits don't get re-geocoded).
  - `db.py` already warns Immich's schema is version-dependent and unstable.
    A separate store owned by us is insulated from Immich upgrades.
- **Open-ended by design.** The goal is not one feature but a framework: many
  future tools, each contributing a different kind of per-photo fact, all keyed
  by UUID. Schema should accommodate new fact-types without migration churn
  (e.g. a typed key/value or per-tool table pattern, TBD in the new chat).
- **Feeds the existing agent.** Augmentation data becomes queryable by
  `run_readonly_sql` (and potentially new structured `search_photos` filters),
  so the agent gains real structured facts instead of inferring frame contents
  indirectly. This is the natural extension of why the raw-SQL tool exists.

## Reverse-geocoding gap — the immediate motivating case

Immich reverse-geocodes only during EXIF extraction, using the GeoNames data in
its own Postgres (`geodata_places`, ~227k rows on this instance; no PostGIS).
Manual coordinate edits never retrigger it, so coordinates-with-no-city photos
stay unreachable by place search.

Options considered (decide in the new chat, ideally after measuring the real-
data gap — on sample data it was only 2 of 15 coords-bearing photos):

- **A — reuse Immich's own geocoder via its API.** Immich exposes
  `GET /map/reverse-geocode?lat=&lon=`, which runs Immich's real matcher
  (population-weighted, the same one that labels everything else). Call it for
  any coords-without-city photo and store the result in the side-car. Avoids
  reimplementing geocoding AND avoids the `lockedProperties` write-back fight,
  since we store our own copy rather than pushing into Immich.
- **B — nearest-neighbor against `geodata_places` in SQL.** Feasible at 227k
  rows without PostGIS (a per-query math scan is cheap), but re-derives what
  Immich already does, more crudely (raw distance ignores the population
  weighting), and can disagree with Immich's own labels on the same coordinate.
- **C — bounding-box region filter at query time.** Crude, box-shaped, but
  needs no reference join. A pragmatic 80%, not a real fix.

Leaning **A**: it's the cleanest, reuses the correct matcher, and fits the
side-car (store `resolved_city/state/country` per UUID; the search agent's
place resolution then also consults the side-car, not just `asset_exif.city`).

## GPU-model candidates for producing augmentation data

Carried over from the "Local GPU for image labeling augmentation" chat. The
organizing principle there: **batch vs. real-time.** Query-time work needs low
latency (why the agent uses the Claude API, not a local LLM). Ingest-time
enrichment has no deadline, so the slow-but-free GTX 1060 (6GB, shared with
`immich-machine-learning` on the `gpu-ml` box) is well-suited — grind the
backfill queue overnight, once per photo, forever.

Priority order and findings:

1. **Object detection — person counts + broader.** The strongest candidate: it
   resolves the named "Kevin alone in frame" case CLIP can't, and generalizes
   to vehicle/animal/object counts and compound queries ("dogs and no people").
   - **YOLO-World** favored — open-vocabulary, prompt-driven classes, so it
     matches how CLIP search already handles arbitrary text rather than being
     locked to COCO's ~80 classes. Closed-vocabulary detectors (YOLO26-N/S,
     RF-DETR) are faster/lighter but a step backward for open-ended queries;
     fine only if common categories suffice.
   - Stored as e.g. `person_count`, plus detected-class counts, per UUID.
2. **OCR — already handled by Immich, do NOT rebuild.** Immich 2.2 added
   built-in OCR (auto on new uploads; English, Simplified/Traditional Chinese,
   Japanese), indexed in Immich's own search. Building our own would be a
   redundant second GPU pass. **Open research task, not a build:** check whether
   Immich's OCR text is queryable via Postgres/API so the SQL agent tool can
   reach it — if not, surfacing it (or copying it into the side-car) is the only
   OCR work worth doing. Also confirm the deployed Immich version actually
   includes 2.2.
3. **Landmark model — DELF/DELG as a second embedding source.** Google's
   open-sourced landmark retrieval model (attentive local descriptors;
   embedding + nearest-neighbor, same shape as the current `match.py`).
   - Caveat: DELF/DELG covers ~30k famous global landmarks (Eiffel-tier). It
     does NOT help the actual bottleneck — vernacular family landmarks (WDW
     attractions, local restaurants), for which the hand-labeled CLIP-embedding
     approach is already correct. So DELF/DELG is a *layered second source* for
     famous landmarks, not a replacement for the curated set.
4. **Dense captioning (BLIP-2 / moondream / small LLaVA) — lowest priority.**
   Generates a per-photo text description to store and full-text-search or feed
   the SQL tool as another column. Most GPU-hungry of the four, and CLIP already
   covers scene search reasonably — probably not worth the batch time unless a
   concrete need appears.

VRAM note: the 1060 has 6GB shared with Immich's ML, which spikes during
embedding backfills. Any batch model must fit alongside that or be scheduled to
avoid overlap (same contention caveat as the retired Ollama service). This is a
real constraint on model choice and concurrency.

## First steps for the new chat

1. **Measure the real gap.** On the real (non-sample) library, how many photos
   have coordinates but no city? That sizes whether reverse-geocode enrichment
   (option A) is worth building now. Also: `count(*)` of photos total, to gauge
   overnight batch feasibility.
2. **Design the side-car schema.** UUID-keyed, open-ended for many fact-types,
   insulated from Immich's schema. Decide per-tool tables vs. typed key/value.
   Decide where it lives (its own Postgres DB? a table in a separate DB? not
   Immich's DB).
3. **Design the ingest/enrichment pipeline.** How new photos (and manual edits)
   get picked up, queued, processed on the `gpu-ml` box, and written to the
   side-car. Batch, idempotent, resumable — mirror the lessons from the search
   work (graceful degradation, structured logging, verify-before-trust).
4. **Wire the side-car into the search agent.** Extend `run_readonly_sql`'s
   readable allowlist (or add structured filters) so augmentation facts are
   queryable. Re-run the relevant parts of the manual test batch.
5. **Pick the first enrichment to build** — reverse-geocode (A) and/or object
   detection (person counts), the two with the clearest, already-motivated
   payoff.

## Pointers into existing code/docs

- Main `photo-search/README.md` — full project snapshot, the search-agent design.
- `search-api/sql_tool.py` — the read-only SQL tool + dedicated Postgres role;
  the model for how the agent would query the side-car safely.
- `search-api/tools.py` — `search_photos` filters (people/cities match modes);
  where structured augmentation filters could be added.
- `gpu-ml` repo — the shared GPU box; where batch enrichment services would run
  alongside `immich-machine-learning` (Ollama there is now retired/dead-ended).
- `search-api/landmark/` — the existing curated CLIP-embedding landmark matcher
  that DELF/DELG would layer onto, not replace.
