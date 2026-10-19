"""4つのDB（numbers / regions / evidence / analysis）を1接続に ATTACH して扱う。

* numbers   固定電話番号指定DB（取得・原本・版・差分）
* regions   地域分類DB（町丁目・特徴量・12分類）
* evidence  公開施設番号エビデンスDB
* analysis  統合分析DB（main。実行記録・品質検査・調査優先順位・評価）

DB をまたぐ統合ビューは TEMP ビューとして接続ごとに作る（SQLite は永続ビューから
別DBを参照できないため）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_DIR = Path(__file__).parent
DATABASES = ("numbers", "regions", "evidence")

INTEGRATION_VIEWS = """
CREATE TEMP VIEW IF NOT EXISTS municipality_code_relation AS
SELECT DISTINCT nam.municipality_code, cd.area_code, cd.local_code, cd.number_area_code,
       cd.carrier, cd.usage_status, cd.as_of_date, cd.series, nam.coverage,
       'area' AS relation_level
FROM numbers.current_designation cd
JOIN numbers.number_area_municipality nam ON nam.number_area_code = cd.number_area_code;

CREATE TEMP VIEW IF NOT EXISTS town_observed_code AS
SELECT town_key, census_year, boundary_version, area_code, local_code,
       COUNT(*) AS observation_count, SUM(verified) AS verified_count,
       CASE WHEN SUM(verified) > 0 THEN 'observed_verified' ELSE 'observed_unverified' END AS relation_level,
       MIN(observed_on) AS first_observed_on, MAX(observed_on) AS last_observed_on,
       group_concat(DISTINCT source_url) AS source_urls
FROM evidence.facility_observation
WHERE town_key IS NOT NULL
GROUP BY town_key, census_year, boundary_version, area_code, local_code;
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _apply_schema(path: Path, name: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript((SCHEMA_DIR / f"{name}.sql").read_text(encoding="utf-8"))
    conn.close()


def _uri(path: Path, read_only: bool) -> str:
    return f"{path.resolve().as_uri()}?mode={'ro' if read_only else 'rwc'}"


def connect(db_dir: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """db_dir 配下の4ファイルを開く。read_only=True ではスキーマ作成もしない。"""
    db_dir = Path(db_dir)
    if not read_only:
        db_dir.mkdir(parents=True, exist_ok=True)
        for name in (*DATABASES, "analysis"):
            _apply_schema(db_dir / f"{name}.sqlite3", name)
    conn = sqlite3.connect(_uri(db_dir / "analysis.sqlite3", read_only), uri=True)
    conn.row_factory = sqlite3.Row
    for name in DATABASES:
        conn.execute(f"ATTACH DATABASE ? AS {name}", (_uri(db_dir / f"{name}.sqlite3", read_only),))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(INTEGRATION_VIEWS)
    return conn


def integrity(conn: sqlite3.Connection) -> dict[str, str]:
    return {name: conn.execute(f"PRAGMA {name}.integrity_check").fetchone()[0]
            for name in ("main", *DATABASES)}
