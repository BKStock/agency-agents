"""固定電話番号指定DB（v1.2）と地域分類DBの接続。

v1.2 のDBは読み取り専用で開き、利用者が書いた SELECT 文で必要な列だけを
写し取る（v1.2 のスキーマはここで仮定しない）。写しは number_designation に
load_id 付きで追記する。
"""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from pathlib import Path

from . import normalize
from .store import connect_readonly, new_load

DESIGNATION_COLUMNS = ("area_code", "local_code", "number_area_code", "carrier", "usage_status",
                       "as_of_date", "series", "source_url", "source_sha256")

FACILITY_TYPES = ("public_office", "school", "medical", "shop", "corporation", "other_facility")


def _digits_text(value: object, field: str) -> str:
    if value is None:
        raise ValueError(f"{field} is required")
    if isinstance(value, (int, float)):
        raise ValueError(f"{field}={value!r} is numeric; select it as TEXT so leading zeros survive")
    text = normalize.normalize_text(value).replace("-", "")
    if not text.isdigit():
        raise ValueError(f"{field}={value!r} is not a digit string")
    return text


def import_designations(conn: sqlite3.Connection, v12_db: str | Path, select_sql: str, *,
                        note: str | None = None) -> tuple[int, int]:
    """v1.2 DB に select_sql を読み取り専用で実行し、結果を写す。(load_id, 行数) を返す。

    select_sql は DESIGNATION_COLUMNS と同名の列を返すこと（不要な列は NULL AS ... で可）。
    """
    if not select_sql.lstrip().lower().startswith(("select", "with")):
        raise ValueError("select_sql must be a SELECT statement")
    src = connect_readonly(v12_db)
    try:
        cur = src.execute(select_sql)
        names = [d[0] for d in cur.description]
        missing = [c for c in DESIGNATION_COLUMNS if c not in names]
        if missing:
            raise ValueError(f"select_sql lacks columns: {missing}")
        rows = cur.fetchall()
    finally:
        src.close()

    digest = hashlib.sha256(Path(v12_db).read_bytes()).hexdigest()
    with conn:
        conn.execute("INSERT OR IGNORE INTO source_document (source_id, title, sha256) VALUES (?,?,?)",
                     (f"v12db:{digest[:16]}", f"v1.2 index.sqlite3 ({Path(v12_db).name})", digest))
        load_id = new_load(conn, "phone", source_id=f"v12db:{digest[:16]}", note=note)
        for r in rows:
            rec = dict(zip(names, r))
            conn.execute(
                "INSERT INTO number_designation VALUES (?,?,?,?,?,?,?,?,?,?)",
                (load_id, _digits_text(rec["area_code"], "area_code"),
                 _digits_text(rec["local_code"], "local_code"),
                 rec["number_area_code"], rec["carrier"], rec["usage_status"],
                 str(rec["as_of_date"]), rec["series"], rec["source_url"], rec["source_sha256"]),
            )
    return load_id, len(rows)


def import_number_area_map(conn: sqlite3.Connection, csv_path: str | Path, *, source_id: str) -> int:
    """番号区画→市区町村の対応表（列: number_area_code, municipality_code, area_code, ma_name,
    coverage, valid_from, valid_to）を取り込む。"""
    count = 0
    with open(csv_path, newline="", encoding="utf-8-sig") as fh, conn:
        for rec in csv.DictReader(fh):
            conn.execute(
                "INSERT INTO number_area_municipality VALUES (?,?,?,?,?,?,?,?)",
                (rec["number_area_code"], normalize.municipality_code(rec["municipality_code"]),
                 rec.get("area_code") or None, rec.get("ma_name") or None,
                 rec.get("coverage") or "unknown", rec["valid_from"], rec.get("valid_to") or None, source_id),
            )
            count += 1
    return count


def add_facility_observation(conn: sqlite3.Connection, *, facility_name: str, facility_type: str,
                             area_code: str, local_code: str, address: str, source_url: str,
                             observed_on: str, town_key: str | None = None, census_year: int | None = None,
                             boundary_version: str | None = None, geocode_method: str | None = None,
                             verified: bool = False) -> int:
    """公開施設の番号観測を1件追加する。個人宅は受け付けない（facility_type で制限）。"""
    if facility_type not in FACILITY_TYPES:
        raise ValueError(f"facility_type must be one of {FACILITY_TYPES}")
    if not source_url:
        raise ValueError("source_url is required for every observation")
    cur = conn.execute(
        """INSERT INTO facility_observation (facility_name, facility_type, area_code, local_code, address,
           town_key, census_year, boundary_version, geocode_method, verified, source_url, observed_on)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (facility_name, facility_type, _digits_text(area_code, "area_code"),
         _digits_text(local_code, "local_code"), address, town_key, census_year, boundary_version,
         geocode_method, int(verified), source_url, observed_on),
    )
    return int(cur.lastrowid)


def designation_changes(conn: sqlite3.Connection) -> list[dict]:
    """取り込んだ指定データの、系統内で隣接する基準日の間の変更を種別ごとに返す。"""
    rows = conn.execute(
        """SELECT COALESCE(series, '') AS series, as_of_date, area_code, local_code,
                  number_area_code, carrier, usage_status
           FROM number_designation nd
           WHERE nd.load_id = (SELECT MAX(n2.load_id) FROM number_designation n2
                               WHERE n2.as_of_date = nd.as_of_date
                                 AND COALESCE(n2.series, '') = COALESCE(nd.series, ''))""").fetchall()
    by_version: dict[tuple[str, str], dict[tuple[str, str], sqlite3.Row]] = {}
    for r in rows:
        by_version.setdefault((r["series"], r["as_of_date"]), {})[(r["area_code"], r["local_code"])] = r
    out = []
    series_dates: dict[str, list[str]] = {}
    for series, date in by_version:
        series_dates.setdefault(series, []).append(date)
    for series, dates in sorted(series_dates.items()):
        dates.sort()
        for old, new in zip(dates, dates[1:]):
            a, b = by_version[(series, old)], by_version[(series, new)]
            for key in sorted(set(a) | set(b)):
                kinds = []
                if key not in a:
                    kinds = ["added"]
                elif key not in b:
                    kinds = ["removed"]
                else:
                    for col, kind in (("carrier", "carrier_changed"), ("usage_status", "status_changed"),
                                      ("number_area_code", "number_area_changed")):
                        if normalize.normalize_text(a[key][col]) != normalize.normalize_text(b[key][col]):
                            kinds.append(kind)
                        elif a[key][col] != b[key][col]:
                            kinds.append(f"{kind}_cosmetic_only")
                for kind in kinds:
                    out.append({"series": series, "old_date": old, "new_date": new,
                                "area_code": key[0], "local_code": key[1], "change_kind": kind,
                                "old_value": _pick(a.get(key), kind), "new_value": _pick(b.get(key), kind)})
    return out


def _pick(row: sqlite3.Row | None, kind: str) -> str | None:
    if row is None:
        return None
    col = {"carrier": "carrier", "status": "usage_status", "number": "number_area_code"}.get(kind.split("_")[0])
    return row[col] if col else None
