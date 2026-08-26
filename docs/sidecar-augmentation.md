# Photo Metadata Augmentation — Side-car Database (design note)

**Status (2026-08, latest):** four enrichment tools built and proven on the
dev test set (reverse-geocode x2, object detection, landmark proximity).
The DINOv3 visual-landmark-matching pipeline is deep in progress: model
access granted, full reference dataset downloaded AND embedded (1.58M
images), generic embedding endpoint built and tested on gpu-ml. The one
remaining piece — the actual matching endpoint — is blocked on one open
infrastructure decision (see "Landmark matching — DINOv3 implementation
progress" below) that needs to be resolved at the start of the next
session before writing more code. This note is the living record of
what's built, why, and what's next; update it as things change rather
than letting chat history be the only record.

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

## Implementation status (2026-08, latest)

What's actually built and proven, mapped to real files:

| Enrichment | File(s) | Status | Notes |
|---|---|---|---|
| Reverse-geocode (Immich's own geocoder) | `sidecar/enrichment/reverse_geocode.py` | Working, tested full test_set | `source='immich_reverse_geocode'` |
| Reverse-geocode (Overture Divisions, richer/county-level) | `sidecar/enrichment/overture_geocode.py` | Working, tested full test_set | `source='overture_divisions'`; chains off the first — only runs on photos still unresolved |
| Object detection (YOLO-World) | `sidecar/enrichment/object_detect.py` + `gpu-ml/inference-service/tasks/object_detect.py` | Working, tested full test_set | 106-term open vocabulary, see `sidecar/config.py` |
| Landmark matching, proximity (Overture Places) | `sidecar/enrichment/overture_landmarks.py` | Working, tested full test_set (v2 category filter) | `source='overture_places'`; residential-building noise partially filtered — see design doc history for the `landmark_and_historical_building` taxonomy caveat |
| Landmark matching, visual (DINOv3) | `sidecar/enrichment/dinov3_landmarks.py` | **NOT YET BUILT** — see detailed status below | Generic embedding piece done; matching + sidecar client remain |

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
  to `resolved_geo`, `source` + `distance_meters` columns added to
  `landmark_matches` (via the new schema-evolution tooling, see below).
- **`sidecar/db.py` has `ensure_column()`/`ensure_table()`** (idempotent
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS`
  wrappers) and **`sidecar/ensure_schema.py`** declares actual schema
  evolutions to apply — run via `python -m sidecar.ensure_schema`. Fixes the
  "typing ALTER TABLE into psql by hand" gap. Add new evolutions there as the
  schema grows.
- **A generic, reusable GPU inference protocol on `gpu-ml`**
  (`gpu-ml/inference-service/`): a task-registry pattern (`POST
  /v1/infer/<task>`, `GET /v1/tasks`, `GET /health`) so new models register as
  new tasks, not new services. Deliberately decoupled from Immich — callers
  send raw image bytes, not asset IDs, so the service stays reusable across
  projects. Registered tasks: `object_detect` (YOLO-World), `embed_image`
  (DINOv3, generic — see below). Audio-to-text (`faster-whisper`) and scene
  captioning (Florence-2) are accepted future candidates, not yet built.
- **`psycopg2.connect(**kwargs)`, never a DSN string**, everywhere in
  `sidecar/`. The real Postgres password contains `%` and `!`, which broke a
  plain DSN string (`postgresql://user:pass@host/db`) on first real
  connection attempt. Keyword-argument connection avoids the whole class of
  bug permanently.
- **`::uuid[]` explicit casts** on every `= ANY(%s)` query against a
  Python list of UUIDs — psycopg2's array adaptation doesn't reliably
  produce a `uuid[]` array on its own, causing a live `uuid = text` type
  error otherwise.
- **DuckDB queries against Python-interpolated float literals need explicit
  `::DOUBLE` casts** — DuckDB infers a fixed-precision DECIMAL type from a
  literal's exact digit count otherwise, which can overflow unpredictably
  depending on how many decimal places a computed value (e.g. a
  latitude-adjusted bbox margin) happens to have. Broke both `overture_geocode.py`
  and `overture_landmarks.py` before the explicit-cast fix.

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

## Landmark matching — proximity component (DONE, 2026-08)

`sidecar/enrichment/overture_landmarks.py`, built and proven against the
full test set. Queries Overture's **Places** theme (not Divisions, which
`overture_geocode.py` uses), bbox-prefiltered + exact Haversine distance
within `MAX_DISTANCE_METERS` (500m, unvalidated starting guess).

**Category filtering** (which Places rows count as "landmarks"):
`taxonomy.primary` checked first (Overture's own docs describe `taxonomy` as
the fix for the older `basic_category` field's inconsistencies —
`basic_category` used only as a secondary fallback). CONFIRMED-real
taxonomy values (from live Disney-area data): `amusement_park`,
`amusement_attraction`, `museum`, `castle`, `mountain`, `island`,
`public_fountain`, `historic_site`, `landmark_and_historical_building`,
`marina`, `beach`.

**Known noise issue, partially fixed:** `landmark_and_historical_building`
turned out to match many ordinary named apartment/condo buildings, not just
real landmarks. `RESIDENTIAL_NAME_KEYWORDS` (a name-substring exclusion —
apartments/condos/lofts/flats/manor/townhomes/residences) catches most of
this real-data noise but not all of it (`Vue at 3rd`, `Madrid Building`
still slip through) — a heuristic, not a structural fix.
`CATEGORY_FILTER_VERSION = "landmark-categories-v2"` tracks this state;
bump it if the filter is refined further.

**Also fixed:** `categories` (the old Overture Places property) is
deprecated and due for removal in the release after the one this project
pins (`2026-07-22.0`) — built against `taxonomy`/`basic_category` from the
start, not the deprecated field.

## Landmark matching — DINOv3 implementation progress (2026-08)

**Design (still holding):** visual recognition (DINOv3) and geospatial
proximity (`overture_landmarks.py`, done above) are genuinely complementary,
not primary+fallback — catches landmarks a photo has no useful GPS for,
lesser-known landmarks outside any fixed vocabulary, and tightly-cropped
photos. A future search-agent query should consult both sources, not prefer
one.

**Model: DINOv3 ViT-S+** (`facebook/dinov3-vits16plus-pretrain-lvd1689m`,
~29M params) — chosen over DELF/DELG (older, TensorFlow) after live
benchmark research; PyTorch-native, fits the `inference-service` task
registry. **Gated model** — required a HuggingFace license application
(approved this session, turnaround was about a day).

**Reference dataset: GLDv2-clean, full set, no compromise on quality.**
Explicitly chosen over a small hand-curated list after discussion — "as
open and agnostic as possible" was the stated priority, and GLDv2-clean
(the noise-filtered subset researchers actually use for this task, not the
noisier raw 5M/200k-label full set) is the right *quality* choice too, not
just a scope compromise. **1,580,470 images, 81,313 landmarks** — confirmed
exact match against the dataset's own documented stats after both the
download and the embedding run completed with zero discrepancy.

**What's actually built and working, in order:**

1. ✅ **`gpu-ml/landmark-reference/download_gldv2_clean.py`** — downloads
   GLDv2's raw `train` shards (500 tars, ~500GB total transfer) and
   selectively extracts only the ~1.58M clean-subset images, streaming one
   shard at a time (never holding the full 500GB on disk at once), resumable
   via `.done` markers per shard. **RUN AND COMPLETE.** Output:
   `/media/sdb1/gldv2-clean/images/*.jpg` + `manifest.csv` (image_id,
   landmark_id, local_path) on the gpu-ml host (NOT in Docker — gpu-ml also
   serves as the household NAS, `/media/sdb1` is one of its drives, chosen
   for available space at decision time).
2. ✅ **`gpu-ml/inference-service/tasks/embed_image.py`** — generic,
   NOT landmark-specific image-embedding HTTP task (`POST
   /v1/infer/embed_image`), registered in `tasks/__init__.py`. Requires
   `HF_TOKEN` (HuggingFace access token, gated-model download) as an env var
   on the container, wired via `docker-compose.yml`. **BUILT, TESTED LIVE**
   — a real curl call returned a genuine 384-dim embedding vector. Refactored
   this session to expose a reusable `.embed(image) -> np.ndarray` method
   (not just the HTTP-facing `.infer()`) specifically so a future matching
   task can share this same loaded model instance rather than loading a
   second copy of DINOv3 into the shared 6GB VRAM budget.
3. ✅ **`gpu-ml/landmark-reference/embed_reference_set.py`** — bulk-embeds
   all 1.58M reference images, batched (64 images per GPU forward pass, not
   one HTTP call per image — the right tool for a one-time job at this
   scale, chosen deliberately over reusing the per-image HTTP task for this
   step). Chunked (50,000 images/chunk, ~32 chunks) + resumable, same
   `.done`-marker pattern as the download script. **RUN AND COMPLETE** —
   `using CUDA` confirmed in the log (not a CPU fallback), all 1,580,470
   images embedded with zero drops. Output: `chunk_NNNN.npy` (embeddings) +
   `chunk_NNNN.csv` (image_id, landmark_id) pairs at
   `/media/sdb1/gldv2-clean/embeddings/` on the gpu-ml host — **~2.4GB
   total, NOT yet loaded anywhere for actual matching.**
   - Real dependency gap hit and fixed along the way: `transformers`'
     `AutoImageProcessor` needs `torchvision` (not just `torch`) as a
     backend — wasn't in this standalone venv's `requirements.txt` (the
     Docker container never hit this because `ultralytics`, installed there
     for `object_detect`, pulls `torchvision` in transitively). Pinned
     `torchvision==0.20.1` per PyTorch's own official compatibility table
     for `torch==2.5.1`.
4. ❌ **NOT YET BUILT — the matching task itself.** Was about to be built
   this session (a `match_landmark` task: takes a query image, embeds it via
   the *shared* `EmbedImageTask.embed()`, computes cosine similarity against
   the loaded reference matrix, returns top-k landmark matches) when a real,
   **unresolved infrastructure question came up — START HERE next
   session:**

   **OPEN QUESTION, blocking further work:** the reference embeddings live
   on gpu-ml's host filesystem (`/media/sdb1/gldv2-clean/embeddings/`,
   computed outside Docker). The `inference-service` container currently has
   no way to see that path — a straightforward fix would be a read-only
   Docker bind mount (`- /media/sdb1/gldv2-clean/embeddings:/reference-embeddings:ro`
   in `docker-compose.yml`), but the person flagged this as conflicting with
   their network's build philosophy and asked to move the embeddings to "a
   NAS folder instead." **Not yet clarified:** whether this means (a) a
   different specific path on one of gpu-ml's existing NAS drives (still a
   host bind mount, just relocated), or (b) accessing the data via a proper
   network-storage mechanism instead of a raw host bind mount — this
   project's `docker-compose.yml` already has a precedent for that exact
   distinction: `photo-search/docker-compose.yml`'s `immich_upload` volume
   uses an explicit NFS-backed Docker volume
   (`driver_opts: {type: nfs, device: ..., addr: ...}`), not a plain bind
   mount, specifically because it's NAS-hosted data. **First step next
   session: ask which of these (or something else) is meant before writing
   the Docker/volume config for the matching task.**

   Also still open, deferred until the matching task is actually built:
   - **Landmark ID → name mapping.** GLDv2 only labels images with a numeric
     `landmark_id`; there's no clean name in the dataset itself. Confirmed
     via the dataset's own repo: `train_label_to_category.csv`
     (`https://s3.amazonaws.com/google-landmark/metadata/train_label_to_category.csv`,
     landmark_id → a Wikimedia Commons category URL) is the real source —
     not yet downloaded/parsed. Plan: derive a rough display name from the
     URL's trailing path segment (e.g. `.../Category:Eiffel_Tower` →
     "Eiffel Tower") — a commonly-used approach for this exact dataset, but
     a rough parse, not a curated name; expect some odd-looking results.
   - **Similarity threshold and top-k.** No empirical calibration yet for
     what cosine-similarity score should count as "a real match" for this
     model/dataset — same "unvalidated starting guess, revisit with real
     data" situation as `overture_landmarks.py`'s `MIN_CONFIDENCE`/
     `MAX_DISTANCE_METERS` were before real testing.
   - **`sidecar/enrichment/dinov3_landmarks.py`** (the actual sidecar-side
     enrichment client) hasn't been started at all yet. Candidate scope,
     already agreed: always include no-coordinate photos (untouched by
     `overture_landmarks`) + coordinate photos where `overture_landmarks`
     found zero matches; deprioritize/skip photos that already got a
     proximity match.

**Schema:** `landmark_matches.source` and `.distance_meters` columns exist
(added via `ensure_schema.py`, see above) — `distance_meters` will be `NULL`
for `source='dinov3_visual'` rows, same pattern as `resolved_geo.county`
being `NULL` for the `immich_reverse_geocode` source.

## Schema evolution tooling (DONE, 2026-08)

`sidecar/db.py`'s `ensure_column()`/`ensure_table()` + `sidecar/ensure_schema.py`
— see "Implementation status" above. Already dogfooded twice (`landmark_matches.source`,
then `.distance_meters`).

## Future enrichment candidates (2026-08)

Two accepted for the roadmap, not yet built (after DINOv3 landmark matching
is finished):

- **Audio-to-text for videos** — model chosen: **`faster-whisper`** (an
  optimized reimplementation of OpenAI's Whisper, ~4x faster with lower
  memory than vanilla Whisper, MIT-licensed, fully self-hosted, no ongoing
  API cost). Considered and rejected: NVIDIA Canary/Parakeet (better on some
  benchmarks, but pull in the heavier NeMo toolkit for no clear payoff at
  this scale) and managed APIs (Deepgram, AssemblyAI, Groq-hosted Whisper —
  all send data externally and cost per-minute, inconsistent with this
  project's self-hosted ethos). Strong fit for the `inference-service`
  task-registry pattern (a new task, same protocol) — good candidate to
  prove the registry's reusability beyond `object_detect`/`embed_image`.
- **Scene/relationship captioning** — model chosen: **Florence-2**
  (Microsoft, MIT license). Fills a real, distinct gap: `object_counts`
  (YOLO-World) answers *what* is in a photo and *how many*, but not
  relationships, actions, or context ("kids building a sandcastle" vs. a
  disconnected `person`/`sand`/`bucket` list).
  - **Explicitly does NOT replace YOLO-World** — researched and confirmed
    2026-08: YOLO-World is a dedicated, purpose-built detector optimized for
    fast, efficient per-class counting (its whole existing job); Florence-2
    is a general multi-task VLM whose real strength is language generation.
    Same complementary-sources pattern as landmark matching (visual +
    proximity) — multiple distinct enrichments each contributing a
    different kind of fact, not one enrichment superseding another.
  - Minor, non-blocking note: YOLO-World inherits Ultralytics' GPL-3.0
    license (mainly a concern for redistributing a proprietary product, not
    for this self-hosted personal tool); Florence-2 is MIT.
- *(Add more here as they come up, rather than letting them live only in
  chat history.)*

## GPU/VRAM constraint (still holds)

The gpu-ml box's GTX 1060 has 6GB VRAM shared across `immich-machine-learning`,
`ollama`, and now `inference-service` (which itself hosts both YOLO-World and
DINOv3 as separate tasks in one process). Confirmed working for `object_detect`
and `embed_image` individually (single worker, lazy model loading per task —
see `gpu-ml/README.md`'s VRAM contention note). **Not yet tested: both
YOLO-World and DINOv3 loaded simultaneously in the same container under real
concurrent load** — worth watching once the matching task is live and both
tasks might realistically be invoked close together in time.

## Process & infrastructure decisions (2026-07/08, still holding)

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
  `gpu-ml` is its own separate repo, one device serving multiple projects —
  and, as of this session, also confirmed to be the household NAS (multiple
  large drives mounted at `/media/*`), which matters for where large
  datasets/models should live and how containers should access them (see the
  open volume-mount question above — this is the first time that NAS role
  has actually mattered for a design decision).
- **Schema shape.** Per-tool typed tables, not EAV — proven correct in
  practice across four real enrichment tools now.
- **UUID stability caveat.** Immich UUIDs are not move-proof. Policy:
  reaugment under the new UUID when it appears; dead duplicates cleaned up
  via Immich's own "Remove offline files" job.
- **Dev test set.** Fixed, pinned ~100-photo sample + hand-picked hard cases
  in `sidecar.test_set` — live now, includes the original 5 geocode hard
  cases (2 resolved, 3 correctly-null-in-wilderness) plus a multi-face photo.

## Next steps (2026-08, latest)

1. **Resolve the volume-mount/NAS-access question** (see "Landmark matching
   — DINOv3 implementation progress" above) — the very next thing to do,
   before writing more code. Ask directly rather than guessing: does "move
   to a NAS folder" mean a different host path (still a bind mount) or an
   NFS-backed Docker volume (matching the existing `immich_upload` pattern
   in `photo-search/docker-compose.yml`)?
2. **Build the `match_landmark` task** on `gpu-ml` — loads the reference
   embeddings (however they end up being mounted/accessed), shares the
   already-loaded DINOv3 model via `EmbedImageTask.embed()`, computes cosine
   similarity, returns top-k matches. Needs the landmark_id → name mapping
   (`train_label_to_category.csv`, not yet downloaded) and an initial
   similarity threshold (unvalidated guess, to be revisited with real data).
3. **Build `sidecar/enrichment/dinov3_landmarks.py`** — the client side,
   same shape as the other enrichment tools; candidate scope already agreed
   (see above).
4. **Test end-to-end against the pinned test set**, same discipline as every
   other enrichment tool here — expect real surprises in the actual match
   quality/threshold, same as `overture_landmarks.py`'s category-noise
   discovery.
5. **Wire the side-car into the search agent** — still not started. Extend
   `run_readonly_sql`'s readable allowlist (or add structured filters) so
   `resolved_geo`/`object_counts`/`landmark_matches` are queryable. Explicitly
   deferred until the enrichment tools themselves were proven — three of
   four planned tables are there now, the fourth (DINOv3 landmarks) close.
6. **Run the full-library pass** (`--scope full`) for whichever enrichments
   are trusted — still deferred until the search agent can actually use the
   data, so there's a real payoff to point at before spending the batch time.
7. **Audio-to-text (`faster-whisper`) and scene captioning (Florence-2)** —
   accepted future candidates, not yet started, come after the above.

## Pointers into existing code/docs

- Main `photo-search/README.md` — full project snapshot, the search-agent design.
- `search-api/sql_tool.py` — the read-only SQL tool + dedicated Postgres role;
  the model for how the agent will query the side-car once wired in.
- `search-api/tools.py` — `search_photos` filters (people/cities match modes);
  where structured augmentation filters could be added.
- `search-api/landmark/` — the existing curated CLIP-embedding landmark
  matcher that DINOv3 visual matching would layer onto, not replace.
- `gpu-ml/` — the shared GPU device (own repo; also the household NAS).
  `gpu-ml/inference-service/` — the generic task-registry inference
  protocol (`object_detect`, `embed_image`). `gpu-ml/landmark-reference/` —
  one-off batch scripts (`download_gldv2_clean.py`, `embed_reference_set.py`),
  run directly on the host in a venv, not through Docker.
- `sidecar/` — the side-car codebase: `migrations/`, `db.py`
  (incl. `ensure_column`/`ensure_table`), `config.py`, `ensure_schema.py`,
  `test_set.py`, `populate_test_set.py`, `enrichment/` (`reverse_geocode.py`,
  `overture_geocode.py`, `object_detect.py`, `overture_landmarks.py`;
  `dinov3_landmarks.py` not yet created), `run_*.py` entry points,
  `spike_overture_schema.py` / `spike_overture_places_schema.py`
  (schema-verification pattern, reuse if a third Overture theme is ever
  needed).
