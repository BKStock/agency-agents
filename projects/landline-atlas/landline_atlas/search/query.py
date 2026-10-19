"""検索。指示書 §8 の全条件を組み合わせられる。

大阪府（27）の住宅系検索（mode="residential"）では R6 を初期設定で表示しない。
これは表示時の絞り込みで、分類結果そのものは削除・変更しない（include_r6=True で表示）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..classify.engine import MATCHED
from ..link import relations

RESIDENTIAL_HIDDEN = {"27": ("R6",)}
LEVELS = ("area", "observed_verified", "observed_unverified")


@dataclass
class Filter:
    prefecture: str | None = None
    municipality: str | None = None
    town: str | None = None
    classes: list[str] = field(default_factory=list)
    all_classes: bool = False
    axis: str | None = None
    primary_only: bool = False
    statuses: tuple[str, ...] = MATCHED
    mode: str | None = None
    include_r6: bool = False
    area_code: str | None = None
    local_code: str | None = None
    number_area_code: str | None = None
    carrier: str | None = None
    as_of_date: str | None = None
    usage_status: str | None = None
    relation_level: str | None = None

    @property
    def phone(self) -> bool:
        return any((self.area_code, self.local_code, self.number_area_code, self.carrier, self.as_of_date,
                    self.usage_status, self.relation_level))


def _designation_where(f: Filter, a: str) -> tuple[str, list]:
    parts, params = [], []
    for col, v in (("area_code", f.area_code), ("local_code", f.local_code),
                   ("number_area_code", f.number_area_code), ("carrier", f.carrier),
                   ("usage_status", f.usage_status)):
        if v:
            parts.append(f"{a}.{col} = ?")
            params.append(v)
    return " AND ".join(parts) or "1=1", params


def search(conn: sqlite3.Connection, f: Filter) -> list[dict]:
    if f.relation_level and f.relation_level not in LEVELS:
        raise ValueError(f"relation_level must be one of {LEVELS}")
    where = [f"c.label_status IN ({','.join('?' * len(f.statuses))})"]
    params: list = list(f.statuses)
    for col, v in (("c.prefecture_code", f.prefecture), ("c.municipality_code", f.municipality),
                   ("c.axis", f.axis)):
        if v:
            where.append(f"{col} = ?")
            params.append(v)
    if f.town:
        where.append("(c.town_key LIKE ? OR t.town_name LIKE ?)")
        params += [f"{f.town}%", f"%{f.town}%"]
    if f.primary_only:
        where.append("c.primary_flag = 1")
    if f.classes:
        marks = ",".join("?" * len(f.classes))
        where.append(f"c.class_code IN ({marks})")
        params += f.classes
        if f.all_classes:
            where.append(f"""c.town_key IN (SELECT c2.town_key FROM regions.current_classification c2
                             WHERE c2.census_year = c.census_year AND c2.boundary_version = c.boundary_version
                               AND c2.class_code IN ({marks}) AND c2.label_status IN ('confirmed', 'candidate')
                             GROUP BY c2.town_key HAVING COUNT(DISTINCT c2.class_code) = ?)""")
            params += f.classes + [len(set(f.classes))]
    if f.mode == "residential" and not f.include_r6:
        for pref, hidden in RESIDENTIAL_HIDDEN.items():
            where.append(f"NOT (c.prefecture_code = ? AND c.class_code IN ({','.join('?' * len(hidden))}))")
            params += [pref, *hidden]
    if f.phone:
        opts = []
        if f.relation_level in (None, "area"):
            w, p = _designation_where(f, "m")
            if f.as_of_date:
                w += " AND m.as_of_date = ?"
                p.append(f.as_of_date)
            opts.append((f"EXISTS (SELECT 1 FROM municipality_code_relation m"
                         f" WHERE m.municipality_code = c.municipality_code AND {w})", p))
        if f.relation_level in (None, "observed_verified", "observed_unverified"):
            w = ["o.town_key = c.town_key", "o.census_year = c.census_year", "o.boundary_version = c.boundary_version"]
            p = []
            for col, v in (("area_code", f.area_code), ("local_code", f.local_code)):
                if v:
                    w.append(f"o.{col} = ?")
                    p.append(v)
            if f.relation_level:
                w.append("o.relation_level = ?")
                p.append(f.relation_level)
            dw, dp = _designation_where(Filter(number_area_code=f.number_area_code, carrier=f.carrier,
                                               usage_status=f.usage_status), "d")
            if dp or f.as_of_date:
                if f.as_of_date:
                    dw += " AND d.as_of_date = ?"
                    dp.append(f.as_of_date)
                w.append(f"EXISTS (SELECT 1 FROM numbers.current_designation d WHERE d.area_code = o.area_code"
                         f" AND d.local_code = o.local_code AND {dw})")
                p += dp
            opts.append((f"EXISTS (SELECT 1 FROM town_observed_code o WHERE {' AND '.join(w)})", p))
        where.append("(" + " OR ".join(o[0] for o in opts) + ")")
        for _, p in opts:
            params += p
    rows = [dict(r) for r in conn.execute(f"""
        SELECT c.prefecture_code, c.municipality_code, c.town_key, t.town_name, c.census_year, c.boundary_version,
               c.class_code, c.axis, c.label_status, c.primary_flag, c.secondary_flag, c.overlap_flag, c.score,
               c.evidence_grade, c.reason, c.rule_version, c.run_id, c.source_year, c.source_url
        FROM regions.current_classification c
        JOIN regions.town t ON t.town_key = c.town_key AND t.census_year = c.census_year
                           AND t.boundary_version = c.boundary_version
        WHERE {' AND '.join(where)} ORDER BY c.town_key, c.class_code""", params)]
    if f.phone:
        for r in rows:
            rels = relations.for_town(conn, r, area_code=f.area_code, local_code=f.local_code)
            if f.relation_level:
                rels = [x for x in rels if x["relation_level"] in (f.relation_level, "no_observation")]
            r["phone_relations"] = rels
    return rows


def town_detail(conn: sqlite3.Connection, key: str) -> dict | None:
    town = conn.execute("SELECT * FROM regions.town WHERE town_key = ? ORDER BY census_year DESC LIMIT 1",
                        (key,)).fetchone()
    if town is None:
        return None
    t = dict(town)
    classes = [dict(r) for r in conn.execute(
        "SELECT * FROM regions.current_classification WHERE town_key = ? AND census_year = ? AND boundary_version = ?"
        " ORDER BY class_code", (key, t["census_year"], t["boundary_version"]))]
    axes = [dict(r) for r in conn.execute(
        "SELECT * FROM regions.current_axis WHERE town_key = ? AND census_year = ? AND boundary_version = ?",
        (key, t["census_year"], t["boundary_version"]))]
    runs = [dict(r) for r in conn.execute(
        "SELECT DISTINCT r.run_id, r.rule_version, r.evaluated_at FROM regions.class_run r"
        " JOIN regions.regional_classification c ON c.run_id = r.run_id WHERE c.town_key = ? ORDER BY r.run_id",
        (key,))]
    return {"town": t, "classes": classes, "axes": axes, "runs": runs,
            "relations": relations.for_town(conn, t)}
