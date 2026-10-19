"""公開施設の電話番号観測。

対象は所在地が公的に確認できる施設（官公署・学校・医療機関・図書館・店舗・法人）だけ。
個人宅・個人名義・未公開の番号は受け付けない。保存するのは市外局番・市内局番までで、
加入者番号は保存しない（局番と地域の関連を見るのに不要なため）。
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from ..normalize import nfkc, phone_part, town_key

FACILITY_TYPES = ("public_office", "school", "medical", "library", "shop", "corporation", "other_facility")


def add(conn: sqlite3.Connection, *, facility_name: str, facility_type: str, area_code: str, local_code: str,
        address: str, source_url: str, observed_on: str, town: str | None = None, census_year: int | None = None,
        boundary_version: str | None = None, geocode_method: str | None = None, verified: bool = False) -> int:
    if facility_type not in FACILITY_TYPES:
        raise ValueError(f"facility_type must be one of {FACILITY_TYPES} (private residences are out of scope)")
    if not nfkc(source_url):
        raise ValueError("source_url is required")
    if town is not None and (census_year is None or boundary_version is None):
        raise ValueError("town requires census_year and boundary_version")
    area, _ = phone_part(area_code, "area_code", leading_zero=False)
    if not area.startswith("0"):
        raise ValueError("area_code must start with 0")
    local, _ = phone_part(local_code, "local_code", leading_zero=False)
    return int(conn.execute(
        "INSERT INTO evidence.facility_observation (facility_name, facility_type, area_code, local_code, address,"
        " town_key, census_year, boundary_version, geocode_method, verified, source_url, observed_on)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (nfkc(facility_name), facility_type, area, local, nfkc(address), town_key(town) if town else None,
         census_year, boundary_version, geocode_method, int(verified), nfkc(source_url), observed_on)).lastrowid)


def import_csv(conn: sqlite3.Connection, path: Path, census_year: int, boundary_version: str) -> int:
    """列: facility_name, facility_type, area_code, local_code, address, town_key, verified, source_url, observed_on"""
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for rec in csv.DictReader(fh):
            add(conn, facility_name=rec["facility_name"], facility_type=rec["facility_type"],
                area_code=rec["area_code"], local_code=rec["local_code"], address=rec["address"],
                source_url=rec["source_url"], observed_on=rec["observed_on"],
                town=nfkc(rec.get("town_key")) or None, census_year=census_year, boundary_version=boundary_version,
                geocode_method=rec.get("geocode_method") or None, verified=nfkc(rec.get("verified")) in ("1", "true"))
            n += 1
    return n
