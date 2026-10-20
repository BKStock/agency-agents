"""テスト用の合成データ。実在の番号指定・町丁目・統計値ではない。"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from landline_atlas.acquire.http import Response  # noqa: E402
from landline_atlas.classify import rules as rules_mod  # noqa: E402
from landline_atlas.db.connection import connect  # noqa: E402
from landline_atlas.regions.features import Feature, add_source, new_load, put  # noqa: E402

CONFIG = ROOT / "config"
YEAR, BV = 2020, "estat-2020"
HEADER = ["市外局番", "市内局番", "番号区画コード", "番号区画", "指定事業者", "使用状況"]


def layout() -> dict:
    return json.loads((CONFIG / "layouts.json").read_text(encoding="utf-8"))


_XLSX_CACHE: dict[str, bytes] = {}


def xlsx(date_text: str, rows: list[list], *, title: str = "固定電話番号の指定状況") -> bytes:
    """同じ内容なら同じバイト列を返す（openpyxl は保存時刻を書き込むため、作り直すと別の原本になる）。"""
    key = repr((date_text, rows, title))
    if key not in _XLSX_CACHE:
        _XLSX_CACHE[key] = _build_xlsx(date_text, rows, title)
    return _XLSX_CACHE[key]


def _build_xlsx(date_text: str, rows: list[list], title: str) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append([title])
    ws.append([date_text])
    ws.append([])
    ws.append(HEADER)
    for r in rows:
        ws.append(r)
    ws.append(["※ 注記：この表は合成データです"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def zipped(name: str, body: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, body)
    return buf.getvalue()


# 系統 06 の3版（2023→2024→2025）
V2023 = [["06", "6200", "A27-1", "大阪", "NTT西日本", "使用中"],
         [None, "6201", "A27-1", "大阪", "NTT西日本", "未使用"],
         [None, "6203", "A27-1", "大阪", "NTT西日本", "使用中"]]
V2024 = [[6, 6200, "A27-1", "大阪", "NTT西日本", "使用中"],          # 数値セル（先頭0欠落）
         [None, 6201, "A27-1", "大阪", "NTT西日本", "使用中"],       # 未使用→使用中
         [None, 6202, "A27-1", "大阪", "KDDI", "使用中"]]            # 追加（6203 は削除）
V2025 = [["06", "6200", "A27-1", "大阪", "ＮＴＴ西日本", "使用中"],  # 全角の表記ゆれのみ
         ["06", "6201", "A27-1", "大阪", "ソフトバンク", "使用中"],   # 事業者変更
         ["06", "6202", "A27-1", "大阪", "KDDI", "使用中"]]


class FakeTransport:
    """URL → 応答の列。列は順に消費し、最後の応答を繰り返す。"""

    def __init__(self, routes: dict[str, list]):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, headers):
        self.calls.append(url)
        seq = self.routes.get(url)
        if seq is None:
            return Response(url, 404, b"")
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, Exception):
            raise item
        status, body = item
        return Response(url, status, body)


PAGE = "https://www.soumu.go.jp/main_sosiki/joho_tsusin/top/tel_number/number_shitei.html"


def official_routes(body_2025: bytes) -> dict:
    html = ("<html><body><a href='/main_content/000999_06.xlsx'>06 市外局番</a>"
            "<a href='/other.pdf'>pdf</a></body></html>").encode()
    return {"https://www.soumu.go.jp/robots.txt": [(404, b"")], PAGE: [(200, html)],
            "https://www.soumu.go.jp/main_content/000999_06.xlsx": [(503, b""), (200, body_2025)]}


def wayback_routes(bodies: dict[str, bytes]) -> dict:
    """bodies: timestamp → ファイル内容。ページの過去版は1つ、ファイルの過去版は bodies の数。"""
    cdx = "https://web.archive.org/cdx/search/cdx"
    file_url = "https://www.soumu.go.jp/main_content/000999_06.xlsx"
    q = "&output=json&fl=timestamp,original,statuscode,digest,mimetype&filter=statuscode:200&collapse=digest&from=2010"
    from urllib.parse import quote
    page_rows = [["timestamp", "original", "statuscode", "digest", "mimetype"],
                 ["20240105000000", PAGE, "200", "PAGEDIGEST", "text/html"]]
    file_rows = [["timestamp", "original", "statuscode", "digest", "mimetype"]] + [
        [ts, file_url, "200", f"D{ts}", "application/vnd"] for ts in bodies]
    html = b"<a href='/main_content/000999_06.xlsx'>06</a>"
    routes = {
        "https://web.archive.org/robots.txt": [(404, b"")],
        f"{cdx}?url={quote(PAGE, safe='')}{q}": [(200, json.dumps(page_rows).encode())],
        f"https://web.archive.org/web/20240105000000id_/{PAGE}": [(200, html)],
        f"{cdx}?url={quote(file_url, safe='')}{q}": [(200, json.dumps(file_rows).encode())],
    }
    for ts, body in bodies.items():
        routes[f"https://web.archive.org/web/{ts}id_/{file_url}"] = [(200, body)]
    return routes


# ---------------------------------------------------------------- 地域

_URBAN = dict(pop_total=2500, pop_age_known=2500, pop_65plus=600, pop_2010=2450, households_general=1000,
              hh_in_housing=1000, hh_main=1000, establishments=30, area_km2=0.5, did_flag=1, agri_settlement_overlap=0,
              fishery_settlement_overlap=0, farmland_ratio=0.01, forest_ratio=0.0, slope_mean_deg=1.0,
              zoning_residential_ratio=0.9, zoning_commercial_ratio=0.05, land_price_pref_pct=0.5,
              metro_core_flag=1, uea_member=1, vacation_home_share_muni=0.002, hh_rented_public=0, hh_company=0)
_RURAL = dict(pop_total=300, pop_age_known=300, pop_65plus=90, pop_2010=330, households_general=120,
              hh_in_housing=120, hh_main=120, hh_detached=115, hh_apartment=5, hh_owned=110, hh_rented_private=10,
              hh_rented_public=0, hh_company=0, establishments=5, area_km2=8.0, did_flag=0,
              agri_settlement_overlap=0.1, fishery_settlement_overlap=0, farmland_ratio=0.05, forest_ratio=0.3,
              slope_mean_deg=3.0, zoning_residential_ratio=0, zoning_commercial_ratio=0, land_price_pref_pct=0.1,
              metro_core_flag=0, uea_member=0, vacation_home_share_muni=0.01)
GIS = {"agri_settlement_overlap", "fishery_settlement_overlap", "farmland_ratio", "forest_ratio", "slope_mean_deg",
       "zoning_residential_ratio", "zoning_commercial_ratio", "land_price_pref_pct"}

TOWNS = {
    "27100000101": ("戸建町", _URBAN, dict(hh_detached=850, hh_apartment=150, hh_owned=800, hh_rented_private=200), "none", None),
    "27100000201": ("高台町", _URBAN, dict(hh_detached=900, hh_apartment=100, hh_owned=880, hh_rented_private=120,
                                         land_price_pref_pct=0.95), "none", None),
    "27100000301": ("分譲町", _URBAN, dict(hh_detached=50, hh_apartment=950, hh_owned=720, hh_rented_private=280), "none", None),
    "27100000401": ("賃貸町", _URBAN, dict(hh_detached=60, hh_apartment=940, hh_owned=150, hh_rented_private=850), "none", None),
    "27100000501": ("団地町", _URBAN, dict(hh_detached=20, hh_apartment=980, hh_owned=100, hh_rented_private=200,
                                         hh_rented_public=650), "none", None),
    "27100000601": ("商住町", _URBAN, dict(hh_detached=400, hh_apartment=600, hh_owned=500, hh_rented_private=500,
                                         establishments=300, zoning_residential_ratio=0.4,
                                         zoning_commercial_ratio=0.5), "merge_target", None),
    "27100000701": ("秘匿町", _URBAN, dict(hh_detached=1, hh_apartment=1, hh_owned=1, hh_rented_private=1), "suppressed", None),
    "27100000801": ("合算元町", _URBAN, dict(hh_detached=1, hh_apartment=1, hh_owned=1, hh_rented_private=1),
                    "merged_into_other", "27100000601"),
    "03215000100": ("農村大字", _RURAL, dict(agri_settlement_overlap=0.8, farmland_ratio=0.5, pop_65plus=135,
                                         pop_2010=420), "none", None),
    "03215000200": ("浜大字", _RURAL, dict(fishery_settlement_overlap=0.6), "none", None),
    "03215000300": ("山中大字", _RURAL, dict(forest_ratio=0.85, slope_mean_deg=15.0), "none", None),
    "03215000400": ("GIS未取得大字", _RURAL, {}, "none", None),
    "16201000100": ("地方住宅町", _URBAN, dict(hh_detached=700, hh_apartment=300, hh_owned=650, hh_rented_private=350,
                                           metro_core_flag=0, zoning_residential_ratio=0.7, pop_total=6000,
                                           pop_age_known=6000, pop_65plus=1500, pop_2010=6100, area_km2=2.0), "none", None),
    "20321000100": ("高原別荘地", _RURAL, dict(vacation_home_share_muni=0.3, forest_ratio=0.5, slope_mean_deg=5.0), "none", None),
}
NO_GIS = {"03215000400"}


def rules():
    return rules_mod.load(CONFIG / "rules_v1.json")


def approved_rules():
    data = json.loads((CONFIG / "rules_v1.json").read_text(encoding="utf-8"))
    data["rule_version"] += "-test-approved"
    for spec in data["classes"].values():
        spec["approved"] = True
        for c in spec["all"]:
            c[3] = "approved"
    return rules_mod.parse(data)


def seed_regions(conn) -> None:
    add_source(conn, "census", "国勢調査（合成）", url="https://example.invalid/census", source_year=2020)
    add_source(conn, "gis", "GIS（合成）", url="https://example.invalid/gis", source_year=2021)
    add_source(conn, "evid", "外部証拠（合成）", url="https://example.invalid/evidence")
    c_load, g_load = new_load(conn, "census", "census"), new_load(conn, "gis", "gis")
    for key, (name, base, over, secrecy, merged) in TOWNS.items():
        conn.execute("INSERT INTO regions.town VALUES (?,?,?,?,?,?,?,?,?)",
                     (key, YEAR, BV, key[:2], key[:5], name, secrecy, merged, "census"))
        crows, grows = [], []
        for feat, v in {**base, **over}.items():
            if feat in GIS:
                if key not in NO_GIS:
                    grows.append((key, feat, Feature.of(v, "gis")))
            elif secrecy == "suppressed":
                crows.append((key, feat, Feature(None, "suppressed", "census")))
            else:
                crows.append((key, feat, Feature.of(v, "census")))
        put(conn, c_load, YEAR, BV, crows)
        put(conn, g_load, YEAR, BV, grows)


def add_evidence(conn, town: str, etype: str, cls: str, grade: str) -> None:
    load = new_load(conn, "evidence")
    conn.execute("INSERT INTO regions.external_evidence (load_id, town_key, census_year, boundary_version,"
                 " evidence_type, supports_class, grade, source_id) VALUES (?,?,?,?,?,?,?,?)",
                 (load, town, YEAR, BV, etype, cls, grade, "evid"))


def db(tmp: Path):
    return connect(Path(tmp) / "db")
