"""ルール検証と正規化のテスト。"""

import copy
import json
import unittest

from fixtures import RULES_PATH

from regional_classifier import normalize
from regional_classifier.features import Feature, derive
from regional_classifier.rules import RuleError, parse_rules


def base():
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


class RuleValidationTest(unittest.TestCase):
    def test_shipped_rules_load_and_are_unapproved(self):
        rs = parse_rules(base())
        self.assertEqual(len(rs.classes), 12)
        self.assertFalse(any(r.approved for r in rs.classes.values()))
        self.assertLess(rs.evaluation_order.index("R1"), rs.evaluation_order.index("R2"))
        self.assertLess(rs.evaluation_order.index("L1"), rs.evaluation_order.index("L6"))

    def test_missing_class_rejected(self):
        data = base()
        del data["classes"]["L5"]
        with self.assertRaises(RuleError):
            parse_rules(data)

    def test_evidence_requirement_cannot_be_removed(self):
        for code in ("R3", "R4", "L5"):
            data = base()
            data["classes"][code]["confirm_requires_evidence"] = []
            with self.subTest(code=code), self.assertRaises(RuleError):
                parse_rules(data)

    def test_marginal_census_tables_cannot_confirm_r3(self):
        data = base()
        data["classes"]["R3"]["confirm_requires_evidence"] = ["census_tenure_marginal"]
        with self.assertRaises(RuleError):
            parse_rules(data)

    def test_l6_must_be_attribute_with_context(self):
        data = base()
        data["classes"]["L6"]["kind"] = "primary"
        with self.assertRaises(RuleError):
            parse_rules(data)

    def test_approval_requires_approved_thresholds(self):
        data = copy.deepcopy(base())
        data["classes"]["R2"]["approved"] = True
        with self.assertRaises(RuleError):
            parse_rules(data)

    def test_hash_changes_with_threshold(self):
        a = parse_rules(base())
        data = base()
        data["classes"]["R2"]["conditions"][0]["value"] = 0.71
        self.assertNotEqual(a.sha256, parse_rules(data).sha256)


class NormalizeTest(unittest.TestCase):
    def test_leading_zero_codes(self):
        self.assertEqual(normalize.prefecture_code("01"), "01")
        self.assertEqual(normalize.prefecture_code(1), "01")
        with self.assertRaises(normalize.CodeError):
            normalize.prefecture_code("1")
        with self.assertRaises(normalize.CodeError):
            normalize.municipality_code(1101.0)  # Excel 由来の float は拒否
        with self.assertRaises(normalize.CodeError):
            normalize.municipality_code("1101")

    def test_municipality_check_digit(self):
        self.assertEqual(normalize.municipality_code("131016"), "13101")   # 千代田区
        self.assertEqual(normalize.municipality_code("011002"), "01100")   # 札幌市
        self.assertEqual(normalize.municipality_code("011011"), "01101")   # 札幌市中央区（余り0）
        self.assertEqual(normalize.municipality_code("271276"), "27127")   # 大阪市北区
        with self.assertRaises(normalize.CodeError):
            normalize.municipality_code("131017")

    def test_town_key_keeps_zeros(self):
        self.assertEqual(normalize.town_key("01101000101"), "01101000101")
        self.assertEqual(normalize.town_key("1101000101", width=11), "01101000101")
        with self.assertRaises(normalize.CodeError):
            normalize.town_key(1101000101.0)

    def test_stat_values_distinguish_zero_missing_suppressed(self):
        self.assertEqual(normalize.parse_stat_value("-"), (0.0, "zero"))
        self.assertEqual(normalize.parse_stat_value("X"), (None, "suppressed"))
        self.assertEqual(normalize.parse_stat_value("…"), (None, "missing"))
        self.assertEqual(normalize.parse_stat_value(""), (None, "missing"))
        self.assertEqual(normalize.parse_stat_value("1,234"), (1234.0, "observed"))
        self.assertEqual(normalize.parse_stat_value("０"), (0.0, "zero"))

    def test_secrecy_state(self):
        self.assertEqual(normalize.secrecy_state("1", "27203000601"), ("merged_into_other", "27203000601"))
        self.assertEqual(normalize.secrecy_state("0", None), ("none", None))
        with self.assertRaises(ValueError):
            normalize.secrecy_state("1", "")
        with self.assertRaises(ValueError):
            normalize.secrecy_state("9", None)

    def test_wayback_url_canonicalization(self):
        same = {
            normalize.canonical_url("https://www.soumu.go.jp/a/06.xls"),
            normalize.canonical_url("http://web.archive.org/web/20240105000000/https://www.soumu.go.jp/a/06.xls"),
            normalize.canonical_url("https://web.archive.org/web/20240105000000id_/http://WWW.soumu.go.jp/a/06.xls/"),
        }
        self.assertEqual(same, {"www.soumu.go.jp/a/06.xls"})


class FeatureTest(unittest.TestCase):
    def test_derived_propagates_worst_state(self):
        f = derive({"hh_detached": Feature(10, "observed"), "hh_in_housing": Feature(None, "suppressed")})
        self.assertEqual(f["detached_ratio"].state, "suppressed")
        f = derive({"hh_detached": Feature(0, "zero"), "hh_in_housing": Feature(0, "zero")})
        self.assertEqual(f["detached_ratio"].state, "undefined")
        f = derive({"hh_detached": Feature(0, "zero"), "hh_in_housing": Feature(10, "observed")})
        self.assertEqual((f["detached_ratio"].value, f["detached_ratio"].state), (0.0, "zero"))
        self.assertEqual(derive({})["aging_rate"].state, "missing")


if __name__ == "__main__":
    unittest.main()
