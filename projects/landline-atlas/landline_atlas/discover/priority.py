"""調査優先順位エンジン。

対象は「番号指定データに載っている使用中の局番」だけ。番号の生成・総当たり・個人宅の
探索は行わない。各局番について、公開施設での観測がどこまで進んでいるかと、指定の新しさ、
番号区画の範囲の狭さから、成分ごとに透明なスコアを付ける。
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from ..db.connection import utc_now
from ..normalize import compact

DEFAULT_WEIGHTS = {"unobserved": 0.5, "recency": 0.3, "specificity": 0.2}
CHECK_FACILITIES = ("public_office", "school", "medical", "library", "corporation", "shop")


def compute(conn: sqlite3.Connection, in_use_values: list[str], weights: dict | None = None,
            limit: int | None = None) -> int:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    in_use = {compact(v) for v in in_use_values}
    current = [r for r in conn.execute("SELECT * FROM numbers.current_designation")
               if compact(r["usage_status"]) in in_use]

    obs = defaultdict(lambda: [0, 0])
    for r in conn.execute("SELECT area_code, local_code, verified FROM evidence.facility_observation"):
        obs[(r["area_code"], r["local_code"])][r["verified"]] += 1

    munis = defaultdict(set)
    for r in conn.execute("SELECT number_area_code, municipality_code FROM numbers.number_area_municipality"):
        munis[r["number_area_code"]].add(r["municipality_code"])

    # 系統ごとの組の並び（新しいほど後ろ）と、局番が使用中になった組
    pair_rank = {}
    for series_pairs in _pairs_by_series(conn).values():
        for i, pid in enumerate(series_pairs):
            pair_rank[pid] = len(series_pairs) - 1 - i  # 0 が最新
    became = {}
    for r in conn.execute("SELECT pair_id, area_code, local_code FROM numbers.diff_row WHERE became_in_use = 1"):
        k = (r["area_code"], r["local_code"])
        if r["pair_id"] in pair_rank:
            became[k] = min(became.get(k, 99), pair_rank[r["pair_id"]])

    items = []
    for r in current:
        key = (r["area_code"], r["local_code"])
        unverified, verified = obs[key]
        level = "observed_verified" if verified else ("observed_unverified" if unverified else "designation_only")
        unobserved = 0.0 if verified else (0.5 if unverified else 1.0)
        recency = 1.0 / (1 + became[key]) if key in became else 0.0
        covered = munis.get(r["number_area_code"], set())
        specificity = 1.0 / len(covered) if covered else 0.0
        comps = {"unobserved": unobserved, "recency": round(recency, 4), "specificity": round(specificity, 4)}
        score = round(sum(w[k] * v for k, v in comps.items()), 4)
        reasons = []
        if level == "designation_only":
            reasons.append("公開施設での観測なし")
        elif level == "observed_unverified":
            reasons.append("観測はあるが町丁目照合が未確認")
        if key in became:
            reasons.append("新たに使用中となった局番" if became[key] == 0 else "近年使用中となった局番")
        if not covered:
            reasons.append("番号区画→市区町村の対応が未登録")
        items.append((score, key, r["number_area_code"], comps, level, sorted(covered), reasons))
    items.sort(key=lambda x: (-x[0], x[1]))
    if limit:
        items = items[:limit]
    run_id = conn.execute("INSERT INTO priority_run (weights, computed_at) VALUES (?,?)",
                          (json.dumps(w), utc_now())).lastrowid
    conn.executemany("INSERT INTO priority_item VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     [(run_id, i + 1, k[0], k[1], na, s, json.dumps(c), lvl, ";".join(m),
                       ";".join(CHECK_FACILITIES), ";".join(rs))
                      for i, (s, k, na, c, lvl, m, rs) in enumerate(items)])
    return int(run_id)


def _pairs_by_series(conn: sqlite3.Connection) -> dict[str, list[int]]:
    from ..history.diff import adjacent_pairs
    out = defaultdict(list)
    for series, a, b in adjacent_pairs(conn):
        row = conn.execute("SELECT pair_id FROM numbers.diff_pair WHERE old_version = ? AND new_version = ?",
                           (a["version_id"], b["version_id"])).fetchone()
        if row:
            out[series].append(row["pair_id"])
    return out
