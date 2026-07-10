# Photo Search — Project Overview

Self-hosted natural language search over a large family photo collection stored on Dropbox. Search by object/scene description, named individuals, landmarks/buildings, and location (city/place name).

## Platform

- **Host:** Raspberry Pi 5 running off an attached SSD (Docker containers)
- **ML offload:** Remote device on home network with a GTX 1060 (6GB) GPU — deployed and managed as a **separate repo, `gpu-ml`**, since that device is expected to serve other projects over time, not just this one. Now runs both `immich-machine-learning` and `ollama` (see Phase 5 below).
- **Network:** Internal home network only; no external exposure, so security hardening is out of scope for v1
- **Photo source:** Dropbox `Apps/` subfolder, shared/edited by other family members (uncontrolled — files are added and moved outside our control)

## Storage architecture

Three separate storage roles, deliberately kept apart. **Deployment goal: `docker compose up` with zero host-level mount setup** — no `/etc/fstab` entries, nothing running outside containers.

- **Local SSD (Pi):** Postgres data directory only, as an ordinary bind mount. Small (gigabytes, even for a huge library) — holds embeddings, face clusters, and metadata. Must stay local: Postgres over NFS is a known-bad pattern (unreliable file locking under concurrent writes can corrupt the database), independent of NAS speed or hardware quality.
- **NAS:** Thumbnails/previews *and* database backups, both mounted as a single **Docker-native NFS volume** (`immich_upload`, `driver_opts: type: nfs`) rather than a host-level NFS mount. Docker handles the mount itself — no host `/etc/fstab` entry needed.
- **Dropbox (via rclone, embedded in `immich-server`, VFS cache):** Original photos. The library is too large to mirror locally, so Immich treats it as an external library rather than holding a local copy.

**Caveat on `make clean`:** it runs `docker compose down -v`, which removes the `immich_upload` volume *definition* — this does **not** delete anything on the NAS itself (NFS-backed volumes hold no local data), it only removes Docker's reference to the mount.

## Backups: solved natively, no extra container needed

Immich's built-in Database Dump Settings (System Settings → Database Dump Settings) — nightly at 2am, 14 backups retained. Because Immich writes these to a `backups/` subfolder under the same upload root already mounted as `immich_upload`, they land on the NAS automatically. **Confirmed directly on the NAS (2026-07-10):** `backups/immich-db-backup-*.sql.gz` files verified present under the exported NFS path, not just visible via Immich's own UI.

## Core stack

**Immich** is the foundation, not a custom build. It already provides CLIP-based semantic search, facial recognition (InsightFace, trainable via name tags), offline reverse geocoding from GPS EXIF, scheduled library scanning, database backups, and a web UI for browsing/viewing/downloading originals.

## Dropbox mount: embedded in immich-server, not a host process or sidecar

`immich-server` is built from a **custom Dockerfile** that layers `rclone` + `fuse3` onto Immich's own image, with `entrypoint-wrapper.sh` mounting Dropbox before handing off to Immich's normal startup process (`tini -- /bin/bash -c "start.sh"` — verified against the upstream image via `docker inspect`, since the original absolute-path guess was wrong). Requires `cap_add: [SYS_ADMIN]` and `/dev/fuse`. `rclone.conf` is mounted at runtime, never committed.

**Verified working (2026-07-07):** including `DB_HOSTNAME=postgres` set explicitly, since Immich defaults to expecting a host named `database` (matching its own example compose file), not our service name.

## Reconciliation: solved natively

Immich's built-in Periodic Scanning (every 6 hours) replaced the originally-planned custom cron wrapper; `trigger_rescan.sh` is now a manual/on-demand tool only. The existence-check before serving a download (so the app never claims a moved/deleted photo is available) is implemented in `search-api`'s `/proxy/download` route.

## GPU device (`gpu-ml` repo, separate from this one)

Runs `immich-machine-learning` (CLIP + face detection, verified working via `nvidia-smi` and `/ping`) and, as of Phase 5, `ollama` for LLM query parsing. Both share the same 6GB card — see `gpu-ml`'s README for the model-size reasoning and an open caveat about contention under simultaneous heavy load.

## Gaps Immich doesn't cover, and how we're closing them

**1. Dropbox as a source** — see above (embedded rclone in `immich-server`).

**2. Uncontrolled, mutating source** — mitigated via Immich's native periodic scanning plus the existence-check on download.

**3. Landmark/building recognition (not v1, but planned)** — reuses Immich's existing CLIP embeddings in a face-recognition-like pattern: a small labeled reference set, nearest-neighbor matched, written back as a tag/description above a confidence threshold. **Working as of the initial ~40-photo test batch** — correctly distinguished "Spaceship Earth" from other Walt Disney World landmarks, a genuinely encouraging sign given CLIP is general-purpose, not landmark-specialized.

**4. Compound natural-language queries — now LLM-based (Phase 5), not just rule-based.**

### Why the switch happened

A structured test pass against ~40 real photos and 4 registered people surfaced multiple failures. Investigating each individually separated them into two categories:

- **Real bugs, unrelated to parser choice** (would have affected an LLM parser too): a date-format bug (`query_parser` sent bare `.isoformat()` timestamps; Immich's API requires full ISO 8601 with a `Z` timezone, causing a 400) and a full-name-only person-matching bug ("Kyle" never matched registered "Kyle Brothers"). Both fixed directly in `query_parser_rules.py`, which remains the fallback backend.
- **Genuine rule-based ceiling**: colloquial location terms ("New York") not matching Immich's actual EXIF-derived city granularity ("Manhattan," "Edgewater"), and the general brittleness of matching arbitrary phrasing with regex. This is the part an LLM genuinely helps with — but only when grounded in real data, not left to guess from its own training knowledge.

### Architecture

`query_parser.py` is now a thin **dispatcher** (`QUERY_PARSER_MODE` env var: `llm` default, `rules` fallback-only), preserving the interface `app.py` always used — this was the specific reason the parser was kept isolated from day one, and the payoff is that this swap touched no other file's logic.

- **`query_parser_rules.py`** — the original regex parser, both bugs above fixed, kept as an explicit, inspectable fallback file (not just relying on version history, since this repo isn't pushed to GitHub yet).
- **`query_parser_llm.py`** — new. Calls Ollama (`llama3.2:3b`) on the `gpu-ml` device, with a system prompt **grounded in real data**: actual registered people names, actual labeled landmark names, and actual city values pulled live from Immich (`ImmichClient.get_cities()`, hitting `/api/search/cities` — response shape unverified, flagged in `immich_client.py`). The model is instructed to only use values from these lists, and `query_parser_llm.py` also filters the response against them afterward as defense in depth against hallucination.
- **Automatic fallback**: if the LLM call fails or returns unparseable JSON, `query_parser.py` catches the exception and falls back to the rules backend for that query, logging a warning — a transient Ollama outage degrades search rather than crashing it.

### What the LLM swap does and doesn't fix

Worth being explicit, since it would be easy to over-credit the LLM: it directly addresses person-name flexibility and (with grounding) location-term mapping. It does **not** fix things that were never parsing problems — the date-format bug was downstream of parsing entirely and needed fixing regardless; "solo person only, no one else in frame" filtering is a missing capability in the search execution layer (Immich has no such filter), not something either parser can produce on its own.

**Not yet tested:** the LLM backend against the same structured test list that surfaced the original bugs. That's the next real validation step.

## Resolved design question: API vs. direct DB access

`search-api`'s landmark matching (`landmark/match.py`, `db.py`) reads CLIP embeddings directly from Immich's Postgres tables, since the public API doesn't expose them. Everything else goes through Immich's public API and has been exercised against a live deploy.

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
│   ├── query_parser.py            # dispatcher — QUERY_PARSER_MODE selects backend
│   ├── query_parser_rules.py      # regex-based fallback parser
│   ├── query_parser_llm.py        # grounded LLM parser (Ollama on gpu-ml)
│   ├── immich_client.py           # wrapper over Immich's REST API + thumbnail/download proxy + get_cities()
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

`gpu-ml` is a separate repo/Dropbox folder (sibling to this one) — now runs both `immich-machine-learning` and `ollama`.

## Build phases

1. **Core infrastructure** — **Done**, verified end-to-end including GPU passthrough.
2. **Reconciliation** — **Done**, solved natively via Immich (periodic scanning, backups).
3. **Orchestration layer** — **Working**, structured test pass run against ~40 photos / 4 people; surfaced real bugs (fixed) and a genuine rule-based ceiling (see Phase 5).
4. **Landmark module** — **Working** for initial test batch; `add_reference()` still has no CLI/route caller, so labeling new landmarks currently requires a manual script/REPL call.
5. **LLM query parsing** — **Built, not yet validated.** Grounded `query_parser_llm.py` deployed via Ollama on `gpu-ml`, with automatic fallback to the rules backend. Next step: re-run the original structured test list against the LLM backend to confirm it actually resolves the person-matching and location-granularity issues it was built for.

Initial ML backfill over the full collection is expected to take days to weeks and hasn't started yet — current library is the ~40-photo test batch used for search validation.
