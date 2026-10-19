-- 固定電話番号指定DB：取得・原本・版・指定行・差分

CREATE TABLE IF NOT EXISTS fetch_log (
    fetch_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL,
    canonical    TEXT NOT NULL,
    origin       TEXT NOT NULL CHECK (origin IN ('official', 'wayback', 'manual')),
    attempt      INTEGER NOT NULL,
    status       INTEGER,              -- HTTP status（通信失敗は NULL）
    error        TEXT,
    sha256       TEXT,
    fetched_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fetch_canonical ON fetch_log (canonical);

-- 原本（内容のSHA-256で一意。同じ内容は二度保存しない）
CREATE TABLE IF NOT EXISTS raw_file (
    sha256       TEXT PRIMARY KEY,
    size         INTEGER NOT NULL,
    stored_path  TEXT NOT NULL,
    first_seen   TEXT NOT NULL
);

-- 原本の入手経路（同じ原本が複数の URL・経路から来てもよい）
CREATE TABLE IF NOT EXISTS raw_source (
    sha256       TEXT NOT NULL REFERENCES raw_file(sha256),
    url          TEXT NOT NULL,
    canonical    TEXT NOT NULL,
    origin       TEXT NOT NULL CHECK (origin IN ('official', 'wayback', 'manual')),
    wayback_ts   TEXT,
    filename     TEXT,
    series       TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (sha256, url)
);

-- 版：原本1つ（ZIPなら中のファイル1つ）×系統。基準日は原本の記載から読む
CREATE TABLE IF NOT EXISTS version (
    version_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256        TEXT NOT NULL REFERENCES raw_file(sha256),
    member        TEXT NOT NULL DEFAULT '',     -- ZIP 内のファイル名
    series        TEXT NOT NULL,
    as_of_date    TEXT,                         -- YYYY-MM-DD（読めなければ NULL）
    date_source   TEXT NOT NULL CHECK (date_source IN ('content', 'wayback_ts', 'manual', 'unknown')),
    layout        TEXT NOT NULL,
    row_count     INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,              -- 正規化した行内容のハッシュ（表記ゆれ以外の同一性）
    parsed_at     TEXT NOT NULL,
    UNIQUE (sha256, member, series)
);

CREATE TABLE IF NOT EXISTS designation_row (
    version_id        INTEGER NOT NULL REFERENCES version(version_id),
    row_no            INTEGER NOT NULL,
    area_code         TEXT NOT NULL,            -- 市外局番（先頭0を含む文字列）
    local_code        TEXT NOT NULL,            -- 市内局番
    number_area_code  TEXT,                     -- 番号区画コード
    number_area_name  TEXT,
    carrier           TEXT,
    usage_status      TEXT,
    repaired          TEXT,                     -- 取込時の補正（leading_zero / fill_down）
    PRIMARY KEY (version_id, row_no),
    CHECK (area_code GLOB '0[0-9]*' AND local_code GLOB '[0-9]*')
);
CREATE INDEX IF NOT EXISTS idx_row_code ON designation_row (area_code, local_code);

-- 系統×基準日ごとの版選択（理由を必ず記録。追記で更新し、最新の決定が有効）
CREATE TABLE IF NOT EXISTS version_selection (
    selection_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    series        TEXT NOT NULL,
    as_of_date    TEXT NOT NULL,
    version_id    INTEGER NOT NULL REFERENCES version(version_id),
    candidates    TEXT NOT NULL,               -- JSON: 候補の version_id と経路
    reason        TEXT NOT NULL,
    decided_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diff_pair (
    pair_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    series        TEXT NOT NULL,
    old_version   INTEGER NOT NULL REFERENCES version(version_id),
    new_version   INTEGER NOT NULL REFERENCES version(version_id),
    old_date      TEXT NOT NULL,
    new_date      TEXT NOT NULL,
    computed_at   TEXT NOT NULL,
    skipped_columns TEXT,                      -- 片方の版で全行空欄のため比較しなかった列
    UNIQUE (old_version, new_version),
    CHECK (old_date < new_date)
);

CREATE TABLE IF NOT EXISTS diff_row (
    pair_id         INTEGER NOT NULL REFERENCES diff_pair(pair_id),
    area_code       TEXT NOT NULL,
    local_code      TEXT NOT NULL,
    change_kind     TEXT NOT NULL CHECK (change_kind IN ('added', 'removed', 'carrier_changed', 'status_changed',
                                                         'number_area_changed', 'cosmetic_only')),
    old_value       TEXT,
    new_value       TEXT,
    in_use_related  INTEGER NOT NULL CHECK (in_use_related IN (0, 1)),
    became_in_use   INTEGER NOT NULL CHECK (became_in_use IN (0, 1)),
    PRIMARY KEY (pair_id, area_code, local_code, change_kind)
);

-- 差分は原本から作り直せる派生データ。計算方法の版が変わったら作り直す
CREATE TABLE IF NOT EXISTS diff_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 番号区画 → 市区町村（区画レベルの関係。町丁目での利用を意味しない）
CREATE TABLE IF NOT EXISTS number_area_municipality (
    number_area_code  TEXT NOT NULL,
    municipality_code TEXT NOT NULL CHECK (length(municipality_code) = 5),
    area_code         TEXT,
    ma_name           TEXT,
    coverage          TEXT NOT NULL CHECK (coverage IN ('whole', 'part', 'unknown')),
    valid_from        TEXT NOT NULL,
    source_url        TEXT,
    PRIMARY KEY (number_area_code, municipality_code, valid_from)
);

-- 有効な版選択（系統×基準日ごとに最新の決定）
CREATE VIEW IF NOT EXISTS selected_version AS
SELECT vs.series, vs.as_of_date, vs.version_id, vs.reason
FROM version_selection vs
WHERE vs.selection_id = (SELECT MAX(s2.selection_id) FROM version_selection s2
                         WHERE s2.series = vs.series AND s2.as_of_date = vs.as_of_date);

-- 各系統の最新基準日の指定行
CREATE VIEW IF NOT EXISTS current_designation AS
SELECT sv.series, sv.as_of_date, r.*
FROM selected_version sv
JOIN designation_row r ON r.version_id = sv.version_id
WHERE sv.as_of_date = (SELECT MAX(s2.as_of_date) FROM selected_version s2 WHERE s2.series = sv.series);

CREATE TRIGGER IF NOT EXISTS trg_raw_no_update BEFORE UPDATE ON raw_file
BEGIN SELECT RAISE(ABORT, 'raw_file is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_raw_no_delete BEFORE DELETE ON raw_file
BEGIN SELECT RAISE(ABORT, 'raw_file is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_version_no_update BEFORE UPDATE ON version
BEGIN SELECT RAISE(ABORT, 'version is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_version_no_delete BEFORE DELETE ON version
BEGIN SELECT RAISE(ABORT, 'version is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_row_no_update BEFORE UPDATE ON designation_row
BEGIN SELECT RAISE(ABORT, 'designation_row is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_row_no_delete BEFORE DELETE ON designation_row
BEGIN SELECT RAISE(ABORT, 'designation_row is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_sel_no_update BEFORE UPDATE ON version_selection
BEGIN SELECT RAISE(ABORT, 'version_selection is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_sel_no_delete BEFORE DELETE ON version_selection
BEGIN SELECT RAISE(ABORT, 'version_selection is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_fetch_no_delete BEFORE DELETE ON fetch_log
BEGIN SELECT RAISE(ABORT, 'fetch_log is append-only'); END;
