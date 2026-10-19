"""地域分類DB の作成と、追記専用の書き込みヘルパー。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import normalize
from .features import Feature

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def open_db(path: str | Path) -> sqlite3.Connection:
    """地域分類DB を開き、なければスキーマを作る（既存の表は変更しない）。"""
    conn = connect(path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    """既存DB（v1.2 など）を読み取り専用で開く。ファイルが無ければ作らずに失敗する。"""
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def add_source(conn: sqlite3.Connection, source_id: str, title: str, *, publisher: str | None = None,
               url: str | None = None, source_year: int | None = None, license: str | None = None,
               sha256: str | None = None, retrieved_at: str | None = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO source_document VALUES (?,?,?,?,?,?,?,?)",
        (source_id, title, publisher, url, source_year, license, sha256, retrieved_at),
    )


def new_load(conn: sqlite3.Connection, kind: str, *, source_id: str | None = None,
             note: str | None = None, loaded_at: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO data_load (loaded_at, kind, source_id, note) VALUES (?,?,?,?)",
        (loaded_at or utc_now(), kind, source_id, note),
    )
    return int(cur.lastrowid)


def add_town(conn: sqlite3.Connection, town_key: str, census_year: int, boundary_version: str,
             municipality_code: str, *, town_name: str | None = None, hierarchy_level: int | None = None,
             secrecy_state: str = "none", merged_into: str | None = None,
             source_id: str | None = None) -> None:
    key = normalize.town_key(town_key)
    muni = normalize.municipality_code(municipality_code)
    if not key.startswith(muni):
        raise normalize.CodeError(f"town_key {key} does not start with municipality_code {muni}")
    conn.execute(
        "INSERT INTO town_master VALUES (?,?,?,?,?,?,?,?,?,?)",
        (key, census_year, boundary_version, muni[:2], muni, town_name, hierarchy_level,
         secrecy_state, merged_into, source_id),
    )


def put_features(conn: sqlite3.Connection, load_id: int, census_year: int, boundary_version: str,
                 rows: Iterable[tuple[str, str, Feature]]) -> int:
    """(town_key, feature_name, Feature) を追記する。"""
    count = 0
    for town_key, name, feat in rows:
        if feat.state not in normalize.VALID_STATES:
            raise ValueError(f"invalid state {feat.state!r}")
        conn.execute(
            "INSERT INTO town_feature VALUES (?,?,?,?,?,?,?,?)",
            (load_id, town_key, census_year, boundary_version, name, feat.value, feat.state, feat.source_id),
        )
        count += 1
    return count


def add_evidence(conn: sqlite3.Connection, load_id: int, town_key: str, census_year: int,
                 boundary_version: str, evidence_type: str, supports_class: str, grade: str,
                 source_id: str, *, value: float | None = None, note: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO external_evidence (load_id, town_key, census_year, boundary_version, evidence_type,"
        " supports_class, grade, value, source_id, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (load_id, town_key, census_year, boundary_version, evidence_type, supports_class, grade,
         value, source_id, note),
    )
    return int(cur.lastrowid)


def max_load_id(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COALESCE(MAX(load_id), 0) FROM data_load").fetchone()[0])
