"""品質検査。指示書 §10 の監査10項目を、このシステム自身の検査として毎回実行する。

severity=error が1つでも不合格なら、パイプラインは分類・出力へ進まない。
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..acquire.archive import sha256_file
from ..db.connection import integrity, utc_now
from ..history import diff


@dataclass
class Check:
    check_id: str
    severity: str
    passed: bool
    detail: dict


def run_checks(conn: sqlite3.Connection, raw_dir: Path, *, min_pair_overlap: float = 0.5,
               bulk_min_rows: int = 30, carriers=None) -> list[Check]:
    out: list[Check] = []

    # Q01 比較組：同一系統・選択済みの版・基準日が隣接
    selected = defaultdict(list)
    for r in conn.execute("SELECT * FROM numbers.selected_version ORDER BY series, as_of_date"):
        selected[r["series"]].append(r["version_id"])
    expected = {(a, b) for vs in selected.values() for a, b in zip(vs, vs[1:])}
    computed = {(r[0], r[1]) for r in conn.execute("SELECT old_version, new_version FROM numbers.diff_pair")}
    missing = sorted(expected - computed)
    out.append(Check("Q01_pairs_adjacent", "error", not missing,
                     {"expected_pairs": len(expected), "missing_pairs": missing,
                      "stale_pairs_not_used": len(computed - expected)}))

    # Q02/Q03 変更の内訳：延べと一意を分けて記録（情報）
    s = diff.summary(conn, bulk_min_rows=bulk_min_rows, carriers=carriers)
    out.append(Check("Q02_Q03_change_breakdown", "info", True,
                     {k: s[k] for k in ("pairs", "change_rows_gross", "in_use_related_rows_gross",
                                        "distinct_codes_changed", "distinct_codes_became_in_use", "kinds",
                                        "individual_changes", "bulk_changes", "pairs_with_skipped_columns",
                                        "carrier_change_types")}))

    # Q04 同一行の複数期間計上・版内重複
    dups = [dict(r) for r in conn.execute(
        """SELECT sv.series, sv.as_of_date, r.area_code, r.local_code, COUNT(*) AS n
           FROM numbers.selected_version sv JOIN numbers.designation_row r ON r.version_id = sv.version_id
           GROUP BY sv.version_id, r.area_code, r.local_code HAVING n > 1 LIMIT 200""")]
    out.append(Check("Q04_duplicates_and_multi_period", "warning", not dups,
                     {"duplicate_keys_in_selected_versions": dups,
                      "codes_changed_in_multiple_pairs": s["codes_changed_in_multiple_pairs"]}))

    # Q05 表形式変更による見かけの差分
    suspicious = [{"pair_id": p["pair_id"], "series": p["series"], "new_date": p["new_date"],
                   "change_rows": p["change_rows"], "cosmetic": p["kinds"].get("cosmetic_only", 0)}
                  for p in s["per_pair"] if p["change_rows"] and p["kinds"].get("cosmetic_only", 0) * 2 > p["change_rows"]]
    out.append(Check("Q05_format_change", "warning", not suspicious, {"pairs_mostly_cosmetic": suspicious}))

    # Q06 同一内容が別の日付で選ばれている。同じ系統で日付が隣り合うだけなら「変更なしの再掲」（情報）、
    #     それ以外（別の系統、または一度変わった内容に戻る）は要注意（警告）
    order = {series: [v for v in vids] for series, vids in selected.items()}
    uses = defaultdict(list)
    for r in conn.execute("""SELECT sv.series, sv.as_of_date, sv.version_id, v.content_sha256
                             FROM numbers.selected_version sv JOIN numbers.version v ON v.version_id = sv.version_id
                             ORDER BY sv.series, sv.as_of_date"""):
        uses[r["content_sha256"]].append((r["series"], r["as_of_date"], r["version_id"]))
    republished, suspicious_same = [], []
    for content, where in uses.items():
        if len(where) < 2:
            continue
        item = {"content_sha256": content, "where_used": [f"{s_}:{d}" for s_, d, _ in where]}
        series_set = {w[0] for w in where}
        idx = sorted(order[w[0]].index(w[2]) for w in where) if len(series_set) == 1 else []
        contiguous = bool(idx) and idx == list(range(idx[0], idx[0] + len(idx)))
        (republished if contiguous else suspicious_same).append(item)
    out.append(Check("Q06_duplicate_content", "warning", not suspicious_same,
                     {"same_content_not_adjacent": suspicious_same, "republished_unchanged": republished}))

    # Q07 取得失敗とリトライ
    fails = [dict(r) for r in conn.execute(
        """SELECT canonical, COUNT(*) AS attempts, MAX(status) AS last_status
           FROM numbers.fetch_log GROUP BY canonical
           HAVING SUM(CASE WHEN status BETWEEN 200 AND 299 THEN 1 ELSE 0 END) = 0""")]
    retried = conn.execute("SELECT COUNT(*) FROM (SELECT canonical FROM numbers.fetch_log GROUP BY canonical"
                           " HAVING COUNT(*) > 1)").fetchone()[0]
    out.append(Check("Q07_fetch_failures", "warning", not fails,
                     {"never_succeeded": fails[:200], "urls_retried": retried}))

    # Q08 原本の SHA-256 再検証
    bad = []
    for r in conn.execute("SELECT sha256, stored_path FROM numbers.raw_file"):
        p = raw_dir / r["stored_path"]
        if not p.is_file():
            bad.append({"sha256": r["sha256"], "problem": "missing"})
        elif sha256_file(p) != r["sha256"]:
            bad.append({"sha256": r["sha256"], "problem": "mismatch"})
    out.append(Check("Q08_raw_sha256", "error", not bad, {"problems": bad}))

    # Q09 同一基準日・異なる内容の版：理由が記録されているか
    differ = [dict(r) for r in conn.execute(
        """SELECT series, as_of_date, COUNT(DISTINCT content_sha256) AS contents FROM numbers.version
           WHERE as_of_date IS NOT NULL GROUP BY series, as_of_date HAVING contents > 1""")]
    unexplained = [d for d in differ if not conn.execute(
        "SELECT 1 FROM numbers.selected_version WHERE series = ? AND as_of_date = ? AND reason LIKE 'content_differs%'",
        (d["series"], d["as_of_date"])).fetchone()]
    undated = conn.execute("SELECT COUNT(*) FROM numbers.version WHERE as_of_date IS NULL").fetchone()[0]
    out.append(Check("Q09_same_date_selection", "error", not unexplained,
                     {"same_date_different_content": differ, "unexplained": unexplained,
                      "versions_without_date": undated}))

    # Q10 DB の整合性
    integ = integrity(conn)
    out.append(Check("Q10_integrity", "error", all(v == "ok" for v in integ.values()), integ))

    # Q12 比較組の重なり率：新旧どちらかにある局番のうち、両方にある割合。低ければ別の表同士を比べている疑い
    low = []
    for series, old, new in diff.adjacent_pairs(conn):
        n_old, n_new = (conn.execute("SELECT COUNT(*) FROM (SELECT DISTINCT area_code, local_code"
                                     " FROM numbers.designation_row WHERE version_id = ?)", (v,)).fetchone()[0]
                        for v in (old["version_id"], new["version_id"]))
        both = conn.execute("SELECT COUNT(*) FROM (SELECT area_code, local_code FROM numbers.designation_row"
                            " WHERE version_id = ? INTERSECT SELECT area_code, local_code FROM numbers.designation_row"
                            " WHERE version_id = ?)", (old["version_id"], new["version_id"])).fetchone()[0]
        union = n_old + n_new - both
        overlap = round(both / union, 4) if union else 1.0
        if overlap < min_pair_overlap:
            low.append({"series": series, "old_date": old["as_of_date"], "new_date": new["as_of_date"],
                        "old_rows": n_old, "new_rows": n_new, "common": both, "overlap": overlap})
    out.append(Check("Q12_pair_overlap", "error", not low,
                     {"min_pair_overlap": min_pair_overlap, "low_overlap_pairs": low}))

    # Q13 一括変更：1つの組で同じ「旧 → 新」が多数。名称・表記・区画コードの一斉変更の疑いがあり、確認が必要
    out.append(Check("Q13_bulk_transitions", "warning", not s["top_transitions"],
                     {"bulk_min_rows": bulk_min_rows, "bulk_changes": s["bulk_changes"],
                      "individual_changes": s["individual_changes"], "carrier_change_types": s["carrier_change_types"],
                      "top": s["top_transitions"][:50]}))

    # 補正の可視化（先頭0の補い・結合セルの引継ぎ）
    repaired = {r[0]: r[1] for r in conn.execute(
        """SELECT r.repaired, COUNT(*) FROM numbers.selected_version sv
           JOIN numbers.designation_row r ON r.version_id = sv.version_id
           WHERE r.repaired IS NOT NULL GROUP BY r.repaired""")}
    out.append(Check("Q11_parse_repairs", "info", True, {"repaired_rows": repaired}))
    return out


def record(conn: sqlite3.Connection, pipeline_id: int | None, checks: list[Check]) -> None:
    now = utc_now()
    conn.executemany("INSERT INTO qa_result VALUES (?,?,?,?,?,?)",
                     [(pipeline_id, c.check_id, c.severity, int(c.passed), json.dumps(c.detail, ensure_ascii=False),
                       now) for c in checks])


def blocking(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.severity == "error" and not c.passed]
