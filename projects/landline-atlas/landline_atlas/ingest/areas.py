"""番号区画 → 市区町村の対応表の取込。

総務省の番号区画（MA）資料から作った CSV を取り込む。
列: number_area_code, municipality_code, area_code, ma_name, coverage(whole/part/unknown), valid_from, source_url
coverage=part は「区画が市区町村の一部だけを含む」ことを表す。
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from ..normalize import municipality_code, nfkc


def import_number_areas(conn: sqlite3.Connection, path: Path) -> int:
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for rec in csv.DictReader(fh):
            coverage = nfkc(rec.get("coverage")) or "unknown"
            conn.execute("INSERT INTO numbers.number_area_municipality VALUES (?,?,?,?,?,?,?)",
                         (nfkc(rec["number_area_code"]), municipality_code(rec["municipality_code"]),
                          nfkc(rec.get("area_code")) or None, nfkc(rec.get("ma_name")) or None,
                          coverage, nfkc(rec["valid_from"]), nfkc(rec.get("source_url")) or None))
            n += 1
    return n
