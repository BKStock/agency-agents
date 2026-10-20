"""12分類エンジン（受入 1, 2, 3, 5, 7, 8, 9, 12）とルール検証。"""

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from support import BV, CONFIG, YEAR, add_evidence, approved_rules, db, rules, seed_regions

from landline_atlas.classify import engine
from landline_atlas.classify.rules import CODES, RuleError, parse
from landline_atlas.regions.features import Feature, derive, new_load, put


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db(Path(self.tmp.name))
        self.addCleanup(self.conn.close)
        seed_regions(self.conn)
        self.run = engine.classify(self.conn, rules(), YEAR, BV)

    def st(self, town, run=None):
        return {r["class_code"]: r["label_status"] for r in self.conn.execute(
            "SELECT * FROM regions.regional_classification WHERE run_id = ? AND town_key = ?", (run or self.run, town))}

    def row(self, town, code, run=None):
        return self.conn.execute("SELECT * FROM regions.regional_classification WHERE run_id = ? AND town_key = ?"
                                 " AND class_code = ?", (run or self.run, town, code)).fetchone()

    def axis(self, town, ax):
        return self.conn.execute("SELECT * FROM regions.axis_result WHERE run_id = ? AND town_key = ? AND axis = ?",
                                 (self.run, town, ax)).fetchone()

    def test_all_twelve_rows_per_town(self):
        counts = {r[0] for r in self.conn.execute("SELECT COUNT(*) FROM regions.regional_classification"
                                                  " WHERE run_id = ? GROUP BY town_key", (self.run,))}
        self.assertEqual(counts, {12})

    def test_r_classes(self):  # 受入1
        for town, code in {"27100000201": "R1", "27100000101": "R2", "27100000301": "R3", "27100000401": "R4",
                           "27100000501": "R5", "27100000601": "R6"}.items():
            with self.subTest(code=code):
                self.assertEqual(self.st(town)[code], "candidate")

    def test_l_classes(self):  # 受入2
        for town, code in {"03215000100": "L1", "03215000200": "L2", "03215000300": "L3", "16201000100": "L4",
                           "20321000100": "L5"}.items():
            with self.subTest(code=code):
                self.assertEqual(self.st(town)[code], "candidate")
        self.assertEqual(self.st("03215000100")["L6"], "candidate")

    def test_no_confirmation_with_unapproved_rules(self):
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM regions.regional_classification WHERE run_id = ?"
                                           " AND label_status = 'confirmed'", (self.run,)).fetchone()[0], 0)

    def test_l1_l6_overlap_and_axes(self):  # 受入3
        l1, l6 = self.row("03215000100", "L1"), self.row("03215000100", "L6")
        self.assertEqual((l1["primary_flag"], l1["overlap_flag"], l6["primary_flag"], l6["overlap_flag"]), (1, 1, 0, 1))
        self.assertEqual(self.axis("16201000100", "R")["primary_class"], "R2")
        self.assertEqual(self.axis("16201000100", "L")["primary_class"], "L4")
        a = self.axis("27100000501", "R")  # R4 と R5 が拮抗
        self.assertEqual((a["state"], a["primary_class"]), ("on_hold", None))
        self.assertEqual(self.row("27100000201", "R2")["reason"], "excluded_by:R1")

    def test_missing_gis_never_classified(self):  # 受入5
        st = self.st("03215000400")
        for code in ("R1", "R2", "R6", "L1", "L2", "L3", "L4"):
            self.assertEqual(st[code], "insufficient_data", code)
        ev = json.loads(self.row("03215000400", "L1")["evidence_json"])
        self.assertIn({"feature": "agri_settlement_overlap", "state": "missing", "value": None},
                      [{k: c[k] for k in ("feature", "state", "value")} for c in ev["conditions"]])

    def test_r3_requires_cross_evidence(self):  # 受入7
        ar = approved_rules()
        run = engine.classify(self.conn, ar, YEAR, BV)
        self.assertIn("evidence_missing", self.row("27100000301", "R3", run)["reason"])
        add_evidence(self.conn, "27100000301", "census_tenure_marginal", "R3", "A")  # 周辺集計は証拠にならない
        add_evidence(self.conn, "27100000301", "tenure_by_structure_owner", "R3", "C")  # 等級Cも不可
        run = engine.classify(self.conn, ar, YEAR, BV)
        self.assertEqual(self.row("27100000301", "R3", run)["label_status"], "candidate")
        add_evidence(self.conn, "27100000301", "plateau_condo_use", "R3", "B")
        run = engine.classify(self.conn, ar, YEAR, BV)
        r3 = self.row("27100000301", "R3", run)
        self.assertEqual((r3["label_status"], r3["evidence_grade"]), ("confirmed", "B"))

    def test_l5_not_confirmed_without_evidence(self):  # 受入8
        run = engine.classify(self.conn, approved_rules(), YEAR, BV)
        self.assertEqual(self.row("20321000100", "L5", run)["label_status"], "candidate")
        self.assertEqual(self.row("27100000201", "R1", run)["label_status"], "confirmed")

    def test_secrecy(self):  # 受入9
        self.assertEqual(set(self.st("27100000701").values()), {"insufficient_data"})
        self.assertEqual({(r[0], r[1]) for r in self.conn.execute(
            "SELECT label_status, reason FROM regions.regional_classification WHERE run_id = ? AND town_key = ?",
            (self.run, "27100000801"))}, {("undeterminable", "merged_into:27100000601")})
        self.assertIn("includes_merged_areas", self.row("27100000601", "R6")["evidence_json"])

    def test_reproduce_after_data_and_rule_change(self):  # 受入12
        before = [tuple(r) for r in self.conn.execute(
            "SELECT * FROM regions.regional_classification WHERE run_id = ? ORDER BY town_key, class_code", (self.run,))]
        put(self.conn, new_load(self.conn, "gis", "gis"), YEAR, BV,
            [("27100000101", "land_price_pref_pct", Feature.of(0.99, "gis"))])
        run2 = engine.classify(self.conn, approved_rules(), YEAR, BV)
        self.assertEqual(self.st("27100000101", run2)["R1"], "confirmed")
        self.assertTrue(engine.reproduce(self.conn, self.run)["reproduced"])
        self.assertTrue(engine.reproduce(self.conn, run2)["reproduced"])
        after = [tuple(r) for r in self.conn.execute(
            "SELECT * FROM regions.regional_classification WHERE run_id = ? ORDER BY town_key, class_code", (self.run,))]
        self.assertEqual(before, after)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE regions.regional_classification SET label_status = 'confirmed'")


class RulesTest(unittest.TestCase):
    def base(self):
        return json.loads((CONFIG / "rules_v1.json").read_text(encoding="utf-8"))

    def test_invariants(self):
        parse(self.base())
        for mutate in (
            lambda d: d["classes"].pop("L5"),
            lambda d: d["classes"]["R3"].update(confirm_evidence=[]),
            lambda d: d["classes"]["R4"].update(confirm_evidence=["census_tenure_marginal"]),
            lambda d: d["classes"]["L5"].pop("confirm_evidence"),
            lambda d: d["classes"]["L6"].update(role="main"),
            lambda d: d["classes"]["R2"].update(approved=True),
            lambda d: d["classes"]["R2"].update(excludes=["R2"]),
        ):
            data = copy.deepcopy(self.base())
            mutate(data)
            with self.assertRaises(RuleError):
                parse(data)

    def test_features_missing_propagates(self):
        f = derive({"hh_detached": Feature.of(10), "hh_main": Feature(None, "suppressed")})
        self.assertEqual(f["detached_ratio"].state, "suppressed")
        self.assertEqual(derive({"hh_detached": Feature.of(0), "hh_main": Feature.of(0)})["detached_ratio"].state,
                         "undefined")
        self.assertEqual(set(CODES), set(parse(self.base()).classes))


if __name__ == "__main__":
    unittest.main()
