"""検索・絞り込み。

R6 の大阪府除外は表示時のフィルタとしてだけ実装する。内部の分類結果は
削除も変更もしない（include_r6=True で常に元に戻せる）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .primary import MATCHED

# 住宅系検索の初期除外（都道府県コード → 除外する分類）
RESIDENTIAL_DEFAULT_EXCLUSIONS: dict[str, frozenset[str]] = {"27": frozenset({"R6"})}

RELATION_LEVELS = ("area", "observed_verified", "observed_unverified")


@dataclass
class SearchFilter:
    prefecture: str | None = None
    municipality: str | None = None
    town: str | None = None                       # town_key の前方一致 または 町丁目名の部分一致
    classes: list[str] = field(default_factory=list)
    require_all_classes: bool = False             # True: 指定分類すべてに該当（複数分類）
    statuses: tuple[str, ...] = MATCHED
    axis: str | None = None
    primary_only: bool = False
    mode: str | None = None                       # "residential" で住宅系の初期除外を適用
    include_r6: bool = False
    area_code: str | None = None                  # 市外局番
    local_code: str | None = None                 # 市内局番
    number_area_code: str | None = None
    carrier: str | None = None
    as_of_date: str | None = None                 # 資料基準日
    usage_status: str | None = None
    relation_level: str | None = None             # area / observed_verified / observed_unverified


def _phone_filters(f: SearchFilter) -> bool:
    return any((f.area_code, f.local_code, f.number_area_code, f.carrier, f.as_of_date,
                f.usage_status, f.relation_level))


def _designation_clause(f: SearchFilter, alias: str) -> tuple[str, list]:
    parts, params = [], []
    for col, val in (("area_code", f.area_code), ("local_code", f.local_code),
                     ("number_area_code", f.number_area_code), ("carrier", f.carrier),
                     ("as_of_date", f.as_of_date), ("usage_status", f.usage_status)):
        if val is not None:
            parts.append(f"{alias}.{col} = ?")
            params.append(val)
    return (" AND ".join(parts) or "1=1"), params


def _observed_clause(f: SearchFilter, alias: str) -> tuple[str, list]:
    parts, params = [], []
    for col, val in (("area_code", f.area_code), ("local_code", f.local_code)):
        if val is not None:
            parts.append(f"{alias}.{col} = ?")
            params.append(val)
    if f.relation_level == "observed_verified":
        parts.append(f"{alias}.verified_count > 0")
    elif f.relation_level == "observed_unverified":
        parts.append(f"{alias}.verified_count = 0")
    # 指定データ側の条件（事業者・基準日・使用状況・区画）は、同じ局番の指定行で照合する
    d_clause, d_params = _designation_clause(
        SearchFilter(number_area_code=f.number_area_code, carrier=f.carrier,
                     as_of_date=f.as_of_date, usage_status=f.usage_status), "nd")
    if d_params:
        parts.append(f"EXISTS (SELECT 1 FROM number_designation nd WHERE nd.area_code = {alias}.area_code"
                     f" AND nd.local_code = {alias}.local_code AND {d_clause})")
        params.extend(d_params)
    return (" AND ".join(parts) or "1=1"), params


def search(conn: sqlite3.Connection, f: SearchFilter) -> list[dict]:
    if f.relation_level is not None and f.relation_level not in RELATION_LEVELS:
        raise ValueError(f"relation_level must be one of {RELATION_LEVELS}")
    where = [f"cc.label_status IN ({','.join('?' * len(f.statuses))})"]
    params: list = list(f.statuses)
    if f.prefecture:
        where.append("cc.prefecture_code = ?")
        params.append(f.prefecture)
    if f.municipality:
        where.append("cc.municipality_code = ?")
        params.append(f.municipality)
    if f.town:
        where.append("(cc.town_key LIKE ? OR tm.town_name LIKE ?)")
        params += [f"{f.town}%", f"%{f.town}%"]
    if f.axis:
        where.append("cc.axis = ?")
        params.append(f.axis)
    if f.primary_only:
        where.append("cc.primary_flag = 1")
    if f.classes:
        marks = ",".join("?" * len(f.classes))
        where.append(f"cc.class_code IN ({marks})")
        params += f.classes
        if f.require_all_classes:
            where.append(
                f"""cc.town_key IN (
                    SELECT c2.town_key FROM current_classification c2
                    WHERE c2.census_year = cc.census_year AND c2.boundary_version = cc.boundary_version
                      AND c2.class_code IN ({marks}) AND c2.label_status IN ({','.join('?' * len(MATCHED))})
                    GROUP BY c2.town_key HAVING COUNT(DISTINCT c2.class_code) = ?)""")
            params += f.classes + list(MATCHED) + [len(set(f.classes))]
    if f.mode == "residential" and not f.include_r6:
        for pref, excluded in RESIDENTIAL_DEFAULT_EXCLUSIONS.items():
            marks = ",".join("?" * len(excluded))
            where.append(f"NOT (cc.prefecture_code = ? AND cc.class_code IN ({marks}))")
            params += [pref, *sorted(excluded)]
    if _phone_filters(f):
        level = f.relation_level
        options = []
        if level in (None, "area"):
            clause, p = _designation_clause(f, "m")
            options.append((f"EXISTS (SELECT 1 FROM municipality_code_relation m"
                            f" WHERE m.municipality_code = cc.municipality_code AND {clause})", p))
        if level in (None, "observed_verified", "observed_unverified"):
            clause, p = _observed_clause(f, "o")
            options.append((f"EXISTS (SELECT 1 FROM town_observed_code o WHERE o.town_key = cc.town_key"
                            f" AND o.census_year = cc.census_year AND o.boundary_version = cc.boundary_version"
                            f" AND {clause})", p))
        where.append("(" + " OR ".join(o[0] for o in options) + ")")
        for _, p in options:
            params += p

    sql = f"""
        SELECT cc.prefecture_code, cc.municipality_code, cc.town_key, tm.town_name,
               cc.census_year, cc.boundary_version, cc.class_code, cc.axis, cc.label_status,
               cc.primary_flag, cc.secondary_flag, cc.overlap_flag, cc.score, cc.evidence_grade,
               cc.reason, cc.rule_version, cc.run_id, cc.source_year, cc.source_url
        FROM current_classification cc
        JOIN town_master tm ON tm.town_key = cc.town_key AND tm.census_year = cc.census_year
                           AND tm.boundary_version = cc.boundary_version
        WHERE {' AND '.join(where)}
        ORDER BY cc.town_key, cc.class_code"""
    rows = [dict(r) for r in conn.execute(sql, params)]
    if _phone_filters(f):
        for row in rows:
            row["phone_relations"] = phone_relations(conn, row, f)
    return rows


def phone_relations(conn: sqlite3.Connection, row: dict, f: SearchFilter) -> list[dict]:
    """検索結果1行に対応する局番関連（原本URL付き）。観測が無ければ no_observation を明示する。"""
    out = []
    if f.relation_level in (None, "area"):
        clause, p = _designation_clause(f, "m")
        for r in conn.execute(
            f"SELECT * FROM municipality_code_relation m WHERE m.municipality_code = ? AND {clause}"
            " ORDER BY area_code, local_code, as_of_date", [row["municipality_code"], *p]):
            out.append({"relation_level": "area", "area_code": r["area_code"], "local_code": r["local_code"],
                        "number_area_code": r["number_area_code"], "carrier": r["carrier"],
                        "usage_status": r["usage_status"], "as_of_date": r["as_of_date"],
                        "coverage": r["coverage"], "source_url": r["source_url"],
                        "note": "番号区画が市区町村を含む（町丁目での利用は未確認）"})
    if f.relation_level in (None, "observed_verified", "observed_unverified"):
        clause, p = _observed_clause(f, "o")
        observed = conn.execute(
            f"""SELECT * FROM town_observed_code o WHERE o.town_key = ? AND o.census_year = ?
                AND o.boundary_version = ? AND {clause} ORDER BY area_code, local_code""",
            [row["town_key"], row["census_year"], row["boundary_version"], *p]).fetchall()
        for r in observed:
            out.append({"relation_level": "observed_verified" if r["verified_count"] else "observed_unverified",
                        "area_code": r["area_code"], "local_code": r["local_code"],
                        "observation_count": r["observation_count"], "source_url": r["source_urls"]})
        if not observed:
            out.append({"relation_level": "no_observation",
                        "note": "公開施設の観測なし（利用されていないことを意味しない）"})
    return out
