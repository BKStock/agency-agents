"""番号側：正規化・取得・原本保管・解析・版選択・差分・品質検査。"""

import json
import tempfile
import unittest
from pathlib import Path

from support import (PAGE, V2023, V2024, V2025, FakeTransport, db, layout, official_routes, wayback_routes, xlsx,
                     zipped)

from landline_atlas import normalize
from landline_atlas.acquire import archive, sources
from landline_atlas.acquire.http import Fetcher
from landline_atlas.history import diff, versions
from landline_atlas.ingest import designation
from landline_atlas.qa import checks


class NormalizeTest(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(normalize.municipality_code("131016"), "13101")
        self.assertEqual(normalize.municipality_code("011011"), "01101")
        with self.assertRaises(normalize.CodeError):
            normalize.municipality_code("131017")
        with self.assertRaises(normalize.CodeError):
            normalize.town_key(1101000101.0)
        self.assertEqual(normalize.prefecture_code(1), "01")

    def test_phone_part_repairs_visibly(self):
        self.assertEqual(normalize.phone_part(6.0, "a", leading_zero=True), ("06", True))
        self.assertEqual(normalize.phone_part("０６", "a", leading_zero=True), ("06", False))
        self.assertEqual(normalize.phone_part(6200, "l", leading_zero=False), ("6200", False))
        with self.assertRaises(normalize.CodeError):
            normalize.phone_part("※注記", "a", leading_zero=True)

    def test_dates(self):
        self.assertEqual(str(normalize.parse_japanese_date("令和6年4月1日現在")), "2024-04-01")
        self.assertEqual(str(normalize.parse_japanese_date("令和元年5月1日")), "2019-05-01")
        self.assertEqual(str(normalize.parse_japanese_date("平成31年3月31日")), "2019-03-31")
        self.assertEqual(str(normalize.parse_japanese_date("2025/01/01 現在")), "2025-01-01")
        self.assertIsNone(normalize.parse_japanese_date("基準日なし"))

    def test_stat_values(self):
        self.assertEqual(normalize.stat_value("-"), (0.0, "zero"))
        self.assertEqual(normalize.stat_value("X"), (None, "suppressed"))
        self.assertEqual(normalize.stat_value(""), (None, "missing"))
        self.assertEqual(normalize.stat_value("1,234"), (1234.0, "observed"))

    def test_canonical_url(self):
        urls = ["https://www.soumu.go.jp/a/06.xls",
                "http://web.archive.org/web/20240105000000/https://www.soumu.go.jp/a/06.xls",
                "https://web.archive.org/web/20240105000000id_/http://WWW.soumu.go.jp/a/06.xls/"]
        self.assertEqual({normalize.canonical_url(u) for u in urls}, {"www.soumu.go.jp/a/06.xls"})


class FetchTest(unittest.TestCase):
    def test_retry_backoff_and_logging(self):
        t = FakeTransport({"https://x.invalid/robots.txt": [(404, b"")],
                           "https://x.invalid/f": [OSError("reset"), (503, b""), (200, b"ok")]})
        sleeps, log = [], []
        f = Fetcher(t, user_agent="ua", min_interval=0, sleep=sleeps.append, log=lambda *a: log.append(a))
        r = f.get("https://x.invalid/f")
        self.assertEqual(r.body, b"ok")
        self.assertEqual(sleeps, [2.0, 4.0])
        self.assertEqual([a[2] for a in log], [None, 503, 200])

    def test_gives_up_and_respects_robots(self):
        t = FakeTransport({"https://x.invalid/robots.txt": [(200, b"User-agent: *\nDisallow: /private\n")],
                           "https://x.invalid/f": [(500, b"")]})
        sleeps = []
        f = Fetcher(t, user_agent="ua", min_interval=0, sleep=sleeps.append)
        self.assertIsNone(f.get("https://x.invalid/f"))
        self.assertEqual(sleeps, [2.0, 4.0, 8.0])
        self.assertIsNone(f.get("https://x.invalid/private/a"))
        self.assertNotIn("https://x.invalid/private/a", t.calls)

    def test_rate_limit_per_host(self):
        now = [0.0]
        sleeps = []
        t = FakeTransport({"https://x.invalid/robots.txt": [(404, b"")], "https://x.invalid/a": [(200, b"1")]})
        f = Fetcher(t, user_agent="ua", min_interval=3.0, sleep=lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s)),
                    clock=lambda: now[0])
        f.get("https://x.invalid/a")
        f.get("https://x.invalid/a")
        self.assertTrue(all(abs(s - 3.0) < 1e-9 for s in sleeps) and sleeps)


class ArchiveIngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.raw = self.dir / "raw"
        self.conn = db(self.dir)
        self.addCleanup(self.conn.close)

    def store(self, body, url, origin="official", ts=None, series="06"):
        return archive.store(self.conn, self.raw, body, url=url, origin=origin, series=series, wayback_ts=ts)

    def test_content_addressed_dedup_and_tamper_detection(self):
        body = xlsx("令和7年1月1日現在", V2025)
        sha, new = self.store(body, "https://www.soumu.go.jp/main_content/06.xlsx")
        sha2, new2 = self.store(body, "https://web.archive.org/web/20250101id_/https://www.soumu.go.jp/main_content/06.xlsx",
                                origin="wayback", ts="20250101")
        self.assertEqual((sha, new, new2), (sha2, True, False))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM numbers.raw_file").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM numbers.raw_source").fetchone()[0], 2)
        path = self.raw / self.conn.execute("SELECT stored_path FROM numbers.raw_file").fetchone()[0]
        path.write_bytes(b"tampered")
        with self.assertRaises(archive.ArchiveCorruption):
            archive.read(self.conn, self.raw, sha)
        q08 = [c for c in checks.run_checks(self.conn, self.raw) if c.check_id == "Q08_raw_sha256"][0]
        self.assertFalse(q08.passed)

    def test_parse_xlsx_with_repairs(self):
        tables = designation.parse_file("06.xlsx", xlsx("令和6年4月1日現在", V2024), layout())
        self.assertEqual(len(tables), 1)
        t = tables[0]
        self.assertEqual(str(t.as_of_date), "2024-04-01")
        self.assertEqual([(r["area_code"], r["local_code"]) for r in t.rows],
                         [("06", "6200"), ("06", "6201"), ("06", "6202")])
        self.assertEqual(t.rows[0]["repaired"], "leading_zero:area_code")
        self.assertIn("fill_down:area_code", t.rows[1]["repaired"])
        self.assertEqual(t.skipped_rows, 1)  # 注記行

    def test_zip_and_unknown_layout(self):
        tables = designation.parse_file("06.zip", zipped("in/06.xlsx", xlsx("令和6年4月1日現在", V2024)), layout())
        self.assertEqual(tables[0].member, "in/06.xlsx")
        from openpyxl import Workbook
        import io
        wb = Workbook()
        wb.active.append(["電話番号", "地域"])
        buf = io.BytesIO()
        wb.save(buf)
        with self.assertRaises(designation.UnknownLayout):
            designation.parse_file("x.xlsx", buf.getvalue(), layout())

    def ingest_three(self):
        for ts, rows, text in (("2023", V2023, "令和5年4月1日現在"), ("2024", V2024, "令和6年4月1日現在"),
                               ("2025", V2025, "令和7年1月1日現在")):
            sha, _ = self.store(xlsx(text, rows), f"https://www.soumu.go.jp/main_content/06_{ts}.xlsx")
            designation.ingest(self.conn, self.raw, sha, "06", layout())

    def test_versions_and_diffs_separate_gross_and_distinct(self):
        self.ingest_three()
        versions.select_versions(self.conn)
        self.assertEqual(diff.compute_diffs(self.conn, layout()["in_use_values"]), 2)
        self.assertEqual(diff.compute_diffs(self.conn, layout()["in_use_values"]), 0)  # 再計算しない
        s = diff.summary(self.conn)
        self.assertEqual(s["pairs"], 2)
        # 2023→2024: 6201 使用状況変更（→使用中）, 6202 追加, 6203 削除 / 2024→2025: 6200 表記ゆれ, 6201 事業者変更
        self.assertEqual(s["kinds"], {"status_changed": 1, "added": 1, "removed": 1, "cosmetic_only": 1,
                                      "carrier_changed": 1})
        self.assertEqual(s["change_rows_gross"], 5)
        self.assertEqual(s["distinct_codes_changed"], 4)
        self.assertEqual(s["distinct_codes_became_in_use"], 2)
        self.assertEqual(s["codes_changed_in_multiple_pairs"], 1)
        results = {c.check_id: c for c in checks.run_checks(self.conn, self.raw)}
        self.assertTrue(all(c.passed for c in results.values() if c.severity == "error"), results)
        self.assertEqual(results["Q11_parse_repairs"].detail["repaired_rows"].get("leading_zero:area_code"), 1)

    def test_same_date_different_content_is_explained(self):
        self.ingest_three()
        other = xlsx("令和7年1月1日現在", V2025[:2])
        sha, _ = self.store(other, "https://web.archive.org/web/20250102id_/https://www.soumu.go.jp/x.xlsx",
                            origin="wayback", ts="20250102")
        designation.ingest(self.conn, self.raw, sha, "06", layout())
        decisions = {d["as_of_date"]: d for d in versions.select_versions(self.conn)}
        d = decisions["2025-01-01"]
        self.assertTrue(d["reason"].startswith("content_differs:2;prefer_origin:official"))
        self.assertEqual(len(d["candidates"]), 2)
        q09 = [c for c in checks.run_checks(self.conn, self.raw) if c.check_id == "Q09_same_date_selection"][0]
        self.assertTrue(q09.passed)
        self.assertEqual(len(q09.detail["same_date_different_content"]), 1)

    def test_duplicate_keys_and_repeated_content_are_flagged(self):
        dup = V2025 + [["06", "6202", "A27-1", "大阪", "KDDI", "使用中"]]
        sha, _ = self.store(xlsx("令和7年1月1日現在", dup), "https://www.soumu.go.jp/main_content/06_dup.xlsx")
        designation.ingest(self.conn, self.raw, sha, "06", layout())
        sha, _ = self.store(xlsx("令和7年7月1日現在", dup), "https://www.soumu.go.jp/main_content/06_same.xlsx")
        designation.ingest(self.conn, self.raw, sha, "06", layout())  # 基準日だけ違う同一内容
        versions.select_versions(self.conn)
        diff.compute_diffs(self.conn, layout()["in_use_values"])
        res = {c.check_id: c for c in checks.run_checks(self.conn, self.raw)}
        self.assertFalse(res["Q04_duplicates_and_multi_period"].passed)
        self.assertEqual(len(res["Q04_duplicates_and_multi_period"].detail["duplicate_keys_in_selected_versions"]), 2)
        self.assertFalse(res["Q06_duplicate_content"].passed)

    def test_append_only(self):
        self.ingest_three()
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM numbers.designation_row")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE numbers.version SET as_of_date = '2000-01-01'")


class CollectTest(unittest.TestCase):
    def test_official_and_wayback_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db(Path(tmp))
            raw = Path(tmp) / "raw"
            cfg = json.loads((Path(__file__).parent.parent / "config" / "sources.json").read_text(encoding="utf-8"))
            body25 = xlsx("令和7年1月1日現在", V2025)
            body24 = xlsx("令和6年4月1日現在", V2024)
            routes = {**official_routes(body25), **wayback_routes({"20240410000000": body24, "20250110000000": body25})}
            t = FakeTransport(routes)
            f = Fetcher(t, user_agent="ua", min_interval=0, sleep=lambda s: None,
                        log=sources.make_logger(conn, "official"))
            got = sources.collect_official(f, conn, raw, cfg)
            self.assertEqual([(c.series, c.origin) for c in got], [("06", "official")])
            wb = sources.collect_wayback(f, conn, raw, cfg)
            self.assertEqual(len(wb), 2)
            self.assertEqual(sum(c.is_new for c in wb), 1)  # 2025 版は公式取得と同一内容
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM numbers.raw_file").fetchone()[0], 2)
            again = sources.collect_wayback(f, conn, raw, cfg)
            self.assertEqual(again, [])  # 取得済みの過去版は再取得しない
            failures = conn.execute("SELECT COUNT(*) FROM numbers.fetch_log WHERE status = 503").fetchone()[0]
            self.assertEqual(failures, 1)
            conn.close()

    def test_discover_links_series(self):
        html = "<a href='/main_content/000777_03.xls'>03</a><a href='x.pdf'>x</a>"
        links = sources.discover_links(html, PAGE, [{"regex": r"/main_content/[^/]*(?P<series>0[1-9])[^/]*\.xls$"}])
        self.assertEqual([(l.url, l.series) for l in links],
                         [("https://www.soumu.go.jp/main_content/000777_03.xls", "03")])


if __name__ == "__main__":
    unittest.main()
