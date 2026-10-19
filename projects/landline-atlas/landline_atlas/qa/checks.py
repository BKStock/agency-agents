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


def run_checks(conn: sqlite3.Connection, raw_dir: Path) -> list[Check]:
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
    s = diff.summary(conn)
    out.append(Check("Q02_Q03_change_breakdown", "info", True,
                     {k: s[k] for k in ("pairs", "change_rows_gross", "in_use_related_rows_gross",
                                        "distinct_codes_changed", "distinct_codes_became_in_use", "kinds")}))

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

    # Q06 同一原本・同一内容の重複計上
    same = [dict(r) for r in conn.execute(
        """SELECT v.content_sha256, COUNT(DISTINCT sv.as_of_date) AS dates, group_concat(sv.series || ':' || sv.as_of_date)
                  AS where_used
           FROM numbers.selected_version sv JOIN numbers.version v ON v.version_id = sv.version_id
           GROUP BY v.content_sha256 HAVING dates > 1""")]
    out.append(Check("Q06_duplicate_content", "warning", not same,
                     {"same_content_selected_under_different_dates": same}))

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
