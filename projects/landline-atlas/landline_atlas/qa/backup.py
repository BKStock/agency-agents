"""4つのDBのバックアップと復旧試験（sqlite3 オンラインバックアップ API）。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

NAMES = ("numbers", "regions", "evidence", "analysis")


def _fingerprint(path: Path) -> dict:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    out = {"integrity": conn.execute("PRAGMA integrity_check").fetchone()[0], "tables": {}}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
        q = '"' + name.replace('"', '""') + '"'
        hashes = sorted(hashlib.sha256(repr(r).encode()).hexdigest() for r in conn.execute(f"SELECT * FROM {q}"))
        out["tables"][name] = {"rows": len(hashes),
                               "sha256": hashlib.sha256("".join(hashes).encode()).hexdigest()}
    conn.close()
    return out


def backup(db_dir: Path, backups_dir: Path) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = backups_dir / stamp
    target.mkdir(parents=True, exist_ok=False)
    report = {"created": stamp, "databases": {}}
    for name in NAMES:
        src_path = db_dir / f"{name}.sqlite3"
        src = sqlite3.connect(f"{src_path.resolve().as_uri()}?mode=ro", uri=True)
        dst = sqlite3.connect(target / f"{name}.sqlite3")
        src.backup(dst)
        dst.close()
        src.close()
        original = _fingerprint(src_path)
        with tempfile.TemporaryDirectory() as tmp:  # 復旧試験：別の場所に戻して照合
            restored = Path(tmp) / f"{name}.sqlite3"
            shutil.copy2(target / f"{name}.sqlite3", restored)
            again = _fingerprint(restored)
        report["databases"][name] = {"integrity": original["integrity"], "restore_integrity": again["integrity"],
                                     "restore_matches": original == again}
    report["ok"] = all(d["integrity"] == "ok" and d["restore_matches"] for d in report["databases"].values())
    (target / "backup_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report
