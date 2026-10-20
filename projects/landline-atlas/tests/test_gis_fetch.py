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
    base = {"KEY": "", "PREF": "39", "CITY": "201", "KCITY": "01"}
    rows = [{**base, "RCOM": "001", "RCOM_NAME": "架空集落", "HININTEI": 0, "geometry": cols(133.000, 133.010)},
            {**base, "RCOM": "999", "RCOM_NAME": None, "HININTEI": flag_for_999, "geometry": cols(133.010, 133.020)},
            {**base, "RCOM": "901", "RCOM_NAME": "埋立地", "HININTEI": 1, "geometry": cols(133.020, 133.030)}]
    return shp_zip({"MA0001_2020_2020_39/rcom.shp": gdf(rows, "EPSG:4612")}, encoding="cp932")


def elevation_zip():
    cells = [("100", "2", cols(133.000, 133.005)), ("300", "4", cols(133.005, 133.010)),
             ("unknown", "unknown", cols(133.010, 133.020)), ("50", "10", cols(133.020, 133.030))]
    rows = [{"G04d_001": f"49330000{i}", "G04d_002": e, "G04d_010": s, "geometry": g}
            for i, (e, s, g) in enumerate(cells)]
    return shp_zip({"G04-d-11_4933-jgd_ElevationAndSlopeAngleFifthMesh.shp": gdf(rows, "EPSG:4612")}, drop_prj=True)


def routes(**override):
    listing = b"<a onclick=\"DownLd('1MB','L03-b-21_4933-jgd2011_GML.zip','x')\">L03-b-21_5339-jgd2011_GML.zip</a>"
    g04 = b"G04-d-11_4933-jgd_GML.zip G04-d-11_5339-jgd_GML.zip"
    bodies = {"did": did_zip(), "zoning": zoning_zip(), "land_price": land_price_zip(), "agri_settlement": agri_zip()}
    bodies.update(override)
    r = {u: [(404, b"")] for u in ("https://nlftp.mlit.go.jp/robots.txt", "https://www.maff.go.jp/robots.txt",
                                    "https://www.machimura.maff.go.jp/robots.txt")}
    for key, body in bodies.items():
        r[D[key]["url_template"].format(pref=PREF)] = [(200, body)]
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
        self.assertEqual((self.count("town_feature"), self.count("raw_gis")), (0, 0))
        self.assertFalse((self.raw / "gis").exists())
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
        # 原本の保管と出典（ファイルごと SHA-256・利用条件）
        self.assertEqual(self.count("raw_gis"), 6)
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

    def test_no_boundary_stops(self):
        from landline_atlas import gis_fetch
        rep = gis_fetch.run(self.conn, self.raw, CFG, "03", probe=True, transport=FakeTransport({}),
                            sleep=lambda s: None)
        self.assertIn("atlas fetch-census --pref 03", rep["stopped"])


if __name__ == "__main__":
    unittest.main()
