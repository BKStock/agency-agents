"""町丁目と局番の関連。根拠のレベルを混同しない。

* area                番号区画がその市区町村を含む（町丁目での利用は未確認）
* observed_unverified 公開施設で観測（町丁目照合は未確認）
* observed_verified   公開施設で観測し、町丁目照合を人が確認
* no_observation      観測なし。「使われていない」という意味ではない
"""

from __future__ import annotations

import sqlite3

AREA_NOTE = "番号区画が市区町村を含む（町丁目での利用は未確認）"
NO_OBS_NOTE = "公開施設の観測なし（利用されていないことを意味しない）"


def for_town(conn: sqlite3.Connection, town: sqlite3.Row | dict, *, area_code: str | None = None,
             local_code: str | None = None) -> list[dict]:
    cond, params = "", []
    if area_code:
        cond += " AND area_code = ?"
        params.append(area_code)
    if local_code:
        cond += " AND local_code = ?"
        params.append(local_code)
    out = [{"relation_level": "area", "note": AREA_NOTE, **dict(r)} for r in conn.execute(
        f"SELECT * FROM municipality_code_relation WHERE municipality_code = ?{cond} ORDER BY area_code, local_code",
        [town["municipality_code"], *params])]
    observed = [dict(r) for r in conn.execute(
        f"SELECT * FROM town_observed_code WHERE town_key = ? AND census_year = ? AND boundary_version = ?{cond}"
        " ORDER BY area_code, local_code", [town["town_key"], town["census_year"], town["boundary_version"], *params])]
    out += observed
    if not observed:
        out.append({"relation_level": "no_observation", "note": NO_OBS_NOTE})
    return out


def for_code(conn: sqlite3.Connection, area_code: str, local_code: str) -> dict:
    history = [dict(r) for r in conn.execute(
        """SELECT sv.series, sv.as_of_date, r.number_area_code, r.number_area_name, r.carrier, r.usage_status,
                  r.repaired, v.sha256, (SELECT url FROM numbers.raw_source s WHERE s.sha256 = v.sha256
                                         ORDER BY s.origin = 'official' DESC LIMIT 1) AS source_url
           FROM numbers.selected_version sv
           JOIN numbers.version v ON v.version_id = sv.version_id
           JOIN numbers.designation_row r ON r.version_id = sv.version_id
           WHERE r.area_code = ? AND r.local_code = ? ORDER BY sv.as_of_date""", (area_code, local_code))]
    municipalities = [dict(r) for r in conn.execute(
        "SELECT DISTINCT municipality_code, coverage FROM municipality_code_relation"
        " WHERE area_code = ? AND local_code = ?", (area_code, local_code))]
    observed = [dict(r) for r in conn.execute(
        "SELECT * FROM town_observed_code WHERE area_code = ? AND local_code = ?", (area_code, local_code))]
    changes = [dict(r) for r in conn.execute(
        """SELECT p.series, p.old_date, p.new_date, d.change_kind, d.old_value, d.new_value
           FROM numbers.diff_row d JOIN numbers.diff_pair p ON p.pair_id = d.pair_id
           WHERE d.area_code = ? AND d.local_code = ? ORDER BY p.new_date""", (area_code, local_code))]
    return {"area_code": area_code, "local_code": local_code, "designation_history": history,
            "changes": changes, "municipalities": municipalities, "observed_towns": observed}
