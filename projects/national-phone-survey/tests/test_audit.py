"""v1.2 監査スクリプトのテスト（受入項目 13、監査項目 A01〜A10）。架空のv1.2風DBを使う。"""

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import FAKE_V12_CONFIG, ROOT, build_fake_v12

sys.path.insert(0, str(ROOT / "audit"))
import audit_v12  # noqa: E402
import backup_and_verify  # noqa: E402
import inspect_schema  # noqa: E402


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "data" / "index.sqlite3"
        self.db.parent.mkdir()
        build_fake_v12(self.db)
        self.raw = self.dir / "raw"
        self.raw.mkdir()
        (self.raw / "06.xls").write_bytes(b"original")

    def test_audit_detects_planted_issues_without_modifying_db(self):
        before, mtime = digest(self.db), os.stat(self.db).st_mtime_ns
        report = audit_v12.run(self.db, FAKE_V12_CONFIG, self.dir / "audit")
        self.assertEqual((digest(self.db), os.stat(self.db).st_mtime_ns), (before, mtime))
        c = report["checks"]
        self.assertTrue(c["A10_preservation"]["db_unchanged"])
        self.assertEqual(c["A01_pairs"]["verdicts"],
                         {"ok": 2, "non_adjacent": 1, "cross_series": 1})
        self.assertEqual(c["A06_urls"]["url_groups_with_multiple_versions"], 1)
        self.assertEqual(c["A06_urls"]["content_hash_shared_by_versions"], 1)
        self.assertEqual(c["A09_same_date"]["content_differs"], 1)
        self.assertEqual(c["A07_fetch"]["urls_never_succeeded"], 1)
        ch = c["A02_A05_changes"]
        self.assertEqual(ch["change_rows_gross"], 5)
        self.assertEqual(ch["distinct_codes_changed"], 3)
        self.assertEqual(ch["distinct_codes_newly_in_use"], 2)  # 変更行数(5) ≠ 新規使用局番数(2)
        self.assertEqual(ch["codes_changed_in_multiple_pairs"], 2)
        self.assertEqual(ch["cosmetic_only_rows"], 1)
        self.assertEqual(ch["duplicate_keys_within_version"], 1)
        rec = c["A02_recorded_vs_recomputed"]
        self.assertEqual((rec["recorded_duplicate_rows"], rec["only_in_recompute"]), (1, 2))
        mism = {r["metric"]: r["match"] for r in report["reported_vs_recomputed"]}
        self.assertEqual(mism, {"comparable_pairs": False, "all_change_rows": False})
        self.assertTrue((self.dir / "audit" / "audit_report.md").exists())

    def test_audit_refuses_existing_output_dir(self):
        (self.dir / "audit").mkdir()
        with self.assertRaises(FileExistsError):
            audit_v12.run(self.db, FAKE_V12_CONFIG, self.dir / "audit")

    def test_missing_sections_are_skipped(self):
        report = audit_v12.run(self.db, {"in_use_values": ["使用中"]}, self.dir / "audit")
        self.assertEqual(report["checks"]["A01_pairs"]["status"], "SKIPPED")
        self.assertEqual(report["checks"]["A07_fetch"]["status"], "SKIPPED")

    def test_backup_restore_and_manifest(self):  # 受入13
        before = digest(self.db)
        report = backup_and_verify.run(self.db, self.raw, self.dir / "backup1", None)
        self.assertTrue(report["ok"], report)
        self.assertEqual(digest(self.db), before)
        self.assertEqual(report["restore_mismatches"], [])
        self.assertEqual(report["tables"]["designation_rows"]["rows"], 12)
        (self.raw / "06.xls").write_bytes(b"tampered")
        report2 = backup_and_verify.run(self.db, self.raw, self.dir / "backup2",
                                        self.dir / "backup1" / "raw_manifest.csv")
        self.assertEqual(report2["manifest_diff"]["changed"], ["06.xls"])
        self.assertFalse(report2["ok"])

    def test_inspect_schema_outputs_structure_only(self):
        text = inspect_schema.describe(self.db)
        self.assertIn("designation_rows", text)
        self.assertNotIn("ＮＴＴ", text)


if __name__ == "__main__":
    unittest.main()
