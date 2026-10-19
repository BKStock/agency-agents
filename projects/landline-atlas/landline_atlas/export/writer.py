"""CSV / XLSX 出力（指示書 §9 の11種＋差分要約・分類別局番集計・調査優先順位）。

分類別の件数は全地域マスタから再計算して照合し、合わなければ ReconciliationError。
延べ該当件数と一意地域数は別の列に出す。先頭ゼロのコードは XLSX でも文字列で書く。
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from ..classify.engine import MATCHED
from ..classify.rules import CODES
from ..history import diff


class ReconciliationError(AssertionError):
    pass


def _write(out: Path, name: str, header: list[str], rows: list[list], files: dict) -> None:
    path = out / f"{name}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    files[name] = (path, header, rows)


def export_all(conn: sqlite3.Connection, out_dir: Path, *, prefecture: str | None = None, xlsx: bool = True) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files: dict = {}
    scope, sp = (" AND c.prefecture_code = ?", [prefecture]) if prefecture else ("", [])

    status = defaultdict(dict)
    rowmeta = {}
    for r in conn.execute(f"SELECT * FROM regions.current_classification c WHERE 1=1{scope}", sp):
        k = (r["town_key"], r["census_year"], r["boundary_version"])
        status[k][r["class_code"]] = r["label_status"]
        rowmeta[k] = (r["run_id"], r["rule_version"])
    axes = defaultdict(dict)
    for r in conn.execute("SELECT * FROM regions.current_axis"):
        axes[(r["town_key"], r["census_year"], r["boundary_version"])][r["axis"]] = (r["primary_class"], r["state"])
    towns = {(t["town_key"], t["census_year"], t["boundary_version"]): t
             for t in conn.execute("SELECT * FROM regions.town")}

    # 01 全国全地域マスタ
    mh = ["prefecture_code", "municipality_code", "town_key", "town_name", "census_year", "boundary_version",
          "secrecy", "run_id", "rule_version", "R_primary", "R_state", "L_primary", "L_state",
          *[f"status_{c}" for c in CODES], "matched_classes"]
    master = []
    for k in sorted(status):
        t, st = towns[k], status[k]
        ra, la = axes[k].get("R", (None, None)), axes[k].get("L", (None, None))
        master.append([t["prefecture_code"], t["municipality_code"], t["town_key"], t["town_name"], k[1], k[2],
                       t["secrecy"], *rowmeta[k], ra[0], ra[1], la[0], la[1], *[st.get(c) for c in CODES],
                       sum(st.get(c) in MATCHED for c in CODES)])
    _write(out, "01_all_regions_master", mh, master, files)
    col = {h: i for i, h in enumerate(mh)}

    # 02 分類別（R1〜R6・L1〜L6）
    ch = ["prefecture_code", "municipality_code", "town_key", "town_name", "label_status", "primary_flag",
          "secondary_flag", "overlap_flag", "score", "evidence_grade", "reason", "source_url"]
    per_class = {}
    for code in CODES:
        rows = [list(r) for r in conn.execute(
            f"""SELECT c.prefecture_code, c.municipality_code, c.town_key, t.town_name, c.label_status,
                       c.primary_flag, c.secondary_flag, c.overlap_flag, c.score, c.evidence_grade, c.reason,
                       c.source_url
                FROM regions.current_classification c JOIN regions.town t ON t.town_key = c.town_key
                 AND t.census_year = c.census_year AND t.boundary_version = c.boundary_version
                WHERE c.class_code = ? AND c.label_status IN ('confirmed', 'candidate'){scope}
                ORDER BY c.town_key""", [code, *sp])]
        per_class[code] = len(rows)
        _write(out, f"02_class_{code}", ch, rows, files)

    # 03 12分類横断
    cross = [list(r) for r in conn.execute(
        f"""SELECT c.town_key, c.census_year, c.boundary_version, c.class_code, c.axis, c.label_status,
                   c.primary_flag, c.secondary_flag, c.overlap_flag, c.score, c.evidence_grade
            FROM regions.current_classification c WHERE c.label_status IN ('confirmed', 'candidate'){scope}
            ORDER BY c.town_key, c.class_code""", sp)]
    _write(out, "03_cross_class", ["town_key", "census_year", "boundary_version", "class_code", "axis",
                                   "label_status", "primary_flag", "secondary_flag", "overlap_flag", "score",
                                   "evidence_grade"], cross, files)

    # 04 未分類・判定保留
    pending = []
    for r in master:
        unresolved = [c for c in CODES if r[col[f"status_{c}"]] in ("on_hold", "insufficient_data", "undeterminable")]
        if r[col["matched_classes"]] == 0 or unresolved:
            pending.append([r[col["town_key"]], r[col["town_name"]], r[col["R_state"]], r[col["L_state"]],
                            r[col["matched_classes"]], ";".join(unresolved)])
    _write(out, "04_unclassified_pending", ["town_key", "town_name", "R_state", "L_state", "matched_classes",
                                            "unresolved_classes"], pending, files)

    # 05 地域別番号指定マスタ
    mscope, mp = (" WHERE substr(municipality_code, 1, 2) = ?", [prefecture]) if prefecture else ("", [])
    _write(out, "05_designation_by_area",
           ["municipality_code", "area_code", "local_code", "number_area_code", "carrier", "usage_status",
            "as_of_date", "series", "coverage"],
           [list(r) for r in conn.execute(
               "SELECT municipality_code, area_code, local_code, number_area_code, carrier, usage_status, as_of_date,"
               f" series, coverage FROM municipality_code_relation{mscope} ORDER BY 1, 2, 3", mp)], files)

    # 06 公開電話番号の観測履歴
    _write(out, "06_facility_observations",
           ["obs_id", "facility_name", "facility_type", "area_code", "local_code", "address", "town_key",
            "verified", "source_url", "observed_on"],
           [list(r) for r in conn.execute(
               "SELECT obs_id, facility_name, facility_type, area_code, local_code, address, town_key, verified,"
               " source_url, observed_on FROM evidence.facility_observation ORDER BY observed_on, obs_id")], files)

    # 07 町丁目×局番×12分類
    matched = defaultdict(list)
    for r in cross:
        matched[(r[0], r[1], r[2])].append(r[3])
    observed = defaultdict(list)
    for r in conn.execute("SELECT * FROM town_observed_code"):
        observed[(r["town_key"], r["census_year"], r["boundary_version"])].append(r)
    matrix = []
    for r in master:
        k = (r[col["town_key"]], r[col["census_year"]], r[col["boundary_version"]])
        classes = ";".join(matched.get(k, []))
        for o in observed.get(k, []):
            matrix.append([k[0], o["area_code"], o["local_code"], o["relation_level"], o["observation_count"], classes])
        if not observed.get(k):
            matrix.append([k[0], None, None, "no_observation", 0, classes])
    _write(out, "07_town_code_class_matrix", ["town_key", "area_code", "local_code", "relation_level",
                                              "observation_count", "matched_classes"], matrix, files)

    # 08 分類の根拠・品質評価
    quality = []
    for code in CODES:
        st = Counter(r[col[f"status_{code}"]] for r in master)
        gr = Counter(x[10] for x in cross if x[3] == code)
        quality.append([code, *[st.get(s, 0) for s in ("confirmed", "candidate", "not_applicable", "on_hold",
                                                        "insufficient_data", "undeterminable")],
                        gr.get("A", 0), gr.get("B", 0), gr.get("C", 0)])
    _write(out, "08_classification_quality", ["class_code", "confirmed", "candidate", "not_applicable", "on_hold",
                                              "insufficient_data", "undeterminable", "grade_A", "grade_B",
                                              "grade_C"], quality, files)

    # 09 過去版差分と変更履歴（種別別）＋要約（延べと一意）
    changes = [list(r) for r in conn.execute(
        """SELECT p.series, p.old_date, p.new_date, d.area_code, d.local_code, d.change_kind, d.old_value,
                  d.new_value, d.in_use_related, d.became_in_use
           FROM numbers.diff_row d JOIN numbers.diff_pair p ON p.pair_id = d.pair_id
           ORDER BY p.series, p.new_date, d.area_code, d.local_code""")]
    _write(out, "09_designation_changes", ["series", "old_date", "new_date", "area_code", "local_code",
                                           "change_kind", "old_value", "new_value", "in_use_related",
                                           "became_in_use"], changes, files)
    s = diff.summary(conn)
    _write(out, "09b_change_summary", ["metric", "value"],
           [["pairs", s["pairs"]], ["change_rows_gross", s["change_rows_gross"]],
            ["in_use_related_rows_gross", s["in_use_related_rows_gross"]],
            ["distinct_codes_changed", s["distinct_codes_changed"]],
            ["distinct_codes_became_in_use", s["distinct_codes_became_in_use"]],
            ["codes_changed_in_multiple_pairs", s["codes_changed_in_multiple_pairs"]],
            *[[f"kind_{k}", v] for k, v in sorted(s["kinds"].items())]], files)

    # 10 都道府県別ダッシュボード
    dash = defaultdict(Counter)
    for r in master:
        d = dash[r[col["prefecture_code"]]]
        d["towns_total"] += 1
        d["towns_merged_excluded"] += r[col["secrecy"]] == "merged_into_other"
        d["unique_towns_matched"] += r[col["matched_classes"]] > 0
        d["gross_class_matches"] += r[col["matched_classes"]]
        d["towns_insufficient"] += any(r[col[f"status_{c}"]] == "insufficient_data" for c in CODES)
        for c in CODES:
            d[f"towns_{c}"] += r[col[f"status_{c}"]] in MATCHED
    dh = ["prefecture_code", "towns_total", "towns_merged_excluded", "unique_towns_matched", "gross_class_matches",
          "towns_insufficient", *[f"towns_{c}" for c in CODES]]
    dash_rows = [[p, *[d[h] for h in dh[1:]]] for p, d in sorted(dash.items())]
    _write(out, "10_prefecture_dashboard", dh, dash_rows, files)

    # 11 12分類別の使用中局番候補（区画レベル・観測を別列）
    by_class = []
    for code in CODES:
        area_codes = {(r[0], r[1]) for r in conn.execute(
            f"""SELECT DISTINCT m.area_code, m.local_code FROM regions.current_classification c
                JOIN municipality_code_relation m ON m.municipality_code = c.municipality_code
                WHERE c.class_code = ? AND c.label_status IN ('confirmed', 'candidate'){scope}""", [code, *sp])}
        obs_codes = {(r[0], r[1]) for r in conn.execute(
            f"""SELECT DISTINCT o.area_code, o.local_code FROM regions.current_classification c
                JOIN town_observed_code o ON o.town_key = c.town_key AND o.census_year = c.census_year
                 AND o.boundary_version = c.boundary_version
                WHERE c.class_code = ? AND c.label_status IN ('confirmed', 'candidate'){scope}""", [code, *sp])}
        by_class.append([code, per_class[code], len(area_codes), len(obs_codes)])
    _write(out, "11_class_code_summary", ["class_code", "towns", "codes_area_level", "codes_observed"],
           by_class, files)

    # 12 調査優先順位（最新）
    _write(out, "12_survey_priority", ["rank", "area_code", "local_code", "number_area_code", "score", "components",
                                       "evidence_level", "municipalities", "facility_types", "reasons"],
           [list(r) for r in conn.execute(
               "SELECT rank, area_code, local_code, number_area_code, score, components, evidence_level,"
               " municipalities, facility_types, reasons FROM priority_item"
               " WHERE priority_run_id = (SELECT MAX(priority_run_id) FROM priority_run) ORDER BY rank")], files)

    report = reconcile(master, col, per_class, cross, dash_rows, dh)
    if xlsx:
        _xlsx(out / "landline_atlas.xlsx", files)
    (out / "export_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"files": sorted(str(v[0]) for v in files.values()), "reconciliation": report}


def reconcile(master, col, per_class, cross, dash_rows, dh) -> dict:
    from_master = {c: sum(r[col[f"status_{c}"]] in MATCHED for r in master) for c in CODES}
    problems = [f"{c}: file {per_class[c]} != master {from_master[c]}" for c in CODES if per_class[c] != from_master[c]]
    gross = sum(from_master.values())
    unique = sum(r[col["matched_classes"]] > 0 for r in master)
    if len(cross) != gross:
        problems.append(f"cross {len(cross)} != gross {gross}")
    di = {h: i for i, h in enumerate(dh)}
    if sum(r[di["gross_class_matches"]] for r in dash_rows) != gross:
        problems.append("dashboard gross mismatch")
    if sum(r[di["unique_towns_matched"]] for r in dash_rows) != unique:
        problems.append("dashboard unique mismatch")
    for c in CODES:
        if sum(r[di[f"towns_{c}"]] for r in dash_rows) != from_master[c]:
            problems.append(f"dashboard {c} mismatch")
    if problems:
        raise ReconciliationError("; ".join(problems))
    return {"per_class": from_master, "gross_class_matches": gross, "unique_towns_matched": unique,
            "towns_total": len(master)}


def _xlsx(path: Path, files: dict) -> None:
    try:
        from openpyxl import Workbook
    except ImportError:
        return
    wb = Workbook()
    wb.remove(wb.active)
    for name, (_, header, rows) in files.items():
        ws = wb.create_sheet(name[:31])
        ws.append(header)
        for r in rows:
            ws.append(list(r))
            for cell in ws[ws.max_row]:
                if isinstance(cell.value, str):
                    cell.number_format = "@"
    wb.save(path)
