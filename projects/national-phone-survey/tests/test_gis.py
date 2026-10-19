"""GIS 結合のテスト。geopandas / rasterio が無い環境では skip する。"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

HAVE_GEO = all(importlib.util.find_spec(m) for m in ("geopandas", "shapely", "pyproj"))
HAVE_RASTER = HAVE_GEO and all(importlib.util.find_spec(m) for m in ("rasterio", "numpy"))


@unittest.skipUnless(HAVE_GEO, "geopandas/shapely/pyproj not installed")
class GisTest(unittest.TestCase):
    def setUp(self):
        import geopandas as gpd
        from shapely.geometry import box

        from regional_classifier import gis
        self.gis, self.gpd, self.box = gis, gpd, box
        # 経度 135.0〜135.01 / 緯度 34.0〜34.01 の正方形 2 つ（JGD2011）
        self.towns = gpd.GeoDataFrame(
            {"town_key": ["27203000101", "27203000201", "27203000901"]},
            geometry=[box(135.00, 34.00, 135.01, 34.01), box(135.01, 34.00, 135.02, 34.01),
                      box(135.05, 34.05, 135.05, 34.05)],
            crs="EPSG:6668")

    def test_overlap_ratio_half_and_zero(self):
        polys = self.gpd.GeoDataFrame(geometry=[self.box(135.005, 33.99, 135.01, 34.02)], crs="EPSG:6668")
        res = {k: f for k, _, f in self.gis.overlap_ratio(self.towns, polys, "agri_settlement_overlap")}
        self.assertAlmostEqual(res["27203000101"].value, 0.5, places=3)
        self.assertEqual(res["27203000201"].state, "zero")
        self.assertEqual(res["27203000901"].state, "undefined")  # 面積0（海域・境界外を想定）

    def test_outside_coverage_is_missing_not_zero(self):
        polys = self.gpd.GeoDataFrame(geometry=[self.box(135.005, 34.0, 135.01, 34.01)], crs="EPSG:6668")
        coverage = self.gpd.GeoDataFrame(geometry=[self.box(135.0, 34.0, 135.01, 34.01)], crs="EPSG:6668")
        res = {k: f for k, _, f in self.gis.overlap_ratio(self.towns, polys, "x", coverage=coverage)}
        self.assertEqual(res["27203000101"].state, "observed")
        self.assertEqual(res["27203000201"].state, "missing")

    def test_reprojected_input_gives_same_ratio(self):
        polys = self.gpd.GeoDataFrame(geometry=[self.box(135.005, 33.99, 135.01, 34.02)], crs="EPSG:6668")
        res = {k: f for k, _, f in self.gis.overlap_ratio(self.towns.to_crs("EPSG:6674"),
                                                          polys.to_crs("EPSG:3857"), "x")}
        self.assertAlmostEqual(res["27203000101"].value, 0.5, places=3)

    def test_category_ratios(self):
        lu = self.gpd.GeoDataFrame(
            {"code": ["0100", "0500"]},
            geometry=[self.box(135.0, 34.0, 135.0025, 34.01), self.box(135.0025, 34.0, 135.02, 34.01)],
            crs="EPSG:6668")
        res = {(k, n): f for k, n, f in self.gis.category_area_ratios(
            self.towns.iloc[:2], lu, "code", {"farmland_ratio": ["0100"], "forest_ratio": ["0500"]})}
        self.assertAlmostEqual(res[("27203000101", "farmland_ratio")].value, 0.25, places=3)
        self.assertAlmostEqual(res[("27203000101", "forest_ratio")].value, 0.75, places=3)
        self.assertEqual(res[("27203000201", "farmland_ratio")].state, "zero")

    @unittest.skipUnless(HAVE_RASTER, "rasterio not installed")
    def test_dem_stats(self):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dem.tif"
            res = 0.0005
            cols = np.arange(40)
            data = np.tile(cols * 10.0, (20, 1)).astype("float32")  # 東向きに 1 画素 10 m 上昇
            with rasterio.open(path, "w", driver="GTiff", height=20, width=40, count=1, dtype="float32",
                               crs="EPSG:6668", transform=from_origin(135.0, 34.01, res, res)) as dst:
                dst.write(data, 1)
            out = {(k, n): f for k, n, f in self.gis.dem_stats(self.towns.iloc[:2], str(path))}
        elev = out[("27203000101", "elevation_mean_m")]
        self.assertEqual(elev.state, "observed")
        slope = out[("27203000101", "slope_mean_deg")].value
        # 10 m / (0.0005° × 111320 × cos34°) ≈ 0.217 → 約 12.2 度
        self.assertAlmostEqual(slope, 12.2, delta=0.5)


if __name__ == "__main__":
    unittest.main()
