"""関連性モデル評価のテスト（受入項目 14）。"""

import random
import unittest

from fixtures import VERSION, YEAR, build_db

from regional_classifier import store
from regional_classifier.evaluate import compare_baselines, cross_validate, load_samples
from regional_classifier.features import Feature
from regional_classifier.phone_link import add_facility_observation


def synthetic_gold_db():
    """12市区町村×各6町丁目に R2/R4 の正解ラベルを付けた合成DB。"""
    conn = store.open_db(":memory:")
    rng = random.Random(7)
    with conn:
        store.add_source(conn, "syn", "合成")
        load = store.new_load(conn, "census", source_id="syn")
        for m in range(12):
            muni = f"{27 + m % 2}1{m:02d}"  # 大阪府(27)と兵庫県(28)に交互に配置
            for t in range(6):
                key = f"{muni}{t:06d}"
                label = "R2" if (m + t) % 2 else "R4"
                store.add_town(conn, key, YEAR, VERSION, muni)
                detached = 0.8 if label == "R2" else 0.2
                detached += rng.uniform(-0.15, 0.15)
                feats = {"hh_in_housing": 100, "hh_detached": round(100 * detached),
                         "hh_apartment": 100 - round(100 * detached), "hh_owned": 60, "hh_rented_private": 40,
                         "hh_rented_public": 0, "hh_company_housing": 0, "pop_total": 250, "pop_age_known": 250,
                         "pop_65plus": 60, "pop_2010": 240, "area_km2": 0.2, "establishments": 5,
                         "households_general": 100, "did_flag": 1}
                store.put_features(conn, load, YEAR, VERSION,
                                   [(key, k, Feature(float(v), "zero" if v == 0 else "observed", "syn"))
                                    for k, v in feats.items()])
                conn.execute("INSERT INTO gold_label VALUES (?,?,?,?,?,?,?,?)",
                             (key, YEAR, VERSION, "R", label, "tester", "合成ラベル", "2026-10-20"))
                if t % 3 == 0:
                    add_facility_observation(conn, facility_name=f"施設{key}", facility_type="shop",
                                             area_code="06", local_code=f"{300 + m}", address="架空",
                                             town_key=key, census_year=YEAR, boundary_version=VERSION,
                                             source_url="https://example.invalid", observed_on="2026-10-01")
    return conn


class EvaluateTest(unittest.TestCase):
    def test_not_evaluable_without_gold(self):
        conn = build_db()
        self.addCleanup(conn.close)
        result = compare_baselines(conn, "R", YEAR, VERSION)
        self.assertEqual(result["status"], "not_evaluable")

    def test_grouped_split_has_no_leakage(self):
        conn = synthetic_gold_db()
        self.addCleanup(conn.close)
        for group_by in ("municipality", "town"):
            samples, _ = load_samples(conn, "R", "B2", YEAR, VERSION, group_by=group_by)
            preds = cross_validate(samples, "B2", k=4)
            self.assertEqual(len(preds), len(samples))  # 各標本はちょうど1回だけ評価される
            self.assertEqual(len({s.town_key for s, _ in preds}), len(samples))

    def test_compare_baselines_reports_metrics_and_verdicts(self):
        conn = synthetic_gold_db()
        self.addCleanup(conn.close)
        result = compare_baselines(conn, "R", YEAR, VERSION, k=4, bootstrap_rounds=100)
        self.assertEqual(result["status"], "evaluated")
        for b in ("B0", "B1", "B2"):
            m = result["baselines"][b]
            self.assertGreater(m["top1_accuracy"], 0.8)
            self.assertIn("R2", m["per_class"])
            self.assertIn("confusion", m)
        for comp in result["comparisons"].values():
            self.assertIn(comp["verdict"], ("improved", "worse", "no_significant_difference"))
            self.assertLessEqual(comp["ci95"][0], comp["ci95"][1])

    def test_prefecture_holdout(self):
        conn = synthetic_gold_db()
        self.addCleanup(conn.close)
        result = compare_baselines(conn, "R", YEAR, VERSION, holdout_prefectures=("28",), bootstrap_rounds=10)
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["baselines"]["B0"]["n"], 36)  # 兵庫県の標本だけを評価
        result = compare_baselines(conn, "R", YEAR, VERSION, holdout_prefectures=("27", "28"))
        self.assertEqual(result["status"], "not_evaluable")  # 学習データが無い


if __name__ == "__main__":
    unittest.main()
