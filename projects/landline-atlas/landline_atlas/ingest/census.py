"""e-Stat 小地域集計・境界属性の取込。

列名は表ごとに異なるため、対応表（JSON）で「CSV の列名 → 特徴量名」を与える。
例は config/estat_mapping.example.json。表記 '-'・'X'・'…' は normalize.stat_value で状態に変換する。
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from pathlib import Path

from ..normalize import municipality_code, nfkc, secrecy, stat_value, town_key
from ..regions.features import Feature, add_source, new_load, put


def _read_csv(path: Path) -> list[dict]:
    body = path.read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = body.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"{path}: cannot decode")
    return list(csv.DictReader(io.StringIO(text)))


def import_towns(conn: sqlite3.Connection, path: Path, mapping: dict, census_year: int,
                 boundary_version: str, source_id: str) -> int:
    """境界属性（KEY_CODE・市区町村コード・名称・HTKSYORI・HTKSAKI）から町丁目マスタを作る。"""
    cols = mapping["town_columns"]
    n = 0
    for rec in _read_csv(path):
        if cols.get("skip_if_empty") and not nfkc(rec.get(cols["skip_if_empty"])):
            continue
        key = town_key(rec[cols["town_key"]])
        muni = municipality_code(rec[cols["municipality_code"]]) if cols.get("municipality_code") else key[:5]
        state, merged = secrecy(rec.get(cols.get("htksyori", ""), "0"), rec.get(cols.get("htksaki", "")))
        conn.execute("INSERT INTO regions.town VALUES (?,?,?,?,?,?,?,?,?)",
                     (key, census_year, boundary_version, muni[:2], muni, nfkc(rec.get(cols.get("name", ""))) or None,
                      state, merged, source_id))
        n += 1
    return n


def import_features(conn: sqlite3.Connection, path: Path, mapping: dict, census_year: int,
                    boundary_version: str, source_id: str, *, kind: str = "census") -> tuple[int, int]:
    """横持ち CSV（1行1町丁目）から features 対応表の列を取り込む。(load_id, 件数) を返す。"""
    key_col = mapping["key_column"]
    feats = mapping["features"]
    load_id = new_load(conn, kind, source_id)
    known = {r[0] for r in conn.execute("SELECT town_key FROM regions.town WHERE census_year = ?"
                                        " AND boundary_version = ?", (census_year, boundary_version))}
    rows = []
    for rec in _read_csv(path):
        raw_key = nfkc(rec.get(key_col))
        if not raw_key.isdigit():
            continue  # 2行目の日本語見出しなど
        key = town_key(raw_key)
        if key not in known:
            raise ValueError(f"{key}: not in town master for {census_year}/{boundary_version}")
        for column, feature in feats.items():
            value, state = stat_value(rec.get(column))
            rows.append((key, feature, Feature(value, state, source_id)))
    return load_id, put(conn, load_id, census_year, boundary_version, rows)


def load_mapping(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def register_source(conn: sqlite3.Connection, source_id: str, title: str, url: str | None, year: int | None) -> None:
    add_source(conn, source_id, title, url=url, source_year=year)
