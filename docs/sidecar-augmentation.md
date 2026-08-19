# Photo Metadata Augmentation — Side-car Database (design note)

**Status (2026-08):** core infrastructure built and proven on the dev test
set. Three enrichment tools working end-to-end: reverse-geocoding (two
sources), object detection, and a generic reusable GPU inference protocol.
Landmark matching (dual-source: visual + geospatial proximity) is designed,
with the visual model now chosen (DINOv3 — see below), but not yet built —
next up. This note is the living record of what's built, why, and what's
next; update it as things change rather than letting chat history be the
only record.

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

## Core design decisions (agreed early, still holding)

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
  by UUID.
- **Feeds the existing agent.** Augmentation data becomes queryable by
  `run_readonly_sql` (and potentially new structured `search_photos` filters),
  so the agent gains real structured facts instead of inferring frame contents
  indirectly. **Not yet done** — see "Next steps."

## Implementation status (2026-08)

What's actually built and proven, mapped to real files:

| Enrichment | File(s) | Status | Notes |
|---|---|---|---|
| Reverse-geocode (Immich's own geocoder) | `sidecar/enrichment/reverse_geocode.py` | Working, tested full test_set | `source='immich_reverse_geocode'` |
| Reverse-geocode (Overture Divisions, richer/county-level) | `sidecar/enrichment/overture_geocode.py` | Working, tested full test_set | `source='overture_divisions'`; chains off the first — only runs on photos still unresolved |
| Object detection (YOLO-World) | `sidecar/enrichment/object_detect.py` + `gpu-ml/inference-service/tasks/object_detect.py` | Working, tested full test_set | 106-term open vocabulary, see `sidecar/config.py` |
| Landmark matching (visual + proximity) | — | Designed, model chosen, not built | see "Landmark matching" section below |

Supporting infrastructure built along the way:

- **`sidecar/` is a real Python package** (`sidecar/__init__.py`), with all
  internal imports relative (`from . import config`, `from .. import db`).
  Required after a real bug: a bare `import config`/`import db` inside
  `sidecar/` silently resolved to `search-api`'s own `config.py`/`db.py`
  instead, because of how the container's Python path was set up.
- **`--scope test|full` on every enrichment entry point**, defaulting to
  `test`. Running against the full library is always an explicit, deliberate
  choice — never a silent default. `sidecar/test_set.py` +
  `sidecar/populate_test_set.py` manage the pinned ~100-photo set + hand-picked
  hard cases.
- **`sidecar_dev` database is live**, migration applied, `county` column added
  to `resolved_geo` after real Kentucky/Alaska test data showed "county but no
  city" is a common, real, search-worthy case for rural/unincorporated areas —
  not an edge case to drop.
- **A generic, reusable GPU inference protocol on `gpu-ml`**
  (`gpu-ml/inference-service/`): a task-registry pattern (`POST
  /v1/infer/<task>`, `GET /v1/tasks`, `GET /health`) so new models register as
  new tasks, not new services. Deliberately decoupled from Immich — callers
  send raw image bytes, not asset IDs, so the service stays reusable across
  projects. `object_detect` is the first registered task; audio-to-text
  (see "Future enrichment candidates") is a strong second candidate for
  proving this out further.
- **`psycopg2.connect(**kwargs)`, never a DSN string**, everywhere in
  `sidecar/`. The real Postgres password contains `%` and `!`, which broke a
  plain DSN string (`postgresql://user:pass@host/db`) on first real
  connection attempt. Keyword-argument connection avoids the whole class of
  bug permanently.
- **`::uuid[]` explicit casts** on every `= ANY(%s)` query against a
  Python list of UUIDs — psycopg2's array adaptation doesn't reliably
  produce a `uuid[]` array on its own, causing a live `uuid = text` type
  error otherwise.

## OCR — resolved, no build needed (2026-08)

Original open question: is Immich's OCR text (added in Immich 2.2) queryable
so the SQL agent could reach it? **Confirmed: yes, no sidecar work required.**
OCR text is stored in a real, normal Postgres column —
`asset_exif.ocrText` — filterable via plain string/full-text matching,
already combined into Immich's own smart-search ranking alongside CLIP
similarity (confirmed via Immich's architecture docs, and empirically: a
"Scotland" search surfaced real OCR text matches *and* separately CLIP's own
well-documented text-sensitivity/visual-pattern matches, both genuinely
present, not one explaining the other).

**Remaining task, not sidecar work:** confirm `asset_exif.ocrText` is included
in `search-api/sql_tool.py`'s readable column allowlist so the SQL agent can
actually query it. A `search-api` check, separate from anything in `sidecar/`.

## Landmark matching — dual-source design (2026-08)

Two genuinely complementary sources, not primary+fallback — a future search
query should consult both, not prefer one:

1. **Visual recognition** — an ML model looking at the photo itself. Catches
   a landmark that dominates the frame even when the photo has no useful
   GPS data nearby (e.g. one photo from a trip where most others weren't
   geotagged).
2. **Geospatial proximity** — nearby named points of interest from map data,
   regardless of what's actually visible in the frame. Catches:
   - Photos taken *near* a landmark where the landmark itself isn't in frame
     at all (standing at its base, camera pointed at your kids).
   - **Lesser-known landmarks a visual model was never trained/prompted on**
     — proximity has no vocabulary ceiling the way a visual model does.
   - **Tightly-cropped photos** where part of a landmark is technically
     visible but there's too little context for a visual model to recognize
     it confidently.

   These last two are broader value than originally framed (not just "the
   landmark is literally absent from the photo") — worth stating explicitly
   since it changes how a future agent query should treat the two sources:
   query both and union/rank, don't treat proximity as merely a fallback.

**Proximity component — reuses proven infrastructure.** Structurally the same
shape as `overture_geocode.py`, against Overture's separate **Places theme**
(points of interest with coordinates/categories/names), not the Divisions
theme already used for geocoding. Same batch/`enrichment_status`/idempotency
pattern. Before writing real code: spike the Places theme schema the same
way `spike_overture_schema.py` did for Divisions — the last two real bugs in
this project both came from unverified table/column-name assumptions, worth
continuing that discipline rather than guessing.

**Visual component — model chosen (2026-08): DINOv3 (Meta), not DELF/DELG.**
Research findings:
- DELF/DELG is an older (2017-2020) Google/TensorFlow release. Current
  academic SOTA for this exact task (CVNet, AMES, reranking transformers)
  pushes benchmark scores further but adds real engineering complexity —
  sparse local-descriptor extraction, cross-image reranking pipelines — that
  makes sense at "millions of product images" scale, not a personal photo
  library's landmark set.
- **DINOv3** (Meta, Aug 2025) is directly benchmarked on this task via plain
  non-parametric retrieval (embed a query image, rank a reference set by
  cosine similarity) against the standard Oxford/Paris landmark-retrieval
  benchmarks, and "achieves the strongest performance by large margins" over
  DINOv2 and other baselines — and DINOv2 itself already significantly
  outperforms older baselines on the same benchmarks. PyTorch-native, no
  TensorFlow dependency, standard HuggingFace/PyTorch install — fits the
  `inference-service` task registry cleanly, same pattern as
  `object_detect.py`.
- **This is architecturally the SAME pattern already in
  `search-api/landmark/match.py`** (embed + nearest-neighbor against a
  curated reference set, currently using CLIP embeddings for vernacular
  family landmarks) — DINOv3 slots in as a stronger backbone for the same
  architecture, not a new system to learn or maintain.
- **DINOv2 is the fallback** if DINOv3's licensing/self-hosted availability
  turns out to be awkward — UNVERIFIED, not yet checked; DINOv2 is more
  battle-tested and still clearly outperforms pre-2023 approaches.

**Real open question before building, not yet answered:** where does the
reference embedding set for "famous landmarks" come from? Google Landmarks
Dataset v2 (5M images, 200k labels, Wikimedia Commons-sourced) is the
standard academic source, but is almost certainly overkill for what would
realistically appear in a family library — a curated few hundred/thousand
iconic landmarks is probably the right scope. Decide deliberately before
building; don't default to "grab the biggest available dataset."

**Schema gap this surfaces:** `landmark_matches` (see
`migrations/001_initial_schema.sql`) has no `source` column — it was
designed before two genuinely different provenances (visual vs. proximity)
were on the table. Needs adding, same reasoning as `resolved_geo.county`.

## Schema evolution tooling (2026-08)

Manually typing `ALTER TABLE ... ADD COLUMN` into `psql` by hand each time a
schema needs to evolve (as happened for `resolved_geo.county`) doesn't scale
and isn't portable — a real gap flagged directly. Postgres already makes the
idempotent version easy (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT
EXISTS`); the fix is just wrapping these in small reusable helpers in
`sidecar/db.py` (`ensure_column(table, column, coltype_sql)`,
`ensure_table(name, create_sql)`) that any enrichment module can call before
running. `migrations/001_initial_schema.sql` stays the source of truth for a
*fresh* database; ongoing evolution becomes self-healing instead of a manual
step to remember. **Not yet built** — first real use will be adding
`landmark_matches.source`.

## Future enrichment candidates (2026-08)

Two accepted for the roadmap (not yet built — after the current landmark-matching
work, which is next):

- **Audio-to-text for videos** — model chosen: **`faster-whisper`** (an
  optimized reimplementation of OpenAI's Whisper, ~4x faster with lower
  memory than vanilla Whisper, MIT-licensed, fully self-hosted, no ongoing
  API cost). Considered and rejected: NVIDIA Canary/Parakeet (better on some
  benchmarks, but pull in the heavier NeMo toolkit for no clear payoff at
  this scale) and managed APIs (Deepgram, AssemblyAI, Groq-hosted Whisper —
  all send data externally and cost per-minute, inconsistent with this
  project's self-hosted ethos). Batch/offline fits this project's existing
  "no deadline, run overnight" pattern — no need for the streaming/real-time
  capability those alternatives compete on. Strong fit for the
  `inference-service` task-registry pattern (a new task, same protocol) —
  good candidate to prove the registry's reusability beyond `object_detect`.
- **Scene/relationship captioning** — model chosen: **Florence-2**
  (Microsoft, MIT license). Fills a real, distinct gap: `object_counts`
  (YOLO-World) answers *what* is in a photo and *how many*, but not
  relationships, actions, or context ("kids building a sandcastle" vs. a
  disconnected `person`/`sand`/`bucket` list) — something CLIP's holistic
  embedding also doesn't reliably surface.
  - **Explicitly does NOT replace YOLO-World** — researched and confirmed
    2026-08: the two excel at genuinely different things. YOLO-World is a
    dedicated, purpose-built detector optimized for fast, efficient
    per-class counting across a batch (its whole existing job); Florence-2
    is a general multi-task VLM whose real strength is language generation.
    An independent comparison of these exact models for production
    deployment states it directly: "YOLO-World's speed... Florence-2's
    language generation" — different strengths, not competing for the same
    one. For this project's actual workload (batch-processing potentially
    thousands of photos overnight on a shared 6GB GPU), a lighter
    purpose-built detector doing one pass per image is also the better
    throughput fit than a heavier general VLM doing double duty.
  - Same complementary-sources pattern as landmark matching (visual +
    proximity) — multiple distinct enrichments each contributing a
    different kind of fact, not one enrichment superseding another.
  - Minor, non-blocking note: YOLO-World inherits Ultralytics' GPL-3.0
    license (mainly a concern for redistributing a proprietary product, not
    for this self-hosted personal tool); Florence-2 is MIT.
- *(Add more here as they come up, rather than letting them live only in
  chat history.)*

## GPU/VRAM constraint (still holds)

The gpu-ml box's GTX 1060 has 6GB VRAM shared across `immich-machine-learning`,
`ollama`, and now `inference-service`. Confirmed working for `object_detect`
(YOLO-World small variant, single worker, lazy model loading) — see
`gpu-ml/README.md`'s VRAM contention note. Any new task (visual landmark
model, audio-to-text) needs to fit within this same shared budget or be
scheduled to avoid overlap; not yet stress-tested under simultaneous load
from multiple services.

## Process & infrastructure decisions (2026-07, still holding)

- **Two containers.** `search-api` (prod) stays completely sidecar-blind —
  no dependency on the sidecar code or DB. `search-api-dev` (same
  image/codebase, different config + a superset build via
  `sidecar/Dockerfile.dev`) is where sidecar integration is built and tested.
- **Sidecar databases: separate Postgres databases, both dev and prod, on
  the same Postgres instance as Immich's own DB.**
  - **Dev (`sidecar_dev`):** no backup. Wipe-and-redevelop freely — live now,
    populated with real test data.
  - **Prod (`sidecar_prod`, not yet built):** will need its own backup
    mechanism.
- **Repo layout.** `sidecar/` is a top-level folder, sibling to `search-api/`.
  `gpu-ml` is its own separate repo, one device serving multiple projects.
- **Schema shape.** Per-tool typed tables, not EAV — proven correct in
  practice across three real enrichment tools now.
- **UUID stability caveat.** Immich UUIDs are not move-proof. Policy:
  reaugment under the new UUID when it appears; dead duplicates cleaned up
  via Immich's own "Remove offline files" job.
- **Dev test set.** Fixed, pinned ~100-photo sample + hand-picked hard cases
  in `sidecar.test_set` — live now, includes the original 5 geocode hard
  cases (2 resolved, 3 correctly-null-in-wilderness) plus a multi-face photo.

## Next steps (agreed order, 2026-08)

1. ~~Update this design doc~~ **Done.**
2. ~~Research current visual-landmark-recognition options~~ **Done — DINOv3
   chosen** (see "Landmark matching" above).
3. **Build schema evolution tooling** (`ensure_column`/`ensure_table` in
   `sidecar/db.py`).
4. **Add `landmark_matches.source`** using the new tooling — first real
   dogfood use of it.
5. **Spike Overture's Places theme schema** (mirrors
   `spike_overture_schema.py`'s approach for Divisions) — don't guess column
   names given the project's track record on this.
6. **Build `overture_landmarks.py`** (proximity matching) — reuses proven
   `overture_geocode.py`-shaped infrastructure.
7. **Build the visual landmark-matching task** (DINOv3) — including deciding
   the reference-landmark-set source (see open question above).
8. **Wire the side-car into the search agent** — still not started. Extend
   `run_readonly_sql`'s readable allowlist (or add structured filters) so
   `resolved_geo`/`object_counts`/`landmark_matches` are queryable. This was
   explicitly deferred until the enrichment tools themselves were proven —
   that's now true for two of three planned tables.
9. **Run the full-library pass** (`--scope full`) for whichever enrichments
   are trusted — deferred until after the search agent can actually use the
   data, so there's a real payoff to point at before spending the batch time.

## Pointers into existing code/docs

- Main `photo-search/README.md` — full project snapshot, the search-agent design.
- `search-api/sql_tool.py` — the read-only SQL tool + dedicated Postgres role;
  the model for how the agent will query the side-car once wired in.
- `search-api/tools.py` — `search_photos` filters (people/cities match modes);
  where structured augmentation filters could be added.
- `search-api/landmark/` — the existing curated CLIP-embedding landmark
  matcher that a visual landmark-matching task would layer onto, not replace.
- `gpu-ml/` — the shared GPU device (own repo). `gpu-ml/inference-service/` —
  the generic task-registry inference protocol; `tasks/object_detect.py` is
  the reference implementation for adding a new task (e.g. visual landmark
  matching via DINOv3, audio-to-text).
- `sidecar/` — the side-car codebase: `migrations/`, `db.py`, `config.py`,
  `test_set.py`, `populate_test_set.py`, `enrichment/` (`reverse_geocode.py`,
  `overture_geocode.py`, `object_detect.py`), `run_*.py` entry points,
  `spike_overture_schema.py` (schema-verification pattern to reuse for
  Overture's Places theme).
