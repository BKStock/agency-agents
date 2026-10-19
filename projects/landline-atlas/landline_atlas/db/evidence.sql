-- 公開施設番号エビデンスDB（個人宅・未公開番号は対象外）

CREATE TABLE IF NOT EXISTS facility_observation (
    obs_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    facility_name    TEXT NOT NULL,
    facility_type    TEXT NOT NULL CHECK (facility_type IN
        ('public_office', 'school', 'medical', 'library', 'shop', 'corporation', 'other_facility')),
    area_code        TEXT NOT NULL CHECK (area_code GLOB '0[0-9]*'),
    local_code       TEXT NOT NULL CHECK (local_code GLOB '[0-9]*'),
    address          TEXT NOT NULL,
    town_key         TEXT,
    census_year      INTEGER,
    boundary_version TEXT,
    geocode_method   TEXT,
    verified         INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
    source_url       TEXT NOT NULL CHECK (source_url <> ''),
    observed_on      TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_obs_no_delete BEFORE DELETE ON facility_observation
BEGIN SELECT RAISE(ABORT, 'facility_observation is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_obs_no_update BEFORE UPDATE ON facility_observation
BEGIN SELECT RAISE(ABORT, 'facility_observation is append-only'); END;
