-- Sidecar DB — initial schema
--
-- Dev database, wipe-and-redevelop freely. Owned entirely by this project,
-- separate from Immich's own Postgres instance (see README: no write-back
-- into Immich's schema, keyed on Immich's asset UUID by convention only —
-- no FK constraint, since the two databases are intentionally decoupled).
--
-- Design: typed per-tool tables, not EAV. Matches the existing pattern the
-- SQL agent already reads (asset_exif, asset_face, asset_ocr in Immich's own
-- schema are all typed), and makes LLM-generated SQL against this schema far
-- more reliable than a generic key/value table would be.

-- enrichment_status is written unconditionally by every enrichment run,
-- regardless of whether the tool produced any fact rows. Without this, a
-- photo with zero detected objects (a real, correct result) is indistinguishable
-- from a photo that was never processed. Also drives "what needs enrichment"
-- (LEFT JOIN ... WHERE status IS NULL) and reaugmentation of reappeared UUIDs
-- after an Immich move/rescan.
CREATE TABLE enrichment_status (
    asset_id      uuid NOT NULL,
    tool          text NOT NULL,          -- e.g. 'reverse_geocode', 'object_detect'
    model_version text NOT NULL,
    status        text NOT NULL CHECK (status IN ('done', 'failed')),
    error_detail  text,                   -- populated when status = 'failed'
    computed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, tool, model_version)
);

CREATE INDEX idx_enrichment_status_tool ON enrichment_status (tool, status);


-- Reverse-geocode gap fill: coords-with-no-city photos, resolved via
-- Immich's own /map/reverse-geocode endpoint (option A from the design note)
-- and/or a richer polygon-based source (e.g. Overture Divisions -- see
-- overture_geocode.py) for cases Immich's own GeoNames-backed matcher
-- misses entirely (confirmed 2026-08: Immich returned no state at all for a
-- real Alaska coordinate that Overture correctly resolved to county/state/
-- country).
--
-- county is nullable and, as of 2026-08, only ever populated by an
-- Overture-based enrichment -- Immich's own geocoder (source =
-- 'immich_reverse_geocode') has no concept of county and will always leave
-- it null. Added specifically because "county but no locality/city" is a
-- common, real, search-worthy case for rural/unincorporated areas (e.g.
-- Alaska's "Unorganized Borough" designations, and rural Kentucky counties
-- with no incorporated city nearby) -- not an edge case to drop silently.
CREATE TABLE resolved_geo (
    asset_id    uuid PRIMARY KEY,
    city        text,
    county      text,
    state       text,
    country     text,
    source      text NOT NULL,            -- e.g. 'immich_reverse_geocode', 'overture_divisions'
    computed_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_resolved_geo_city ON resolved_geo (city);
CREATE INDEX idx_resolved_geo_county ON resolved_geo (county);


-- Object/person/animal counts, open-vocabulary (YOLO-World candidate).
-- model_version is part of the primary key so re-running with a new model
-- doesn't overwrite prior results — old and new coexist until pruned.
CREATE TABLE object_counts (
    asset_id       uuid NOT NULL,
    class          text NOT NULL,         -- open-vocab label, e.g. 'person', 'dog'
    count          int NOT NULL,
    avg_confidence real,
    model_version  text NOT NULL,
    computed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, class, model_version)
);

CREATE INDEX idx_object_counts_class ON object_counts (class);


-- Second embedding source for famous landmarks (DELF/DELG candidate),
-- layered on top of the existing curated CLIP-embedding matcher in
-- search-api/landmark/, not a replacement for it.
CREATE TABLE landmark_matches (
    asset_id      uuid NOT NULL,
    landmark_name text NOT NULL,
    confidence    real,
    model_version text NOT NULL,
    computed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, landmark_name, model_version)
);


-- Fixed pinned dev test set: 100 photos + hand-picked hard cases, so runs
-- across different models/approaches are comparable over time. `label`
-- distinguishes 'random' fill from specific hard cases (e.g.
-- 'hard_case:disney_no_city').
CREATE TABLE test_set (
    asset_id  uuid PRIMARY KEY,
    label     text,
    added_at  timestamptz NOT NULL DEFAULT now()
);
