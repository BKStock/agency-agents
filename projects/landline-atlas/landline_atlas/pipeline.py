"""atlas update：取得 → 原本保管 → 解析 → 版選択 → 差分 → 品質検査 → 分類 → 優先順位 → 出力 → バックアップ。

品質検査に error があれば、分類以降へ進まない。実行ごとに runs/<時刻>/run_manifest.json を残す。
"""

from __future__ import annotations

import json
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .acquire import sources as acq
from .acquire.http import Fetcher, Transport, UrllibTransport
from .classify import engine, rules as rules_mod
from .db.connection import connect, utc_now
from .discover import priority
from .export.writer import export_all
from .history import diff, versions
from .history.carriers import CarrierMap
from .ingest import designation
from .paths import DataPaths
from .qa import backup, checks

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def update(paths: DataPaths, *, config_dir: Path = CONFIG_DIR, online: bool = True, wayback: bool = False,
           local_files: list[Path] = (), transport: Transport | None = None,
           do_export: bool = True, do_backup: bool = True, sleep=None) -> dict:
    paths.ensure()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = paths.runs / stamp
    run_dir.mkdir(parents=True)
    sources = _json(config_dir / "sources.json")
    layout = _json(config_dir / "layouts.json")
    rules = rules_mod.load(config_dir / "rules_v1.json")
    carriers = CarrierMap.load(config_dir / "carriers.json")
    conn = connect(paths.db)
    manifest: dict = {"started_at": utc_now(), "data_dir": str(paths.root), "stages": {}}
    pid = conn.execute("INSERT INTO pipeline_run (started_at) VALUES (?)", (manifest["started_at"],)).lastrowid
    conn.commit()
    outcome = "ok"
    try:
        # 1. 取得
        collected = []
        if online or transport is not None:
            kw = {"sleep": sleep} if sleep else {}
            off = Fetcher(transport or UrllibTransport(), user_agent=sources["user_agent"],
                          min_interval=sources.get("min_interval_seconds", 3.0),
                          max_attempts=sources.get("max_attempts", 4), log=acq.make_logger(conn, "official"), **kw)
            collected += acq.collect_official(off, conn, paths.raw, sources)
            conn.commit()
            if wayback:
                wb = Fetcher(transport or UrllibTransport(), user_agent=sources["user_agent"],
                             min_interval=sources.get("min_interval_seconds", 3.0),
                             max_attempts=sources.get("max_attempts", 4), log=acq.make_logger(conn, "wayback"), **kw)
                collected += acq.collect_wayback(wb, conn, paths.raw, sources)
                conn.commit()
        for path in local_files:
            collected.append(acq.import_local(conn, paths.raw, Path(path)))
        conn.commit()
        manifest["stages"]["acquire"] = {"collected": len(collected), "new_raw_files": sum(c.is_new for c in collected)}

        # 2. 解析（未解析の原本×系統）
        todo = conn.execute("""SELECT DISTINCT s.sha256 FROM numbers.raw_source s
                               WHERE NOT EXISTS (SELECT 1 FROM numbers.version v
                                                 WHERE v.sha256 = s.sha256)""").fetchall()
        parsed, errors = 0, []
        for r in todo:
            try:
                parsed += len(designation.ingest(conn, paths.raw, r["sha256"], layout))
                conn.commit()
            except designation.UnknownLayout as exc:
                conn.rollback()
                errors.append({"sha256": r["sha256"], "error": str(exc)})
        manifest["stages"]["ingest"] = {"new_versions": parsed, "unknown_layouts": errors}

        # 3. 版選択と差分
        decisions = versions.select_versions(conn)
        new_pairs = diff.compute_diffs(conn, layout["in_use_values"])
        conn.commit()
        s = diff.summary(conn, bulk_min_rows=layout.get("bulk_min_rows", 30), carriers=carriers)
        manifest["stages"]["history"] = {
            "series": len({d["series"] for d in decisions}), "selected_versions": len(decisions),
            "content_differs": sum(d["reason"].startswith("content_differs") for d in decisions),
            "new_pairs": new_pairs, **{k: s[k] for k in ("pairs", "change_rows_gross", "in_use_related_rows_gross",
                                                          "distinct_codes_changed", "distinct_codes_became_in_use",
                                                          "codes_changed_in_multiple_pairs", "kinds",
                                                          "individual_changes", "bulk_changes",
                                                          "pairs_with_skipped_columns", "carrier_change_types")}}

        # 4. 品質検査
        results = checks.run_checks(conn, paths.raw, min_pair_overlap=layout.get("min_pair_overlap", 0.5),
                                    bulk_min_rows=layout.get("bulk_min_rows", 30), carriers=carriers)
        if errors:
            results.append(checks.Check("Q00_unknown_layout", "error", False, {"files": errors}))
        checks.record(conn, pid, results)
        conn.commit()
        manifest["stages"]["qa"] = {c.check_id: {"severity": c.severity, "passed": c.passed} for c in results}
        if checks.blocking(results):
            outcome = "blocked_by_qa"
            return manifest

        # 5. 分類（町丁目マスタがある調査年×境界版ごと）
        runs = []
        for y, v in conn.execute("SELECT DISTINCT census_year, boundary_version FROM regions.town").fetchall():
            runs.append(engine.classify(conn, rules, y, v))
        conn.commit()
        manifest["stages"]["classify"] = {"runs": runs, "rule_version": rules.version}

        # 6. 調査優先順位
        manifest["stages"]["priority"] = {"priority_run_id": priority.compute(conn, layout["in_use_values"])}
        conn.commit()

        # 7. 出力
        if do_export:
            res = export_all(conn, paths.exports / stamp, bulk_min_rows=layout.get("bulk_min_rows", 30),
                             carriers=carriers)
            manifest["stages"]["export"] = {"dir": str(paths.exports / stamp), **res["reconciliation"]}
    except Exception as exc:  # 失敗も記録してから再送出する
        outcome = "failed"
        manifest["error"] = "".join(traceback.format_exception_only(exc)).strip()
        conn.rollback()
        raise
    finally:
        manifest["outcome"] = outcome
        manifest["finished_at"] = utc_now()
        conn.execute("UPDATE pipeline_run SET finished_at = ?, outcome = ?, manifest = ? WHERE pipeline_id = ?",
                     (manifest["finished_at"], outcome, json.dumps(manifest, ensure_ascii=False), pid))
        conn.commit()
        conn.close()
        if do_backup and outcome != "failed":
            manifest["backup"] = backup.backup(paths.db, paths.backups)
        (run_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                                    encoding="utf-8")
    return manifest
