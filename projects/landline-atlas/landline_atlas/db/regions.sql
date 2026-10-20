-- 地域分類DB：町丁目・統計/GIS特徴量・外部証拠・12分類の run

CREATE TABLE IF NOT EXISTS source_document (
    source_id    TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    url          TEXT,
    source_year  INTEGER,
    license      TEXT,
    sha256       TEXT,
    retrieved_at TEXT
);

CREATE TABLE IF NOT EXISTS data_load (
    load_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL CHECK (kind IN ('census', 'economic', 'housing', 'gis', 'evidence', 'manual')),
    source_id TEXT REFERENCES source_document(source_id),
    loaded_at TEXT NOT NULL,
    note      TEXT
);

CREATE TABLE IF NOT EXISTS town (
    town_key          TEXT NOT NULL,
    census_year       INTEGER NOT NULL,
    boundary_version  TEXT NOT NULL,
    prefecture_code   TEXT NOT NULL CHECK (length(prefecture_code) = 2),
    municipality_code TEXT NOT NULL CHECK (length(municipality_code) = 5),
    town_name         TEXT,
    secrecy           TEXT NOT NULL DEFAULT 'none'
        CHECK (secrecy IN ('none', 'suppressed', 'merged_into_other', 'merge_target')),
    merged_into       TEXT,
    source_id         TEXT REFERENCES source_document(source_id),
    PRIMARY KEY (town_key, census_year, boundary_version),
    CHECK (substr(municipality_code, 1, 2) = prefecture_code),
    CHECK (substr(town_key, 1, 5) = municipality_code),
    CHECK ((secrecy = 'merged_into_other') = (merged_into IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS town_feature (
    load_id          INTEGER NOT NULL REFERENCES data_load(load_id),
    town_key         TEXT NOT NULL,
    census_year      INTEGER NOT NULL,
    boundary_version TEXT NOT NULL,
    feature          TEXT NOT NULL,
    value            REAL,
    state            TEXT NOT NULL
        CHECK (state IN ('observed', 'zero', 'missing', 'suppressed', 'merged', 'undefined')),
    source_id        TEXT REFERENCES source_document(source_id),
    PRIMARY KEY (load_id, town_key, census_year, boundary_version, feature),
    CHECK ((state IN ('observed', 'zero')) = (value IS NOT NULL)),
    CHECK (state <> 'zero' OR value = 0)
);
CREATE INDEX IF NOT EXISTS idx_feature_lookup ON town_feature (census_year, boundary_version, town_key, feature);

CREATE TABLE IF NOT EXISTS external_evidence (
    evidence_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    load_id          INTEGER NOT NULL REFERENCES data_load(load_id),
    town_key         TEXT NOT NULL,
    census_year      INTEGER NOT NULL,
    boundary_version TEXT NOT NULL,
    evidence_type    TEXT NOT NULL,
    supports_class   TEXT NOT NULL,
    grade            TEXT NOT NULL CHECK (grade IN ('A', 'B', 'C')),
    source_id        TEXT NOT NULL REFERENCES source_document(source_id),
    note             TEXT
);

CREATE TABLE IF NOT EXISTS class_run (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_version     TEXT NOT NULL,
    rule_sha256      TEXT NOT NULL,
    rule_json        TEXT NOT NULL,
    input_sha256     TEXT NOT NULL,
    max_load_id      INTEGER NOT NULL,
    code_version     TEXT NOT NULL,
    census_year      INTEGER NOT NULL,
    boundary_version TEXT NOT NULL,
    scope            TEXT NOT NULL,
    evaluated_at     TEXT NOT NULL
);

-- 指示書 §6 の必須出力構造 regional_classification（＋ run_id, axis, reason, evidence_json）
CREATE TABLE IF NOT EXISTS regional_classification (
    run_id            INTEGER NOT NULL REFERENCES class_run(run_id),
    prefecture_code   TEXT NOT NULL,
    municipality_code TEXT NOT NULL,
    town_key          TEXT NOT NULL,
    census_year       INTEGER NOT NULL,
    boundary_version  TEXT NOT NULL,
    class_code        TEXT NOT NULL
        CHECK (class_code IN ('R1','R2','R3','R4','R5','R6','L1','L2','L3','L4','L5','L6')),
    axis              TEXT NOT NULL CHECK (axis IN ('R', 'L')),
    primary_flag      INTEGER NOT NULL CHECK (primary_flag IN (0, 1)),
    secondary_flag    INTEGER NOT NULL CHECK (secondary_flag IN (0, 1)),
    overlap_flag      INTEGER NOT NULL CHECK (overlap_flag IN (0, 1)),
    score             REAL,
    evidence_grade    TEXT CHECK (evidence_grade IN ('A', 'B', 'C')),
    label_status      TEXT NOT NULL CHECK (label_status IN
        ('confirmed', 'candidate', 'not_applicable', 'on_hold', 'insufficient_data', 'undeterminable')),
    reason            TEXT NOT NULL,
    rule_version      TEXT NOT NULL,
    source_year       INTEGER,
    source_url        TEXT,
    evidence_json     TEXT NOT NULL,
    evaluated_at      TEXT NOT NULL,
    PRIMARY KEY (run_id, town_key, census_year, boundary_version, class_code),
    CHECK (NOT (primary_flag = 1 AND secondary_flag = 1)),
    CHECK (primary_flag = 0 OR label_status IN ('confirmed', 'candidate'))
);
CREATE INDEX IF NOT EXISTS idx_rc_town ON regional_classification (town_key, census_year, boundary_version, run_id);

CREATE TABLE IF NOT EXISTS axis_result (
    run_id           INTEGER NOT NULL REFERENCES class_run(run_id),
    town_key         TEXT NOT NULL,
    census_year      INTEGER NOT NULL,
    boundary_version TEXT NOT NULL,
    axis             TEXT NOT NULL CHECK (axis IN ('R', 'L')),
    primary_class    TEXT,
    state            TEXT NOT NULL CHECK (state IN ('decided', 'on_hold', 'none', 'insufficient_data', 'undeterminable')),
    reason           TEXT NOT NULL,
    PRIMARY KEY (run_id, town_key, census_year, boundary_version, axis),
    CHECK ((state = 'decided') = (primary_class IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS gold_label (
    town_key         TEXT NOT NULL,
    census_year      INTEGER NOT NULL,
    boundary_version TEXT NOT NULL,
    axis             TEXT NOT NULL CHECK (axis IN ('R', 'L')),
    class_code       TEXT NOT NULL,
    annotator        TEXT NOT NULL,
    basis            TEXT NOT NULL,
    labeled_at       TEXT NOT NULL,
    PRIMARY KEY (town_key, census_year, boundary_version, axis, annotator)
);

CREATE VIEW IF NOT EXISTS current_classification AS
SELECT rc.* FROM regional_classification rc
WHERE rc.run_id = (SELECT MAX(r2.run_id) FROM regional_classification r2
                   WHERE r2.town_key = rc.town_key AND r2.census_year = rc.census_year
                     AND r2.boundary_version = rc.boundary_version);

CREATE VIEW IF NOT EXISTS current_axis AS
SELECT ar.* FROM axis_result ar
WHERE ar.run_id = (SELECT MAX(a2.run_id) FROM axis_result a2
                   WHERE a2.town_key = ar.town_key AND a2.census_year = ar.census_year
                     AND a2.boundary_version = ar.boundary_version);

CREATE TRIGGER IF NOT EXISTS trg_feat_no_update BEFORE UPDATE ON town_feature
BEGIN SELECT RAISE(ABORT, 'town_feature is append-only; add a new data_load'); END;
CREATE TRIGGER IF NOT EXISTS trg_feat_no_delete BEFORE DELETE ON town_feature
BEGIN SELECT RAISE(ABORT, 'town_feature is append-only; add a new data_load'); END;
CREATE TRIGGER IF NOT EXISTS trg_ev_no_update BEFORE UPDATE ON external_evidence
BEGIN SELECT RAISE(ABORT, 'external_evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_ev_no_delete BEFORE DELETE ON external_evidence
BEGIN SELECT RAISE(ABORT, 'external_evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_run_no_update BEFORE UPDATE ON class_run
BEGIN SELECT RAISE(ABORT, 'class_run is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_run_no_delete BEFORE DELETE ON class_run
BEGIN SELECT RAISE(ABORT, 'class_run is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_rc_no_update BEFORE UPDATE ON regional_classification
BEGIN SELECT RAISE(ABORT, 'regional_classification is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_rc_no_delete BEFORE DELETE ON regional_classification
BEGIN SELECT RAISE(ABORT, 'regional_classification is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_axis_no_update BEFORE UPDATE ON axis_result
BEGIN SELECT RAISE(ABORT, 'axis_result is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_axis_no_delete BEFORE DELETE ON axis_result
BEGIN SELECT RAISE(ABORT, 'axis_result is append-only'); END;

-- 国勢調査などの原本（ZIP）。内容の SHA-256 で一意。番号側の原本とは分けて保管する
CREATE TABLE IF NOT EXISTS raw_census (
    sha256       TEXT NOT NULL,
    url          TEXT NOT NULL,
    filename     TEXT,
    stored_path  TEXT NOT NULL,
    item         TEXT NOT NULL,      -- boundary / 表 ID（例 T001081）
    prefecture   TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (sha256, url)
);
CREATE TRIGGER IF NOT EXISTS trg_rawc_no_update BEFORE UPDATE ON raw_census
BEGIN SELECT RAISE(ABORT, 'raw_census is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_rawc_no_delete BEFORE DELETE ON raw_census
BEGIN SELECT RAISE(ABORT, 'raw_census is append-only'); END;
