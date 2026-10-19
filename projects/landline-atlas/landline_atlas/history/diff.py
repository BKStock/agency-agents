"""選択済みの版のうち、同じ系統で基準日が隣接する版同士だけを比較する。

変更種別は added / removed / carrier_changed / status_changed / number_area_changed /
cosmetic_only（NFKC・空白除去で消える差分）。延べ件数（変更行）と一意件数（局番の種類）を
常に分けて集計する。片方の版で全行空欄の列（その年の表に無い列）は比較しない。
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict

from ..db.connection import utc_now
from ..normalize import compact

_COMPARED = (("carrier", "carrier_changed"), ("usage_status", "status_changed"),
             ("number_area_code", "number_area_changed"))
_OPTIONAL = ("carrier", "usage_status", "number_area_code", "number_area_name")
_BULK_KINDS = ("carrier_changed", "status_changed", "number_area_changed")
# 差分の計算方法の版。変えたら既存の差分（派生データ）を作り直す
ALGORITHM = "2-skip-empty-columns"


def _rows(conn: sqlite3.Connection, version_id: int) -> dict[tuple[str, str], sqlite3.Row]:
    out = {}
    for r in conn.execute("SELECT * FROM numbers.designation_row WHERE version_id = ? ORDER BY row_no",
                          (version_id,)):
        out[(r["area_code"], r["local_code"])] = r  # 版内の重複キーは品質検査 Q04 で報告する
    return out


def _present(rows: dict) -> set[str]:
    """値が1つでもある列。全行空欄の列は、その版には実質的に無いものとして扱う。"""
    return {c for c in _OPTIONAL if any(compact(r[c]) for r in rows.values())}


def adjacent_pairs(conn: sqlite3.Connection) -> list[tuple[str, sqlite3.Row, sqlite3.Row]]:
    by_series = defaultdict(list)
    for r in conn.execute("SELECT * FROM numbers.selected_version ORDER BY series, as_of_date"):
        by_series[r["series"]].append(r)
    return [(s, a, b) for s, vs in by_series.items() for a, b in zip(vs, vs[1:])]


def _reset_if_algorithm_changed(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT value FROM numbers.diff_meta WHERE key = 'algorithm'").fetchone()
    if row is None or row[0] != ALGORITHM:
        # 差分は原本・版から作り直せる派生データなので、消して計算し直す（原本・版・指定行には触れない）
        conn.execute("DELETE FROM numbers.diff_row")
        conn.execute("DELETE FROM numbers.diff_pair")
        conn.execute("INSERT OR REPLACE INTO numbers.diff_meta VALUES ('algorithm', ?)", (ALGORITHM,))


def compute_diffs(conn: sqlite3.Connection, in_use_values: list[str]) -> int:
    """未計算の隣接組の差分を計算して追記する。新しく計算した組の数を返す。"""
    _reset_if_algorithm_changed(conn)
    in_use = {compact(v) for v in in_use_values}

    def used(r) -> bool:
        return r is not None and compact(r["usage_status"]) in in_use

    created = 0
    for series, old, new in adjacent_pairs(conn):
        exists = conn.execute("SELECT 1 FROM numbers.diff_pair WHERE old_version = ? AND new_version = ?",
                              (old["version_id"], new["version_id"])).fetchone()
        if exists:
            continue
        a, b = _rows(conn, old["version_id"]), _rows(conn, new["version_id"])
        both = _present(a) & _present(b)
        skipped = sorted((_present(a) | _present(b)) - both)
        pair_id = conn.execute(
            "INSERT INTO numbers.diff_pair (series, old_version, new_version, old_date, new_date, computed_at,"
            " skipped_columns) VALUES (?,?,?,?,?,?,?)",
            (series, old["version_id"], new["version_id"], old["as_of_date"], new["as_of_date"], utc_now(),
             ",".join(skipped) or None)).lastrowid
        compared = [(c, k) for c, k in _COMPARED if c in both]
        for key in sorted(set(a) | set(b)):
            ra, rb = a.get(key), b.get(key)
            changes: list[tuple[str, str | None, str | None]] = []
            if ra is None:
                changes = [("added", None, rb["usage_status"])]
            elif rb is None:
                changes = [("removed", ra["usage_status"], None)]
            else:
                cosmetic = False
                for col, kind in compared:
                    if compact(ra[col]) != compact(rb[col]):
                        changes.append((kind, ra[col], rb[col]))
                    elif ra[col] != rb[col]:
                        cosmetic = True
                name_differs = "number_area_name" in both and ra["number_area_name"] != rb["number_area_name"]
                if not changes and (cosmetic or name_differs):
                    if name_differs and compact(ra["number_area_name"]) != compact(rb["number_area_name"]):
                        changes = [("number_area_changed", ra["number_area_name"], rb["number_area_name"])]
                    else:
                        changes = [("cosmetic_only", None, None)]
            related = int(used(ra) or used(rb))
            became = int(used(rb) and not used(ra))
            conn.executemany("INSERT INTO numbers.diff_row VALUES (?,?,?,?,?,?,?,?)",
                             [(pair_id, key[0], key[1], kind, ov, nv, related, became) for kind, ov, nv in changes])
        created += 1
    return created


def summary(conn: sqlite3.Connection, *, bulk_min_rows: int = 30) -> dict:
    """差分の集計。延べ（変更行）と一意（局番の種類）を分ける。

    1つの組の中で同じ「旧の値 → 新の値」が bulk_min_rows 行以上ある変更は、名称・表記・区画コードの
    一斉変更（一括変更）として、個別の変更と分けて数える。生の行はそのまま残す。
    """
    pairs = [dict(r) for r in conn.execute("SELECT * FROM numbers.diff_pair ORDER BY series, old_date")]
    current_pairs = {(s, a["version_id"], b["version_id"]) for s, a, b in adjacent_pairs(conn)}
    pairs = [p for p in pairs if (p["series"], p["old_version"], p["new_version"]) in current_pairs]
    ids = [p["pair_id"] for p in pairs]
    empty = {"pairs": 0, "per_pair": [], "kinds": {}, "change_rows_gross": 0, "in_use_related_rows_gross": 0,
             "distinct_codes_changed": 0, "distinct_codes_became_in_use": 0, "codes_changed_in_multiple_pairs": 0,
             "bulk_changes": {}, "individual_changes": {}, "top_transitions": [], "bulk_min_rows": bulk_min_rows,
             "pairs_with_skipped_columns": 0}
    if not ids:
        return empty
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM numbers.diff_row WHERE pair_id IN ({marks})", ids).fetchall()
    by_pair = {p["pair_id"]: p for p in pairs}

    transitions = Counter((r["pair_id"], r["change_kind"], compact(r["old_value"]), compact(r["new_value"]))
                          for r in rows if r["change_kind"] in _BULK_KINDS)
    bulk_keys = {k for k, n in transitions.items() if n >= bulk_min_rows}

    def is_bulk(r) -> bool:
        return (r["pair_id"], r["change_kind"], compact(r["old_value"]), compact(r["new_value"])) in bulk_keys

    example = {}
    for r in rows:
        k = (r["pair_id"], r["change_kind"], compact(r["old_value"]), compact(r["new_value"]))
        if k in bulk_keys:
            example.setdefault(k, (r["old_value"], r["new_value"]))
    top = sorted(({"series": by_pair[k[0]]["series"], "old_date": by_pair[k[0]]["old_date"],
                   "new_date": by_pair[k[0]]["new_date"], "kind": k[1], "old_value": example[k][0],
                   "new_value": example[k][1], "rows": transitions[k]} for k in bulk_keys),
                 key=lambda t: (-t["rows"], t["series"], t["new_date"], t["kind"]))

    per_key_pair = {(r["pair_id"], r["area_code"], r["local_code"]): r for r in rows}
    rows_by_pair, keys_by_pair = defaultdict(list), defaultdict(list)
    for r in rows:
        rows_by_pair[r["pair_id"]].append(r)
    for k in per_key_pair:
        keys_by_pair[k[0]].append(k)
    per_pair = []
    for p in pairs:
        prow, keys = rows_by_pair[p["pair_id"]], keys_by_pair[p["pair_id"]]
        per_pair.append({**p, "skipped_columns": p.get("skipped_columns"), "change_rows": len(keys),
                         "in_use_related_rows": sum(per_key_pair[k]["in_use_related"] for k in keys),
                         "bulk_rows": sum(is_bulk(r) for r in prow),
                         "kinds": dict(Counter(r["change_kind"] for r in prow))})
    changed_pairs = Counter((k[1], k[2]) for k in per_key_pair)
    return {
        "pairs": len(pairs),
        "per_pair": per_pair,
        "kinds": dict(Counter(r["change_kind"] for r in rows)),
        "bulk_changes": dict(Counter(r["change_kind"] for r in rows if is_bulk(r))),
        "individual_changes": dict(Counter(r["change_kind"] for r in rows if not is_bulk(r))),
        "top_transitions": top,
        "bulk_min_rows": bulk_min_rows,
        "pairs_with_skipped_columns": sum(1 for p in pairs if p.get("skipped_columns")),
        "change_rows_gross": len(per_key_pair),
        "in_use_related_rows_gross": sum(r["in_use_related"] for r in per_key_pair.values()),
        "distinct_codes_changed": len(changed_pairs),
        "distinct_codes_became_in_use": len({(r["area_code"], r["local_code"]) for r in rows if r["became_in_use"]}),
        "codes_changed_in_multiple_pairs": sum(1 for n in changed_pairs.values() if n > 1),
    }
