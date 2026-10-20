"""atlas コマンド。データ置き場は --data-dir または環境変数 LANDLINE_ATLAS_DATA（既定 ~/landline_atlas_data）。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import paths as paths_mod
from .classify import engine, rules as rules_mod
from .db.connection import connect
from .discover import priority
from .evaluate.baselines import compare
from .evidence import facilities
from .export.writer import export_all
from .history.carriers import CarrierMap
from .ingest import areas, census
from .link import relations
from .pipeline import CONFIG_DIR, update
from .qa import backup, checks
from .regions.features import add_source, new_load
from .search.query import Filter, search


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def _conn(a, read_only=False):
    conn = connect(a.paths.db, read_only=read_only)
    a.conns.append(conn)
    return conn


def cmd_update(a):
    files = []
    for item in a.import_file or []:
        head, _, tail = item.rpartition(":")
        if head and re.fullmatch(r"0\d{1,3}", tail):  # 旧形式 FILE:SERIES
            print(f"注意: 系統の指定 :{tail} は不要になりました（表の中の市外局番から自動で決めます）", file=sys.stderr)
            item = head
        files.append(Path(item).expanduser())
    m = update(a.paths, config_dir=a.config_dir, online=not a.offline, wayback=a.wayback, local_files=files,
               do_export=not a.no_export)
    _print(m)
    return 0 if m["outcome"] == "ok" else 1


def cmd_init(a):
    a.paths.ensure()
    connect(a.paths.db).close()
    print(f"initialized {a.paths.root}")


def cmd_fetch_census(a):
    from . import census_fetch
    cfg = census_fetch.load_config(a.config_dir)
    a.paths.ensure()
    conn = _conn(a)
    sources = json.loads((a.config_dir / "sources.json").read_text(encoding="utf-8"))
    report = census_fetch.run(conn, a.paths.raw, cfg, a.pref, probe=a.probe, user_agent=sources["user_agent"])
    if not a.probe and not report["stopped"]:
        conn.commit()
    else:
        conn.rollback()
    print(census_fetch.render(report))
    return 1 if report["stopped"] else 0


def cmd_fetch_gis(a):
    from . import gis_fetch
    cfg = gis_fetch.load_config(a.config_dir)
    a.paths.ensure()
    conn = _conn(a)
    sources = json.loads((a.config_dir / "sources.json").read_text(encoding="utf-8"))
    datasets = [d.strip() for d in a.datasets.split(",")] if a.datasets else None
    report = gis_fetch.run(conn, a.paths.raw, cfg, a.pref, probe=a.probe, user_agent=sources["user_agent"],
                           datasets=datasets, refresh=a.refresh)
    if not a.probe and not report["stopped"]:
        conn.commit()
    else:
        conn.rollback()
    print(gis_fetch.render(report))
    return 1 if report["stopped"] else 0


def cmd_import_census(a):
    conn = _conn(a)
    mapping = census.load_mapping(a.mapping)
    add_source(conn, a.source_id, a.source_title or a.source_id, url=a.source_url, source_year=a.census_year)
    if a.towns:
        print("towns:", census.import_towns(conn, Path(a.towns), mapping, a.census_year, a.boundary_version,
                                            a.source_id))
    if a.features:
        print("load_id, features:", census.import_features(conn, Path(a.features), mapping, a.census_year,
                                                           a.boundary_version, a.source_id, kind=a.kind))
    conn.commit()


def cmd_import_number_areas(a):
    conn = _conn(a)
    print("rows:", areas.import_number_areas(conn, Path(a.csv)))
    conn.commit()


def cmd_import_facilities(a):
    conn = _conn(a)
    print("observations:", facilities.import_csv(conn, Path(a.csv), a.census_year, a.boundary_version))
    conn.commit()


def cmd_import_evidence(a):
    import csv
    conn = _conn(a)
    load = new_load(conn, "evidence", note=a.csv)
    n = 0
    with open(a.csv, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            add_source(conn, r["source_id"], r.get("source_title") or r["source_id"], url=r.get("source_url"))
            conn.execute("INSERT INTO regions.external_evidence (load_id, town_key, census_year, boundary_version,"
                         " evidence_type, supports_class, grade, source_id, note) VALUES (?,?,?,?,?,?,?,?,?)",
                         (load, r["town_key"], a.census_year, a.boundary_version, r["evidence_type"],
                          r["supports_class"], r["grade"], r["source_id"], r.get("note")))
            n += 1
    conn.commit()
    print("evidence:", n)


def cmd_classify(a):
    conn = _conn(a)
    run = engine.classify(conn, rules_mod.load(a.rules or a.config_dir / "rules_v1.json"), a.census_year,
                          a.boundary_version, prefecture=a.prefecture)
    conn.commit()
    _print({"run_id": run, "counts": [list(r) for r in conn.execute(
        "SELECT class_code, label_status, COUNT(*) FROM regions.regional_classification WHERE run_id = ?"
        " GROUP BY 1, 2", (run,))]})


def cmd_reproduce(a):
    r = engine.reproduce(_conn(a, read_only=True), a.run_id)
    _print(r)
    return 0 if r["reproduced"] else 1


def cmd_search(a):
    f = Filter(prefecture=a.prefecture, municipality=a.municipality, town=a.town, classes=a.cls or [],
               all_classes=a.all, axis=a.axis, primary_only=a.primary_only, mode=a.mode, include_r6=a.include_r6,
               area_code=a.area_code, local_code=a.local_code, number_area_code=a.number_area_code,
               carrier=a.carrier, as_of_date=a.as_of_date, usage_status=a.usage_status,
               relation_level=a.relation_level)
    if a.status:
        f.statuses = tuple(a.status)
    _print(search(_conn(a, read_only=True), f))


def cmd_code(a):
    _print(relations.for_code(_conn(a, read_only=True), a.area_code, a.local_code))


def cmd_priority(a):
    conn = _conn(a)
    layout = json.loads((a.config_dir / "layouts.json").read_text(encoding="utf-8"))
    run = priority.compute(conn, layout["in_use_values"])
    conn.commit()
    _print([dict(r) for r in conn.execute("SELECT * FROM priority_item WHERE priority_run_id = ? ORDER BY rank"
                                          " LIMIT ?", (run, a.top))])


def cmd_export(a):
    out = Path(a.out) if a.out else a.paths.exports / "manual"
    _print(export_all(_conn(a), out, prefecture=a.prefecture, xlsx=not a.no_xlsx))


def cmd_evaluate(a):
    conn = _conn(a)
    r = compare(conn, a.axis, a.census_year, a.boundary_version, k=a.k, group_by=a.group_by,
                holdout=tuple(a.holdout or ()))
    conn.execute("INSERT INTO evaluation_result (axis, result_json, evaluated_at) VALUES (?,?,datetime('now'))",
                 (a.axis, json.dumps(r, ensure_ascii=False)))
    conn.commit()
    _print(r)


def cmd_migrate_raw(a):
    from .acquire.sources import migrate_raw
    old = paths_mod.check_writable_location(Path(a.source), protected=())
    a.paths.ensure()
    conn = _conn(a)
    r = migrate_raw(conn, a.paths.raw, old)
    conn.commit()
    _print(r)
    print("次は: atlas update --offline", file=sys.stderr)


def cmd_qa(a):
    conn = _conn(a)
    layout = json.loads((a.config_dir / "layouts.json").read_text(encoding="utf-8"))
    res = checks.run_checks(conn, a.paths.raw, min_pair_overlap=layout.get("min_pair_overlap", 0.5),
                            bulk_min_rows=layout.get("bulk_min_rows", 30),
                            carriers=CarrierMap.load(a.config_dir / "carriers.json"))
    _print([c.__dict__ for c in res])
    return 1 if checks.blocking(res) else 0


KIND_JA = {"added": "追加", "removed": "削除", "carrier_changed": "事業者変更", "status_changed": "使用状況変更",
           "number_area_changed": "番号区画変更", "cosmetic_only": "表記ゆれのみ"}


CARRIER_TYPE_JA = {"notation": "書き方の違い", "rename": "社名変更・略称", "succession": "合併等の引継ぎ",
                   "vacated": "事業者なし（返却など）", "assigned": "新たに指定", "transfer": "別の事業者へ"}


def cmd_changes_report(a):
    """差分の内訳（個別の変更と一括変更）を CSV に出し、要点を画面に表示する。"""
    import csv
    from .history import diff
    layout = json.loads((a.config_dir / "layouts.json").read_text(encoding="utf-8"))
    s = diff.summary(_conn(a, read_only=True), bulk_min_rows=layout.get("bulk_min_rows", 30),
                     carriers=CarrierMap.load(a.config_dir / "carriers.json"))
    out = Path(a.out) if a.out else a.paths.exports / "changes_report"
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "bulk_transitions.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["series", "old_date", "new_date", "kind", "old_value", "new_value", "rows",
                                           "carrier_type"])
        w.writeheader()
        w.writerows(s["top_transitions"])
    with open(out / "per_pair.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["series", "old_date", "new_date", "change_rows", "bulk_rows", "in_use_related_rows",
                    "skipped_columns", "kinds"])
        for p in s["per_pair"]:
            w.writerow([p["series"], p["old_date"], p["new_date"], p["change_rows"], p["bulk_rows"],
                        p["in_use_related_rows"], p["skipped_columns"] or "", json.dumps(p["kinds"], ensure_ascii=False)])
    def line_types(d):
        return "、".join(f"{CARRIER_TYPE_JA.get(k, k)} {v:,}" for k, v in sorted(d.items(), key=lambda x: -x[1])) or "なし"

    def line(d):
        return "、".join(f"{KIND_JA.get(k, k)} {v:,}" for k, v in sorted(d.items(), key=lambda x: -x[1])) or "なし"
    print(f"比較した組: {s['pairs']}（片方の年に無い列を比べなかった組: {s['pairs_with_skipped_columns']}）")
    print(f"個別の変更: {line(s['individual_changes'])}")
    print(f"一括変更（同じ書き換えが {s['bulk_min_rows']} 行以上）: {line(s['bulk_changes'])}")
    print(f"新たに使用中になった局番: {s['distinct_codes_became_in_use']:,} 種類")
    print(f"事業者変更の内訳: {line_types(s['carrier_change_types'])}")
    with open(out / "carrier_transfers.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["old_value", "new_value", "rows"])
        w.writeheader()
        w.writerows(s["carrier_transfers"])
    print("\n一括変更の上位（系統 旧の日付→新の日付｜種別｜旧の値 → 新の値｜行数）")
    for t in s["top_transitions"][:a.top]:
        label = KIND_JA.get(t["kind"], t["kind"])
        if t.get("carrier_type"):
            label += f"（{CARRIER_TYPE_JA[t['carrier_type']]}）"
        print(f"  {t['series']} {t['old_date']}→{t['new_date']}｜{label}｜{t['old_value']} → {t['new_value']}｜{t['rows']:,}")
    print("\n別の事業者への変化（本当の移転の候補。旧の値 → 新の値｜行数）")
    for t in s["carrier_transfers"][:a.top]:
        print(f"  {t['old_value']} → {t['new_value']}｜{t['rows']:,}")
    if not s["carrier_transfers"]:
        print("  なし")
    skipped = [p for p in s["per_pair"] if p["skipped_columns"]]
    if skipped:
        print("\n比べなかった列がある組（系統 旧の日付→新の日付｜列）")
        for p in skipped[:a.top]:
            print(f"  {p['series']} {p['old_date']}→{p['new_date']}｜{p['skipped_columns']}")
    print(f"\nCSV: {out}")


def cmd_backup(a):
    r = backup.backup(a.paths.db, a.paths.backups)
    _print(r)
    return 0 if r["ok"] else 1


def cmd_serve(a):
    from .web.server import serve
    httpd = serve(a.paths.db, a.port)
    print(f"http://127.0.0.1:{a.port}/  (Ctrl+C で終了)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="atlas", description="全国固定電話番号調査システム（landline_atlas）")
    p.add_argument("--data-dir")
    p.add_argument("--config-dir", type=Path, default=CONFIG_DIR)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_text):
        s = sub.add_parser(name, help=help_text)
        s.set_defaults(fn=fn)
        return s

    def yv(s):
        s.add_argument("--census-year", type=int, required=True)
        s.add_argument("--boundary-version", required=True)

    add("init", cmd_init, "データ置き場とDBを作成")
    s = add("update", cmd_update, "取得から出力までを一括実行")
    s.add_argument("--offline", action="store_true", help="ネットワーク取得をしない（--import のみ）")
    s.add_argument("--wayback", action="store_true", help="Internet Archive の過去版も取得")
    s.add_argument("--import", dest="import_file", action="append", help="手元の原本ファイル（何度でも指定可）")
    s.add_argument("--no-export", action="store_true")

    s = add("migrate-raw", cmd_migrate_raw, "古いデータ置き場から原本と取得記録を写す（再ダウンロード不要）")
    s.add_argument("--from", dest="source", required=True, help="古いデータ置き場（例 ~/landline_atlas_data_old）")
    s = add("fetch-census", cmd_fetch_census, "e-Stat から国勢調査（小地域）を自動で取得して取り込む")
    s.add_argument("--pref", required=True, help="都道府県コード（例 27＝大阪府）")
    s.add_argument("--probe", action="store_true", help="確認だけ（DBに書き込まない）")
    s = add("fetch-gis", cmd_fetch_gis, "国土数値情報・農業／漁業集落境界・経済センサス等から町丁目の特徴量を作って取り込む")
    s.add_argument("--pref", required=True, help="都道府県コード（先に fetch-census で境界を取得しておく）")
    s.add_argument("--probe", action="store_true", help="取得と属性の確認だけ（DBに書き込まない）")
    s.add_argument("--datasets", help="一部だけ処理する（例 did,zoning）。既定は config/gis_sources.json の全て")
    s.add_argument("--refresh", action="store_true", help="保管済みの原本を使わずに取り直す")
    s = add("import-census", cmd_import_census, "e-Stat 小地域の町丁目マスタ・特徴量を取込")
    s.add_argument("--mapping", required=True)
    s.add_argument("--towns")
    s.add_argument("--features")
    s.add_argument("--kind", default="census", choices=["census", "economic", "housing", "gis", "manual"])
    s.add_argument("--source-id", required=True)
    s.add_argument("--source-title")
    s.add_argument("--source-url")
    yv(s)
    s = add("import-number-areas", cmd_import_number_areas, "番号区画→市区町村の対応CSVを取込")
    s.add_argument("--csv", required=True)
    s = add("import-facilities", cmd_import_facilities, "公開施設の番号観測CSVを取込")
    s.add_argument("--csv", required=True)
    yv(s)
    s = add("import-evidence", cmd_import_evidence, "外部証拠CSVを取込")
    s.add_argument("--csv", required=True)
    yv(s)

    s = add("classify", cmd_classify, "12分類を判定（新しい run）")
    s.add_argument("--rules", type=Path)
    s.add_argument("--prefecture")
    yv(s)
    s = add("reproduce", cmd_reproduce, "過去の run を再計算して照合")
    s.add_argument("--run-id", type=int, required=True)

    s = add("search", cmd_search, "検索（JSON）")
    for o in ("prefecture", "municipality", "town", "axis", "area-code", "local-code", "number-area-code",
              "carrier", "as-of-date", "usage-status"):
        s.add_argument(f"--{o}")
    s.add_argument("--class", dest="cls", action="append")
    s.add_argument("--all", action="store_true", help="指定分類すべてに該当")
    s.add_argument("--status", action="append")
    s.add_argument("--primary-only", action="store_true")
    s.add_argument("--mode", choices=["residential"])
    s.add_argument("--include-r6", action="store_true")
    s.add_argument("--relation-level", choices=["area", "observed_verified", "observed_unverified"])

    s = add("code", cmd_code, "局番の詳細（指定履歴・変更・区画・観測）")
    s.add_argument("area_code")
    s.add_argument("local_code")
    s = add("priority", cmd_priority, "調査優先順位を計算")
    s.add_argument("--top", type=int, default=50)
    s = add("export", cmd_export, "CSV/XLSX 出力")
    s.add_argument("--out")
    s.add_argument("--prefecture")
    s.add_argument("--no-xlsx", action="store_true")
    s = add("evaluate", cmd_evaluate, "B0/B1/B2 比較")
    s.add_argument("--axis", choices=["R", "L"], required=True)
    s.add_argument("--k", type=int, default=5)
    s.add_argument("--group-by", choices=["municipality", "town"], default="municipality")
    s.add_argument("--holdout", action="append")
    yv(s)
    add("qa", cmd_qa, "品質検査")
    s = add("changes-report", cmd_changes_report, "差分の内訳（個別の変更と一括変更）")
    s.add_argument("--top", type=int, default=20)
    s.add_argument("--out")
    add("backup", cmd_backup, "バックアップと復旧試験")
    s = add("serve", cmd_serve, "検索画面（127.0.0.1）")
    s.add_argument("--port", type=int, default=8765)
    return p


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    a.paths = paths_mod.resolve(a.data_dir)
    a.conns = []
    try:
        return a.fn(a) or 0
    finally:
        for conn in a.conns:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
