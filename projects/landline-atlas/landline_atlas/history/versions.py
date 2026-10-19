"""系統×基準日ごとの版選択。選んだ理由と候補を必ず記録する。

規則（上から順に適用）:
1. 候補の正規化内容（content_sha256）がすべて同じ → 内容同一。経路の優先度で代表を選ぶ
2. 内容が異なる → 経路の優先度（official > manual > wayback）、同じなら後から解析された版
   理由に content_differs を記録し、品質検査で警告として必ず表に出す
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from ..db.connection import utc_now

ORIGIN_PRIORITY = {"official": 0, "manual": 1, "wayback": 2}


def _origin(conn: sqlite3.Connection, sha: str) -> str:
    origins = [r[0] for r in conn.execute("SELECT DISTINCT origin FROM numbers.raw_source WHERE sha256 = ?", (sha,))]
    return min(origins, key=ORIGIN_PRIORITY.__getitem__)


def select_versions(conn: sqlite3.Connection) -> list[dict]:
    """全系統の版選択を見直し、変化があったものだけ追記する。決定の一覧を返す。"""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM numbers.version WHERE as_of_date IS NOT NULL ORDER BY version_id"):
        groups[(r["series"], r["as_of_date"])].append(
            {"version_id": r["version_id"], "origin": _origin(conn, r["sha256"]),
             "content_sha256": r["content_sha256"], "date_source": r["date_source"], "rows": r["row_count"]})
    current = {(r["series"], r["as_of_date"]): r["version_id"]
               for r in conn.execute("SELECT * FROM numbers.selected_version")}
    decisions = []
    for (series, as_of), cands in sorted(groups.items()):
        ranked = sorted(cands, key=lambda c: (ORIGIN_PRIORITY[c["origin"]], c["date_source"] != "content",
                                              -c["version_id"]))
        chosen = ranked[0]
        contents = {c["content_sha256"] for c in cands}
        if len(cands) == 1:
            reason = "single_candidate"
        elif len(contents) == 1:
            reason = f"identical_content;prefer_origin:{chosen['origin']}"
        else:
            reason = (f"content_differs:{len(contents)};prefer_origin:{chosen['origin']}"
                      f";latest_parsed_within_origin")
        decision = {"series": series, "as_of_date": as_of, "version_id": chosen["version_id"],
                    "reason": reason, "candidates": cands}
        decisions.append(decision)
        if current.get((series, as_of)) != chosen["version_id"]:
            conn.execute("INSERT INTO numbers.version_selection (series, as_of_date, version_id, candidates, reason,"
                         " decided_at) VALUES (?,?,?,?,?,?)",
                         (series, as_of, chosen["version_id"], json.dumps(cands, ensure_ascii=False), reason,
                          utc_now()))
    return decisions
