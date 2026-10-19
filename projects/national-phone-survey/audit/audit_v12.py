#!/usr/bin/env python3
"""v1.2 時系列差分の監査（読み取り専用）。

v1.2 のスキーマはここで仮定しない。設定ファイル（audit_config.example.json を
複製して編集）の各節に、正規の列名へ別名を付けた SELECT 文を書く。
節が無い検査は SKIPPED と報告する。

  python3 audit/audit_v12.py --db ~/official_phone_data/national_tool/data/index.sqlite3 \
      --config audit/audit_config.json --out ~/official_phone_data_audit/audit_YYYYMMDD

出力: audit_report.md / audit_report.json / 各検査の CSV
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from regional_classifier.normalize import canonical_url, normalize_text  # noqa: E402

SECTION_COLUMNS = {
    "versions": ("version_id", "series", "as_of_date", "source_url", "sha256", "file_path", "selected"),
    "pairs": ("old_version", "new_version"),
    "rows": ("version_id", "area_code", "local_code", "number_area", "carrier", "status"),
    "changes": ("old_version", "new_version", "area_code", "local_code"),
    "fetch_log": ("url", "status"),
}
OPTIONAL_COLUMNS = {"versions": {"source_url", "sha256", "file_path", "selected"},
                    "fetch_log": set(), "changes": set(), "pairs": set(), "rows": set()}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(conn: sqlite3.Connection, cfg: dict, section: str) -> list[dict] | None:
    spec = cfg.get(section)
    if not spec:
        return None
    sql = spec["sql"]
    if not sql.lstrip().lower().startswith(("select", "with")):
        raise ValueError(f"{section}.sql must be a SELECT")
    cur = conn.execute(sql)
    names = [d[0] for d in cur.description]
    required = [c for c in SECTION_COLUMNS[section] if c not in OPTIONAL_COLUMNS[section]]
    missing = [c for c in required if c not in names]
    if missing:
        raise ValueError(f"{section}.sql lacks columns {missing}")
    return [dict(zip(names, r)) for r in cur.fetchall()]


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


# ------------------------------------------------------------------ checks

def check_pairs(versions: list[dict], pairs: list[dict] | None) -> tuple[dict, list[dict], list[tuple]]:
    """A01: 比較組の正当性。戻り値の最後は差分再計算に使う組のリスト。"""
    by_id = {str(v["version_id"]): v for v in versions}
    series_dates: dict[str, list[str]] = defaultdict(list)
    for v in versions:
        series_dates[str(v["series"])].append(str(v["as_of_date"]))
    expected = set()
    for s, dates in series_dates.items():
        uniq = sorted(set(dates))
        expected |= {(s, a, b) for a, b in zip(uniq, uniq[1:])}

    rows = []
    if pairs is None:
        # 組の表が無ければ、系統内の隣接基準日から期待される組を使う（各基準日の代表版は最小 version_id）
        rep = {}
        for v in sorted(versions, key=lambda x: str(x["version_id"])):
            rep.setdefault((str(v["series"]), str(v["as_of_date"])), str(v["version_id"]))
        used = [(rep[(s, a)], rep[(s, b)]) for s, a, b in sorted(expected)]
        return ({"status": "DERIVED", "expected_pairs": len(expected), "actual_pairs": None},
                [{"old_version": o, "new_version": n, "verdict": "derived"} for o, n in used], used)

    seen = set()
    for p in pairs:
        o, n = str(p["old_version"]), str(p["new_version"])
        vo, vn = by_id.get(o), by_id.get(n)
        if vo is None or vn is None:
            verdict = "unknown_version"
        elif str(vo["series"]) != str(vn["series"]):
            verdict = "cross_series"
        elif str(vo["as_of_date"]) == str(vn["as_of_date"]):
            verdict = "same_date"
        elif str(vo["as_of_date"]) > str(vn["as_of_date"]):
            verdict = "reversed"
        elif (str(vo["series"]), str(vo["as_of_date"]), str(vn["as_of_date"])) in expected:
            verdict = "ok"
        else:
            verdict = "non_adjacent"
        key = (str(vo["series"]), str(vo["as_of_date"]), str(vn["as_of_date"])) if vo and vn else (o, n)
        if key in seen:
            verdict += ";duplicate_pair"
        seen.add(key)
        rows.append({"old_version": o, "new_version": n,
                     "series": vo and vo["series"], "old_date": vo and vo["as_of_date"],
                     "new_date": vn and vn["as_of_date"], "verdict": verdict})
    missing = sorted(expected - seen)
    for s, a, b in missing:
        rows.append({"series": s, "old_date": a, "new_date": b, "verdict": "expected_but_missing"})
    verdicts = Counter(r["verdict"] for r in rows)
    used = [(r["old_version"], r["new_version"]) for r in rows if r.get("old_version") and r["verdict"] == "ok"]
    summary = {"status": "CHECKED", "actual_pairs": len(pairs), "expected_pairs": len(expected),
               "verdicts": dict(verdicts)}
    return summary, rows, used


def recompute_changes(rows: list[dict], used_pairs: list[tuple], in_use_values: set[str]) -> dict:
    """A02〜A05: 行データから差分を再計算し、種別ごと・延べ／一意を分けて集計する。"""
    by_version: dict[str, dict[tuple, dict]] = defaultdict(dict)
    duplicates = []
    for r in rows:
        v = str(r["version_id"])
        key = (normalize_text(r["area_code"]), normalize_text(r["local_code"]))
        if key in by_version[v]:
            duplicates.append({"version_id": v, "area_code": key[0], "local_code": key[1]})
        by_version[v][key] = r

    def in_use(rec):
        return rec is not None and normalize_text(rec["status"]) in in_use_values

    detail, per_pair = [], []
    changed_pairs_by_key: dict[tuple, int] = Counter()
    newly_in_use: set[tuple] = set()
    for old, new in used_pairs:
        a, b = by_version.get(old, {}), by_version.get(new, {})
        kinds_count = Counter()
        changed_rows = in_use_rows = 0
        for key in sorted(set(a) | set(b)):
            ra, rb = a.get(key), b.get(key)
            kinds = []
            if ra is None:
                kinds = ["added"]
            elif rb is None:
                kinds = ["removed"]
            else:
                raw_diff = False
                for col, kind in (("carrier", "carrier_changed"), ("status", "status_changed"),
                                  ("number_area", "number_area_changed")):
                    if normalize_text(ra[col]) != normalize_text(rb[col]):
                        kinds.append(kind)
                    elif ra[col] != rb[col]:
                        raw_diff = True
                if not kinds and raw_diff:
                    kinds = ["cosmetic_only"]
            if not kinds:
                continue
            related = in_use(ra) or in_use(rb)
            changed_rows += 1
            in_use_rows += related
            changed_pairs_by_key[key] += 1
            if in_use(rb) and not in_use(ra):
                newly_in_use.add(key)
            for k in kinds:
                kinds_count[k] += 1
            detail.append({"old_version": old, "new_version": new, "area_code": key[0], "local_code": key[1],
                           "kinds": ";".join(kinds), "in_use_related": int(related)})
        total_rows = max(len(a), len(b), 1)
        per_pair.append({"old_version": old, "new_version": new, "changed_rows": changed_rows,
                         "in_use_related_rows": in_use_rows, **{f"kind_{k}": v for k, v in kinds_count.items()},
                         "changed_share": round(changed_rows / total_rows, 4),
                         "suspected_format_change": int(changed_rows / total_rows > 0.2
                                                        or kinds_count["cosmetic_only"] > changed_rows / 2)})
    multi = [{"area_code": k[0], "local_code": k[1], "changed_in_pairs": n}
             for k, n in sorted(changed_pairs_by_key.items()) if n > 1]
    return {
        "detail": detail, "per_pair": per_pair, "multi_period": multi, "duplicates": duplicates,
        "totals": {
            "change_rows_gross": sum(p["changed_rows"] for p in per_pair),
            "in_use_related_change_rows_gross": sum(p["in_use_related_rows"] for p in per_pair),
            "distinct_codes_changed": len(changed_pairs_by_key),
            "distinct_codes_newly_in_use": len(newly_in_use),
            "codes_changed_in_multiple_pairs": len(multi),
            "cosmetic_only_rows": sum(p.get("kind_cosmetic_only", 0) for p in per_pair),
            "duplicate_keys_within_version": len(duplicates),
        },
    }


def compare_recorded_changes(changes: list[dict], detail: list[dict]) -> dict:
    """v1.2 が記録した変更行と、再計算結果を組ごとに照合する。"""
    recorded = Counter((str(c["old_version"]), str(c["new_version"]),
                        normalize_text(c["area_code"]), normalize_text(c["local_code"])) for c in changes)
    dup = sum(n - 1 for n in recorded.values() if n > 1)
    recomputed = {(d["old_version"], d["new_version"], d["area_code"], d["local_code"]) for d in detail}
    return {"recorded_rows": sum(recorded.values()), "recorded_duplicate_rows": dup,
            "only_in_v12": len(set(recorded) - recomputed), "only_in_recompute": len(recomputed - set(recorded))}


def check_urls(versions: list[dict], fetch_log: list[dict] | None) -> tuple[dict, list[dict]]:
    """A06: Wayback 包装・scheme 違いによる URL 重複と、同一内容（SHA-256）の重複版。"""
    groups: dict[str, set[str]] = defaultdict(set)
    for v in versions:
        if v.get("source_url"):
            groups[canonical_url(str(v["source_url"]))].add(str(v["version_id"]))
    out = [{"canonical_url": u, "version_ids": ";".join(sorted(ids)), "kind": "same_url_multiple_versions"}
           for u, ids in sorted(groups.items()) if len(ids) > 1]
    by_hash: dict[str, set[str]] = defaultdict(set)
    for v in versions:
        if v.get("sha256"):
            by_hash[str(v["sha256"]).lower()].add(str(v["version_id"]))
    out += [{"sha256": h, "version_ids": ";".join(sorted(ids)), "kind": "same_content_multiple_versions"}
            for h, ids in sorted(by_hash.items()) if len(ids) > 1]
    fetch_dups = 0
    if fetch_log:
        fl: dict[str, set[str]] = defaultdict(set)
        for f in fetch_log:
            fl[canonical_url(str(f["url"]))].add(str(f["url"]))
        fetch_dups = sum(1 for raw in fl.values() if len(raw) > 1)
    return ({"url_groups_with_multiple_versions": sum(1 for r in out if r["kind"].startswith("same_url")),
             "content_hash_shared_by_versions": sum(1 for r in out if r["kind"].startswith("same_content")),
             "fetch_urls_with_variant_spellings": fetch_dups}, out)


def check_fetch(fetch_log: list[dict] | None) -> dict:
    """A07: 取得失敗とリトライ。"""
    if fetch_log is None:
        return {"status": "SKIPPED"}
    by_url: dict[str, list[dict]] = defaultdict(list)
    for f in fetch_log:
        by_url[canonical_url(str(f["url"]))].append(f)

    def ok(f):
        return str(f["status"]).strip() in ("200", "ok", "OK", "success")

    never = sorted(u for u, fs in by_url.items() if not any(ok(f) for f in fs))
    retried = {u: len(fs) for u, fs in by_url.items() if len(fs) > 1}
    return {"status": "CHECKED", "attempts": len(fetch_log), "urls": len(by_url),
            "failed_attempts": sum(1 for f in fetch_log if not ok(f)),
            "urls_never_succeeded": len(never), "urls_never_succeeded_list": never[:200],
            "urls_retried": len(retried), "max_attempts_per_url": max(retried.values(), default=1)}


def check_sha(versions: list[dict], raw_dir: Path | None) -> tuple[dict, list[dict]]:
    """A08: 原本ファイルの SHA-256 再計算。"""
    rows = []
    for v in versions:
        fp, recorded = v.get("file_path"), v.get("sha256")
        if not fp:
            rows.append({"version_id": v["version_id"], "result": "no_file_path"})
            continue
        path = Path(str(fp)).expanduser()
        if not path.is_absolute() and raw_dir:
            path = raw_dir / path
        if not path.is_file():
            rows.append({"version_id": v["version_id"], "file": str(path), "result": "missing_file"})
            continue
        actual = sha256_file(path)
        result = "no_recorded_hash" if not recorded else ("ok" if actual == str(recorded).lower() else "mismatch")
        rows.append({"version_id": v["version_id"], "file": str(path), "result": result, "sha256": actual})
    return dict(Counter(r["result"] for r in rows)), rows


def check_same_date(versions: list[dict]) -> tuple[dict, list[dict]]:
    """A09: 同一系統・同一基準日で内容が異なる版と、その選択状況。"""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for v in versions:
        groups[(str(v["series"]), str(v["as_of_date"]))].append(v)
    out = []
    for (s, d), vs in sorted(groups.items()):
        if len(vs) < 2:
            continue
        hashes = {str(v.get("sha256") or "").lower() for v in vs}
        selected = [str(v["version_id"]) for v in vs if str(v.get("selected") or "0") not in ("0", "", "None")]
        out.append({"series": s, "as_of_date": d, "versions": ";".join(str(v["version_id"]) for v in vs),
                    "distinct_hashes": len(hashes - {""}), "selected": ";".join(selected),
                    "verdict": ("content_differs" if len(hashes - {""}) > 1 else "identical_or_unhashed")
                               + ("" if len(selected) == 1 else ";selection_ambiguous")})
    return {"groups": len(out), "content_differs": sum(1 for r in out if r["verdict"].startswith("content"))}, out


def run(db: Path, cfg: dict, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=False)
    before = sha256_file(db)
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    report: dict = {"db": str(db), "audited_at": datetime.now(timezone.utc).isoformat(),
                    "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
                    "reported": cfg.get("reported", {}), "checks": {}}
    raw_dir = Path(cfg["raw_dir"]).expanduser() if cfg.get("raw_dir") else None
    in_use_values = {normalize_text(v) for v in cfg.get("in_use_values", [])}

    versions = fetch(conn, cfg, "versions")
    pairs = fetch(conn, cfg, "pairs")
    rows = fetch(conn, cfg, "rows")
    changes = fetch(conn, cfg, "changes")
    fetch_log = fetch(conn, cfg, "fetch_log")
    conn.close()
    c = report["checks"]

    if versions is None:
        for k in ("A01_pairs", "A06_urls", "A08_sha256", "A09_same_date"):
            c[k] = {"status": "SKIPPED"}
        used = []
    else:
        c["A01_pairs"], pair_rows, used = check_pairs(versions, pairs)
        write_csv(out / "A01_pairs.csv", pair_rows)
        c["A06_urls"], url_rows = check_urls(versions, fetch_log)
        write_csv(out / "A06_url_duplicates.csv", url_rows)
        c["A08_sha256"], sha_rows = check_sha(versions, raw_dir)
        write_csv(out / "A08_sha256.csv", sha_rows)
        c["A09_same_date"], sd_rows = check_same_date(versions)
        write_csv(out / "A09_same_date_versions.csv", sd_rows)
        selected = [v for v in versions if str(v.get("selected") or "0") not in ("0", "", "None")]
        c["A00_inventory"] = {"versions": len(versions), "series": len({str(v["series"]) for v in versions}),
                              "selected_versions": len(selected)}

    if rows is None or not used:
        c["A02_A05_changes"] = {"status": "SKIPPED", "reason": "rows section or valid pairs missing"}
    else:
        if not in_use_values:
            raise ValueError("in_use_values must list the status strings meaning 使用中")
        rc = recompute_changes(rows, used, in_use_values)
        write_csv(out / "A02_changes_detail.csv", rc["detail"])
        write_csv(out / "A03_changes_per_pair.csv", rc["per_pair"])
        write_csv(out / "A04_multi_period_codes.csv", rc["multi_period"])
        write_csv(out / "A04_duplicate_keys.csv", rc["duplicates"])
        c["A02_A05_changes"] = {"status": "CHECKED", **rc["totals"],
                                "pairs_suspected_format_change":
                                    sum(p["suspected_format_change"] for p in rc["per_pair"])}
        if changes is not None:
            c["A02_recorded_vs_recomputed"] = compare_recorded_changes(changes, rc["detail"])
        if versions is not None:
            sel = {str(v["version_id"]) for v in versions if str(v.get("selected") or "0") not in ("0", "", "None")}
            if sel:
                c["A00_inventory"]["selected_rows_total"] = sum(1 for r in rows if str(r["version_id"]) in sel)

    c["A07_fetch"] = check_fetch(fetch_log)
    after = sha256_file(db)
    c["A10_preservation"] = {"db_sha256_before": before, "db_sha256_after": after, "db_unchanged": before == after,
                             "integrity_check": report["integrity_check"]}
    report["reported_vs_recomputed"] = _reconcile(report)
    (out / "audit_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    (out / "audit_report.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def _reconcile(report: dict) -> list[dict]:
    rep = report.get("reported", {})
    c = report["checks"]
    ch = c.get("A02_A05_changes", {})
    inv = c.get("A00_inventory", {})
    pairs = c.get("A01_pairs", {})
    mapping = [
        ("comparable_pairs", (pairs.get("verdicts") or {}).get("ok", 0) if pairs.get("status") == "CHECKED"
         else pairs.get("expected_pairs")),
        ("series", inv.get("series")),
        ("selected_rows_total", inv.get("selected_rows_total")),
        ("all_change_rows", ch.get("change_rows_gross")),
        ("in_use_change_rows", ch.get("in_use_related_change_rows_gross")),
    ]
    out = []
    for key, recomputed in mapping:
        if key in rep:
            out.append({"metric": key, "reported": rep[key], "recomputed": recomputed,
                        "match": recomputed is not None and int(rep[key]) == int(recomputed)})
    return out


def render_markdown(report: dict) -> str:
    lines = ["# v1.2 監査レポート", "", f"- 対象DB: `{report['db']}`", f"- 実行日時: {report['audited_at']}",
             f"- integrity_check: {report['integrity_check']}", "",
             "## 報告値と再計算値", "", "| 指標 | 報告値 | 再計算値 | 一致 |", "|---|---:|---:|---|"]
    for r in report["reported_vs_recomputed"]:
        lines.append(f"| {r['metric']} | {r['reported']} | {r['recomputed']} | {'○' if r['match'] else '×'} |")
    lines += ["", "注: 変更行数（延べ）と、新規に使用中になった局番の種類数（一意）は別の指標です。", ""]
    for name, body in report["checks"].items():
        lines += [f"## {name}", "", "```json", json.dumps(body, ensure_ascii=False, indent=1)[:4000], "```", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path, help="新規ディレクトリ（既存なら停止）")
    a = p.parse_args(argv)
    cfg = json.loads(a.config.read_text(encoding="utf-8"))
    report = run(a.db.expanduser(), cfg, a.out.expanduser())
    print(render_markdown(report))
    return 0 if report["checks"]["A10_preservation"]["db_unchanged"] else 1


if __name__ == "__main__":
    sys.exit(main())
