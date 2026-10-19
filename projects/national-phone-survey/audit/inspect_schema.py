#!/usr/bin/env python3
"""v1.2 DB の構造だけを出力する（データ値は出さない）。共有・設定ファイル作成用。

  python3 audit/inspect_schema.py --db ~/official_phone_data/national_tool/data/index.sqlite3 > schema.md
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def describe(db: Path, with_counts: bool = True) -> str:
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    lines = [f"# schema of {db.name}", "",
             f"- journal_mode: {conn.execute('PRAGMA journal_mode').fetchone()[0]}",
             f"- user_version: {conn.execute('PRAGMA user_version').fetchone()[0]}", ""]
    for name, kind, sql in conn.execute(
            "SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"):
        q = '"' + name.replace('"', '""') + '"'
        lines.append(f"## {kind} {name}")
        if kind in ("table", "view"):
            if with_counts:
                lines.append(f"- rows: {conn.execute(f'SELECT COUNT(*) FROM {q}').fetchone()[0]}")
            lines.append("")
            lines.append("| cid | column | type | notnull | pk |")
            lines.append("|---|---|---|---|---|")
            for cid, col, typ, notnull, _default, pk in conn.execute(f"PRAGMA table_info({q})"):
                lines.append(f"| {cid} | {col} | {typ} | {notnull} | {pk} |")
        lines += ["", "```sql", (sql or "").strip(), "```", ""]
    conn.close()
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--no-counts", action="store_true")
    a = p.parse_args(argv)
    print(describe(a.db.expanduser(), with_counts=not a.no_counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
