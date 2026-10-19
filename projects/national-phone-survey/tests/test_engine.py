"""12分類エンジンの受入テスト（受入項目 1, 2, 3, 5, 7, 8, 9, 12）。"""

import json
import sqlite3
import unittest

from fixtures import VERSION, YEAR, add_condo_evidence, approved_rules, build_db, rules

from regional_classifier import store
from regional_classifier.engine import classify, reproduce
from regional_classifier.features import Feature
from regional_classifier.rules import CLASS_CODES


def statuses(conn, run_id, town_key):
    return {r["class_code"]: r["label_status"] for r in conn.execute(
        "SELECT class_code, label_status FROM regional_classification WHERE run_id = ? AND town_key = ?",
        (run_id, town_key))}


def row(conn, run_id, town_key, code):
    return conn.execute("SELECT * FROM regional_classification WHERE run_id = ? AND town_key = ? AND class_code = ?",
                        (run_id, town_key, code)).fetchone()


def axis(conn, run_id, town_key, ax):
    return conn.execute("SELECT * FROM town_axis_result WHERE run_id = ? AND town_key = ? AND axis = ?",
                        (run_id, town_key, ax)).fetchone()


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.conn = build_db()
        self.run = classify(self.conn, rules(), YEAR, VERSION, evaluated_at="2026-10-20T00:00:00+00:00")
        self.addCleanup(self.conn.close)

    def test_every_town_has_all_twelve_rows(self):
        counts = self.conn.execute("SELECT town_key, COUNT(*) FROM regional_classification WHERE run_id = ?"
                                   " GROUP BY town_key", (self.run,)).fetchall()
        self.assertTrue(counts)
        self.assertTrue(all(n == 12 for _, n in counts))

    def test_r_classes_individually(self):  # 受入1
        expect = {"27203000201": "R1", "27203000101": "R2", "27203000301": "R3", "27203000401": "R4",
                  "27203000501": "R5", "27203000601": "R6"}
        for town, code in expect.items():
            with self.subTest(town=town):
                self.assertEqual(statuses(self.conn, self.run, town)[code], "candidate")

    def test_l_classes_individually(self):  # 受入2
        expect = {"03215000100": "L1", "03215000200": "L2", "03215000300": "L3", "16201000100": "L4",
                  "20321000100": "L5", "03215000100#": "L6"}
        for town, code in expect.items():
            with self.subTest(town=town):
                self.assertEqual(statuses(self.conn, self.run, town.rstrip("#"))[code], "candidate")

    def test_unapproved_rules_never_confirm(self):
        n = self.conn.execute("SELECT COUNT(*) FROM regional_classification WHERE run_id = ?"
                              " AND label_status = 'confirmed'", (self.run,)).fetchone()[0]
        self.assertEqual(n, 0)

    def test_l1_and_l6_overlap(self):  # 受入3
        l1, l6 = row(self.conn, self.run, "03215000100", "L1"), row(self.conn, self.run, "03215000100", "L6")
        self.assertEqual((l1["label_status"], l6["label_status"]), ("candidate", "candidate"))
        self.assertEqual((l1["overlap_flag"], l6["overlap_flag"]), (1, 1))
        self.assertEqual(l1["primary_flag"], 1)
        self.assertEqual(l6["primary_flag"], 0)  # 属性クラスは主分類にならない
        self.assertIn("satisfied_by:L1", l6["evidence_json"])

    def test_r_and_l_axes_independent(self):
        self.assertEqual(axis(self.conn, self.run, "16201000100", "R")["primary_class"], "R2")
        self.assertEqual(axis(self.conn, self.run, "16201000100", "L")["primary_class"], "L4")

    def test_tie_leaves_primary_on_hold(self):
        st = statuses(self.conn, self.run, "27203000501")
        self.assertEqual((st["R4"], st["R5"]), ("candidate", "candidate"))
        a = axis(self.conn, self.run, "27203000501", "R")
        self.assertEqual((a["state"], a["primary_class"]), ("on_hold", None))

    def test_r2_excluded_when_r1_matches(self):
        r2 = row(self.conn, self.run, "27203000201", "R2")
        self.assertEqual((r2["label_status"], r2["reason"]), ("not_applicable", "excluded_by:R1"))

    def test_gis_missing_is_not_classified(self):  # 受入5
        st = statuses(self.conn, self.run, "03215000400")
        for code in ("L1", "L2", "L3", "L4", "R1", "R2", "R6"):
            self.assertEqual(st[code], "insufficient_data", code)
        self.assertEqual(axis(self.conn, self.run, "03215000400", "L")["state"], "insufficient_data")

    def test_r3_needs_tenure_by_structure_evidence(self):  # 受入7
        ar = approved_rules()
        run = classify(self.conn, ar, YEAR, VERSION)
        r3 = row(self.conn, run, "27203000301", "R3")
        self.assertEqual(r3["label_status"], "candidate")
        self.assertIn("evidence_missing", r3["reason"])
        add_condo_evidence(self.conn, grade="A")
        run2 = classify(self.conn, ar, YEAR, VERSION)
        r3 = row(self.conn, run2, "27203000301", "R3")
        self.assertEqual((r3["label_status"], r3["evidence_grade"]), ("confirmed", "A"))
        # 等級Cの証拠では確定しない
        conn = build_db()
        self.addCleanup(conn.close)
        add_condo_evidence(conn, grade="C")
        run3 = classify(conn, ar, YEAR, VERSION)
        self.assertEqual(row(conn, run3, "27203000301", "R3")["label_status"], "candidate")

    def test_l5_not_confirmed_from_household_stats(self):  # 受入8
        run = classify(self.conn, approved_rules(), YEAR, VERSION)
        l5 = row(self.conn, run, "20321000100", "L5")
        self.assertEqual(l5["label_status"], "candidate")
        self.assertIn("evidence_missing", l5["reason"])
        # 承認済みルールでは、証拠不要の分類は確定できる（確定経路の存在確認）
        self.assertEqual(row(self.conn, run, "27203000201", "R1")["label_status"], "confirmed")

    def test_secrecy_and_merged_areas(self):  # 受入9
        self.assertEqual(set(statuses(self.conn, self.run, "27203000701").values()), {"insufficient_data"})
        merged = self.conn.execute("SELECT DISTINCT label_status, reason FROM regional_classification"
                                   " WHERE run_id = ? AND town_key = '27203000801'", (self.run,)).fetchall()
        self.assertEqual([tuple(m) for m in merged], [("undeterminable", "merged_into:27203000601")])
        target = row(self.conn, self.run, "27203000601", "R6")
        self.assertIn("includes_merged_areas", target["evidence_json"])

    def test_missing_is_not_zero(self):
        r1 = row(self.conn, self.run, "03215000400", "R1")
        ev = json.loads(r1["evidence_json"])
        land = [c for c in ev["conditions"] if c["feature"] == "land_price_pref_pct"][0]
        self.assertEqual((land["value"], land["state"]), (None, "missing"))

    def test_rule_update_keeps_past_results_reproducible(self):  # 受入12
        before = [tuple(r) for r in self.conn.execute(
            "SELECT * FROM regional_classification WHERE run_id = ? ORDER BY town_key, class_code", (self.run,))]
        with self.conn:
            load = store.new_load(self.conn, "census", note="2025 update")
            store.put_features(self.conn, load, YEAR, VERSION,
                               [("27203000101", "land_price_pref_pct", Feature(0.99, "observed", "gis2020"))])
        run2 = classify(self.conn, approved_rules(), YEAR, VERSION)
        self.assertNotEqual(run2, self.run)
        self.assertEqual(statuses(self.conn, run2, "27203000101")["R1"], "confirmed")
        result = reproduce(self.conn, self.run)
        self.assertTrue(result["reproduced"], result)
        after = [tuple(r) for r in self.conn.execute(
            "SELECT * FROM regional_classification WHERE run_id = ? ORDER BY town_key, class_code", (self.run,))]
        self.assertEqual(before, after)
        current = self.conn.execute("SELECT DISTINCT run_id FROM current_classification").fetchall()
        self.assertEqual([r[0] for r in current], [run2])

    def test_results_are_append_only(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE regional_classification SET label_status = 'confirmed'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM regional_classification")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM town_feature")

    def test_class_codes_complete(self):
        codes = {r[0] for r in self.conn.execute("SELECT DISTINCT class_code FROM regional_classification")}
        self.assertEqual(codes, set(CLASS_CODES))


if __name__ == "__main__":
    unittest.main()
