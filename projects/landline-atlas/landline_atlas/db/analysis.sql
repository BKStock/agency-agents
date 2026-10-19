-- 統合分析DB：パイプライン実行・品質検査・調査優先順位・評価

CREATE TABLE IF NOT EXISTS pipeline_run (
    pipeline_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    outcome      TEXT,
    manifest     TEXT
);

CREATE TABLE IF NOT EXISTS qa_result (
    pipeline_id  INTEGER,
    check_id     TEXT NOT NULL,
    severity     TEXT NOT NULL CHECK (severity IN ('error', 'warning', 'info')),
    passed       INTEGER NOT NULL CHECK (passed IN (0, 1)),
    detail       TEXT NOT NULL,
    checked_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS priority_run (
    priority_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    weights         TEXT NOT NULL,
    computed_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS priority_item (
    priority_run_id   INTEGER NOT NULL REFERENCES priority_run(priority_run_id),
    rank              INTEGER NOT NULL,
    area_code         TEXT NOT NULL,
    local_code        TEXT NOT NULL,
    number_area_code  TEXT,
    score             REAL NOT NULL,
    components        TEXT NOT NULL,     -- JSON：成分ごとの値
    evidence_level    TEXT NOT NULL CHECK (evidence_level IN
        ('designation_only', 'observed_unverified', 'observed_verified')),
    municipalities    TEXT NOT NULL,     -- 区画が含む市区町村
    facility_types    TEXT NOT NULL,     -- 確認に使う公開施設の種別
    reasons           TEXT NOT NULL,
    PRIMARY KEY (priority_run_id, area_code, local_code)
);

CREATE TABLE IF NOT EXISTS evaluation_result (
    evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    axis          TEXT NOT NULL,
    result_json   TEXT NOT NULL,
    evaluated_at  TEXT NOT NULL
);
