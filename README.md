# Photo Search — Project Overview

Self-hosted natural language search over a large family photo collection stored on Dropbox. Search by object/scene description, named individuals, landmarks/buildings, and location (city/place name).

**This README is a full project snapshot as of 2026-07-11**, written to seed a fresh conversation. It covers what's built and verified, the agreed design for the in-progress search-agent rework, and the concrete remaining tasks.

## Platform

- **Host:** Raspberry Pi 5 running off an attached SSD (Docker containers)
- **ML offload:** Remote device on home network with a GTX 1060 (6GB) GPU — deployed and managed as a **separate repo, `gpu-ml`**, since that device is expected to serve other projects over time
- **Network:** Internal home network only; no external exposure, so security hardening is out of scope for v1 *except* for the new SQL tool (see below), which needs real safeguards regardless of network exposure
- **Photo source:** Dropbox `Apps/` subfolder, shared/edited by other family members (uncontrolled — files are added and moved outside our control)

## Storage architecture — done, verified

- **Local SSD (Pi):** Postgres data directory only, ordinary bind mount. Must stay local — Postgres over NFS is a known-bad pattern regardless of NAS quality.
- **NAS:** Thumbnails/previews *and* database backups, both via a single Docker-native NFS volume (`immich_upload`). **Backups confirmed present directly on the NAS** (not just via Immich's UI) as of 2026-07-10.
- **Dropbox:** Original photos, mounted via `rclone` **embedded inside the `immich-server` container** (custom Dockerfile + `entrypoint-wrapper.sh`), not a host process or sidecar. `entrypoint-wrapper.sh`'s hand-off to Immich's real startup command (`tini -- /bin/bash -c "start.sh"`) was verified against the upstream image via `docker inspect` after an initial wrong guess.

## Core stack — done, verified end-to-end

**Immich** (not a custom build) provides CLIP-based semantic search, facial recognition, offline reverse geocoding, scheduled library scanning (every 6 hours, native), database backups (nightly, native), and the web UI used for actually viewing/downloading originals. `DB_HOSTNAME=postgres` must be set explicitly (Immich defaults to expecting a host named `database`). GPU passthrough to `immich-machine-learning` on the `gpu-ml` device confirmed via `nvidia-smi` and `/ping`.

## search-api — orchestration layer, built but mid-redesign

`search-api` (Flask) exists to combine Immich's separate search modes (CLIP, people, metadata) into one natural-language query, and to proxy thumbnails/downloads so the browser never needs Immich's API key directly. Currently working end-to-end for basic queries. Repository layout as of now:

```
search-api/
├── Dockerfile / requirements.txt / config.py
├── app.py                     # /, /api/search, /proxy/thumbnail, /proxy/download
├── immich_client.py           # Immich REST API wrapper + get_cities() (verified working, returns a list)
├── db.py                      # direct Postgres access for CLIP embeddings only (landmark matching)
├── query_parser.py            # dispatcher: QUERY_PARSER_MODE=llm|rules
├── query_parser_rules.py      # regex-based fallback parser — KEEP regardless of Phase 5 outcome
├── query_parser_llm.py        # Ollama-based grounded parser — being SUPERSEDED, see below
├── landmark/
│   ├── reference_embeddings.py   # add_reference() has no caller yet — still a gap
│   └── match.py
└── templates/search.html      # has console logging (timing + raw response) for debugging
```

## The search-quality problem: full history, so the reasoning isn't lost

A structured manual test (~15 real queries against ~40 photos, 4 registered people) surfaced real failures. Investigating separated them into categories:

1. **Real bugs, unrelated to parser design** — a date-format bug (Immich's API needs full ISO 8601 with `Z`; bare `.isoformat()` caused 400s) and full-name-only person matching (registered as "Kyle Brothers," typing "Kyle" never matched). **Both fixed** in `query_parser_rules.py`.
2. **A genuine rule-based ceiling** — colloquial location terms ("New York") not matching Immich's actual EXIF-derived granularity ("Manhattan," "Edgewater"), and general phrasing brittleness. This motivated trying an LLM parser.
3. **The local LLM attempt (`query_parser_llm.py`, Ollama + `llama3.2:3b` on `gpu-ml`) — tried, found insufficient, documented as a real empirical finding, not abandoned casually:**
   - First attempt: bare `"format": "json"` + default temperature → model hallucinated content unrelated to the query (e.g. `object_query: "house"` for "Kevin only") — it was echoing prompt fragments, not extracting real content.
   - Fixed via full JSON Schema (`format` as a schema object, not the string `"json"`) + `temperature: 0` + a worked example in the prompt → hallucination stopped completely.
   - **But then:** list-matching against grounded people failed for partial/lowercase input ("kevin" didn't match "Kevin Brothers") while working for exact matches ("Kevin Brothers" matched fine). Isolated conclusively via a controlled A/B query — **this is a real capability ceiling of a 3B model doing semantic list-matching under strict schema constraints**, not a prompt-tuning problem.
   - Attempted mitigation (have the LLM extract raw mentions only, resolve against lists in Python) was designed but **not filed/built** — superseded by the architecture decision below before implementation.

## Agreed design going forward: tool-calling search agent (Claude API)

**Decision, made explicitly rather than defaulting into it:** the one-shot "parse query into a fixed JSON schema" approach is the wrong shape for this problem regardless of model size — it can't express things Immich's fixed API can't already do (e.g. "only Kevin, no one else in frame"), and rigid schema extraction is exactly where the local model struggled. The fix is architectural: a **tool-calling agent loop**, not a bigger model doing the same rigid task.

**Model: Claude API, not local.** Two independent reasons converged: (1) empirically, the 3B local model already struggled with a *simpler* task than this redesign requires; (2) a tool-calling loop means multiple sequential model calls per search — at ~4–5s/call measured on the 1060, a multi-turn loop would mean 15–20+ seconds per search locally, likely a bad user experience regardless of quality. Claude's API latency makes the architecture viable.

**Privacy, decided knowingly, not glossed over:** using the API means the query text and grounding lists (people's names, city names) — and now, SQL query results, which could include names/dates/locations — leave the home network to Anthropic's API. Verified 2026-07-11: Anthropic's **commercial API** terms (distinct from consumer claude.ai plans) contractually prohibit training on API data by default, with short retention (~7 days) for abuse-screening only. This is *not* the same policy as consumer Claude Free/Pro/Max, which now default to training-eligible — that distinction doesn't apply to API usage. Re-verify current terms before final commitment, since policies do change; this was accurate as of the date above. No photos/images need to leave the network for this — text only.

**Tool: raw read-only SQL, decided explicitly over safer structured alternatives** (the user's call, made knowingly against the "start narrow" recommendation, specifically to avoid recurring "nickel and diming" friction from adding one narrow tool per discovered gap). This is the highest-risk, most novel piece and needs real safeguards, not just "read-only" as a hand-wave:

- **Dedicated Postgres role** for this tool only, `REVOKE ALL` then `GRANT SELECT` on only the specific tables needed (assets, people, exif/metadata, face-asset links) — **explicitly excluding** Immich's `users`, `sessions`, `api_keys`, and any auth tables. A blanket read-only grant on the whole schema is not sufficient.
- **Server-side verification the generated query is a single `SELECT`** before execution — don't trust the model's own restraint, verify.
- **`statement_timeout`** on the role/connection.
- **Hard row-limit cap** on results returned to the model (context size + cost control).

**Model tier for the SQL tool specifically — undecided, to be settled empirically:** leaning Haiku (this is tool-orchestration, closer to its strengths than deep reasoning; cost/latency favor it for a multi-call loop) but SQL generation is a step harder than the structured tool calls discussed earlier, closer to code generation — exactly the kind of task where local-model capability gaps showed up before. **Plan: build with the model swappable per-tool-call (not necessarily one model for the whole loop), start with Haiku, run a structured test list that specifically stresses SQL correctness (compound conditions, the solo-person case, a join across people/assets), escalate to Sonnet only if Haiku's SQL is unreliable.**

### Proposed tool set (design agreed, not yet built)

- `search_people(name_fragment)` — fuzzy match against registered people
- `search_cities(place_fragment)` — fuzzy match against real EXIF city values
- `search_landmarks(name_fragment)` — same, against the labeled landmark reference set
- `search_photos(person_ids, city, date_from, date_to, object_query)` — thin wrapper over Immich's existing `smart_search`/`search_metadata`
- `run_readonly_sql(query)` — the safeguarded raw-SQL tool described above
- `finalize_search(asset_ids, explanation)` — explicit end-of-loop signal; the `explanation` becomes a real UX improvement over today's opaque `parsed` dict

### Loop bounds (agreed, not yet built)

- Hard cap on tool-call turns (e.g. 6) — force a stop and return best-effort if not finalized by then
- Wall-clock timeout on the whole agent call, independent of Gunicorn's timeout (same race-condition lesson learned from the Ollama timeout bug earlier)
- **On total failure, fall back to `query_parser_rules.py`** — it remains the permanent safety net regardless of what happens above it
- Log the full tool-call trace (which tools, what arguments, what came back) and expose it in the API response — debuggability changes shape with this design (no longer a deterministic Python trace), so this replaces that lost visibility

### Open question, not yet decided

**Fate of the Ollama/local-LLM path** (`query_parser_llm.py`, the `gpu-ml` `ollama` service). Options: keep as a documented dead-end/reference for why the architecture changed; keep as a possible future fully-offline fallback tier; remove entirely. Not decided — flag this explicitly in the next conversation rather than assuming.

## Other known gaps, unrelated to the search-agent redesign

- **Landmark labeling has no caller.** `reference_embeddings.add_reference()` exists but nothing (CLI or route) invokes it yet. Landmark matching itself works (correctly distinguished "Spaceship Earth" from other WDW landmarks in the initial test batch).
- **Full Dropbox backfill hasn't started.** Still running against the initial ~40-photo test batch used for search validation.

## Repository structure (current + planned)

```
photo-search/
├── README.md
├── docker-compose.yml / Makefile / .env.example / .gitignore
├── immich-server/          # Dockerfile, entrypoint-wrapper.sh, rclone.conf.example — done, verified
├── search-api/             # see detailed listing above
└── scripts/
    ├── trigger_rescan.sh    # manual/on-demand only — periodic scanning is native
    └── check_asset_exists.py

# NOT YET CREATED — planned for the tool-calling redesign:
search-api/
├── search_agent.py          # new: the tool-calling loop, replaces app.py's inline logic
├── tools.py                 # new: search_people/cities/landmarks/photos, finalize_search
├── sql_tool.py               # new: run_readonly_sql with all safeguards above
```

`gpu-ml` is a separate repo (sibling to this one) — runs `immich-machine-learning` (in active use) and `ollama` (status pending the open question above).

## Concrete next steps, in order

1. **Decide the Ollama/local-LLM fate** (open question above) — affects whether `query_parser_llm.py` and `gpu-ml`'s `ollama` service are touched at all.
2. **Set up Anthropic API access** — API key, confirm commercial (not consumer) terms apply, decide if ZDR is worth pursuing given actual usage scale (probably not, but worth a conscious no).
3. **Build the dedicated read-only Postgres role** for the SQL tool — this is infrastructure, not application code, and should exist before `sql_tool.py` is written against it.
4. **Build the agent loop + four base tools** (people/cities/landmarks/photos search + `finalize_search`) — prove the loop works before adding SQL.
5. **Build `sql_tool.py`** with all safeguards, defaulting to Haiku.
6. **Build a SQL-specific structured test list** (compound conditions, solo-person, joins) and run it against Haiku; escalate that tool's model to Sonnet only if needed.
7. **Re-run the original structured test list** (the "Right/Wrong" format used earlier) against the full new agent, to confirm it actually resolves the person-matching and location-granularity problems that motivated this redesign.
8. Only after the above: revisit the still-open landmark-labeling-CLI gap and the full Dropbox backfill.
