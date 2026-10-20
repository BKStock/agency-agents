"""atlas fetch-gis（GIS 特徴量）のテスト。配布元は模擬応答で、形・属性・値はすべて合成。"""

import copy
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from support import CONFIG, FakeTransport, db

try:
    import geopandas as gpd
    from shapely.geometry import Point, box
except ImportError:  # GIS の任意依存が無い環境
    gpd = None

from landline_atlas.regions.features import Feature

CFG = json.loads((CONFIG / "gis_sources.json").read_text(encoding="utf-8"))
D = CFG["datasets"]
PREF = "39"
A, B, C = "39201000101", "39201000201", "39202000101"
Y0, Y1 = 33.30, 33.31


def cols(x0, x1):
    return box(x0, Y0, x1, Y1)


def shp_zip(members: dict, *, drop_prj=False, encoding=None) -> bytes:
    """{ZIP 内のパス（.shp）: GeoDataFrame} → Shapefile を収めた ZIP。"""
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(buf, "w") as zf:
        for i, (member, gdf) in enumerate(members.items()):
            stem = Path(tmp) / f"m{i}"
            stem.mkdir()
            kw = {"encoding": encoding} if encoding else {}
            gdf.to_file(stem / "x.shp", driver="ESRI Shapefile", **kw)
            base = member[:-4]
            for f in stem.iterdir():
                if drop_prj and f.suffix == ".prj":
                    continue
                zf.write(f, base + f.suffix)
    return buf.getvalue()


def gdf(rows, crs="EPSG:6668"):
    return gpd.GeoDataFrame([{k: v for k, v in r.items() if k != "geometry"} for r in rows],
                            geometry=[r["geometry"] for r in rows], crs=crs)


def boundary():
    # A は2つの図形（飛び地の形）に分かれている。KEY_CODE が都道府県コードだけの図形は町丁目に属さない
    return shp_zip({"r2ka39.shp": gdf([
        {"KEY_CODE": A, "CITY_NAME": "架空市", "geometry": cols(133.000, 133.005)},
        {"KEY_CODE": A, "CITY_NAME": "架空市", "geometry": cols(133.005, 133.010)},
        {"KEY_CODE": B, "CITY_NAME": "架空市", "geometry": cols(133.010, 133.020)},
        {"KEY_CODE": C, "CITY_NAME": "別市", "geometry": cols(133.020, 133.030)},
        {"KEY_CODE": "39", "CITY_NAME": None, "geometry": cols(133.030, 133.031)}])}, encoding="cp932")


def did_zip():
    return shp_zip({"A16-20_39_DID.shp": gdf([
        {"A16_001": "3920101", "A16_002": "39201", "A16_011": 2020, "geometry": cols(133.000, 133.014)}])})


def zoning_zip(extra_code=None):
    rows = [{"A29_001": "39201", "A29_003": "架空市", "A29_004": 1, "A29_005": "第一種低層住居専用地域", "geometry": cols(133.000, 133.010)},
            {"A29_001": "39201", "A29_003": "架空市", "A29_004": 8, "A29_005": "近隣商業地域", "geometry": cols(133.010, 133.016)},
            {"A29_001": "39201", "A29_003": "架空市", "A29_004": 99, "A29_005": "不明", "geometry": cols(133.0198, 133.020)}]
    if extra_code:
        rows.append({"A29_001": "39201", "A29_003": "架空市", "A29_004": extra_code, "A29_005": "?", "geometry": cols(133.016, 133.017)})
    return shp_zip({"A29-19_39/01-01_x/A29-19_39201.shp": gdf(rows)})


def landuse_zip(member="L03-b-21_4933-jgd2011_GML\\L03-b-21_4933.shp"):
    # 実物の一部のメッシュ（例 5538）は、フォルダ付き・区切り「\」で入っている
    return shp_zip({member: gdf([
        {"L03b_001": "4933000001", "L03b_002": "0100", "geometry": cols(133.000, 133.010)},
        {"L03b_001": "4933000002", "L03b_002": "0500", "geometry": cols(133.010, 133.020)},
        {"L03b_001": "4933000003", "L03b_002": "0700", "geometry": cols(133.020, 133.025)}])})


def land_price_zip(year="2020"):
    pts = [(133.002, 100), (133.008, 300), (133.015, 500)]
    rows = [{"L01_005": year, "L01_006": str(v), "L01_021": "39201", "geometry": Point(x, 33.305)} for x, v in pts]
    rows.append({"L01_005": year, "L01_006": "50", "L01_021": "39999", "geometry": Point(134.0, 34.0)})
    return shp_zip({"L01-20_39_GML/L01-20_39.shp": gdf(rows, "EPSG:4612")})


def agri_zip(flag_for_999=1):
    base = {"PREF": "39", "CITY": "201", "KCITY": "01"}
    rows = [{**base, "KEY": "3920101001", "RCOM": "001", "RCOM_NAME": "架空集落", "HININTEI": 0,
             "geometry": cols(133.000, 133.010)},
            {**base, "KEY": "3920101999", "RCOM": "999", "RCOM_NAME": None, "HININTEI": flag_for_999,
             "geometry": cols(133.010, 133.020)},
            {**base, "KEY": "3920101901", "RCOM": "901", "RCOM_NAME": "埋立地", "HININTEI": 1,
             "geometry": cols(133.020, 133.030)}]
    return shp_zip({"MA0001_2020_2020_39/rcom.shp": gdf(rows, "EPSG:4612")}, encoding="cp932")


def elevation_zip():
    cells = [("100", "2", cols(133.000, 133.005)), ("300", "4", cols(133.005, 133.010)),
             ("unknown", "unknown", cols(133.010, 133.020)), ("50", "10", cols(133.020, 133.030))]
    rows = [{"G04d_001": f"49330000{i}", "G04d_002": e, "G04d_010": s, "geometry": g}
            for i, (e, s, g) in enumerate(cells)]
    return shp_zip({"G04-d-11_4933-jgd_ElevationAndSlopeAngleFifthMesh.shp": gdf(rows, "EPSG:4612")}, drop_prj=True)


def xlsx(rows) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    for r in rows:
        wb.active.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def types_xlsx(settlement_type=3, extra=()):
    head = D["rural_settlement"]["types_header"]
    rows = [head, ["3900000000", "39", "000", "00", "000", "高知県", "…", "…", None, "…", "…"],
            ["3920101000", "39", "201", "01", "000", "高知県", "架空市", "（旧）", None, 3, 1],
            ["3920101001", "39", "201", "01", "001", "高知県", "架空市", "（旧）", "架空集落", settlement_type, 1],
            ["3920101999", "39", "201", "01", "999", "高知県", "架空市", "（旧）", "", "…", "…"], *extra]
    return xlsx(rows)


def scaled(points, x0=133.000):
    """4×4 の格子の座標を町丁目 A の範囲（0.01 度四方）に置く。"""
    from shapely.geometry import Polygon
    return Polygon([(x0 + u * 0.0025, Y0 + v * 0.0025) for u, v in points])


SELF_TOUCH = [(0, 0), (4, 0), (4, 4), (2, 4), (3, 2), (1, 2), (2, 4), (0, 4)]   # 1点で自分に接する（実物の形）
BOWTIE = [(0, 0), (4, 4), (4, 0), (0, 4)]                                         # 直すと面積が変わる


def fishery_zip(other_name="その他地域", settlement_geom=None):
    base = {"PREF": "39", "CITY": "201", "GAREA": "001", "CITY_NAME": "架空市", "GAREA_NAME": "架空浦"}
    return shp_zip({"MB0001_2023_39/shuraku.shp": gdf([
        {**base, "KEY": "39201001001", "GCOM": "001", "GCOM_NAME": "架空浦",
         "geometry": settlement_geom or cols(133.000, 133.010)},
        {**base, "KEY": "39201001999", "GCOM": "999", "GCOM_NAME": other_name, "geometry": cols(133.010, 133.020)}])},
        encoding="cp932")


def fishery_listing(prefs=None):
    prefs = prefs or [f"{i:02d}" for i in range(1, 48) if i not in (9, 10, 11, 19, 20, 21, 25, 29)]
    return "".join(f'<a href="/j/tokei/census/fc/database/mapdata/MB0001_2023_{p}.zip">{p}</a>' for p in prefs).encode()


def metro_xlsx(flag="中心市"):
    head = ["市区町村コード", "市区町村名", "大都市圏フラグ", "都市圏フラグ", "札幌大都市圏", "関東大都市圏",
            "中京大都市圏", "近畿大都市圏", "松山都市圏"]
    return xlsx([["令和２年国勢調査　大都市圏・都市圏　対象市区町村データ（総務省統計局）"], head,
                 ["39201", "架空市", flag, None, None, None, None, "近畿", None],
                 ["39202", "別市", None, None, None, None, None, None, None]])


def housing_xlsx(second_home=50):
    return xlsx([["令和５年住宅・土地統計調査　住宅及び世帯に関する基本集計"],
                 ["第１－２表　居住世帯の有無(8区分)別住宅数及び住宅以外で人が居住する建物数－全国、都道府県、市区町村"],
                 [None], [None, "表章項目", "住宅数", "住宅数"],
                 [None, "項目名", "0_総数", "1_居住世帯あり", "224_二次的住宅"],
                 ["地域識別コード", "地域区分", " "],
                 ["a", "39000_高知県", 388100, 307400, 2500],
                 ["2", "39201_架空市", 1000, 900, second_home],
                 ["2", "39202_別市", 800, 700, "-"]])


EC_A, EC_B1, EC_B2, EC_C = "39201000100000100", "39201000200000200", "39201000300000300", "39202000100000100"


def estab_table_zip(a_count="12"):
    head = "KEY_CODE,CITY_NAME,AZA_CODE,AZA_NAME," + ",".join(f"T001168{i:03d}" for i in range(1, 17))
    labels = ",,,,0_総数,1_民営,11_個人,12_法人,121_会社,122_会社以外の法人,13_法人でない団体,2_国、地方公共団体," \
             "0_総数,1_民営,11_個人,12_法人,121_会社,122_会社以外の法人,13_法人でない団体,2_国、地方公共団体"
    def row(key, name, n, m):
        return f"{key},架空市,{key[5:]},{name},{n}," + ",".join(["-"] * 7) + f",{m}," + ",".join(["-"] * 7)
    lines = [head, labels, row(EC_A, "甲町", a_count, "90"), row(EC_B1, "乙町１丁目", "5", "20"),
             row(EC_B2, "乙町２丁目", "-", "-"), row(EC_C, "丙町", "-", "-"),
             row("39201999999999999", "その他", "3", "4")]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("tblT001168C39.txt", "\r\n".join(lines).encode("cp932"))
    return buf.getvalue()


def estab_boundary_zip(jigyosho_a=12):
    def r(key, kigo, n, g):
        return {"KEY_CODE": key, "S_AREA": key and key[5:], "KIGO_E": kigo, "JIGYOSHO": n, "geometry": g}
    # 甲町は飛び地（E1 に代表して事業所数、E2 は 0）。乙町は国勢調査の B を2つに分けた形（B とは対応しない）
    return shp_zip({"r3ca39.shp": gdf([
        r(EC_A, "E1", jigyosho_a, cols(133.000, 133.006)), r(EC_A, "E2", 0, cols(133.006, 133.010)),
        r(EC_B1, None, 5, cols(133.010, 133.016)), r(EC_B2, None, 0, cols(133.016, 133.020)),
        r(EC_C, None, 0, cols(133.020, 133.030)), r(None, None, 0, cols(133.030, 133.031))])}, encoding="cp932")


def routes(**override):
    listing = b"<a onclick=\"DownLd('1MB','L03-b-21_4933-jgd2011_GML.zip','x')\">L03-b-21_5339-jgd2011_GML.zip</a>"
    g04 = b"G04-d-11_4933-jgd_GML.zip G04-d-11_5339-jgd_GML.zip"
    bodies = {"did": did_zip(), "zoning": zoning_zip(), "land_price": land_price_zip(), "agri_settlement": agri_zip(),
              "rural_settlement": types_xlsx(), "fishery_settlement": fishery_zip(), "metro": metro_xlsx(),
              "vacation_home": housing_xlsx()}
    bodies.update({k: v for k, v in override.items() if k in bodies})
    r = {u: [(404, b"")] for u in ("https://nlftp.mlit.go.jp/robots.txt", "https://www.maff.go.jp/robots.txt",
                                    "https://www.machimura.maff.go.jp/robots.txt", "https://www.stat.go.jp/robots.txt",
                                    "https://www.e-stat.go.jp/robots.txt")}
    for key, body in bodies.items():
        r[D[key]["url_template"].format(pref=PREF)] = [(200, body)]
    r[D["fishery_settlement"]["page"]] = [(200, override.get("fishery_listing", fishery_listing()))]
    r[D["establishments"]["table_url_template"].format(pref=PREF)] = [(200, override.get("estab_table", estab_table_zip()))]
    r[D["establishments"]["boundary_url_template"].format(pref=PREF)] = [
        (200, override.get("estab_boundary", estab_boundary_zip()))]
    r[D["landuse"]["page"]] = [(200, listing)]
    r[D["landuse"]["url_template"].format(mesh="4933")] = [(200, landuse_zip())]
    r[D["elevation"]["page"]] = [(200, g04)]
    r[D["elevation"]["url_template"].format(mesh="4933")] = [(200, elevation_zip())]
    return r



@unittest.skipIf(gpd is None, "GIS の任意依存（geopandas）が無い")
class GisFunctionsTest(unittest.TestCase):
    def test_percentile_rank_ties_and_single(self):
        from landline_atlas.regions import gis
        self.assertEqual(gis.percentile_rank({"a": 1, "b": 5, "c": 5, "d": 9}),
                         {"a": 0.0, "b": 0.5, "c": 0.5, "d": 1.0})
        self.assertEqual(gis.percentile_rank({"a": 1}), {})

    def test_ward_coverage_by_city_file(self):
        from landline_atlas.gis_fetch import ward_coverage
        towns = {"27141": "堺市堺区", "27142": "堺市中区", "27102": "大阪市都島区", "27203": "豊中市",
                 "27140": "堺市", "28102": "神戸市灘区", "13999": "神戸市架空区"}
        files = {"27140": "堺市", "27203": "豊中市", "28100": "神戸市"}
        # 大阪市（ファイルなし）の区・他の都道府県の同名の市は覆わない。区でない市はそれ自体のファイルで判定する
        self.assertEqual(ward_coverage(files, towns), {"27141": "27140", "27142": "27140", "28102": "28100"})

    def test_share_states(self):
        from landline_atlas.regions import gis
        self.assertEqual(gis.share(0, 0, 0, 0.95).state, "undefined")
        self.assertEqual(gis.share(100, 10, 90, 0.95).state, "missing")
        self.assertEqual(gis.share(100, 0, 100, 0.95), Feature(0.0, "zero"))
        self.assertEqual(gis.share(100, 40, 96, 0.95), Feature(0.4, "observed"))

    def test_value_sums_accumulate_across_meshes(self):
        from landline_atlas.regions import gis
        towns = gdf([{"town_key": A, "geometry": cols(133.0, 133.01)}])
        sums = {}
        gis.value_area_sums(towns, gdf([{"v": 10.0, "geometry": cols(133.0, 133.005)}]), ["v"], into=sums)
        gis.value_area_sums(towns, gdf([{"v": 30.0, "geometry": cols(133.005, 133.01)}]), ["v"], into=sums)
        total = gis.town_areas(towns)[A]
        self.assertAlmostEqual(gis.mean_feature(total, sums[A]["v"], 0.95).value, 20.0, places=4)
        self.assertEqual(gis.mean_feature(total, [5.0, total * 0.5], 0.95).state, "missing")


    def test_match_units_one_to_one_only(self):
        from landline_atlas.regions import gis
        towns = gdf([{"town_key": A, "geometry": cols(133.00, 133.01)},
                     {"town_key": B, "geometry": cols(133.01, 133.02)},
                     {"town_key": C, "geometry": cols(133.02, 133.03)}])
        units = gdf([{"u": "same", "geometry": cols(133.00, 133.01)},
                     # B は2つの単位に分かれ、C は飛び地を町丁目の外にもつ単位に含まれる（どちらも対応しない）
                     {"u": "half1", "geometry": cols(133.01, 133.015)}, {"u": "half2", "geometry": cols(133.015, 133.02)},
                     {"u": "enclave", "geometry": cols(133.02, 133.03)}, {"u": "enclave", "geometry": cols(133.03, 133.04)}])
        self.assertEqual(gis.match_units(towns, units, "u", 0.95), {A: "same"})

@unittest.skipIf(gpd is None, "GIS の任意依存（geopandas）が無い")
class FetchGisTest(unittest.TestCase):
    def setUp(self):
        from landline_atlas.acquire import estat
        from landline_atlas.ingest import estat_tables
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.conn = db(self.dir)
        self.addCleanup(self.conn.close)
        self.raw = self.dir / "raw"
        body = boundary()
        import hashlib
        f = estat.Fetched("boundary", True, "https://example.invalid/boundary", hashlib.sha256(body).hexdigest(), body)
        estat_tables.register(self.conn, "estat:boundary", "境界（合成）", f.url, 2020, "test", f.sha256)
        estat.store(self.conn, self.raw, f, PREF)
        for key in (A, B, C):
            self.conn.execute("INSERT INTO regions.town VALUES (?,?,?,?,?,?,?,?,?)",
                              (key, 2020, "estat-2020", key[:2], key[:5], "架空", "none", None, "estat:boundary"))

    def run_fetch(self, *, probe, r=None, cfg=CFG):
        from landline_atlas import gis_fetch
        return gis_fetch.run(self.conn, self.raw, cfg, PREF, probe=probe, transport=FakeTransport(r or routes()),
                             sleep=lambda s: None)

    def features(self):
        rows = self.conn.execute("SELECT town_key, feature, value, state FROM regions.town_feature").fetchall()
        return {(r[0], r[1]): (r[2], r[3]) for r in rows}

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM regions.{table}").fetchone()[0]

    def test_probe_reports_and_writes_nothing(self):
        rep = self.run_fetch(probe=True)
        self.assertIsNone(rep["stopped"])
        self.assertEqual((rep["towns"]["towns_in_boundary"], rep["towns"]["polygons_without_town"]), (3, 1))
        self.assertEqual(rep["datasets"]["did"]["features"]["did_flag"], {"observed": 1, "zero": 2})
        self.assertEqual(rep["datasets"]["landuse"]["meshes"]["listed_and_needed"], ["4933"])
        self.assertEqual(rep["datasets"]["agri_settlement"]["polygons"],
                         {"recognized": 1, "not_recognized": 1, "uncertain": 1})
        self.assertEqual(rep["datasets"]["agri_settlement"]["uncertain"]["rcom"], {"901": 1})
        self.assertEqual((self.count("town_feature"), self.count("raw_gis"), self.count("data_load"),
                          self.count("source_document")), (0, 0, 0, 1))
        # 原本は raw/gis に残し（DB には書かない）、索引に URL と SHA-256 を載せる
        index = json.loads((self.raw / "gis" / "probe_index.json").read_text(encoding="utf-8"))
        self.assertIn(D["did"]["url_template"].format(pref=PREF), index)
        from landline_atlas import gis_fetch
        self.assertIn("確認モード", gis_fetch.render(rep))

    def test_full_import_values_and_states(self):
        rep = self.run_fetch(probe=False)
        self.assertTrue(rep["imported"], rep["stopped"])
        f = self.features()
        self.assertEqual(f[(A, "did_flag")], (1.0, "observed"))
        self.assertEqual(f[(B, "did_flag")], (0.0, "zero"))          # DID に 4 割だけ入る
        self.assertEqual(f[(A, "zoning_residential_ratio")], (1.0, "observed"))
        self.assertAlmostEqual(f[(B, "zoning_commercial_ratio")][0], 0.6, places=4)
        self.assertEqual(f[(C, "zoning_residential_ratio")], (None, "missing"))   # 用途地域のファイルが無い市
        self.assertEqual(f[(A, "farmland_ratio")], (1.0, "observed"))
        self.assertEqual(f[(B, "forest_ratio")], (1.0, "observed"))
        self.assertEqual(f[(C, "forest_ratio")], (None, "missing"))  # セルが半分しかない
        self.assertEqual(f[(A, "land_price_pref_pct")], (0.0, "zero"))
        self.assertEqual(f[(B, "land_price_pref_pct")], (1.0, "observed"))
        self.assertEqual(f[(C, "land_price_pref_pct")], (None, "missing"))  # 地点が無い
        self.assertEqual(f[(A, "agri_settlement_overlap")], (1.0, "observed"))
        self.assertEqual(f[(B, "agri_settlement_overlap")], (0.0, "zero"))   # 認定されていない区域
        self.assertEqual(f[(C, "agri_settlement_overlap")], (None, "missing"))  # 定義書に無い区分
        self.assertAlmostEqual(f[(A, "elevation_mean_m")][0], 200.0, places=3)
        self.assertAlmostEqual(f[(A, "slope_mean_deg")][0], 3.0, places=3)
        self.assertEqual(f[(B, "elevation_mean_m")], (None, "missing"))     # unknown のセルだけ
        self.assertAlmostEqual(f[(C, "slope_mean_deg")][0], 10.0, places=3)
        self.assertEqual(rep["datasets"]["land_price"]["points_outside_towns"], 1)
        self.assertEqual(f[(A, "rural_settlement_overlap")], (1.0, "observed"))   # 中間農業地域の集落
        self.assertEqual(f[(B, "rural_settlement_overlap")], (0.0, "zero"))
        self.assertEqual(f[(C, "rural_settlement_overlap")], (None, "missing"))
        self.assertEqual(f[(A, "fishery_settlement_overlap")], (1.0, "observed"))
        self.assertEqual(f[(B, "fishery_settlement_overlap")], (0.0, "zero"))     # その他地域
        self.assertEqual(f[(C, "fishery_settlement_overlap")], (0.0, "zero"))     # 沿海市区町村でない
        self.assertEqual((f[(A, "metro_core_flag")], f[(C, "metro_core_flag")]), ((1.0, "observed"), (0.0, "zero")))
        self.assertEqual(f[(A, "vacation_home_share_muni")], (0.05, "observed"))
        self.assertEqual(f[(C, "vacation_home_share_muni")], (None, "missing"))    # 「-」は 0 と区別できない
        self.assertEqual(f[(A, "establishments")], (12.0, "observed"))             # 飛び地は E1 に代表
        self.assertEqual(f[(C, "establishments")], (0.0, "zero"))                  # 「-」＝境界の JIGYOSHO 0
        self.assertNotIn((B, "establishments"), f)                                 # 境界が1対1で重ならない
        e = rep["datasets"]["establishments"]
        self.assertEqual((e["towns_matched"], e["towns_not_matched"], e["areas_not_matched"]), (2, 1, 2))
        self.assertEqual((e["areas_in_table"], e["areas_with_polygon"], e["areas_without_polygon"]), (5, 4, 1))   # 「その他」
        self.assertEqual(e["unlocated_establishments"], {"39201": 3})
        # 原本の保管と出典（ファイルごと SHA-256・利用条件）。農業集落境界は2つのデータで同じ原本を使う
        self.assertEqual(self.count("raw_gis"), 12)
        for path, sha in self.conn.execute("SELECT stored_path, sha256 FROM regions.raw_gis"):
            import hashlib
            self.assertEqual(hashlib.sha256((self.raw / path).read_bytes()).hexdigest(), sha)
        lic = self.conn.execute("SELECT license, sha256 FROM regions.source_document WHERE source_id LIKE "
                                "'gis:agri_settlement:%'").fetchone()
        self.assertIn("出典を明示", lic[0])
        self.assertIsNotNone(lic[1])

    def assert_stops(self, r, needle):
        rep = self.run_fetch(probe=False, r=r)
        self.assertIsNotNone(rep["stopped"])
        self.assertIn(needle, rep["stopped"])
        self.assertFalse(rep["imported"])
        self.assertEqual((self.count("town_feature"), self.count("raw_gis")), (0, 0))

    def test_unexpected_zoning_code_stops(self):
        self.assert_stops(routes(zoning=zoning_zip(extra_code=13)), "unexpected A29_004 codes {'13': 1}")

    def test_land_price_year_mismatch_stops(self):
        self.assert_stops(routes(land_price=land_price_zip(year="2019")), "L01_005 should be 2020")

    def test_not_recognized_code_with_flag_zero_stops(self):
        self.assert_stops(routes(agri_settlement=agri_zip(flag_for_999=0)), "RCOM 999/-99")

    def test_missing_member_stops(self):
        r = routes()
        r[D["landuse"]["url_template"].format(mesh="4933")] = [(200, landuse_zip(member="other.shp"))]
        self.assert_stops(r, "expected one L03-b-21_4933.shp")

    def test_missing_crs_without_override_stops(self):
        cfg = copy.deepcopy(CFG)
        del cfg["datasets"]["elevation"]["crs_if_missing"]
        rep = self.run_fetch(probe=True, cfg=cfg)
        self.assertIn("has no CRS", rep["stopped"])

    def test_settlement_type_urban_is_not_rural(self):
        self.run_fetch(probe=False, r=routes(rural_settlement=types_xlsx(settlement_type=1)))
        self.assertEqual(self.features()[(A, "rural_settlement_overlap")], (0.0, "zero"))

    def test_untyped_settlement_stops(self):
        self.assert_stops(routes(rural_settlement=types_xlsx(settlement_type="…")), "have no type")

    def test_unexpected_settlement_type_stops(self):
        self.assert_stops(routes(rural_settlement=types_xlsx(settlement_type=5)), "unexpected 地域類型1次分類 values")

    def test_fishery_other_code_with_other_name_stops(self):
        self.assert_stops(routes(fishery_settlement=fishery_zip(other_name="架空")), "GCOM=999 named")

    def test_fishery_invalid_polygon_is_repaired(self):
        rep = self.run_fetch(probe=False, r=routes(fishery_settlement=fishery_zip(settlement_geom=scaled(SELF_TOUCH))))
        self.assertTrue(rep["imported"], rep["stopped"])
        self.assertEqual(rep["datasets"]["fishery_settlement"]["repaired_polygons"], ["39201001001"])
        # 1/8 が切れ込みで欠けるので、値が分かる部分が min_coverage 未満になる（0 で埋めない）
        self.assertEqual(self.features()[(A, "fishery_settlement_overlap")], (None, "missing"))

    def test_fishery_repair_changing_area_stops(self):
        self.assert_stops(routes(fishery_settlement=fishery_zip(settlement_geom=scaled(BOWTIE))), "changes its area")

    def test_fishery_prefecture_not_listed_is_zero(self):
        self.run_fetch(probe=False, r=routes(fishery_listing=fishery_listing(["01", "27"] + [f"{i}" for i in range(40, 70)])))
        f = self.features()
        self.assertEqual({f[(k, "fishery_settlement_overlap")] for k in (A, B, C)}, {(0.0, "zero")})
        src = self.conn.execute("SELECT url FROM regions.source_document WHERE source_id LIKE 'gis:fishery_settlement:%'")
        self.assertEqual([r[0] for r in src], [D["fishery_settlement"]["page"]])

    def test_metro_unexpected_flag_stops(self):
        self.assert_stops(routes(metro=metro_xlsx(flag="郊外")), "大都市圏フラグ='郊外'")

    def test_establishments_table_boundary_disagree_stops(self):
        self.assert_stops(routes(estab_boundary=estab_boundary_zip(jigyosho_a=11)), "table and boundary disagree")

    def test_establishments_unexpected_symbol_stops(self):
        self.assert_stops(routes(estab_table=estab_table_zip(a_count="X")), "unexpected values {'X': 1}")

    def test_settlement_type_header_changed_stops(self):
        rows = [D["rural_settlement"]["types_header"][:-1] + ["地域類型3次分類"],
                ["3920101001", "39", "201", "01", "001", "高知県", "架空市", "（旧）", "架空集落", 3, 1]]
        self.assert_stops(routes(rural_settlement=xlsx(rows)), "type table header")

    def test_fishery_listing_too_short_stops(self):
        self.assert_stops(routes(fishery_listing=fishery_listing(["39", "27"])), "only 2 prefectures listed")

    def test_metro_area_without_flag_stops(self):
        self.assert_stops(routes(metro=metro_xlsx(flag=None)), "is in")

    def test_housing_table_title_changed_stops(self):
        body = housing_xlsx()
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(body))
        wb.active["A1"], wb.active["A2"] = "平成30年住宅・土地統計調査", "第１－１表"
        buf = io.BytesIO()
        wb.save(buf)
        self.assert_stops(routes(vacation_home=buf.getvalue()), "vacation_home: title")

    def test_housing_unexpected_symbol_stops(self):
        self.assert_stops(routes(vacation_home=housing_xlsx(second_home="x")), "39201 has value 'x'")

    def test_probe_then_import_reuses_raw_files(self):
        from landline_atlas import gis_fetch
        t = FakeTransport(routes())
        gis_fetch.run(self.conn, self.raw, CFG, PREF, probe=True, transport=t, sleep=lambda s: None)
        downloads = [u for u in t.calls if not u.endswith("robots.txt") and u not in self.pages()]
        self.assertEqual(len(downloads), len(set(downloads)))     # 同じ実行の中でも同じ URL は1回
        t.calls.clear()
        rep = gis_fetch.run(self.conn, self.raw, CFG, PREF, probe=False, transport=t, sleep=lambda s: None)
        self.assertTrue(rep["imported"], rep["stopped"])
        self.assertEqual([u for u in t.calls if not u.endswith("robots.txt") and u not in self.pages()], [])
        self.assertEqual(rep["reused_raw_files"], len(set(downloads)))
        t.calls.clear()
        gis_fetch.run(self.conn, self.raw, CFG, PREF, probe=True, transport=t, sleep=lambda s: None, refresh=True)
        self.assertEqual(sorted(set(u for u in t.calls if u in downloads)), sorted(set(downloads)))

    def test_tampered_raw_file_is_not_reused(self):
        from landline_atlas import gis_fetch
        t = FakeTransport(routes())
        gis_fetch.run(self.conn, self.raw, CFG, PREF, probe=True, transport=t, sleep=lambda s: None,
                      datasets=["did"])
        index = json.loads((self.raw / "gis" / "probe_index.json").read_text(encoding="utf-8"))
        url = D["did"]["url_template"].format(pref=PREF)
        (self.raw / index[url]["stored_path"]).write_bytes(b"PK broken")
        t.calls.clear()
        # 壊れた原本は使わずに取り直す。保管済みのファイルは上書きせず（消さない）、食い違いで止める
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            gis_fetch.run(self.conn, self.raw, CFG, PREF, probe=True, transport=t, sleep=lambda s: None,
                          datasets=["did"])
        self.assertIn(url, t.calls)
        self.assertEqual((self.raw / index[url]["stored_path"]).read_bytes(), b"PK broken")

    def pages(self):
        return {D["landuse"]["page"], D["elevation"]["page"], D["fishery_settlement"]["page"]}

    def test_no_boundary_stops(self):
        from landline_atlas import gis_fetch
        rep = gis_fetch.run(self.conn, self.raw, CFG, "03", probe=True, transport=FakeTransport({}),
                            sleep=lambda s: None)
        self.assertIn("atlas fetch-census --pref 03", rep["stopped"])


if __name__ == "__main__":
    unittest.main()
