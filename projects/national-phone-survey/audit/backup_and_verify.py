#!/usr/bin/env python3
"""v1.2 の SQLite と原本ファイルのバックアップ・保全検査・復旧試験（読み取り専用）。

  python3 audit/backup_and_verify.py \
      --db ~/official_phone_data/national_tool/data/index.sqlite3 \
      --raw-dir ~/official_phone_data \
      --out ~/official_phone_data_audit/backup_YYYYMMDD

* 元DBは file:...?mode=ro で開く。書き込みは --out 配下だけ。
* sqlite3 のオンラインバックアップ API で複製し、表ごとの行数と内容ハッシュを照合する。
* 原本ディレクトリの全ファイルの SHA-256 マニフェストを作る。--compare-manifest で前回と比較する。
* 実行前後で元DBファイルの SHA-256 が変わっていないことを確認する。
* v1.2 の処理を止めた状態で実行すること（書き込み中の DB は整合しない）。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def table_fingerprints(conn: sqlite3.Connection) -> dict[str, dict]:
    out = {}
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for t in tables:
        q = '"' + t.replace('"', '""') + '"'
        h = hashlib.sha256()
        n = 0
        # 行順に依存しないよう、各行のハッシュを並べ替えてから集約する
        row_hashes = sorted(hashlib.sha256(repr(row).encode()).hexdigest() for row in conn.execute(f"SELECT * FROM {q}"))
        for rh in row_hashes:
            h.update(rh.encode())
            n += 1
        out[t] = {"rows": n, "content_sha256": h.hexdigest()}
    return out


def integrity(conn: sqlite3.Connection) -> str:
    return conn.execute("PRAGMA integrity_check").fetchone()[0]


def build_manifest(raw_dir: Path, exclude: list[Path]) -> list[dict]:
    rows = []
    excluded = [e.resolve() for e in exclude]
    for p in sorted(raw_dir.rglob("*")):
        if not p.is_file() or any(p.resolve().is_relative_to(e) for e in excluded):
            continue
        st = p.stat()
        rows.append({"path": str(p.relative_to(raw_dir)), "size": st.st_size,
                     "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                     "sha256": sha256_file(p)})
    return rows


def compare_manifest(old_csv: Path, new_rows: list[dict]) -> dict:
    with open(old_csv, newline="", encoding="utf-8") as fh:
        old = {r["path"]: r for r in csv.DictReader(fh)}
    new = {r["path"]: r for r in new_rows}
    return {
        "missing": sorted(set(old) - set(new)),
        "added": sorted(set(new) - set(old)),
        "changed": sorted(p for p in set(old) & set(new) if old[p]["sha256"] != new[p]["sha256"]),
    }


def run(db: Path, raw_dir: Path | None, out: Path, compare: Path | None) -> dict:
    out.mkdir(parents=True, exist_ok=False)
    report: dict = {"db": str(db), "started_at": datetime.now(timezone.utc).isoformat()}
    before = sha256_file(db)
    report["db_sha256_before"] = before

    src = open_ro(db)
    report["source_integrity"] = integrity(src)
    src_fp = table_fingerprints(src)
    backup_path = out / f"{db.stem}.backup{db.suffix}"
    dst = sqlite3.connect(backup_path)
    src.backup(dst)
    dst.close()
    src.close()
    (out / f"{backup_path.name}.sha256").write_text(f"{sha256_file(backup_path)}  {backup_path.name}\n")

    # 復旧試験：バックアップを一時領域へ複製して開き、内容を照合する
    with tempfile.TemporaryDirectory() as tmp:
        restored = Path(tmp) / "restored.sqlite3"
        shutil.copy2(backup_path, restored)
        rconn = open_ro(restored)
        report["restore_integrity"] = integrity(rconn)
        rest_fp = table_fingerprints(rconn)
        rconn.close()
    report["tables"] = src_fp
    report["restore_mismatches"] = sorted(t for t in set(src_fp) | set(rest_fp) if src_fp.get(t) != rest_fp.get(t))

    if raw_dir:
        rows = build_manifest(raw_dir, exclude=[out])
        with open(out / "raw_manifest.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["path", "size", "mtime", "sha256"])
            w.writeheader()
            w.writerows(rows)
        report["raw_files"] = len(rows)
        if compare:
            report["manifest_diff"] = compare_manifest(compare, rows)

    after = sha256_file(db)
    report["db_sha256_after"] = after
    report["db_unchanged"] = before == after
    report["ok"] = (report["db_unchanged"] and report["source_integrity"] == "ok"
                    and report["restore_integrity"] == "ok" and not report["restore_mismatches"]
                    and not (report.get("manifest_diff") or {}).get("missing")
                    and not (report.get("manifest_diff") or {}).get("changed"))
    (out / "backup_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--raw-dir", type=Path)
    p.add_argument("--out", required=True, type=Path, help="新規ディレクトリ（既存なら停止）")
    p.add_argument("--compare-manifest", type=Path)
    a = p.parse_args(argv)
    report = run(a.db.expanduser(), a.raw_dir.expanduser() if a.raw_dir else None, a.out.expanduser(),
                 a.compare_manifest)
    print(json.dumps({k: v for k, v in report.items() if k != "tables"}, ensure_ascii=False, indent=1))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
