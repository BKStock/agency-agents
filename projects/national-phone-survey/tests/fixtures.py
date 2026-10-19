"""合成フィクスチャ。実在の町丁目・統計値ではない（形式だけを模した架空データ）。"""

from __future__ import annotations

import copy
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from regional_classifier import store  # noqa: E402
from regional_classifier.features import Feature  # noqa: E402
from regional_classifier.rules import parse_rules  # noqa: E402

RULES_PATH = ROOT / "rules" / "rules_v0.1.json"
YEAR, VERSION = 2020, "estat-2020-r2"

# 共通の都市部の値
_URBAN = dict(pop_total=2500, pop_age_known=2500, pop_65plus=600, pop_2010=2450, pop_2015=2480,
              households_general=1000, hh_in_housing=1000, establishments=30, area_km2=0.5, did_flag=1,
              agri_settlement_overlap=0, fishery_settlement_overlap=0, farmland_ratio=0.01, forest_ratio=0.0,
              slope_mean_deg=1.0, elevation_mean_m=20, zoning_residential_ratio=0.9, zoning_commercial_ratio=0.05,
              land_price_pref_pct=0.5, metro_core_flag=1, uea_member=1, vacation_home_share_muni=0.002,
              hh_rented_public=0, hh_company_housing=0)
_RURAL = dict(pop_total=300, pop_age_known=300, pop_65plus=90, pop_2010=330, pop_2015=315,
              households_general=120, hh_in_housing=120, hh_detached=115, hh_apartment=5, hh_owned=110,
              hh_rented_private=10, hh_rented_public=0, hh_company_housing=0, establishments=5, area_km2=8.0,
              did_flag=0, agri_settlement_overlap=0.1, fishery_settlement_overlap=0, farmland_ratio=0.05,
              forest_ratio=0.3, slope_mean_deg=3.0, elevation_mean_m=120, zoning_residential_ratio=0,
              zoning_commercial_ratio=0, land_price_pref_pct=0.1, metro_core_flag=0, uea_member=0,
              vacation_home_share_muni=0.01)

GIS_FEATURES = {"agri_settlement_overlap", "fishery_settlement_overlap", "farmland_ratio", "forest_ratio",
                "slope_mean_deg", "elevation_mean_m", "zoning_residential_ratio", "zoning_commercial_ratio",
                "land_price_pref_pct"}

# town_key: (municipality_code, name, base, overrides, secrecy_state, merged_into)
TOWNS = {
    "27203000101": ("27203", "戸建町1丁目", _URBAN,
                    dict(hh_detached=850, hh_apartment=150, hh_owned=800, hh_rented_private=200), "none", None),
    "27203000201": ("27203", "高台町1丁目", _URBAN,
                    dict(hh_detached=900, hh_apartment=100, hh_owned=880, hh_rented_private=120,
                         land_price_pref_pct=0.95), "none", None),
    "27203000301": ("27203", "分譲町1丁目", _URBAN,
                    dict(hh_detached=50, hh_apartment=950, hh_owned=720, hh_rented_private=280), "none", None),
    "27203000401": ("27203", "賃貸町1丁目", _URBAN,
                    dict(hh_detached=60, hh_apartment=940, hh_owned=150, hh_rented_private=850), "none", None),
    "27203000501": ("27203", "団地町1丁目", _URBAN,
                    dict(hh_detached=20, hh_apartment=980, hh_owned=100, hh_rented_private=200,
                         hh_rented_public=650), "none", None),
    "27203000601": ("27203", "商住町1丁目", _URBAN,
                    dict(hh_detached=400, hh_apartment=600, hh_owned=500, hh_rented_private=500,
                         establishments=300, zoning_residential_ratio=0.4, zoning_commercial_ratio=0.5),
                    "merge_target", None),
    "03215000100": ("03215", "農村大字", _RURAL,
                    dict(agri_settlement_overlap=0.8, farmland_ratio=0.5, pop_65plus=135, pop_total=300,
                         pop_2010=420), "none", None),
    "03215000200": ("03215", "浜大字", _RURAL,
                    dict(fishery_settlement_overlap=0.6, farmland_ratio=0.02), "none", None),
    "03215000300": ("03215", "山中大字", _RURAL,
                    dict(forest_ratio=0.85, slope_mean_deg=15.0), "none", None),
    "16201000100": ("16201", "地方住宅町", _URBAN,
                    dict(hh_detached=700, hh_apartment=300, hh_owned=650, hh_rented_private=350,
                         metro_core_flag=0, uea_member=1, zoning_residential_ratio=0.7, pop_total=6000,
                         pop_age_known=6000, pop_65plus=1500, pop_2010=6100, area_km2=2.0, did_flag=1),
                    "none", None),
    "20321000100": ("20321", "高原別荘地", _RURAL,
                    dict(vacation_home_share_muni=0.3, forest_ratio=0.5, slope_mean_deg=5.0), "none", None),
    "27203000701": ("27203", "秘匿町", _URBAN, dict(hh_detached=1, hh_apartment=1, hh_owned=1,
                                                   hh_rented_private=1), "suppressed", None),
    "27203000801": ("27203", "合算元町", _URBAN, dict(hh_detached=1, hh_apartment=1, hh_owned=1,
                                                     hh_rented_private=1), "merged_into_other", "27203000601"),
    "03215000400": ("03215", "GIS未取得大字", _RURAL, dict(), "none", None),
}
NO_GIS = {"03215000400"}


def rules():
    return parse_rules(json.loads(RULES_PATH.read_text(encoding="utf-8")))


def approved_rules():
    """承認済みにした版（確定判定の経路を試すためのテスト専用ルール）。"""
    data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    data["rule_version"] = "0.1.0-test-approved"
    for spec in data["classes"].values():
        spec["approved"] = True
        for c in spec["conditions"]:
            c["threshold_origin"] = "approved"
    return parse_rules(data)


def build_db(path: Path | str = ":memory:") -> sqlite3.Connection:
    conn = store.open_db(path)
    with conn:
        store.add_source(conn, "census2020", "令和2年国勢調査 小地域集計（合成）", url="https://example.invalid/census",
                         source_year=2020)
        store.add_source(conn, "gis2020", "GIS 特徴量（合成）", url="https://example.invalid/gis", source_year=2021)
        store.add_source(conn, "evid", "外部証拠（合成）", url="https://example.invalid/evidence", source_year=2024)
        census = store.new_load(conn, "census", source_id="census2020", loaded_at="2026-10-01T00:00:00+00:00")
        gis = store.new_load(conn, "gis", source_id="gis2020", loaded_at="2026-10-02T00:00:00+00:00")
        for key, (muni, name, base, over, secrecy, merged) in TOWNS.items():
            store.add_town(conn, key, YEAR, VERSION, muni, town_name=name, secrecy_state=secrecy,
                           merged_into=merged, source_id="census2020")
            values = {**base, **over}
            census_rows, gis_rows = [], []
            for feat, v in values.items():
                if feat in GIS_FEATURES:
                    if key in NO_GIS:
                        continue
                    gis_rows.append((key, feat, Feature(float(v), "zero" if v == 0 else "observed", "gis2020")))
                elif secrecy == "suppressed":
                    census_rows.append((key, feat, Feature(None, "suppressed", "census2020")))
                else:
                    census_rows.append((key, feat, Feature(float(v), "zero" if v == 0 else "observed",
                                                           "census2020")))
            store.put_features(conn, census, YEAR, VERSION, census_rows)
            store.put_features(conn, gis, YEAR, VERSION, gis_rows)
    return conn


def add_condo_evidence(conn: sqlite3.Connection, grade: str = "A") -> None:
    with conn:
        load = store.new_load(conn, "evidence", loaded_at="2026-10-03T00:00:00+00:00")
        store.add_evidence(conn, load, "27203000301", YEAR, VERSION, "tenure_by_structure_owner", "R3", grade, "evid")


def build_fake_v12(path: Path) -> None:
    """v1.2 を模した架空の DB（列名は架空）。監査で検出させる問題を意図的に仕込む。"""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE source_versions (id TEXT, series_name TEXT, base_date TEXT, url TEXT, sha256 TEXT,
                                      local_path TEXT, is_selected INTEGER);
        CREATE TABLE version_pairs (old_version_id TEXT, new_version_id TEXT);
        CREATE TABLE designation_rows (version_id TEXT, area_code TEXT, local_code TEXT, number_area TEXT,
                                       carrier TEXT, status TEXT);
        CREATE TABLE change_rows (old_version_id TEXT, new_version_id TEXT, area_code TEXT, local_code TEXT);
        CREATE TABLE fetch_log (url TEXT, http_status TEXT);
    """)
    conn.executemany("INSERT INTO source_versions VALUES (?,?,?,?,?,?,?)", [
        ("v1", "06", "2023-01-01", "https://www.soumu.go.jp/a/06.xls", "aa", None, 0),
        ("v2", "06", "2024-01-01", "http://web.archive.org/web/20240105000000/https://www.soumu.go.jp/a/06.xls",
         "bb", None, 0),
        ("v3", "06", "2025-01-01", "https://www.soumu.go.jp/a/06.xls/", "cc", None, 1),
        ("v3b", "06", "2025-01-01", "https://www.soumu.go.jp/a/06b.xls", "dd", None, 1),   # 同一基準日・別内容
        ("w1", "07", "2024-01-01", "https://www.soumu.go.jp/a/07.xls", "bb", None, 1),     # v2 と同一内容
    ])
    conn.executemany("INSERT INTO version_pairs VALUES (?,?)", [
        ("v1", "v2"), ("v2", "v3"), ("v1", "v3"), ("v3", "w1"),
    ])
    rows = []
    for v in ("v1", "v2", "v3", "v3b", "w1"):
        rows.append((v, "06", "200", "A1", "NTT", "使用中"))
    rows += [("v2", "06", "201", "A1", "NTT", "使用中"), ("v3", "06", "201", "A1", "KDDI", "使用中"),
             ("v1", "06", "202", "A1", "NTT", "未使用"), ("v2", "06", "202", "A1", "NTT", "使用中"),
             ("v3", "06", "202", "A1", "ＮＴＴ", "使用中"),   # 全角表記ゆれ
             ("v1", "06", "203", "A1", "NTT", "使用中"), ("v1", "06", "203", "A1", "NTT", "使用中")]
    conn.executemany("INSERT INTO designation_rows VALUES (?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO change_rows VALUES (?,?,?,?)", [
        ("v1", "v2", "06", "201"), ("v1", "v2", "06", "201"), ("v1", "v2", "06", "202"), ("v2", "v3", "06", "201"),
    ])
    conn.executemany("INSERT INTO fetch_log VALUES (?,?)", [
        ("https://www.soumu.go.jp/a/06.xls", "200"),
        ("http://web.archive.org/web/20240105000000id_/https://www.soumu.go.jp/a/06.xls", "503"),
        ("http://web.archive.org/web/20240105000000id_/https://www.soumu.go.jp/a/06.xls", "200"),
        ("https://www.soumu.go.jp/a/missing.xls", "404"),
    ])
    conn.commit()
    conn.close()


FAKE_V12_CONFIG = {
    "in_use_values": ["使用中"],
    "reported": {"comparable_pairs": 4, "all_change_rows": 99},
    "versions": {"sql": "SELECT id AS version_id, series_name AS series, base_date AS as_of_date, url AS source_url,"
                        " sha256, local_path AS file_path, is_selected AS selected FROM source_versions"},
    "pairs": {"sql": "SELECT old_version_id AS old_version, new_version_id AS new_version FROM version_pairs"},
    "rows": {"sql": "SELECT version_id, area_code, local_code, number_area, carrier, status FROM designation_rows"},
    "changes": {"sql": "SELECT old_version_id AS old_version, new_version_id AS new_version, area_code, local_code"
                       " FROM change_rows"},
    "fetch_log": {"sql": "SELECT url, http_status AS status FROM fetch_log"},
}
