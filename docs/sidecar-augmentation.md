# Photo Metadata Augmentation — Side-car Database (design note)

**Status (2026-09, latest):** all four planned enrichment tools are now
built (reverse-geocode x2, object detection, landmark proximity, and
DINOv3 visual landmark matching's real client,
`sidecar/enrichment/dinov3_landmarks.py`). The side-car is now wired into
the search agent: a second, independently-toggled read-only SQL tool
(`run_readonly_sidecar_sql`) reaches the sidecar database, composed with
Immich-side queries via the existing `combine_results` handle mechanism —
see "Wiring the side-car into the search agent" below for the full design
and why. This is dev-only for now (`search-api-dev`); production
`search-api` remains sidecar-blind by config absence, same as always. This
note is the living record of what's built, why, and what's next; update it
as things change rather than letting chat history be the only record.

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
- **Feeds the existing agent.** Augmentation data is now queryable via a
  dedicated second SQL tool (see "Wiring the side-car into the search
  agent" below) — **DONE, 2026-09**, dev-only.

## Implementation status (2026-09, latest)

What's actually built and proven, mapped to real files:

| Enrichment | File(s) | Status | Notes |
|---|---|---|---|
| Reverse-geocode (Immich's own geocoder) | `sidecar/enrichment/reverse_geocode.py` | Working, tested full test_set | `source='immich_reverse_geocode'` |
| Reverse-geocode (Overture Divisions, richer/county-level) | `sidecar/enrichment/overture_geocode.py` | Working, tested full test_set | `source='overture_divisions'`; chains off the first — only runs on photos still unresolved |
| Object detection (YOLO-World) | `sidecar/enrichment/object_detect.py` + `gpu-ml/inference-service/tasks/object_detect.py` | Working, tested full test_set | 106-term open vocabulary, see `sidecar/config.py` |
| Landmark matching, proximity (Overture Places) | `sidecar/enrichment/overture_landmarks.py` | Working, tested full test_set (v2 category filter) | `source='overture_places'`; residential-building noise partially filtered — see design doc history for the `landmark_and_historical_building` taxonomy caveat |
| Landmark matching, visual (DINOv3) | `sidecar/enrichment/dinov3_landmarks.py` | **Built.** Not yet run at `--scope full` | `source='dinov3_visual'`; candidate scope = all images minus ones `overture_landmarks` already matched; stores `landmark_id` (GLDv2's stable numeric ID) alongside `landmark_name` specifically so the planned name-overrides tool can group on the ID, not the name |

All four enrichments are queryable from the search agent as of this
session — see "Wiring the side-car into the search agent" below.

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
  to `resolved_geo`, `source` + `distance_meters` + `landmark_id` columns
  added to `landmark_matches` (via the schema-evolution tooling, see below).
- **`sidecar/db.py` has `ensure_column()`/`ensure_table()`** (idempotent
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS`
  wrappers) and **`sidecar/ensure_schema.py`** declares actual schema
  evolutions to apply — run via `python -m sidecar.ensure_schema`. Fixes the
  "typing ALTER TABLE into psql by hand" gap. Add new evolutions there as the
  schema grows.
- **`sidecar/landmark_labels.py`** — shared `landmark_id` → display-name
  lookup (parses GLDv2's `train_label_to_hierarchical.csv`, cached durably
  on the same NAS folder as the reference embeddings). Used by both
  `dinov3_landmarks.py` and `dinov3_landmark_report.py` — factored out
  specifically so there's one place parsing this CSV, not two that could
  drift apart.
- **A generic, reusable GPU inference protocol on `gpu-ml`**
  (`gpu-ml/inference-service/`): a task-registry pattern (`POST
  /v1/infer/<task>`, `GET /v1/tasks`, `GET /health`) so new models register as
  new tasks, not new services. Deliberately decoupled from Immich — callers
  send raw image bytes, not asset IDs, so the service stays reusable across
  projects. Registered tasks: `object_detect` (YOLO-World), `embed_image`
  (DINOv3, generic), `match_landmark` (tested against real data). Audio-to-text
  (`faster-whisper`) and scene captioning (Florence-2) are accepted future
  candidates, not yet built.
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
present, not one explaining the other). Included in `run_readonly_sql`'s
existing allowlist and schema prompt (`asset_ocr` table) — nothing further
needed here.

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

## Landmark matching — DINOv3 (DONE, 2026-08/09)

**Design (still holding):** visual recognition (DINOv3) and geospatial
proximity (`overture_landmarks.py`, done above) are genuinely complementary,
not primary+fallback — catches landmarks a photo has no useful GPS for,
lesser-known landmarks outside any fixed vocabulary, and tightly-cropped
photos. The search agent now consults both (see the two-SQL-tool design
below), not one in preference to the other.

**Model: DINOv3 ViT-S+** (`facebook/dinov3-vits16plus-pretrain-lvd1689m`,
~29M params), gated on HuggingFace (license approved 2026-08).

**Reference dataset: GLDv2-clean, full set** — 1,580,470 images, 81,313
landmarks, embedded and stored on the NAS at
`/Docker/gpu-ml/landmark-embeddings/` (an NFS-backed Docker volume,
`gpu_ml_landmark_embeddings`, following the household's standard NAS
volume convention — see `flask-app-template/README.md`).

**Pipeline (all done):**
1. `gpu-ml/landmark-reference/download_gldv2_clean.py` — downloaded the
   clean subset.
2. `gpu-ml/inference-service/tasks/embed_image.py` — generic DINOv3
   embedding task, exposes a reusable `.embed()` shared with `match_landmark`.
3. `gpu-ml/landmark-reference/embed_reference_set.py` — bulk-embedded all
   1.58M images.
4. NFS volume set up, embeddings copied over (WinSCP).
5. `gpu-ml/inference-service/tasks/match_landmark.py` — cosine similarity
   against the reference matrix. `min_similarity` (default 0.7) and `top_k`
   are per-call `params`, not hardcoded — added so a diagnostic caller
   could probe below-default candidates without a code change.
6. `sidecar/landmark_labels.py` — `landmark_id` → display-name lookup
   (GLDv2's `train_label_to_hierarchical.csv`, stored durably on the same
   NAS folder). **Real, confirmed data gap:** a meaningful number of GLDv2
   IDs have thin/missing/non-English category names — these fall back to a
   bare `landmark <id>` placeholder even when the match itself is correct.
   This is a naming problem, not a matching-accuracy problem — see
   "Landmark name overrides" below for the planned fix.
7. **Calibration test, DONE:** `sidecar/dinov3_landmark_report.py` (a
   diagnostic script, writes no sidecar DB rows) ran 500 real Disney World
   photos, stratified-sampled across the full date range, at
   `--min-similarity 0.4` specifically to see below-default candidates.
   Findings: **`min_similarity = 0.7` confirmed as a good cutoff** (real
   negatives mostly scored below it, real positives mostly above), **a
   meaningful number of real high-confidence correct matches occurred**
   (the approach has real value), and the landmark-name gap (point 6)
   surfaced repeatedly.
8. **`sidecar/enrichment/dinov3_landmarks.py` — the real enrichment
   client, BUILT (2026-09).** Candidate scope: all real image assets
   EXCEPT ones `overture_landmarks` already matched (`source =
   'overture_places'` in `landmark_matches`) — this single condition
   covers both no-coordinate photos and coordinate photos where proximity
   search came up empty, without two separate query branches. Stores
   `landmark_id` alongside `landmark_name` (see schema note below) and
   `top_k=1` per photo (unlike the diagnostic report's `top_k=5` — for a
   *stored* row, the single best visual match is what's meaningful).
   `sidecar/run_dinov3_landmarks.py` is the entry point. **Not yet run at
   `--scope full`** — see "Next steps."

**Schema:** `landmark_matches.source`, `.distance_meters`, and
`.landmark_id` columns all exist (via `ensure_schema.py`). `landmark_id`
was added specifically because `landmark_name` is the field the planned
name-overrides tool will let a user manually correct — `landmark_id` is
GLDv2's own stable identifier and won't change under an override, so it's
the right thing to group/cluster on, not the name.

## Landmark name overrides — planned, not yet built (2026-09)

New tool, scoped after the real-data test above. Purpose: turn the
`landmark <id>` fallback gap into a usable name, using the model's own
consistency across the library as the signal, rather than depending on
GLDv2's sparse metadata.

**Planned shape:**
- Group DINOv3-matched photos by `landmark_id` (now a real column — see
  schema note above).
- Simple review interface: for each cluster, show the matched photos
  together so it's visually obvious whether the model is consistently
  matching the same real-world thing.
- Let the user manually assign/correct a name where it is.
- Store overrides in a small local table (tentatively
  `landmark_name_overrides`, keyed on `landmark_id`) — checked first,
  falling back to the GLDv2 CSV name (or the numeric ID) only if no
  override exists. Never touches GLDv2's own data.

**Deliberately scoped for AFTER the full-collection DINOv3 pass** (see
Next steps below) — clustering needs enough real match data across the
whole library to be worth reviewing.

## Wiring the side-car into the search agent (DONE, 2026-09)

**The problem:** Immich's database and the sidecar database are
deliberately separate Postgres databases (see "Core design decisions" —
schema-stability isolation was the whole point). Standard Postgres cannot
`JOIN` across databases in a single query, so simply adding sidecar tables
to `run_readonly_sql`'s existing allowlist doesn't work — that tool
connects to Immich's database only.

**Two options considered:**
- **`postgres_fdw`** (foreign data wrapper) — would let one query `JOIN`
  across both databases, but requires foreign-table definitions and grants
  spanning both databases, quietly eroding the isolation the split was
  built for.
- **A second, independent SQL tool** — reached instead. `run_readonly_sql`
  gains a sibling, `run_readonly_sidecar_sql`, pointed at the sidecar
  database, and the two are composed at the AGENT level via
  `combine_results` (which already existed for exactly this kind of set
  composition — it operates on handles regardless of which tool produced
  them). **Chosen** — reuses machinery already built rather than inventing
  cross-database plumbing, and keeps the two databases genuinely isolated.

**What changed, concretely:**
- **`search-api/sql_tool.py` refactored into a DB-agnostic factory**,
  `make_readonly_sql_tool(tool_name, description, schema_prompt,
  dsn_config_attr)`. All the actual mechanics (SQL generation via
  `SQL_MODEL`, single-SELECT verification, statement timeout, row cap,
  handle-vs-inline-rows routing) are shared code, parameterized only by
  which database/DSN and which schema-description prompt to use. Two
  instances now exist: `RUN_READONLY_SQL_SCHEMA`/`execute_run_readonly_sql`
  (Immich, unchanged names — nothing importing these needed to change) and
  the new `RUN_READONLY_SIDECAR_SQL_SCHEMA`/`execute_run_readonly_sidecar_sql`
  (sidecar). This DB-agnostic shape is deliberate for future growth — a
  third database later would be a third factory call, not new mechanics.
- **`sql/create_sidecar_readonly_role.sql`** — new, mirrors
  `create_readonly_role.sql`'s allowlist approach against the sidecar
  database. Grants `SELECT` on `landmark_matches`, `object_counts`,
  `resolved_geo` only — `enrichment_status` deliberately excluded (internal
  bookkeeping, not search-relevant, and its `error_detail` strings
  shouldn't be agent-readable).
- **`search-api/config.py`** — new `SIDECAR_SQL_READONLY_DSN` (empty by
  default) and `AGENT_SIDECAR_SQL_ENABLED` (`false` by default). Both
  unset on production `search-api` — sidecar-blindness there is now
  enforced purely by config absence, the same mechanism that already kept
  it sidecar-blind, not a special-cased code path.
- **`search-api/tools.py`** — `build_tool_schemas()` gained an
  `include_sidecar_sql` parameter, independent of `include_sql`.
  `combine_results` itself needed NO changes — it already worked on
  arbitrary handles.
- **`search-api/search_agent.py`** — the executors map and system prompt
  both updated: the agent is told to use `run_readonly_sidecar_sql` for a
  named landmark, an object count, or county-level location, and — when a
  query needs both a sidecar fact and an Immich fact (e.g. "Kevin's photos
  of the Eiffel Tower") — to run one query against each database and
  combine the two handles with `combine_results`, never to attempt both in
  a single request to either tool.

**Promoting to production later** (once `sidecar_prod` exists): re-run
`create_sidecar_readonly_role.sql` against `sidecar_prod`, set
`SIDECAR_SQL_READONLY_DSN` and `AGENT_SIDECAR_SQL_ENABLED=true` on
production `search-api`'s environment. No code change — this was the
explicit point of the DB-agnostic factory design.

**Not yet done:** re-running the structured test list (README) against
queries that actually exercise `run_readonly_sidecar_sql` and the
cross-database `combine_results` pattern — this has been wired but not
yet validated against real test queries.

## Schema evolution tooling (DONE, 2026-08)

`sidecar/db.py`'s `ensure_column()`/`ensure_table()` + `sidecar/ensure_schema.py`
— see "Implementation status" above. Dogfooded three times now
(`landmark_matches.source`, `.distance_meters`, `.landmark_id`).

## Future enrichment candidates (2026-08)

Two accepted for the roadmap, not yet built:

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
- **Landmark name overrides** — see its own section above; scoped after the
  full-collection DINOv3 pass, not a from-scratch future idea but also not
  yet built.
- *(Add more here as they come up, rather than letting them live only in
  chat history.)*

## GPU/VRAM constraint (still holds)

The gpu-ml box's GTX 1060 has 6GB VRAM shared across `immich-machine-learning`,
`ollama`, and now `inference-service` (which itself hosts both YOLO-World and
DINOv3 as separate tasks in one process). Confirmed working for `object_detect`,
`embed_image`, and `match_landmark` individually (single worker, lazy model
loading per task — see `gpu-ml/README.md`'s VRAM contention note). **Not yet
tested: YOLO-World and DINOv3 loaded and actively inferring at the same
time** — worth watching once `dinov3_landmarks.py` runs at `--scope full`
and could realistically overlap with an `object_detect` pass.

## Process & infrastructure decisions (2026-07/09, still holding)

- **Two containers.** `search-api` (prod) stays completely sidecar-blind —
  no dependency on the sidecar code or DB, and now also no
  `SIDECAR_SQL_READONLY_DSN`/`AGENT_SIDECAR_SQL_ENABLED` set (see "Wiring
  the side-car into the search agent"). `search-api-dev` (same
  image/codebase, different config + a superset build via
  `sidecar/Dockerfile.dev`) is where sidecar integration is built and
  tested, and — as of this session — the only place the sidecar SQL tool
  is enabled. **Deliberately staying split for a while longer** — the
  DB-agnostic SQL-tool factory exists specifically so promoting to
  production later is a config change, not a rebuild.
- **Sidecar databases: separate Postgres databases, both dev and prod, on
  the same Postgres instance as Immich's own DB.**
  - **Dev (`sidecar_dev`):** no backup. Wipe-and-redevelop freely — live now,
    populated with real test data.
  - **Prod (`sidecar_prod`, not yet built):** will need its own backup
    mechanism.
- **Cross-database queries: two separate SQL tools + `combine_results`,
  never `postgres_fdw`.** See "Wiring the side-car into the search agent"
  for the full reasoning — this is now the established pattern for any
  future case where the agent needs to correlate data across genuinely
  separate databases.
- **Repo layout.** `sidecar/` is a top-level folder, sibling to `search-api/`.
  `gpu-ml` is its own separate repo, one device serving multiple projects —
  and also confirmed to be the household NAS (multiple large drives
  mounted at `/media/*`), which matters for where large datasets/models
  should live and how containers should access them.
- **NAS access: NFS-backed Docker volumes, always, even for same-host
  cases.** Confirmed as a deliberate, general household policy: the whole
  Docker architecture is built on containers being mobile/reproducible
  from compose, so one consistent NAS-access mechanism is preferred over
  deciding per-container whether a bind mount would technically work
  today. **Standard NAS volume convention** (see
  `flask-app-template/README.md`): `{appname}_{purpose}` volume naming,
  device path hardcoded directly in `docker-compose.yml`, only `NAS_IP`
  env-driven with a fallback default. The `gpu_ml_landmark_embeddings`
  volume follows this exactly, including being mounted a second time (once
  read-only from gpu-ml, once read-write from photo-search) rather than
  duplicating the underlying data.
- **Schema shape.** Per-tool typed tables, not EAV — proven correct in
  practice across all four enrichment tools now.
- **UUID stability caveat.** Immich UUIDs are not move-proof. Policy:
  reaugment under the new UUID when it appears; dead duplicates cleaned up
  via Immich's own "Remove offline files" job.
- **Dev test set.** Fixed, pinned ~100-photo sample + hand-picked hard cases
  in `sidecar.test_set` — live now, includes the original 5 geocode hard
  cases (2 resolved, 3 correctly-null-in-wilderness) plus a multi-face photo.

## Next steps (2026-09, latest)

1. ✅ **Resolve the volume-mount/NAS-access question** — done.
2. ✅ **Set up the actual NFS export on the NAS side** — done.
3. ✅ **Test `match_landmark.py` end-to-end** — done, against 500 real
   photos. `min_similarity = 0.7` confirmed as a good cutoff.
4. ✅ **Build `sidecar/enrichment/dinov3_landmarks.py`** — done. Not yet
   run at `--scope full` (see 6 below).
5. ✅ **Wire the side-car into the search agent** — done, see "Wiring the
   side-car into the search agent" above. Dev-only for now.
6. **Run the full-library pass** (`--scope full`) for `dinov3_landmarks.py`
   specifically (the other three enrichments' full-library status should
   be double-checked too) — this is also the prerequisite for the
   landmark-name-overrides tool (needs a full library's worth of matches
   to usefully cluster).
7. **Re-run the structured test list against `run_readonly_sidecar_sql`
   and cross-database `combine_results` queries** — the wiring exists but
   hasn't been validated against real test queries yet (e.g. "photos of
   the Eiffel Tower", "Kevin's photos with 3+ dogs").
8. **Build the landmark name overrides tool** — see its own section above.
   Scoped for after the full-collection pass (6).
9. **Audio-to-text (`faster-whisper`) and scene captioning (Florence-2)** —
   accepted future candidates, not yet started, come after the above.

## Pointers into existing code/docs

- Main `photo-search/README.md` — full project snapshot, the search-agent design.
- `search-api/sql_tool.py` — `make_readonly_sql_tool()`, the DB-agnostic
  factory, plus its two instances (`run_readonly_sql` for Immich,
  `run_readonly_sidecar_sql` for the sidecar database).
- `sql/create_readonly_role.sql` / `sql/create_sidecar_readonly_role.sql` —
  the dedicated read-only Postgres roles, one per database.
- `search-api/tools.py` — `search_photos` filters (people/cities match
  modes), `combine_results` (the cross-tool/cross-database composition
  mechanism), `build_tool_schemas()`.
- `search-api/search_agent.py` — the agent loop and system prompt,
  including when to use `run_readonly_sidecar_sql` vs. `run_readonly_sql`.
- `search-api/landmark/` — the existing curated CLIP-embedding landmark
  matcher that DINOv3 visual matching layers onto, not replaces.
- `gpu-ml/` — the shared GPU device (own repo; also the household NAS).
  `gpu-ml/inference-service/` — the generic task-registry inference
  protocol (`object_detect`, `embed_image`, `match_landmark`).
  `gpu-ml/landmark-reference/` — one-off batch scripts
  (`download_gldv2_clean.py`, `embed_reference_set.py`), run directly on the
  host in a venv, not through Docker. `gpu-ml/.env.example` — documents
  `HF_TOKEN`, `NAS_IP`.
- `sidecar/` — the side-car codebase: `migrations/`, `db.py`
  (incl. `ensure_column`/`ensure_table`), `config.py`, `ensure_schema.py`,
  `test_set.py`, `populate_test_set.py`, `landmark_labels.py` (shared
  landmark_id → name lookup), `enrichment/` (`reverse_geocode.py`,
  `overture_geocode.py`, `object_detect.py`, `overture_landmarks.py`,
  `dinov3_landmarks.py`), `dinov3_landmark_report.py` (diagnostic HTML
  report, not a formal enrichment tool), `run_*.py` entry points,
  `spike_overture_schema.py` / `spike_overture_places_schema.py`
  (schema-verification pattern, reuse if a third Overture theme is ever
  needed).
- `flask-app-template/README.md` — the household's standard NAS volume
  convention (`{appname}_{purpose}` naming, hardcoded device paths,
  `NAS_IP`-only env parameterization), followed by
  `gpu_ml_landmark_embeddings`.
