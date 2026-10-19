"""CSV / XLSX 出力。

分類別の件数はすべて全地域マスタから再計算して照合し、合わなければ
ReconciliationError で停止する。重複分類の集計では「延べ該当件数」と
「一意地域数」を別の列に出す。
"""

from __future__ import annotations

import csv
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from .phone_link import designation_changes
from .primary import MATCHED
from .rules import CLASS_CODES


class ReconciliationError(AssertionError):
    pass


def _write(path: Path, header: list[str], rows: list[list]) -> Path:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


def _scope(prefecture: str | None, alias: str) -> tuple[str, list]:
    return (f" AND {alias}.prefecture_code = ?", [prefecture]) if prefecture else ("", [])


def build_master(conn: sqlite3.Connection, prefecture: str | None = None) -> tuple[list[str], list[list]]:
    sc, sp = _scope(prefecture, "tm")
    towns = conn.execute(
        f"""SELECT tm.* FROM town_master tm
            WHERE EXISTS (SELECT 1 FROM current_classification cc WHERE cc.town_key = tm.town_key
                          AND cc.census_year = tm.census_year AND cc.boundary_version = tm.boundary_version)
            {sc} ORDER BY tm.town_key, tm.census_year, tm.boundary_version""", sp).fetchall()
    status = defaultdict(dict)
    meta = {}
    for r in conn.execute("SELECT * FROM current_classification"):
        k = (r["town_key"], r["census_year"], r["boundary_version"])
        status[k][r["class_code"]] = r["label_status"]
        meta[k] = (r["run_id"], r["rule_version"])
    axes = defaultdict(dict)
    for r in conn.execute("SELECT * FROM current_axis_result"):
        axes[(r["town_key"], r["census_year"], r["boundary_version"])][r["axis"]] = (r["primary_class"], r["state"])

    header = ["prefecture_code", "municipality_code", "town_key", "town_name", "census_year",
              "boundary_version", "secrecy_state", "run_id", "rule_version",
              "R_primary", "R_axis_state", "L_primary", "L_axis_state",
              *[f"status_{c}" for c in CLASS_CODES], "matched_class_count"]
    rows = []
    for t in towns:
        k = (t["town_key"], t["census_year"], t["boundary_version"])
        st = status[k]
        r_ax, l_ax = axes[k].get("R", (None, None)), axes[k].get("L", (None, None))
        rows.append([t["prefecture_code"], t["municipality_code"], t["town_key"], t["town_name"],
                     t["census_year"], t["boundary_version"], t["secrecy_state"], *meta[k],
                     r_ax[0], r_ax[1], l_ax[0], l_ax[1],
                     *[st.get(c) for c in CLASS_CODES],
                     sum(1 for c in CLASS_CODES if st.get(c) in MATCHED)])
    return header, rows


def export_all(conn: sqlite3.Connection, out_dir: str | Path, *, prefecture: str | None = None,
               include_area_level_matrix: bool = False, xlsx: bool = True) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    sc, sp = _scope(prefecture, "cc")

    header, master = build_master(conn, prefecture)
    written["01_all_regions_master"] = _write(out / "01_all_regions_master.csv", header, master)
    col = {name: i for i, name in enumerate(header)}

    # 02: 分類別（1分類1ファイル）
    class_counts: dict[str, int] = {}
    class_header = ["prefecture_code", "municipality_code", "town_key", "town_name", "label_status",
                    "primary_flag", "secondary_flag", "overlap_flag", "score", "evidence_grade",
                    "reason", "rule_version", "source_year", "source_url"]
    for code in CLASS_CODES:
        rows = [list(r) for r in conn.execute(
            f"""SELECT cc.prefecture_code, cc.municipality_code, cc.town_key, tm.town_name, cc.label_status,
                       cc.primary_flag, cc.secondary_flag, cc.overlap_flag, cc.score, cc.evidence_grade,
                       cc.reason, cc.rule_version, cc.source_year, cc.source_url
                FROM current_classification cc
                JOIN town_master tm ON tm.town_key = cc.town_key AND tm.census_year = cc.census_year
                                   AND tm.boundary_version = cc.boundary_version
                WHERE cc.class_code = ? AND cc.label_status IN ('confirmed', 'candidate') {sc}
                ORDER BY cc.town_key""", [code, *sp])]
        class_counts[code] = len(rows)
        written[f"02_class_{code}"] = _write(out / f"02_class_{code}.csv", class_header, rows)

    # 03: 12分類横断（該当行の縦持ち）
    cross = [list(r) for r in conn.execute(
        f"""SELECT cc.town_key, cc.census_year, cc.boundary_version, cc.class_code, cc.axis, cc.label_status,
                   cc.primary_flag, cc.secondary_flag, cc.overlap_flag, cc.score, cc.evidence_grade
            FROM current_classification cc WHERE cc.label_status IN ('confirmed', 'candidate') {sc}
            ORDER BY cc.town_key, cc.census_year, cc.boundary_version, cc.class_code""", sp)]
    written["03_cross_class"] = _write(out / "03_cross_class.csv",
                                       ["town_key", "census_year", "boundary_version", "class_code",
                                        "axis", "label_status", "primary_flag",
                                        "secondary_flag", "overlap_flag", "score", "evidence_grade"], cross)

    # 04: 未分類・判定保留
    pending = []
    for row in master:
        unresolved = [c for c in CLASS_CODES
                      if row[col[f"status_{c}"]] in ("on_hold", "insufficient_data", "undeterminable")]
        if row[col["matched_class_count"]] == 0 or unresolved:
            pending.append([row[col["town_key"]], row[col["town_name"]], row[col["R_axis_state"]],
                            row[col["L_axis_state"]], row[col["matched_class_count"]], ";".join(unresolved)])
    written["04_unclassified_pending"] = _write(
        out / "04_unclassified_pending.csv",
        ["town_key", "town_name", "R_axis_state", "L_axis_state", "matched_class_count", "unresolved_classes"],
        pending)

    # 05: 地域別番号指定マスタ（区画レベル）
    msc, msp = (" WHERE substr(m.municipality_code, 1, 2) = ?", [prefecture]) if prefecture else ("", [])
    designation = [list(r) for r in conn.execute(
        f"""SELECT m.municipality_code, m.area_code, m.local_code, m.number_area_code, m.carrier,
                   m.usage_status, m.as_of_date, m.coverage, m.source_url
            FROM municipality_code_relation m{msc}
            ORDER BY m.municipality_code, m.area_code, m.local_code, m.as_of_date""", msp)]
    written["05_designation_by_area"] = _write(
        out / "05_designation_by_area.csv",
        ["municipality_code", "area_code", "local_code", "number_area_code", "carrier", "usage_status",
         "as_of_date", "coverage", "source_url"], designation)

    # 06: 公開電話番号の観測履歴
    obs = [list(r) for r in conn.execute(
        "SELECT obs_id, facility_name, facility_type, area_code, local_code, address, town_key, verified,"
        " source_url, observed_on FROM facility_observation ORDER BY observed_on, obs_id")]
    written["06_facility_observations"] = _write(
        out / "06_facility_observations.csv",
        ["obs_id", "facility_name", "facility_type", "area_code", "local_code", "address", "town_key",
         "verified", "source_url", "observed_on"], obs)

    # 07: 町丁目×局番×12分類
    matched_by_town: dict[tuple, list[str]] = defaultdict(list)
    for r in cross:
        matched_by_town[(r[0], r[1], r[2])].append(r[3])
    observed = defaultdict(list)
    for r in conn.execute("SELECT * FROM town_observed_code"):
        observed[(r["town_key"], r["census_year"], r["boundary_version"])].append(r)
    matrix = []
    for row in master:
        key, muni = row[col["town_key"]], row[col["municipality_code"]]
        tk = (key, row[col["census_year"]], row[col["boundary_version"]])
        classes = ";".join(matched_by_town.get(tk, []))
        for r in observed.get(tk, []):
            level = "observed_verified" if r["verified_count"] else "observed_unverified"
            matrix.append([key, muni, r["area_code"], r["local_code"], level, r["observation_count"], classes])
        if not observed.get(tk):
            matrix.append([key, muni, None, None, "no_observation", 0, classes])
        if include_area_level_matrix:
            for r in conn.execute("SELECT DISTINCT area_code, local_code FROM municipality_code_relation"
                                  " WHERE municipality_code = ?", (muni,)):
                matrix.append([key, muni, r["area_code"], r["local_code"], "area", None, classes])
    written["07_town_code_class_matrix"] = _write(
        out / "07_town_code_class_matrix.csv",
        ["town_key", "municipality_code", "area_code", "local_code", "relation_level", "observation_count",
         "matched_classes"], matrix)

    # 08: 分類の根拠・品質評価
    quality = []
    for code in CLASS_CODES:
        st = Counter(r[0] for r in conn.execute(
            f"SELECT cc.label_status FROM current_classification cc WHERE cc.class_code = ? {sc}", [code, *sp]))
        gr = Counter(r[0] for r in conn.execute(
            f"""SELECT cc.evidence_grade FROM current_classification cc WHERE cc.class_code = ?
                AND cc.label_status IN ('confirmed', 'candidate') {sc}""", [code, *sp]))
        quality.append([code, *[st.get(s, 0) for s in ("confirmed", "candidate", "not_applicable", "on_hold",
                                                        "insufficient_data", "undeterminable")],
                        gr.get("A", 0), gr.get("B", 0), gr.get("C", 0)])
    written["08_classification_quality"] = _write(
        out / "08_classification_quality.csv",
        ["class_code", "confirmed", "candidate", "not_applicable", "on_hold", "insufficient_data",
         "undeterminable", "grade_A", "grade_B", "grade_C"], quality)

    # 09: 過去版差分（種別ごと）と、新規使用開始局番の種類数
    changes = designation_changes(conn)
    written["09_designation_changes"] = _write(
        out / "09_designation_changes.csv",
        ["series", "old_date", "new_date", "area_code", "local_code", "change_kind", "old_value", "new_value"],
        [[c[k] for k in ("series", "old_date", "new_date", "area_code", "local_code", "change_kind",
                         "old_value", "new_value")] for c in changes])
    kinds = Counter(c["change_kind"] for c in changes)
    distinct_added = len({(c["area_code"], c["local_code"]) for c in changes if c["change_kind"] == "added"})
    summary = [[k, v] for k, v in sorted(kinds.items())]
    summary += [["TOTAL_CHANGE_ROWS", len(changes)], ["DISTINCT_ADDED_CODES", distinct_added]]
    written["09b_change_summary"] = _write(out / "09b_change_summary.csv", ["metric", "value"], summary)

    # 10: 都道府県別ダッシュボード
    dash: dict[str, Counter] = defaultdict(Counter)
    for row in master:
        d = dash[row[col["prefecture_code"]]]
        d["towns_total"] += 1
        d["towns_merged_excluded"] += row[col["secrecy_state"]] == "merged_into_other"
        n = row[col["matched_class_count"]]
        d["unique_towns_matched"] += n > 0
        d["gross_class_matches"] += n
        d["towns_with_insufficient_data"] += any(
            row[col[f"status_{c}"]] == "insufficient_data" for c in CLASS_CODES)
        for c in CLASS_CODES:
            d[f"towns_{c}"] += row[col[f"status_{c}"]] in MATCHED
    dash_header = ["prefecture_code", "towns_total", "towns_merged_excluded", "unique_towns_matched",
                   "gross_class_matches", "towns_with_insufficient_data", *[f"towns_{c}" for c in CLASS_CODES]]
    dash_rows = [[p, *[d[h] for h in dash_header[1:]]] for p, d in sorted(dash.items())]
    written["10_prefecture_dashboard"] = _write(out / "10_prefecture_dashboard.csv", dash_header, dash_rows)

    report = reconcile(master, col, class_counts, cross, dash_rows, dash_header)
    if xlsx:
        path = _write_xlsx(out / "regional_classification.xlsx", written)
        if path:
            written["xlsx"] = path
    return {"files": {k: str(v) for k, v in written.items()}, "reconciliation": report}


def reconcile(master, col, class_counts, cross, dash_rows, dash_header) -> dict:
    """全分類の件数を全地域マスタから再計算し、各出力と照合する。"""
    from_master = {c: sum(1 for r in master if r[col[f"status_{c}"]] in MATCHED) for c in CLASS_CODES}
    problems = [f"{c}: class file {class_counts[c]} != master {from_master[c]}"
                for c in CLASS_CODES if class_counts[c] != from_master[c]]
    gross = sum(from_master.values())
    unique = sum(1 for r in master if r[col["matched_class_count"]] > 0)
    if len(cross) != gross:
        problems.append(f"cross_class rows {len(cross)} != gross {gross}")
    dcol = {h: i for i, h in enumerate(dash_header)}
    if sum(r[dcol["gross_class_matches"]] for r in dash_rows) != gross:
        problems.append("dashboard gross mismatch")
    if sum(r[dcol["unique_towns_matched"]] for r in dash_rows) != unique:
        problems.append("dashboard unique mismatch")
    for c in CLASS_CODES:
        if sum(r[dcol[f"towns_{c}"]] for r in dash_rows) != from_master[c]:
            problems.append(f"dashboard {c} mismatch")
    if problems:
        raise ReconciliationError("; ".join(problems))
    return {"per_class": from_master, "gross_class_matches": gross, "unique_towns_matched": unique,
            "towns_total": len(master)}


def _write_xlsx(path: Path, csv_files: dict[str, Path]) -> Path | None:
    try:
        from openpyxl import Workbook
    except ImportError:
        return None
    wb = Workbook()
    wb.remove(wb.active)
    for name, csv_path in csv_files.items():
        ws = wb.create_sheet(title=name[:31])
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.reader(fh):
                # 先頭ゼロ付きコードを数値に変換させないため、すべて文字列のまま書く
                ws.append(row)
    wb.save(path)
    return path
