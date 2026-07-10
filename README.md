# Photo Search — Project Overview

Self-hosted natural language search over a large family photo collection stored on Dropbox. Search by object/scene description, named individuals, landmarks/buildings, and location (city/place name).

## Platform

- **Host:** Raspberry Pi 5 running off an attached SSD (Docker containers)
- **ML offload:** Remote device on home network with a GTX 1060 (6GB) GPU — deployed and managed as a **separate repo, `gpu-ml`**, since that device is expected to serve other projects over time, not just this one
- **Network:** Internal home network only; no external exposure, so security hardening is out of scope for v1
- **Photo source:** Dropbox `Apps/` subfolder, shared/edited by other family members (uncontrolled — files are added and moved outside our control)

## Storage architecture

Three separate storage roles, deliberately kept apart. **Deployment goal: `docker compose up` with zero host-level mount setup** — no `/etc/fstab` entries, nothing running outside containers.

- **Local SSD (Pi):** Postgres data directory only, as an ordinary bind mount. Small (gigabytes, even for a huge library) — holds embeddings, face clusters, and metadata. Must stay local: Postgres over NFS is a known-bad pattern (unreliable file locking under concurrent writes can corrupt the database), independent of NAS speed or hardware quality.
- **NAS:** Thumbnails/previews *and* database backups, both mounted as a single **Docker-native NFS volume** (`immich_upload`, `driver_opts: type: nfs`) rather than a host-level NFS mount. Docker handles the mount itself — no host `/etc/fstab` entry needed.
- **Dropbox (via rclone, embedded in `immich-server`, VFS cache):** Original photos. The library is too large to mirror locally, so Immich treats it as an external library rather than holding a local copy.

**Caveat on `make clean`:** it runs `docker compose down -v`, which removes the `immich_upload` volume *definition* — this does **not** delete anything on the NAS itself (NFS-backed volumes hold no local data), it only removes Docker's reference to the mount. Recreating it via `make up` reattaches to the same NAS path.

## Backups: solved natively, no extra container needed

Immich has its own built-in Database Dump Settings (System Settings → Database Dump Settings) — scheduled `pg_dump` snapshots with configurable cron timing and retention count, verifiable via Administration → Maintenance, which lists each backup with size and a one-click Restore. **Currently enabled: nightly at 2am, 14 backups retained.**

Because Immich writes these backups to a `backups/` subfolder under the same upload root already mounted as the `immich_upload` NFS volume, they land on the NAS automatically — no separate backup container, no host-side script, nothing extra to build. This replaces the "backup jobs need to run inside a container" plan from an earlier revision of this doc, which turned out to be unnecessary once we confirmed where Immich actually writes dumps.

**Not yet verified:** confirm directly on the NAS (not just via Immich's UI) that `backups/immich-db-backup-*.sql.gz` files are actually visible under the exported NFS path — the UI showing successful dumps confirms Immich's side, not that the NFS mount is truly persisting them off the Pi.

## Core stack

**Immich** is the foundation, not a custom build. It already provides CLIP-based semantic search, facial recognition (InsightFace, trainable via name tags), offline reverse geocoding from GPS EXIF, scheduled library scanning, database backups, and a web UI for browsing/viewing/downloading originals.

## Dropbox mount: embedded in immich-server, not a host process or sidecar

Constraint: nothing should run outside containers on the host. A separate rclone sidecar container was considered and rejected — sharing a FUSE mount between two containers needs privileged mode or shared bind-mount propagation, which is fragile and still touches the host's mount namespace to set up.

Instead, `immich-server` is built from a **custom Dockerfile** (`immich-server/Dockerfile`) that layers `rclone` + `fuse3` onto Immich's own image, with `entrypoint-wrapper.sh` mounting Dropbox before handing off to Immich's normal startup process. This works cleanly because only `immich-server` ever needs the Dropbox files — `search-api` never touches them directly, only through Immich's API/DB — so there's no cross-container sharing problem to solve.

The container needs `cap_add: [SYS_ADMIN]` and `/dev/fuse` (lighter than full `--privileged`). `rclone.conf` (containing Dropbox OAuth credentials) is mounted at runtime as a volume, never baked into the image or committed — see `.gitignore` and `rclone.conf.example`.

**Verified working (2026-07-07):**
- `entrypoint-wrapper.sh`'s hand-off to Immich's real startup process — the original guess (`/usr/src/app/start.sh` as an absolute path) was wrong; confirmed via `docker inspect` against the upstream image that Immich actually runs `tini -- /bin/bash -c "start.sh"` (bare command via `$PATH`, not an absolute path). Fixed and now matches the base image's own invocation exactly.
- `DB_HOSTNAME` required explicitly — Immich defaults to expecting a host named `database` (matching Immich's own example compose file), not `postgres` (our service name). Set explicitly in `docker-compose.yml`.

**Known caveat, not silently resolved:** restarting the `immich-server` container remounts Dropbox and rebuilds the local VFS cache — harmless, just a brief cold-start cost.

## Reconciliation: periodic scanning solved natively

Immich has a built-in Periodic Scanning setting (System Settings → Periodic Scanning) — cron-based, currently enabled at every 6 hours. This replaces the originally-planned custom cron wrapper around `trigger_rescan.sh`; that script now serves only as a manual/on-demand tool (forcing an immediate scan without waiting for the next window), not something we schedule ourselves.

The other reconciliation piece — an existence check before serving a download, so the app never claims a moved/deleted photo is available — is implemented in `search-api`'s `/proxy/download` route (`immich.asset_exists()`).

## GPU device (`gpu-ml` repo, separate from this one)

Runs `immich-machine-learning` via CUDA — CLIP embedding generation and face detection/recognition, the two heaviest jobs. `immich-server` points at it over `IMMICH_MACHINE_LEARNING_URL`. Kept in its own repo because the GPU box is meant to serve more than just this project over time; see that repo's README for how to add services to it.

Later (conditional, Phase 5 below), that same device could also run a local LLM via Ollama for compound query parsing, if the rule-based parser proves insufficient. The device has 6GB VRAM — a 4-bit quantized 7B model (~4–5GB) still fits, but with much less headroom than an 8GB card, so model choice should be reassessed against actual available memory when that phase happens rather than assumed.

**Verified working (2026-07-07):** GPU passthrough confirmed via `nvidia-smi` inside the `immich-machine-learning` container, and the service responds on `/ping`.

## Gaps Immich doesn't cover, and how we're closing them

**1. Dropbox as a source** — see above (embedded rclone in `immich-server`).

**2. Uncontrolled, mutating source** — other family members add/move files independently, and Immich treats a moved file as delete+re-add, not a move, so the index can go stale or duplicate entries. Mitigations: Immich's own periodic scanning (see above), plus an existence check at view/download time so the app never claims a photo is available when it's actually gone.

**3. Landmark/building recognition (not v1, but planned)** — no mature open-source landmark recognizer exists as a drop-in tool. Reuses Immich's existing CLIP embeddings in a face-recognition-like pattern: a small labeled reference set, nearest-neighbor matched against new photos, written back as a tag/description via Immich's API above a confidence threshold. Expect lower accuracy than face recognition, since CLIP is general-purpose, not landmark-specialized; low-confidence cases can optionally fall back to a paid cloud API later.

**4. Compound natural-language queries** — Immich's search bar handles one mode at a time. `search-api` (Flask) parses a query into structured filters (person, object/scene, landmark, location, date), queries Immich's API for most of it, and hands results off to Immich's own viewer for browsing and download. The parser is rule-based, isolated behind `query_parser.py` so an LLM can replace it later without touching the rest of the pipeline — but a real bug should not be mistaken for that ceiling being reached (see below).

**Verified bug, fixed (2026-07-09):** leftover connector words after stripping recognized names/landmarks (e.g. "Kevin **and** Kyle" → "and", "Kevin **alone**" → "alone") were being sent to CLIP's `smart_search` as if they were real object/scene queries, degrading or zeroing out results a pure person/metadata filter would have answered correctly. Fixed with a stopword filter in `query_parser.py`; if no meaningful text remains after filtering, `search-api` correctly skips CLIP and filters on people/location/date alone.

**Still unverified, next to test:** whether Immich's `search_metadata` treats multiple `personIds` as AND (both people present) or OR (either present) — this determines whether "Kevin and Kyle" actually works as intended now that the noise-word bug is fixed. Not yet a case for the Phase 5 LLM swap; that decision should wait for a clean test pass now that this bug is out of the way, not be made on data contaminated by it.

**Not yet built:** filtering to "only Kevin, no one else in the photo" (solo-person search) — distinct from "any photo containing Kevin," which already works. Unbuilt, not broken; a scope decision for later.

## Resolved design question: API vs. direct DB access

Immich's public API doesn't expose raw CLIP embeddings, only text-to-image search — so `search-api`'s landmark matching (`landmark/match.py`, `db.py`) reads embeddings directly from Immich's Postgres tables. This is the one place `search-api` goes around the API. Table/column names there are an unverified assumption about Immich's internal schema and need confirming against a live instance; everything else in `search-api` goes through Immich's public API and has been exercised against a live deploy (`/api/people`, `/api/search/smart` both confirmed working).

## Repository structure

```
photo-search/
├── README.md
├── docker-compose.yml
├── Makefile
├── .env.example
├── .gitignore
├── immich-server/
│   ├── Dockerfile
│   ├── entrypoint-wrapper.sh
│   └── rclone.conf.example        # real rclone.conf is gitignored, runtime-only
├── search-api/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── config.py
│   ├── app.py                     # Flask entry point, / (search page), /api/search, /proxy/*
│   ├── query_parser.py            # rule-based now; swappable for an LLM call later
│   ├── immich_client.py           # wrapper over Immich's REST API + thumbnail/download proxy
│   ├── db.py                      # direct Postgres access, embeddings only
│   ├── landmark/
│   │   ├── reference_embeddings.py
│   │   └── match.py
│   └── templates/
│       └── search.html
└── scripts/
    ├── trigger_rescan.sh          # manual/on-demand only — periodic scanning is native now
    └── check_asset_exists.py
```

`gpu-ml` is a separate repo/Dropbox folder (sibling to this one) — see its own README.

## Build phases

1. **Core infrastructure** — `immich-server` (with embedded rclone) + Postgres + Redis on the Pi; `immich-machine-learning` on the `gpu-ml` device. **Done and verified end-to-end**, including GPU passthrough and a live search test.
2. **Reconciliation** — **Done.** Periodic scanning and database backups both solved natively via Immich's own settings; existence-check before download implemented in `search-api`.
3. **Orchestration layer (v1)** — `search-api`: rule-based query parser → combined-filter search against Immich → hand-off to Immich's viewer/download. **Working**, tested against ~40 real photos, 4 recognized people. One real bug found and fixed (stopword noise in CLIP queries); multi-person AND/OR semantics still to be tested.
4. **Landmark module** — CLIP nearest-neighbor tagging against a labeled reference set. `reference_embeddings.py`/`match.py` exist as a library; nothing yet calls `add_reference()` to actually label a photo — a CLI or route for that is still needed.
5. **LLM query parsing (conditional)** — swap `query_parser.py`'s implementation for a local LLM on the `gpu-ml` device, only if a clean test pass (post-stopword-fix) shows rule-based parsing genuinely insufficient — not before, since the earlier failures were a bug, not a ceiling.

Initial ML backfill over the full collection is expected to take days to weeks and is allowed to run at low priority in the background. A partial, testable index should be usable well before the full backfill completes. Current status: ~40 photos indexed as an initial test batch, full Dropbox backfill not yet started.
