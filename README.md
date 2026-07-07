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
- **NAS:** Thumbnails/previews, mounted as a **Docker-native NFS volume** (`driver_opts: type: nfs`, same convention as the flask-app-template repo) rather than a host-level NFS mount. Docker handles the mount itself — no host `/etc/fstab` entry needed. Thumbnails are plain image files, not a database needing POSIX locks, so NFS is fine here. Periodic backups (`pg_dump` snapshots + thumbnail cache copies) will also land on the NAS, but since the host has no direct NAS mount, backup jobs will need to run **inside a container** (e.g. a `docker compose run` job mounting the same volume) rather than as a host script — not yet built, flagging for Phase 2.
- **Dropbox (via rclone, embedded in `immich-server`, VFS cache):** Original photos. The library is too large to mirror locally, so Immich treats it as an external library rather than holding a local copy.

**Caveat on `make clean`:** it runs `docker compose down -v`, which removes the `immich_upload` volume *definition* — this does **not** delete anything on the NAS itself (NFS-backed volumes hold no local data), it only removes Docker's reference to the mount. Recreating it via `make up` reattaches to the same NAS path.

## Core stack

**Immich** is the foundation, not a custom build. It already provides CLIP-based semantic search, facial recognition (InsightFace, trainable via name tags), offline reverse geocoding from GPS EXIF, and a web UI for browsing/viewing/downloading originals.

## Dropbox mount: embedded in immich-server, not a host process or sidecar

Constraint: nothing should run outside containers on the host. A separate rclone sidecar container was considered and rejected — sharing a FUSE mount between two containers needs privileged mode or shared bind-mount propagation, which is fragile and still touches the host's mount namespace to set up.

Instead, `immich-server` is built from a **custom Dockerfile** (`immich-server/Dockerfile`) that layers `rclone` + `fuse3` onto Immich's own image, with `entrypoint-wrapper.sh` mounting Dropbox before handing off to Immich's normal startup process. This works cleanly because only `immich-server` ever needs the Dropbox files — `search-api` never touches them directly, only through Immich's API/DB — so there's no cross-container sharing problem to solve.

The container needs `cap_add: [SYS_ADMIN]` and `/dev/fuse` (lighter than full `--privileged`). `rclone.conf` (containing Dropbox OAuth credentials) is mounted at runtime as a volume, never baked into the image or committed — see `.gitignore` and `rclone.conf.example`.

**Known caveats, not silently resolved:**
- Restarting the `immich-server` container remounts Dropbox and rebuilds the local VFS cache — harmless, just a brief cold-start cost.
- `entrypoint-wrapper.sh`'s hand-off to Immich's real startup script is an unverified guess at the base image's internals — must be confirmed before first deploy, and rechecked after any Immich base image update.

## GPU device (`gpu-ml` repo, separate from this one)

Runs `immich-machine-learning` via CUDA — CLIP embedding generation and face detection/recognition, the two heaviest jobs. `immich-server` points at it over `IMMICH_MACHINE_LEARNING_URL`. Kept in its own repo because the GPU box is meant to serve more than just this project over time; see that repo's README for how to add services to it.

Later (conditional, Phase 5 below), that same device could also run a local LLM via Ollama for compound query parsing, if the rule-based parser proves insufficient. The device has 6GB VRAM — a 4-bit quantized 7B model (~4–5GB) still fits, but with much less headroom than an 8GB card, so model choice should be reassessed against actual available memory when that phase happens rather than assumed.

**Verified working (2026-07-07):** GPU passthrough confirmed via `nvidia-smi` inside the `immich-machine-learning` container, and the service responds on `/ping`.

## Gaps Immich doesn't cover, and how we're closing them

**1. Dropbox as a source** — see above (embedded rclone in `immich-server`).

**2. Uncontrolled, mutating source** — other family members add/move files independently, and Immich treats a moved file as delete+re-add, not a move, so the index can go stale or duplicate entries. Mitigations: scheduled recurring rescans, plus an existence check at view/download time so the app never claims a photo is available when it's actually gone.

**3. Landmark/building recognition (not v1, but planned)** — no mature open-source landmark recognizer exists as a drop-in tool. Reuses Immich's existing CLIP embeddings in a face-recognition-like pattern: a small labeled reference set, nearest-neighbor matched against new photos, written back as a tag/description via Immich's API above a confidence threshold. Expect lower accuracy than face recognition, since CLIP is general-purpose, not landmark-specialized; low-confidence cases can optionally fall back to a paid cloud API later.

**4. Compound natural-language queries** — Immich's search bar handles one mode at a time. `search-api` (Flask) parses a query into structured filters (person, object/scene, landmark, location, date), queries Immich's API for most of it, and hands results off to Immich's own viewer for browsing and download. The parser starts rule-based, isolated behind `query_parser.py` so an LLM can replace it later without touching the rest of the pipeline.

## Resolved design question: API vs. direct DB access

Immich's public API doesn't expose raw CLIP embeddings, only text-to-image search — so `search-api`'s landmark matching (`landmark/match.py`, `db.py`) reads embeddings directly from Immich's Postgres tables. This is the one place `search-api` goes around the API. Table/column names there are an unverified assumption about Immich's internal schema and need confirming against a live instance; everything else in `search-api` goes through Immich's public API.

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
    ├── trigger_rescan.sh
    └── check_asset_exists.py
```

`gpu-ml` is a separate repo/Dropbox folder (sibling to this one) — see its own README.

## Build phases

1. **Core infrastructure** — `immich-server` (with embedded rclone) + Postgres + Redis on the Pi; `immich-machine-learning` on the `gpu-ml` device. *(`gpu-ml` deployed and GPU passthrough verified; `photo-search` side in progress.)*
2. **Reconciliation** — scheduled library rescans; existence-check before serving downloads.
3. **Orchestration layer (v1)** — `search-api`: rule-based query parser → combined-filter search against Immich → hand-off to Immich's viewer/download. *(Built — pending verification of the API/DB assumptions noted throughout this doc.)*
4. **Landmark module** — CLIP nearest-neighbor tagging against a labeled reference set. `reference_embeddings.py`/`match.py` exist as a library; nothing yet calls `add_reference()` to actually label a photo — a CLI or route for that is still needed.
5. **LLM query parsing (conditional)** — swap `query_parser.py`'s implementation for a local LLM on the `gpu-ml` device, only if rule-based parsing proves insufficient.

Initial ML backfill over the full collection is expected to take days to weeks and is allowed to run at low priority in the background. A partial, testable index should be usable well before the full backfill completes.
