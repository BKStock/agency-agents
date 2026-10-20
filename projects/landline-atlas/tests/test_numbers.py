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
            designation.ingest(self.conn, self.raw, sha, layout())

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
        designation.ingest(self.conn, self.raw, sha, layout())
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
        designation.ingest(self.conn, self.raw, sha, layout())
        sha, _ = self.store(xlsx("令和7年7月1日現在", dup), "https://www.soumu.go.jp/main_content/06_same.xlsx")
        designation.ingest(self.conn, self.raw, sha, layout())  # 基準日だけ違う同一内容
        versions.select_versions(self.conn)
        diff.compute_diffs(self.conn, layout()["in_use_values"])
        res = {c.check_id: c for c in checks.run_checks(self.conn, self.raw)}
        self.assertFalse(res["Q04_duplicates_and_multi_period"].passed)
        self.assertEqual(len(res["Q04_duplicates_and_multi_period"].detail["duplicate_keys_in_selected_versions"]), 2)
        # 隣り合う日付の同一内容は「変更なしの再掲」として情報扱い（警告にしない）
        self.assertTrue(res["Q06_duplicate_content"].passed)
        self.assertEqual(len(res["Q06_duplicate_content"].detail["republished_unchanged"]), 1)

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
            self.assertEqual([(c.series, c.origin) for c in got], [("auto", "official")])  # 系統は中身から決める
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


# 北海道（01）と大阪（06）の表。総務省のような連番 ID のファイル名では、名前から地域は分からない
HOKKAIDO_2023 = [["011", "200", "A01-1", "札幌", "NTT東日本", "使用中"], ["011", "201", "A01-1", "札幌", "NTT東日本", "使用中"]]
HOKKAIDO_2024 = [["011", "200", "A01-1", "札幌", "NTT東日本", "使用中"], ["011", "201", "A01-1", "札幌", "KDDI", "使用中"]]


class SeriesFromContentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raw = Path(self.tmp.name) / "raw"
        self.conn = db(Path(self.tmp.name))
        self.addCleanup(self.conn.close)

    def put(self, name, text, rows, link_series="08"):
        sha, _ = archive.store(self.conn, self.raw, xlsx(text, rows),
                               url=f"https://www.soumu.go.jp/main_content/{name}", origin="official",
                               series=link_series)
        return designation.ingest(self.conn, self.raw, sha, layout())

    def test_numeric_id_filenames_do_not_mix_regions(self):
        # どちらのファイル名からも「08」が取れてしまうが、中身は 01 と 06
        self.put("000879543.xlsx", "令和5年4月1日現在", HOKKAIDO_2023)
        self.put("000881543.xlsx", "令和5年4月1日現在", V2023)
        self.put("000901543.xlsx", "令和6年4月1日現在", HOKKAIDO_2024)
        self.put("000908543.xlsx", "令和6年4月1日現在", V2024)
        series = {r[0] for r in self.conn.execute("SELECT DISTINCT series FROM numbers.version")}
        self.assertEqual(series, {"01", "06"})
        versions.select_versions(self.conn)
        diff.compute_diffs(self.conn, layout()["in_use_values"])
        s = diff.summary(self.conn)
        self.assertEqual(s["pairs"], 2)
        self.assertEqual(s["kinds"].get("carrier_changed"), 1)  # 北海道 201 の事業者変更
        res = {c.check_id: c for c in checks.run_checks(self.conn, self.raw)}
        self.assertTrue(res["Q12_pair_overlap"].passed, res["Q12_pair_overlap"].detail)
        self.assertTrue(res["Q09_same_date_selection"].passed)
        self.assertEqual(res["Q09_same_date_selection"].detail["same_date_different_content"], [])

    def test_one_file_with_two_regions_splits(self):
        ids = self.put("000123456.xlsx", "令和5年4月1日現在", HOKKAIDO_2023 + V2023)
        rows = {r["series"]: r["row_count"] for r in self.conn.execute(
            "SELECT series, row_count FROM numbers.version WHERE version_id IN (%s)" % ",".join("?" * len(ids)), ids)}
        self.assertEqual(rows, {"01": 2, "06": 3})
        self.assertEqual(self.put("000123456.xlsx", "令和5年4月1日現在", HOKKAIDO_2023 + V2023), ids)  # 再解析しない

    def test_non_geographic_numbers_get_their_own_series(self):
        rows = HOKKAIDO_2023 + [["0120", "123", None, None, "NTT東日本", "使用中"],
                                ["050", "5000", None, None, "IP事業者", "使用中"],
                                ["0123", "22", "A01-9", "千歳", "NTT東日本", "使用中"]]
        self.put("000333333.xlsx", "令和5年4月1日現在", rows)
        got = {r[0]: r[1] for r in self.conn.execute("SELECT series, row_count FROM numbers.version")}
        self.assertEqual(got, {"01": 3, "0120": 1, "050": 1})  # 0123（千歳）は北海道の地域番号

    def test_q12_blocks_comparison_of_unrelated_tables(self):
        # 同じ地域 06 でも、範囲がほとんど重ならない表同士の比較は error
        self.put("a.xlsx", "令和5年4月1日現在", V2023)
        self.put("b.xlsx", "令和6年4月1日現在", [["06", f"{7000 + i}", "A27-9", "大阪", "NTT西日本", "使用中"]
                                                for i in range(10)])
        versions.select_versions(self.conn)
        diff.compute_diffs(self.conn, layout()["in_use_values"])
        q12 = [c for c in checks.run_checks(self.conn, self.raw) if c.check_id == "Q12_pair_overlap"][0]
        self.assertFalse(q12.passed)
        self.assertEqual(q12.severity, "error")
        self.assertEqual(q12.detail["low_overlap_pairs"][0]["overlap"], 0.0)


def _osaka(n, carrier, area_code="A27-1"):
    return [["06", f"{6000 + i}", area_code, "大阪", carrier, "使用中"] for i in range(n)]


class ApparentChangeTest(unittest.TestCase):
    """見かけの変更（列の欠落・一斉の名称変更）を、実際の変更と分けて扱う。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raw = Path(self.tmp.name) / "raw"
        self.conn = db(Path(self.tmp.name))
        self.addCleanup(self.conn.close)

    def put(self, name, text, rows):
        sha, _ = archive.store(self.conn, self.raw, xlsx(text, rows), url=f"https://www.soumu.go.jp/main_content/{name}",
                               origin="official")
        designation.ingest(self.conn, self.raw, sha, layout())

    def build(self):
        self.put("1.xlsx", "令和5年4月1日現在", _osaka(40, "NTT西日本", area_code=None))  # 区画コードの列が空
        self.put("2.xlsx", "令和6年4月1日現在", _osaka(40, "NTT西日本"))
        renamed = _osaka(40, "西日本電信電話株式会社")
        renamed[0][4] = "KDDI"  # 個別の変更は1件だけ
        self.put("3.xlsx", "令和7年4月1日現在", renamed)
        versions.select_versions(self.conn)
        diff.compute_diffs(self.conn, layout()["in_use_values"])

    def test_empty_column_is_not_compared(self):
        self.build()
        s = diff.summary(self.conn)
        self.assertNotIn("number_area_changed", s["kinds"])
        first = s["per_pair"][0]
        self.assertEqual(first["skipped_columns"], "number_area_code")
        self.assertEqual(first["change_rows"], 0)

    def test_bulk_rename_is_separated_from_individual_changes(self):
        self.build()
        s = diff.summary(self.conn, bulk_min_rows=30)
        self.assertEqual(s["kinds"]["carrier_changed"], 40)  # 生の行は残す
        self.assertEqual(s["bulk_changes"], {"carrier_changed": 39})
        self.assertEqual(s["individual_changes"], {"carrier_changed": 1})
        top = s["top_transitions"][0]
        self.assertEqual((top["old_value"], top["new_value"], top["rows"]), ("NTT西日本", "西日本電信電話株式会社", 39))
        q13 = [c for c in checks.run_checks(self.conn, self.raw) if c.check_id == "Q13_bulk_transitions"][0]
        self.assertFalse(q13.passed)
        self.assertEqual(q13.severity, "warning")

    def test_diffs_rebuilt_when_algorithm_changes(self):
        self.build()
        n_versions = self.conn.execute("SELECT COUNT(*) FROM numbers.version").fetchone()[0]
        self.conn.execute("UPDATE numbers.diff_meta SET value = 'old' WHERE key = 'algorithm'")
        self.assertEqual(diff.compute_diffs(self.conn, layout()["in_use_values"]), 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM numbers.diff_pair").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM numbers.version").fetchone()[0], n_versions)

    def test_unchanged_republication_is_info_not_warning(self):
        same = _osaka(5, "NTT西日本")
        self.put("a.xlsx", "令和5年1月1日現在", same)
        self.put("b.xlsx", "令和5年2月1日現在", same)  # 隣り合う日付で同一内容 → 再掲
        versions.select_versions(self.conn)
        q06 = [c for c in checks.run_checks(self.conn, self.raw) if c.check_id == "Q06_duplicate_content"][0]
        self.assertTrue(q06.passed)
        self.assertEqual(len(q06.detail["republished_unchanged"]), 1)
        changed = _osaka(5, "KDDI")
        self.put("c.xlsx", "令和5年3月1日現在", changed)
        self.put("d.xlsx", "令和5年4月1日現在", same)  # 一度変わった内容に戻る → 要注意
        versions.select_versions(self.conn)
        q06 = [c for c in checks.run_checks(self.conn, self.raw) if c.check_id == "Q06_duplicate_content"][0]
        self.assertFalse(q06.passed)
        self.assertEqual(len(q06.detail["same_content_not_adjacent"]), 1)


class CarrierClassificationTest(unittest.TestCase):
    """実データの qa 結果（Q13）に出た事業者名の変化で確かめる。"""

    def setUp(self):
        from landline_atlas.history.carriers import CarrierMap
        self.cm = CarrierMap.load(Path(__file__).parent.parent / "config" / "carriers.json")

    def test_real_transitions(self):
        cases = {
            ("ＫＤＤＩ", "ＫＤＤＩ株式会社"): "notation",
            ("ソフトバンク", "ソフトバンク株式会社"): "notation",
            ("オプテージ", "株式会社オプテージ"): "notation",
            ("ＮＴＴ東日本", "東日本電信電話株式会社"): "rename",
            ("ＮＴＴ西日本", "西日本電信電話株式会社"): "rename",
            ("西日本電信電話株式会社", "ＮＴＴ西日本株式会社"): "rename",
            ("ソフトバンクモバイル", "ソフトバンク"): "rename",
            ("ケイ・オプティコム", "オプテージ"): "rename",
            ("ソフトバンクテレコム", "ソフトバンクモバイル"): "succession",
            ("テクノロジーネットワークス", "ジュピターテレコム"): "succession",
            ("東京テレメッセージ", None): "vacated",
            (None, "ＫＤＤＩ"): "assigned",
            ("ＮＴＴ東日本", "ＫＤＤＩ株式会社"): "transfer",
            # 2回目の実データ確認（changes-report の「別の事業者へ」に残っていたもの）
            ("ＮＴＴコミュニケーションズ", "エヌ・ティ・ティ・コミュニケーションズ株式会社"): "rename",
            ("エヌ・ティ・ティ・コミュニケーションズ株式会社", "ＮＴＴドコモビジネス株式会社"): "rename",
            ("フュージョンコミュニケーションズ", "楽天コミュニケーションズ"): "rename",
            ("東北インテリジェント通信株式会社", "株式会社トークネット"): "rename",
            ("九州通信ネットワーク", "ＱＴｎｅｔ"): "rename",
            ("株式会社エネルギア・コミュニケーションズ", "株式会社エネコム"): "rename",
            ("ドコモ", "ＮＴＴドコモ"): "rename",
            ("ＫＶＨ", "Ｃｏｌｔテクノロジーサービス"): "rename",
            ("楽天コミュニケーションズ", "楽天モバイル株式会社"): "succession",
            ("ＵＣＯＭ", "アルテリア・ネットワークス"): "succession",
            ("ジュピターテレコム", "ＫＤＤＩ株式会社"): "transfer",
            ("株式会社ジュピターテレコム", "ＫＤＤＩ株式会社"): "transfer",
            ("ＡＧテレコム", "大江戸テレコム株式会社"): "transfer",
            ("ドコモ", "ＵＣＯＭ"): "transfer",
        }
        for (old, new), expected in cases.items():
            with self.subTest(old=old, new=new):
                self.assertEqual(self.cm.classify(old, new), expected)

    def test_summary_separates_carrier_change_types(self):
        from landline_atlas.history.carriers import CarrierMap
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            conn = db(Path(tmp))
            rows = lambda carriers: [["03", f"{3000 + i}", "A13-1", "東京", c, "使用中"] for i, c in enumerate(carriers)]
            old = ["ＮＴＴ東日本"] * 35 + ["ＫＤＤＩ", "ソフトバンクテレコム", "東京テレメッセージ", "ＮＴＴ東日本"]
            new = ["東日本電信電話株式会社"] * 35 + ["ＫＤＤＩ株式会社", "ソフトバンクモバイル", None, "ＫＤＤＩ"]
            for name, text, carriers in (("a.xlsx", "令和元年5月21日現在", old), ("b.xlsx", "令和2年12月1日現在", new)):
                sha, _ = archive.store(conn, raw, xlsx(text, rows(carriers)), url=f"https://x.invalid/{name}",
                                       origin="official")
                designation.ingest(conn, raw, sha, layout())
            versions.select_versions(conn)
            diff.compute_diffs(conn, layout()["in_use_values"])
            s = diff.summary(conn, carriers=self.cm)
            self.assertEqual(s["carrier_change_types"],
                             {"rename": 35, "notation": 1, "succession": 1, "vacated": 1, "transfer": 1})
            self.assertEqual(s["top_transitions"][0]["carrier_type"], "rename")
            self.assertEqual(s["carrier_transfers"][0], {"old_value": "ＮＴＴ東日本", "new_value": "ＫＤＤＩ", "rows": 1})
            # 対応表なしでも「株式会社」の有無などは書き方の違いとして判定される
            plain = diff.summary(conn, carriers=CarrierMap())
            self.assertEqual(plain["carrier_change_types"]["notation"], 1)
            self.assertEqual(plain["carrier_change_types"]["transfer"], 37)
            conn.close()
