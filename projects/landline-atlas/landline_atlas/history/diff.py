"""選択済みの版のうち、同じ系統で基準日が隣接する版同士だけを比較する。

変更種別は added / removed / carrier_changed / status_changed / number_area_changed /
cosmetic_only（NFKC・空白除去で消える差分）。延べ件数（変更行）と一意件数（局番の種類）を
常に分けて集計する。
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict

from ..db.connection import utc_now
from ..normalize import compact

_COMPARED = (("carrier", "carrier_changed"), ("usage_status", "status_changed"),
             ("number_area_code", "number_area_changed"))


def _rows(conn: sqlite3.Connection, version_id: int) -> dict[tuple[str, str], sqlite3.Row]:
    out = {}
    for r in conn.execute("SELECT * FROM numbers.designation_row WHERE version_id = ? ORDER BY row_no",
                          (version_id,)):
        out[(r["area_code"], r["local_code"])] = r  # 版内の重複キーは品質検査 Q04 で報告する
    return out


def adjacent_pairs(conn: sqlite3.Connection) -> list[tuple[str, sqlite3.Row, sqlite3.Row]]:
    by_series = defaultdict(list)
    for r in conn.execute("SELECT * FROM numbers.selected_version ORDER BY series, as_of_date"):
        by_series[r["series"]].append(r)
    return [(s, a, b) for s, vs in by_series.items() for a, b in zip(vs, vs[1:])]


def compute_diffs(conn: sqlite3.Connection, in_use_values: list[str]) -> int:
    """未計算の隣接組の差分を計算して追記する。新しく計算した組の数を返す。"""
    in_use = {compact(v) for v in in_use_values}

    def used(r) -> bool:
        return r is not None and compact(r["usage_status"]) in in_use

    created = 0
    for series, old, new in adjacent_pairs(conn):
        exists = conn.execute("SELECT 1 FROM numbers.diff_pair WHERE old_version = ? AND new_version = ?",
                              (old["version_id"], new["version_id"])).fetchone()
        if exists:
            continue
        pair_id = conn.execute(
            "INSERT INTO numbers.diff_pair (series, old_version, new_version, old_date, new_date, computed_at)"
            " VALUES (?,?,?,?,?,?)",
            (series, old["version_id"], new["version_id"], old["as_of_date"], new["as_of_date"], utc_now())).lastrowid
        a, b = _rows(conn, old["version_id"]), _rows(conn, new["version_id"])
        for key in sorted(set(a) | set(b)):
            ra, rb = a.get(key), b.get(key)
            changes: list[tuple[str, str | None, str | None]] = []
            if ra is None:
                changes = [("added", None, rb["usage_status"])]
            elif rb is None:
                changes = [("removed", ra["usage_status"], None)]
            else:
                cosmetic = False
                for col, kind in _COMPARED:
                    if compact(ra[col]) != compact(rb[col]):
                        changes.append((kind, ra[col], rb[col]))
                    elif ra[col] != rb[col]:
                        cosmetic = True
                if not changes and (cosmetic or ra["number_area_name"] != rb["number_area_name"]):
                    if compact(ra["number_area_name"]) != compact(rb["number_area_name"]):
                        changes = [("number_area_changed", ra["number_area_name"], rb["number_area_name"])]
                    else:
                        changes = [("cosmetic_only", None, None)]
            related = int(used(ra) or used(rb))
            became = int(used(rb) and not used(ra))
            conn.executemany("INSERT INTO numbers.diff_row VALUES (?,?,?,?,?,?,?,?)",
                             [(pair_id, key[0], key[1], kind, ov, nv, related, became) for kind, ov, nv in changes])
        created += 1
    return created


def summary(conn: sqlite3.Connection) -> dict:
    """差分の集計。延べ（変更行）と一意（局番の種類）を分ける。"""
    pairs = [dict(r) for r in conn.execute("SELECT * FROM numbers.diff_pair ORDER BY series, old_date")]
    current_pairs = {(s, a["version_id"], b["version_id"]) for s, a, b in adjacent_pairs(conn)}
    pairs = [p for p in pairs if (p["series"], p["old_version"], p["new_version"]) in current_pairs]
    ids = [p["pair_id"] for p in pairs]
    if not ids:
        return {"pairs": 0, "per_pair": [], "kinds": {}, "change_rows_gross": 0,
                "in_use_related_rows_gross": 0, "distinct_codes_changed": 0,
                "distinct_codes_became_in_use": 0, "codes_changed_in_multiple_pairs": 0}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM numbers.diff_row WHERE pair_id IN ({marks})", ids).fetchall()
    per_key_pair = {(r["pair_id"], r["area_code"], r["local_code"]): r for r in rows}
    kinds = Counter(r["change_kind"] for r in rows)
    per_pair = []
    for p in pairs:
        keys = [k for k in per_key_pair if k[0] == p["pair_id"]]
        per_pair.append({**p, "change_rows": len(keys),
                         "in_use_related_rows": sum(per_key_pair[k]["in_use_related"] for k in keys),
                         "kinds": dict(Counter(r["change_kind"] for r in rows if r["pair_id"] == p["pair_id"]))})
    changed_pairs = Counter((k[1], k[2]) for k in per_key_pair)
    return {
        "pairs": len(pairs),
        "per_pair": per_pair,
        "kinds": dict(kinds),
        "change_rows_gross": len(per_key_pair),
        "in_use_related_rows_gross": sum(r["in_use_related"] for r in per_key_pair.values()),
        "distinct_codes_changed": len(changed_pairs),
        "distinct_codes_became_in_use": len({(r["area_code"], r["local_code"]) for r in rows if r["became_in_use"]}),
        "codes_changed_in_multiple_pairs": sum(1 for n in changed_pairs.values() if n > 1),
    }
