"""コマンドライン入口: python -m regional_classifier <command> ...

すべてのコマンドは地域分類DB（--db）にだけ書き込む。v1.2 のDBは
import-designations で読み取り専用に開くだけで、変更しない。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from . import normalize, store
from .engine import classify, reproduce
from .evaluate import compare_baselines
from .export import export_all
from .features import Feature
from .phone_link import import_designations, import_number_area_map
from .query import SearchFilter, search
from .rules import load_rules


def _read_csv(path: str):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        yield from csv.DictReader(fh)


def cmd_init(a):
    store.open_db(a.db).close()
    print(f"initialized {a.db}")


def cmd_import_towns(a):
    conn = store.open_db(a.db)
    n = 0
    with conn:
        if a.source_id:
            store.add_source(conn, a.source_id, a.source_title or a.source_id, url=a.source_url,
                             source_year=a.census_year)
        for rec in _read_csv(a.csv):
            state, merged = normalize.secrecy_state(rec.get("HTKSYORI", "0"), rec.get("HTKSAKI"))
            store.add_town(conn, rec["town_key"], a.census_year, a.boundary_version, rec["municipality_code"],
                           town_name=rec.get("town_name"), hierarchy_level=int(rec["hierarchy_level"])
                           if rec.get("hierarchy_level") else None,
                           secrecy_state=state, merged_into=merged, source_id=a.source_id)
            n += 1
    print(f"towns imported: {n}")


def cmd_import_features(a):
    """縦持ちCSV（town_key, feature, value）を取り込む。value は e-Stat 表記のままでよい。"""
    conn = store.open_db(a.db)
    with conn:
        store.add_source(conn, a.source_id, a.source_title or a.source_id, url=a.source_url,
                         source_year=a.source_year)
        load_id = store.new_load(conn, a.kind, source_id=a.source_id, note=a.note)
        rows = []
        for rec in _read_csv(a.csv):
            value, state = normalize.parse_stat_value(rec["value"])
            rows.append((normalize.town_key(rec["town_key"]), rec["feature"], Feature(value, state, a.source_id)))
        n = store.put_features(conn, load_id, a.census_year, a.boundary_version, rows)
    print(f"load_id={load_id} features={n}")


def cmd_import_evidence(a):
    conn = store.open_db(a.db)
    with conn:
        load_id = store.new_load(conn, "evidence", note=a.note)
        n = 0
        for rec in _read_csv(a.csv):
            store.add_source(conn, rec["source_id"], rec.get("source_title") or rec["source_id"],
                             url=rec.get("source_url"))
            store.add_evidence(conn, load_id, rec["town_key"], a.census_year, a.boundary_version,
                               rec["evidence_type"], rec["supports_class"], rec["grade"], rec["source_id"],
                               value=float(rec["value"]) if rec.get("value") else None, note=rec.get("note"))
            n += 1
    print(f"load_id={load_id} evidence={n}")


def cmd_classify(a):
    conn = store.open_db(a.db)
    run_id = classify(conn, load_rules(a.rules), a.census_year, a.boundary_version,
                      prefecture=a.prefecture, note=a.note)
    counts = conn.execute("SELECT class_code, label_status, COUNT(*) FROM regional_classification"
                          " WHERE run_id = ? GROUP BY 1, 2 ORDER BY 1, 2", (run_id,)).fetchall()
    print(json.dumps({"run_id": run_id, "counts": [list(r) for r in counts]}, ensure_ascii=False, indent=1))


def cmd_reproduce(a):
    result = reproduce(store.open_db(a.db), a.run_id)
    print(json.dumps(result, ensure_ascii=False, indent=1, default=list))
    return 0 if result["reproduced"] else 1


def cmd_search(a):
    f = SearchFilter(prefecture=a.prefecture, municipality=a.municipality, town=a.town,
                     classes=a.classes or [], require_all_classes=a.require_all, axis=a.axis,
                     primary_only=a.primary_only, mode=a.mode, include_r6=a.include_r6,
                     area_code=a.area_code, local_code=a.local_code, number_area_code=a.number_area_code,
                     carrier=a.carrier, as_of_date=a.as_of_date, usage_status=a.usage_status,
                     relation_level=a.relation_level)
    if a.status:
        f.statuses = tuple(a.status)
    rows = search(store.open_db(a.db), f)
    print(json.dumps(rows, ensure_ascii=False, indent=1))


def cmd_export(a):
    result = export_all(store.open_db(a.db), a.out, prefecture=a.prefecture,
                        include_area_level_matrix=a.area_level_matrix, xlsx=not a.no_xlsx)
    print(json.dumps(result, ensure_ascii=False, indent=1))


def cmd_import_designations(a):
    load_id, n = import_designations(store.open_db(a.db), a.v12_db,
                                     Path(a.select_sql_file).read_text(encoding="utf-8"), note=a.note)
    print(f"load_id={load_id} designations={n}")


def cmd_import_number_areas(a):
    conn = store.open_db(a.db)
    with conn:
        store.add_source(conn, a.source_id, a.source_title or a.source_id, url=a.source_url)
    print(f"rows={import_number_area_map(conn, a.csv, source_id=a.source_id)}")


def cmd_evaluate(a):
    result = compare_baselines(store.open_db(a.db), a.axis, a.census_year, a.boundary_version, k=a.k,
                               group_by=a.group_by, holdout_prefectures=tuple(a.holdout or ()))
    print(json.dumps(result, ensure_ascii=False, indent=1))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="regional_classifier")
    sub = p.add_subparsers(dest="command", required=True)

    def cmd(name, fn, help_text):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--db", required=True)
        s.set_defaults(fn=fn)
        return s

    def year_version(s):
        s.add_argument("--census-year", type=int, required=True)
        s.add_argument("--boundary-version", required=True)

    def source(s, required=True):
        s.add_argument("--source-id", required=required)
        s.add_argument("--source-title")
        s.add_argument("--source-url")

    cmd("init", cmd_init, "地域分類DBを作成")

    s = cmd("import-towns", cmd_import_towns, "町丁目マスタCSVを取込")
    s.add_argument("--csv", required=True)
    year_version(s)
    source(s, required=False)

    s = cmd("import-features", cmd_import_features, "特徴量CSV（縦持ち）を取込")
    s.add_argument("--csv", required=True)
    s.add_argument("--kind", default="census", choices=["census", "gis", "manual"])
    s.add_argument("--source-year", type=int)
    s.add_argument("--note")
    year_version(s)
    source(s)

    s = cmd("import-evidence", cmd_import_evidence, "外部証拠CSVを取込")
    s.add_argument("--csv", required=True)
    s.add_argument("--note")
    year_version(s)

    s = cmd("classify", cmd_classify, "12分類を判定（新しい run として追記）")
    s.add_argument("--rules", required=True)
    s.add_argument("--prefecture")
    s.add_argument("--note")
    year_version(s)

    s = cmd("reproduce", cmd_reproduce, "過去 run を再計算して照合")
    s.add_argument("--run-id", type=int, required=True)

    s = cmd("search", cmd_search, "検索")
    for opt in ("prefecture", "municipality", "town", "axis", "area-code", "local-code", "number-area-code",
                "carrier", "as-of-date", "usage-status"):
        s.add_argument(f"--{opt}")
    s.add_argument("--class", dest="classes", action="append")
    s.add_argument("--status", action="append")
    s.add_argument("--require-all", action="store_true", help="指定した分類すべてに該当する町丁目")
    s.add_argument("--primary-only", action="store_true")
    s.add_argument("--mode", choices=["residential"])
    s.add_argument("--include-r6", action="store_true", help="住宅系モードでも大阪府R6を表示")
    s.add_argument("--relation-level", choices=["area", "observed_verified", "observed_unverified"])

    s = cmd("export", cmd_export, "CSV/XLSX 出力")
    s.add_argument("--out", required=True)
    s.add_argument("--prefecture")
    s.add_argument("--area-level-matrix", action="store_true")
    s.add_argument("--no-xlsx", action="store_true")

    s = cmd("import-designations", cmd_import_designations, "v1.2 DBから番号指定を読み取り専用で写す")
    s.add_argument("--v12-db", required=True)
    s.add_argument("--select-sql-file", required=True)
    s.add_argument("--note")

    s = cmd("import-number-areas", cmd_import_number_areas, "番号区画→市区町村対応CSVを取込")
    s.add_argument("--csv", required=True)
    source(s)

    s = cmd("evaluate", cmd_evaluate, "B0/B1/B2 ベースライン比較")
    s.add_argument("--axis", choices=["R", "L"], required=True)
    s.add_argument("--k", type=int, default=5)
    s.add_argument("--group-by", choices=["municipality", "town"], default="municipality")
    s.add_argument("--holdout", action="append", help="評価専用に残す都道府県コード")
    year_version(s)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
