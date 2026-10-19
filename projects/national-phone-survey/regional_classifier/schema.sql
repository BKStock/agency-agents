-- regional_classifier schema (地域分類DB + 統合分析DB)
--
-- 原則
--   * 行政コード・町丁目キーはすべて TEXT（先頭ゼロ保持）
--   * 欠損とゼロを区別するため、数値には必ず state を併記する
--   * 分類結果・入力データは追記専用（UPDATE / DELETE はトリガーで拒否）
--   * 電話番号指定データ（v1.2 DB）は別ファイルのまま、読み取り専用で取り込む

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 出典台帳：統計・GIS・番号指定原本・外部証拠のすべてがここを参照する
CREATE TABLE IF NOT EXISTS source_document (
    source_id    TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    publisher    TEXT,
    url          TEXT,
    source_year  INTEGER,
    license      TEXT,
    sha256       TEXT,
    retrieved_at TEXT
);

-- 取込単位。特徴量・外部証拠は load_id 付きで追記し、分類 run は参照した最大 load_id を記録する
CREATE TABLE IF NOT EXISTS data_load (
    load_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    loaded_at TEXT NOT NULL,
    kind      TEXT NOT NULL,          -- census / gis / evidence / phone / manual
    source_id TEXT REFERENCES source_document(source_id),
    note      TEXT
);

-- 町丁目・大字マスタ（調査年×境界版ごと）
CREATE TABLE IF NOT EXISTS town_master (
    town_key          TEXT NOT NULL,
    census_year       INTEGER NOT NULL,
    boundary_version  TEXT NOT NULL,
    prefecture_code   TEXT NOT NULL CHECK (length(prefecture_code) = 2),
    municipality_code TEXT NOT NULL CHECK (length(municipality_code) = 5),
    town_name         TEXT,
    hierarchy_level   INTEGER,
    secrecy_state     TEXT NOT NULL DEFAULT 'none'
        CHECK (secrecy_state IN ('none', 'suppressed', 'merged_into_other', 'merge_target')),
    merged_into       TEXT,
    source_id         TEXT REFERENCES source_document(source_id),
    PRIMARY KEY (town_key, census_year, boundary_version),
    CHECK (substr(municipality_code, 1, 2) = prefecture_code),
    CHECK ((secrecy_state = 'merged_into_other') = (merged_into IS NOT NULL))
);

-- 特徴量（生値・GIS値）。value が NULL でも state で理由を区別する
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
CREATE INDEX IF NOT EXISTS idx_town_feature_lookup
    ON town_feature (census_year, boundary_version, town_key, feature, load_id);

-- 国勢調査以外の裏付け（分譲/賃貸、公営住宅・UR、別荘台帳など）
CREATE TABLE IF NOT EXISTS external_evidence (
    evidence_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    load_id          INTEGER NOT NULL REFERENCES data_load(load_id),
    town_key         TEXT NOT NULL,
    census_year      INTEGER NOT NULL,
    boundary_version TEXT NOT NULL,
    evidence_type    TEXT NOT NULL,
    supports_class   TEXT NOT NULL,
    grade            TEXT NOT NULL CHECK (grade IN ('A', 'B', 'C')),
    value            REAL,
    source_id        TEXT NOT NULL REFERENCES source_document(source_id),
    note             TEXT
);

-- 分類 run：ルール本文と入力ハッシュを保存し、過去結果を再現できるようにする
CREATE TABLE IF NOT EXISTS classification_run (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_version     TEXT NOT NULL,
    rule_sha256      TEXT NOT NULL,
    rule_json        TEXT NOT NULL,
    input_sha256     TEXT NOT NULL,
    max_load_id      INTEGER NOT NULL,
    code_version     TEXT NOT NULL,
    census_year      INTEGER NOT NULL,
    boundary_version TEXT NOT NULL,
    scope            TEXT,
    evaluated_at     TEXT NOT NULL,
    note             TEXT
);

CREATE TABLE IF NOT EXISTS regional_classification (
    run_id            INTEGER NOT NULL REFERENCES classification_run(run_id),
    prefecture_code   TEXT NOT NULL,
    municipality_code TEXT NOT NULL,
    town_key          TEXT NOT NULL,
    census_year       INTEGER NOT NULL,
    boundary_version  TEXT NOT NULL,
    class_code        TEXT NOT NULL
        CHECK (class_code IN ('R1','R2','R3','R4','R5','R6','L1','L2','L3','L4','L5','L6')),
    axis              TEXT NOT NULL CHECK (axis IN ('R', 'L')),
    primary_flag      INTEGER NOT NULL DEFAULT 0 CHECK (primary_flag IN (0, 1)),
    secondary_flag    INTEGER NOT NULL DEFAULT 0 CHECK (secondary_flag IN (0, 1)),
    overlap_flag      INTEGER NOT NULL DEFAULT 0 CHECK (overlap_flag IN (0, 1)),
    score             REAL,
    evidence_grade    TEXT CHECK (evidence_grade IN ('A', 'B', 'C')),
    label_status      TEXT NOT NULL
        CHECK (label_status IN ('confirmed', 'candidate', 'not_applicable',
                                'on_hold', 'insufficient_data', 'undeterminable')),
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
CREATE INDEX IF NOT EXISTS idx_rc_town
    ON regional_classification (town_key, census_year, boundary_version, run_id);
CREATE INDEX IF NOT EXISTS idx_rc_class
    ON regional_classification (class_code, label_status);

-- 軸ごとの主分類判定（R軸・L軸を独立に保持）
CREATE TABLE IF NOT EXISTS town_axis_result (
    run_id           INTEGER NOT NULL REFERENCES classification_run(run_id),
    town_key         TEXT NOT NULL,
    census_year      INTEGER NOT NULL,
    boundary_version TEXT NOT NULL,
    axis             TEXT NOT NULL CHECK (axis IN ('R', 'L')),
    primary_class    TEXT,
    state            TEXT NOT NULL
        CHECK (state IN ('decided', 'on_hold', 'none', 'insufficient_data', 'undeterminable')),
    reason           TEXT NOT NULL,
    PRIMARY KEY (run_id, town_key, census_year, boundary_version, axis),
    CHECK ((state = 'decided') = (primary_class IS NOT NULL))
);

-- 正解ラベル（評価専用。判定者と根拠を必須とする）
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

-- ===== 固定電話番号側（v1.2 DB から読み取り専用で取り込んだ写し） =====

CREATE TABLE IF NOT EXISTS number_designation (
    load_id          INTEGER NOT NULL REFERENCES data_load(load_id),
    area_code        TEXT NOT NULL,      -- 市外局番（0 を含む文字列）
    local_code       TEXT NOT NULL,      -- 市内局番
    number_area_code TEXT,               -- 番号区画コード
    carrier          TEXT,
    usage_status     TEXT,
    as_of_date       TEXT NOT NULL,      -- 資料基準日
    series           TEXT,
    source_url       TEXT,
    source_sha256    TEXT,
    PRIMARY KEY (load_id, area_code, local_code, as_of_date)
);

-- 番号区画 → 市区町村（区画が市区町村を含むという「区画レベル」の関係）
CREATE TABLE IF NOT EXISTS number_area_municipality (
    number_area_code  TEXT NOT NULL,
    municipality_code TEXT NOT NULL CHECK (length(municipality_code) = 5),
    area_code         TEXT,
    ma_name           TEXT,
    coverage          TEXT NOT NULL CHECK (coverage IN ('whole', 'part', 'unknown')),
    valid_from        TEXT NOT NULL,
    valid_to          TEXT,
    source_id         TEXT REFERENCES source_document(source_id),
    PRIMARY KEY (number_area_code, municipality_code, valid_from)
);

-- 公開施設の電話番号観測（個人宅は対象外）
CREATE TABLE IF NOT EXISTS facility_observation (
    obs_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    facility_name    TEXT NOT NULL,
    facility_type    TEXT NOT NULL
        CHECK (facility_type IN ('public_office', 'school', 'medical', 'shop', 'corporation', 'other_facility')),
    area_code        TEXT NOT NULL,
    local_code       TEXT NOT NULL,
    address          TEXT NOT NULL,
    town_key         TEXT,
    census_year      INTEGER,
    boundary_version TEXT,
    geocode_method   TEXT,
    verified         INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
    source_url       TEXT NOT NULL,
    observed_on      TEXT NOT NULL
);

-- 町丁目×局番の関連。区画レベルと施設観測を relation_type で区別する
CREATE VIEW IF NOT EXISTS municipality_code_relation AS
SELECT DISTINCT nam.municipality_code,
       nd.area_code, nd.local_code, nd.number_area_code, nd.carrier,
       nd.usage_status, nd.as_of_date, nd.source_url,
       'designation_area' AS relation_type,
       nam.coverage
FROM number_designation nd
JOIN number_area_municipality nam ON nam.number_area_code = nd.number_area_code;

CREATE VIEW IF NOT EXISTS town_observed_code AS
SELECT fo.town_key, fo.census_year, fo.boundary_version,
       fo.area_code, fo.local_code,
       'observed_facility' AS relation_type,
       COUNT(*)            AS observation_count,
       SUM(fo.verified)    AS verified_count,
       CASE WHEN SUM(fo.verified) > 0 THEN 'B' ELSE 'C' END AS relation_grade,
       MIN(fo.observed_on) AS first_observed_on,
       MAX(fo.observed_on) AS last_observed_on,
       group_concat(DISTINCT fo.source_url) AS source_urls
FROM facility_observation fo
WHERE fo.town_key IS NOT NULL
GROUP BY fo.town_key, fo.census_year, fo.boundary_version, fo.area_code, fo.local_code;

-- 最新 run の分類結果（町丁目ごとに最大 run_id）
CREATE VIEW IF NOT EXISTS current_classification AS
SELECT rc.*
FROM regional_classification rc
WHERE rc.run_id = (
    SELECT MAX(r2.run_id) FROM regional_classification r2
    WHERE r2.town_key = rc.town_key
      AND r2.census_year = rc.census_year
      AND r2.boundary_version = rc.boundary_version
);

CREATE VIEW IF NOT EXISTS current_axis_result AS
SELECT ar.*
FROM town_axis_result ar
WHERE ar.run_id = (
    SELECT MAX(a2.run_id) FROM town_axis_result a2
    WHERE a2.town_key = ar.town_key
      AND a2.census_year = ar.census_year
      AND a2.boundary_version = ar.boundary_version
);

-- ===== 追記専用の強制 =====
CREATE TRIGGER IF NOT EXISTS trg_rc_no_update BEFORE UPDATE ON regional_classification
BEGIN SELECT RAISE(ABORT, 'regional_classification is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_rc_no_delete BEFORE DELETE ON regional_classification
BEGIN SELECT RAISE(ABORT, 'regional_classification is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_run_no_update BEFORE UPDATE ON classification_run
BEGIN SELECT RAISE(ABORT, 'classification_run is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_run_no_delete BEFORE DELETE ON classification_run
BEGIN SELECT RAISE(ABORT, 'classification_run is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_axis_no_update BEFORE UPDATE ON town_axis_result
BEGIN SELECT RAISE(ABORT, 'town_axis_result is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_axis_no_delete BEFORE DELETE ON town_axis_result
BEGIN SELECT RAISE(ABORT, 'town_axis_result is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_feature_no_update BEFORE UPDATE ON town_feature
BEGIN SELECT RAISE(ABORT, 'town_feature is append-only; add a new data_load'); END;
CREATE TRIGGER IF NOT EXISTS trg_feature_no_delete BEFORE DELETE ON town_feature
BEGIN SELECT RAISE(ABORT, 'town_feature is append-only; add a new data_load'); END;
CREATE TRIGGER IF NOT EXISTS trg_evidence_no_update BEFORE UPDATE ON external_evidence
BEGIN SELECT RAISE(ABORT, 'external_evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_evidence_no_delete BEFORE DELETE ON external_evidence
BEGIN SELECT RAISE(ABORT, 'external_evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_designation_no_update BEFORE UPDATE ON number_designation
BEGIN SELECT RAISE(ABORT, 'number_designation is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_designation_no_delete BEFORE DELETE ON number_designation
BEGIN SELECT RAISE(ABORT, 'number_designation is append-only'); END;
