"""地域データの取込・GIS・評価（受入 5, 9, 14）。"""

import importlib.util
import json
import random
import tempfile
import unittest
from pathlib import Path

from support import BV, YEAR, db, seed_regions

from landline_atlas.cli import main as atlas
from landline_atlas.evaluate.baselines import compare, predict, samples
from landline_atlas.evidence import facilities
from landline_atlas.ingest import census
from landline_atlas.regions.features import Feature, add_source, new_load, put

HAVE_GEO = all(importlib.util.find_spec(m) for m in ("geopandas", "shapely", "pyproj"))
HAVE_RASTER = HAVE_GEO and all(importlib.util.find_spec(m) for m in ("rasterio", "numpy"))


class CensusImportTest(unittest.TestCase):
    def test_estat_style_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "towns.csv").write_text(
                "KEY_CODE,CITY_CODE,S_NAME,HTKSYORI,HTKSAKI\n"
                "01101000101,011011,一丁目,0,\n01101000102,01101,二丁目,1,01101000101\n01101000103,01101,三丁目,2,\n",
                encoding="cp932")
            (d / "pop.csv").write_text(
                "KEY_CODE,T001101001,T001101034\n,総数,65歳以上\n01101000101,1,234,X\n01101000103,-,…\n"
                .replace("1,234", "\"1,234\""), encoding="utf-8")
            mapping = {"town_columns": {"town_key": "KEY_CODE", "municipality_code": "CITY_CODE", "name": "S_NAME",
                                        "htksyori": "HTKSYORI", "htksaki": "HTKSAKI"},
                       "key_column": "KEY_CODE", "features": {"T001101001": "pop_total", "T001101034": "pop_65plus"}}
            conn = db(d)
            add_source(conn, "c", "census")
            self.assertEqual(census.import_towns(conn, d / "towns.csv", mapping, YEAR, BV, "c"), 3)
            census.import_features(conn, d / "pop.csv", mapping, YEAR, BV, "c")
            towns = {r["town_key"]: (r["secrecy"], r["merged_into"]) for r in conn.execute("SELECT * FROM regions.town")}
            self.assertEqual(towns["01101000102"], ("merged_into_other", "01101000101"))
            self.assertEqual(towns["01101000103"], ("merge_target", None))
            feats = {(r["town_key"], r["feature"]): (r["value"], r["state"])
                     for r in conn.execute("SELECT * FROM regions.town_feature")}
            self.assertEqual(feats[("01101000101", "pop_total")], (1234.0, "observed"))
            self.assertEqual(feats[("01101000101", "pop_65plus")], (None, "suppressed"))
            self.assertEqual(feats[("01101000103", "pop_total")], (0.0, "zero"))
            self.assertEqual(feats[("01101000103", "pop_65plus")], (None, "missing"))
            conn.close()


def gold_db(tmp: Path):
    conn = db(tmp)
    rng = random.Random(3)
    add_source(conn, "s", "synthetic")
    load = new_load(conn, "census", "s")
    for m in range(12):
        muni = f"{27 + m % 2}1{m:02d}"
        for t in range(6):
            key = f"{muni}{t:06d}"
            label = "R2" if (m + t) % 2 else "R4"
            conn.execute("INSERT INTO regions.town VALUES (?,?,?,?,?,?,?,?,?)",
                         (key, YEAR, BV, muni[:2], muni, None, "none", None, "s"))
            det = (0.8 if label == "R2" else 0.2) + rng.uniform(-0.15, 0.15)
            vals = {"hh_in_housing": 100, "hh_detached": round(100 * det), "hh_apartment": 100 - round(100 * det),
                    "pop_total": 250, "pop_age_known": 250, "pop_65plus": 60, "area_km2": 0.2}
            put(conn, load, YEAR, BV, [(key, k, Feature.of(v, "s")) for k, v in vals.items()])
            conn.execute("INSERT INTO regions.gold_label VALUES (?,?,?,?,?,?,?,?)",
                         (key, YEAR, BV, "R", label, "tester", "synthetic", "2026-10-19"))
            if t % 3 == 0:
                facilities.add(conn, facility_name=f"施設{key}", facility_type="shop", area_code="06",
                               local_code=str(6300 + m), address="架空", town=key, census_year=YEAR,
                               boundary_version=BV, source_url="https://example.invalid", observed_on="2026-10-01")
    return conn


class EvaluateTest(unittest.TestCase):
    def test_not_evaluable_without_gold(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db(Path(tmp))
            seed_regions(conn)
            self.assertEqual(compare(conn, "R", YEAR, BV)["status"], "not_evaluable")
            conn.close()

    def test_grouped_cv_and_holdout(self):  # 受入14
        with tempfile.TemporaryDirectory() as tmp:
            conn = gold_db(Path(tmp))
            for group_by in ("municipality", "town"):
                data = samples(conn, "R", "B2", YEAR, BV, group_by)
                preds = predict(data, "B2", 4, ())
                self.assertEqual(sorted(d["key"] for d, _ in preds), sorted(d["key"] for d in data))
            r = compare(conn, "R", YEAR, BV, k=4, rounds=100)
            self.assertEqual(r["status"], "evaluated")
            self.assertGreater(r["baselines"]["B0"]["top1"], 0.8)
            for c in r["comparisons"].values():
                self.assertIn(c["verdict"], ("improved", "worse", "no_significant_difference"))
            h = compare(conn, "R", YEAR, BV, holdout=("28",), rounds=20)
            self.assertEqual((h["status"], h["baselines"]["B0"]["n"]), ("evaluated", 36))
            self.assertEqual(compare(conn, "R", YEAR, BV, holdout=("27", "28"))["status"], "not_evaluable")
            conn.close()


class CliTest(unittest.TestCase):
    def test_cli_offline_flow(self):
        from support import V2023, xlsx
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            f = d / "06.xlsx"
            f.write_bytes(xlsx("令和5年4月1日現在", V2023))
            data = str(d / "data")
            self.assertEqual(atlas(["--data-dir", data, "update", "--offline", "--import", f"{f}:06"]), 0)
            self.assertEqual(atlas(["--data-dir", data, "qa"]), 0)
            self.assertEqual(atlas(["--data-dir", data, "backup"]), 0)
            runs = list((d / "data" / "runs").iterdir())
            m = json.loads((runs[0] / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(m["stages"]["history"]["selected_versions"], 1)


@unittest.skipUnless(HAVE_GEO, "geopandas/shapely/pyproj not installed")
class GisTest(unittest.TestCase):
    def setUp(self):
        import geopandas as gpd
        from shapely.geometry import box
        from landline_atlas.regions import gis
        self.gpd, self.box, self.gis = gpd, box, gis
        self.towns = gpd.GeoDataFrame({"town_key": ["a", "b", "c"]},
                                      geometry=[box(135.0, 34.0, 135.01, 34.01), box(135.01, 34.0, 135.02, 34.01),
                                                box(135.05, 34.05, 135.05, 34.05)], crs="EPSG:6668")

    def test_overlap_states(self):
        polys = self.gpd.GeoDataFrame(geometry=[self.box(135.005, 33.99, 135.01, 34.02)], crs="EPSG:6668")
        r = {k: f for k, _, f in self.gis.overlap(self.towns, polys, "x")}
        self.assertAlmostEqual(r["a"].value, 0.5, places=3)
        self.assertEqual((r["b"].state, r["c"].state), ("zero", "undefined"))
        cover = self.gpd.GeoDataFrame(geometry=[self.box(135.0, 34.0, 135.01, 34.01)], crs="EPSG:6668")
        r = {k: f for k, _, f in self.gis.overlap(self.towns.iloc[:2], polys, "x", coverage=cover)}
        self.assertEqual(r["b"].state, "missing")
        r = {k: f for k, _, f in self.gis.overlap(self.towns.to_crs("EPSG:6674"), polys.to_crs("EPSG:3857"), "x")}
        self.assertAlmostEqual(r["a"].value, 0.5, places=3)

    @unittest.skipUnless(HAVE_RASTER, "rasterio not installed")
    def test_dem(self):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "dem.tif"
            with rasterio.open(p, "w", driver="GTiff", height=20, width=40, count=1, dtype="float32",
                               crs="EPSG:6668", transform=from_origin(135.0, 34.01, 0.0005, 0.0005)) as dst:
                dst.write(np.tile(np.arange(40) * 10.0, (20, 1)).astype("float32"), 1)
            r = {(k, n): f for k, n, f in self.gis.dem_stats(self.towns.iloc[:1], str(p))}
        self.assertAlmostEqual(r[("a", "slope_mean_deg")].value, 12.2, delta=0.5)


if __name__ == "__main__":
    unittest.main()
