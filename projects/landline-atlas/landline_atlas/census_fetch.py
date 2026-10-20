"""atlas fetch-census：e-Stat から国勢調査（小地域）を取得し、町丁目マスタと特徴量に取り込む。

--probe では取得と確認だけを行い、DB にもディスクにも書かない。
必須の表（人口総数）が取れない、または列が見出しで特定できないときは取り込まずに停止する。
"""

from __future__ import annotations

import json
from pathlib import Path

from .acquire import estat
from .acquire.http import Fetcher, UrllibTransport
from .acquire.sources import make_logger
from .ingest import estat_tables
from .normalize import prefecture_code


def load_config(config_dir: Path) -> dict:
    return json.loads((config_dir / "estat.json").read_text(encoding="utf-8"))


def run(conn, raw_dir: Path, cfg: dict, pref: str, *, probe: bool, transport=None, sleep=None,
        user_agent: str = "landline-atlas/1.0") -> dict:
    pref = prefecture_code(pref)
    year, version = cfg["census_year"], cfg["boundary_version"]
    kw = {"sleep": sleep} if sleep else {}
    fetcher = Fetcher(transport or UrllibTransport(), user_agent=user_agent, min_interval=3.0,
                      log=None if probe else make_logger(conn, "official"), **kw)
    report: dict = {"prefecture": pref, "census_year": year, "boundary_version": version, "probe": probe,
                    "tables": {}, "boundary": None, "imported": False, "stopped": None}

    fetched_tables = []
    for spec in cfg["tables"]:
        f = estat.fetch_table(fetcher, cfg, spec, pref)
        entry = {"name": spec["name"], "ok": f.ok, "url": f.url, "tried": f.tried}
        if f.ok:
            table = estat_tables.parse_table_zip(f.body)
            entry.update(estat_tables.describe_table(table, spec))
            fetched_tables.append((spec, f, table))
        report["tables"][spec["stats_id"]] = entry
    b = estat.fetch_boundary(fetcher, cfg, pref)
    report["boundary"] = {"ok": b.ok, "url": b.url, "tried": b.tried}
    records = None
    if b.ok:
        records = estat_tables.parse_boundary_zip(b.body)
        report["boundary"].update(estat_tables.describe_boundary(records, cfg["boundary"]["fields"]))

    # 停止の判定：必須の表が無い、または取れた表で見出しが特定できない列がある
    for spec in cfg["tables"]:
        entry = report["tables"][spec["stats_id"]]
        if spec.get("required") and not entry["ok"]:
            report["stopped"] = f"required table {spec['stats_id']} could not be downloaded"
        elif entry["ok"]:
            bad = {k: v["status"] for k, v in entry["matches"].items() if v["status"] != "ok"}
            if bad and spec.get("required"):
                report["stopped"] = f"{spec['stats_id']}: columns not identified {bad}"
    if probe or report["stopped"]:
        return report

    for spec, f, table in sorted(fetched_tables, key=lambda x: not x[0].get("creates_towns")):
        source_id = f"estat:{spec['stats_id']}:{pref}:{f.sha256[:12]}"
        estat_tables.register(conn, source_id, f"令和2年国勢調査 {spec['name']}（{pref}）", f.url, year,
                              cfg["license"], f.sha256)
        estat.store(conn, raw_dir, f, pref, table.member)
        report["tables"][spec["stats_id"]]["import"] = estat_tables.import_table(conn, table, spec, year, version,
                                                                                source_id)
    if records is not None:
        source_id = f"estat:boundary:{pref}:{b.sha256[:12]}"
        estat_tables.register(conn, source_id, f"令和2年国勢調査 小地域境界（{pref}）", b.url, year,
                              cfg["license"], b.sha256)
        estat.store(conn, raw_dir, b, pref)
        report["boundary"]["import"] = estat_tables.import_boundary(conn, records, cfg["boundary"]["fields"], year,
                                                                    version, source_id)
    report["imported"] = True
    report["towns"] = conn.execute("SELECT secrecy, COUNT(*) FROM regions.town WHERE prefecture_code = ?"
                                   " AND census_year = ? AND boundary_version = ? GROUP BY secrecy",
                                   (pref, year, version)).fetchall()
    report["towns"] = {r[0]: r[1] for r in report["towns"]}
    return report


STATUS_JA = {"ok": "○ 1列に特定", "none": "× 見つからない", "multiple": "△ 複数の列に当たる"}


def render(report: dict) -> str:
    lines = [f"国勢調査 {report['census_year']}年・都道府県 {report['prefecture']}"
             f"（{'確認モード：DBには書き込みません' if report['probe'] else '取込'}）", ""]
    for sid, t in report["tables"].items():
        lines.append(f"■ {sid} {t['name']}：{'取得できた' if t['ok'] else '取得できなかった'}")
        if t["ok"]:
            lines.append(f"  URL: {t['url']}")
            lines.append(f"  ファイル: {t['member']}｜全 {t['rows']:,} 行／町丁・字等 {t['town_rows']:,} 行"
                         f"（除いた行: 上位の階層 {t['skipped']['upper_level']:,}・町丁目でない {t['skipped']['not_town_key']:,}）")
            if t["htksyori_values"]:
                lines.append("  秘匿処理(HTKSYORI)の値: " + "、".join(f"{k or '空欄'}={v:,}" for k, v in sorted(t["htksyori_values"].items())))
            for feat, m in t["matches"].items():
                lines.append(f"  {feat}: {STATUS_JA[m['status']]}" + (f"「{m['label']}」" if m["label"] else "")
                             + (f" 候補: {', '.join(m['candidates'])}" if m["status"] == "multiple" else ""))
            lines.append("  列（先頭60）: " + " / ".join(t["columns"]))
            if "import" in t:
                im = t["import"]
                lines.append(f"  → 取込: 町丁目 {im['towns_created']:,} 件を作成、値 {im['features_written']:,} 件"
                             + (f"、取り込まなかった項目 {im['unmatched_features']}" if im["unmatched_features"] else ""))
        else:
            for tr in t["tried"]:
                lines.append(f"  試した: {tr['url']} → {tr['result']}")
        lines.append("")
    bnd = report["boundary"]
    lines.append(f"■ 境界データ：{'取得できた' if bnd['ok'] else '取得できなかった'}")
    if bnd["ok"]:
        lines.append(f"  URL: {bnd['url']}")
        lines.append(f"  ポリゴン {bnd['records']:,} 件｜属性: {', '.join(bnd['fields'])}")
        lines.append(f"  使う属性: {bnd['resolved']}")
        if "import" in bnd:
            im = bnd["import"]
            lines.append(f"  → 取込: 面積 {im['area_written']:,} 件、町丁目に当たらないポリゴン {im['records_without_town']:,}、"
                         f"人口が統計表と合わない町丁目 {im['population_mismatch_towns']:,}")
    else:
        for tr in bnd["tried"]:
            lines.append(f"  試した: {tr['url']} → {tr['result']}")
    lines.append("")
    if report["stopped"]:
        lines.append(f"停止しました（取り込んでいません）: {report['stopped']}")
    elif report["probe"]:
        lines.append("確認だけ行いました。この内容をチャットに貼ってください。")
    else:
        lines.append(f"取り込みました。町丁目（秘匿の状態別）: {report.get('towns')}")
        lines.append(f"次は: atlas classify --census-year {report['census_year']} --boundary-version {report['boundary_version']}")
    return "\n".join(lines)
