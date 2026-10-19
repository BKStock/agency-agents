"""町丁目の特徴量。値は Feature(value, state)。欠損を 0 にしない。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from ..db.connection import utc_now

STATES = ("observed", "zero", "missing", "suppressed", "merged", "undefined")
_RANK = {"observed": 0, "zero": 0, "undefined": 1, "merged": 2, "suppressed": 3, "missing": 4}


@dataclass(frozen=True)
class Feature:
    value: float | None
    state: str
    source_id: str | None = None

    @property
    def usable(self) -> bool:
        return self.state in ("observed", "zero")

    @classmethod
    def of(cls, value: float, source_id: str | None = None) -> "Feature":
        return cls(float(value), "zero" if value == 0 else "observed", source_id)


MISSING = Feature(None, "missing")

# 取り込む生特徴量（統計・GIS）。名前と意味は docs/class_definitions.md と共通
RAW = {
    "pop_total": "総人口", "pop_age_known": "年齢判明人口", "pop_65plus": "65歳以上人口",
    "pop_2010": "2010年人口（2020年境界に組替え）", "pop_2015": "2015年人口（同）",
    "households_general": "一般世帯数", "hh_in_housing": "住宅に住む一般世帯",
    "hh_detached": "一戸建", "hh_apartment": "共同住宅", "hh_owned": "持ち家",
    "hh_rented_public": "公営・UR・公社の借家", "hh_rented_private": "民営借家", "hh_company": "給与住宅",
    "area_km2": "面積", "did_flag": "DID内=1", "establishments": "事業所数",
    "agri_settlement_overlap": "農業集落重複率", "fishery_settlement_overlap": "漁業集落重複率",
    "farmland_ratio": "農地比率", "forest_ratio": "森林比率", "slope_mean_deg": "平均傾斜",
    "elevation_mean_m": "平均標高", "zoning_residential_ratio": "住居系用途地域比",
    "zoning_commercial_ratio": "商業系用途地域比", "land_price_pref_pct": "地価の都道府県内順位(0-1)",
    "metro_core_flag": "三大都市圏=1", "uea_member": "都市雇用圏=1",
    "vacation_home_share_muni": "市区町村の二次的住宅比率",
}


def _worst(*fs: Feature) -> Feature | None:
    w = max(fs, key=lambda f: _RANK[f.state])
    return None if w.usable else Feature(None, w.state)


def _div(num: Callable, den: str) -> Callable[[Mapping[str, Feature]], Feature]:
    def fn(f):
        parts = num(f)
        d = f.get(den, MISSING)
        bad = _worst(*parts, d)
        if bad:
            return bad
        if d.value == 0:
            return Feature(None, "undefined")
        return Feature.of(sum(p.value for p in parts) / d.value, parts[0].source_id)
    return fn


def _get(*names: str):
    return lambda f: [f.get(n, MISSING) for n in names]


def _change(now: str, before: str):
    def fn(f):
        a, b = f.get(now, MISSING), f.get(before, MISSING)
        bad = _worst(a, b)
        if bad:
            return bad
        if b.value == 0:
            return Feature(None, "undefined")
        return Feature.of((a.value - b.value) / b.value, a.source_id)
    return fn


DERIVED = {
    "detached_ratio": _div(_get("hh_detached"), "hh_in_housing"),
    "apartment_ratio": _div(_get("hh_apartment"), "hh_in_housing"),
    "owner_ratio": _div(_get("hh_owned"), "hh_in_housing"),
    "rental_ratio": _div(_get("hh_rented_public", "hh_rented_private", "hh_company"), "hh_in_housing"),
    "public_rental_ratio": _div(_get("hh_rented_public"), "hh_in_housing"),
    "aging_rate": _div(_get("pop_65plus"), "pop_age_known"),
    "pop_change_10y": _change("pop_total", "pop_2010"),
    "pop_density": _div(_get("pop_total"), "area_km2"),
    "estab_per_household": _div(_get("establishments"), "households_general"),
}


def derive(raw: Mapping[str, Feature]) -> dict[str, Feature]:
    out = dict(raw)
    for name, fn in DERIVED.items():
        out.setdefault(name, fn(raw))
    return out


def new_load(conn: sqlite3.Connection, kind: str, source_id: str | None = None, note: str | None = None) -> int:
    return int(conn.execute("INSERT INTO regions.data_load (kind, source_id, loaded_at, note) VALUES (?,?,?,?)",
                            (kind, source_id, utc_now(), note)).lastrowid)


def add_source(conn: sqlite3.Connection, source_id: str, title: str, *, url: str | None = None,
               source_year: int | None = None, license: str | None = None) -> None:
    conn.execute("INSERT OR IGNORE INTO regions.source_document VALUES (?,?,?,?,?,?,?)",
                 (source_id, title, url, source_year, license, None, utc_now()))


def put(conn: sqlite3.Connection, load_id: int, census_year: int, boundary_version: str,
        rows: Iterable[tuple[str, str, Feature]]) -> int:
    n = 0
    for key, name, f in rows:
        if f.state not in STATES:
            raise ValueError(f.state)
        conn.execute("INSERT INTO regions.town_feature VALUES (?,?,?,?,?,?,?,?)",
                     (load_id, key, census_year, boundary_version, name, f.value, f.state, f.source_id))
        n += 1
    return n


def load_latest(conn: sqlite3.Connection, census_year: int, boundary_version: str,
                as_of_load: int) -> dict[str, dict[str, Feature]]:
    out: dict[str, dict[str, Feature]] = {}
    for r in conn.execute(
        """SELECT tf.* FROM regions.town_feature tf
           JOIN (SELECT town_key, feature, MAX(load_id) AS m FROM regions.town_feature
                 WHERE census_year = ? AND boundary_version = ? AND load_id <= ? GROUP BY town_key, feature) x
             ON x.town_key = tf.town_key AND x.feature = tf.feature AND x.m = tf.load_id
           WHERE tf.census_year = ? AND tf.boundary_version = ?""",
            (census_year, boundary_version, as_of_load, census_year, boundary_version)):
        out.setdefault(r["town_key"], {})[r["feature"]] = Feature(r["value"], r["state"], r["source_id"])
    return out
