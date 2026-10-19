"""検索・出力・電話番号連携のテスト（受入項目 4, 10, 11, 13 の一部）。"""

import csv
import hashlib
import tempfile
import unittest
from pathlib import Path

from fixtures import VERSION, YEAR, build_db, build_fake_v12, rules

from regional_classifier.engine import classify, input_hash, load_inputs
from regional_classifier.export import export_all
from regional_classifier.phone_link import add_facility_observation, import_designations
from regional_classifier.query import SearchFilter, search

SELECT_SQL = """SELECT area_code, local_code, number_area AS number_area_code, carrier, status AS usage_status,
                       base_date AS as_of_date, series_name AS series, url AS source_url, sha256 AS source_sha256
                FROM designation_rows JOIN source_versions ON source_versions.id = designation_rows.version_id
                WHERE version_id IN ('v1', 'v2')
                GROUP BY version_id, area_code, local_code"""


class QueryExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = build_db(Path(self.tmp.name) / "regional.sqlite3")
        self.addCleanup(self.conn.close)
        self.run = classify(self.conn, rules(), YEAR, VERSION)

    def test_osaka_r6_hidden_but_kept(self):  # 受入4
        stored = self.conn.execute("SELECT COUNT(*) FROM regional_classification WHERE class_code = 'R6'"
                                   " AND prefecture_code = '27'").fetchone()[0]
        res = search(self.conn, SearchFilter(prefecture="27", mode="residential"))
        self.assertNotIn("R6", {r["class_code"] for r in res})
        res_all = search(self.conn, SearchFilter(prefecture="27", mode="residential", include_r6=True))
        self.assertIn("R6", {r["class_code"] for r in res_all})
        plain = search(self.conn, SearchFilter(classes=["R6"]))
        self.assertEqual([r["town_key"] for r in plain], ["27203000601"])
        after = self.conn.execute("SELECT COUNT(*) FROM regional_classification WHERE class_code = 'R6'"
                                  " AND prefecture_code = '27'").fetchone()[0]
        self.assertEqual(stored, after)

    def test_l_classes_searchable_nationwide(self):
        res = search(self.conn, SearchFilter(axis="L"))
        self.assertEqual({r["class_code"] for r in res}, {"L1", "L2", "L3", "L4", "L5", "L6"})
        both = search(self.conn, SearchFilter(classes=["L1", "L6"], require_all_classes=True))
        self.assertEqual({r["town_key"] for r in both}, {"03215000100"})
        self.assertTrue(all(r["source_url"] for r in both))

    def test_status_filter_lists_pending(self):
        res = search(self.conn, SearchFilter(statuses=("insufficient_data",), prefecture="03"))
        self.assertIn("03215000400", {r["town_key"] for r in res})

    def test_export_reconciles_with_master(self):  # 受入10（合成データ）
        out = Path(self.tmp.name) / "export"
        result = export_all(self.conn, out)
        rec = result["reconciliation"]
        with open(out / "01_all_regions_master.csv", encoding="utf-8-sig") as fh:
            master = list(csv.DictReader(fh))
        for code, n in rec["per_class"].items():
            with open(out / f"02_class_{code}.csv", encoding="utf-8-sig") as fh:
                self.assertEqual(sum(1 for _ in csv.DictReader(fh)), n, code)
            self.assertEqual(sum(1 for m in master if m[f"status_{code}"] in ("confirmed", "candidate")), n)
        self.assertGreater(rec["gross_class_matches"], rec["unique_towns_matched"])  # 延べ ≠ 一意
        self.assertEqual(master[0]["town_key"][:2], "03")  # 先頭ゼロが保持されている
        self.assertTrue((out / "regional_classification.xlsx").exists())
        merged = [m for m in master if m["town_key"] == "27203000801"][0]
        self.assertEqual(merged["matched_class_count"], "0")

    def test_phone_link_is_read_only_and_independent(self):  # 受入11, 13
        v12 = Path(self.tmp.name) / "index.sqlite3"
        build_fake_v12(v12)
        digest = hashlib.sha256(v12.read_bytes()).hexdigest()
        before = input_hash(load_inputs(self.conn, YEAR, VERSION))
        _, n = import_designations(self.conn, v12, SELECT_SQL)
        self.assertGreater(n, 0)
        self.assertEqual(hashlib.sha256(v12.read_bytes()).hexdigest(), digest)
        self.conn.execute("INSERT INTO number_area_municipality VALUES ('A1', '27203', '06', 'テストMA', 'part',"
                          " '2020-01-01', NULL, NULL)")
        # 番号指定の取込は分類入力に影響しない（住宅・法人属性を番号から推定しない）
        self.assertEqual(before, input_hash(load_inputs(self.conn, YEAR, VERSION)))

        res = search(self.conn, SearchFilter(town="27203000101", local_code="200"))
        rel = res[0]["phone_relations"]
        levels = {r["relation_level"] for r in rel}
        self.assertEqual(levels, {"area", "no_observation"})
        self.assertTrue(all("未確認" in r["note"] for r in rel if r["relation_level"] == "area"))

        add_facility_observation(self.conn, facility_name="市立テスト小学校", facility_type="school",
                                 area_code="06", local_code="201", address="架空市戸建町1丁目",
                                 town_key="27203000101", census_year=YEAR, boundary_version=VERSION,
                                 verified=True, source_url="https://example.invalid/school",
                                 observed_on="2026-10-01")
        res = search(self.conn, SearchFilter(town="27203000101", local_code="201",
                                             relation_level="observed_verified"))
        self.assertEqual(res[0]["phone_relations"][0]["relation_level"], "observed_verified")
        with self.assertRaises(ValueError):
            add_facility_observation(self.conn, facility_name="x", facility_type="residence", area_code="06",
                                     local_code="201", address="x", source_url="https://example.invalid",
                                     observed_on="2026-10-01")

    def test_numeric_codes_rejected_on_import(self):
        v12 = Path(self.tmp.name) / "index.sqlite3"
        build_fake_v12(v12)
        bad = SELECT_SQL.replace("SELECT area_code,", "SELECT CAST(area_code AS INTEGER) AS area_code,")
        with self.assertRaises(ValueError):
            import_designations(self.conn, v12, bad)


if __name__ == "__main__":
    unittest.main()
