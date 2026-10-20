"""atlas fetch-census（e-Stat 国勢調査の自動取得）のテスト。e-Stat は模擬応答で、データは合成。"""

import copy
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from support import CONFIG, FakeTransport, db

from landline_atlas import census_fetch
from landline_atlas.ingest.dbf import read_dbf, write_dbf

CFG = json.loads((CONFIG / "estat.json").read_text(encoding="utf-8"))
HEAD = ["KEY_CODE", "HYOSYO", "CITYNAME", "NAME", "HTKSYORI", "HTKSAKI", "GASSAN"]


def table_zip(stats_id, labels, rows):
    codes = HEAD + [f"{stats_id}{i:03d}" for i in range(1, len(labels) + 1)]
    lines = [",".join(codes), ",".join([""] * len(HEAD) + labels)] + [",".join(map(str, r)) for r in rows]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"tbl{stats_id}C27.txt", ("\r\n".join(lines) + "\r\n").encode("cp932"))
    return buf.getvalue()


# 市区町村の行・大字（上位の階層）の行・町丁目3件（うち1件は秘匿で合算）
def town_rows(values):
    base = [["27100", 1, "架空市", "", 0, "", ""],
            ["271000001", 2, "架空市", "本町", 0, "", ""],
            ["27100000101", 3, "架空市", "本町一丁目", 0, "", ""],
            ["27100000102", 3, "架空市", "本町二丁目", 1, "27100000201", ""],
            ["27100000201", 3, "架空市", "新町", 2, "", ""]]
    return [r + v for r, v in zip(base, values)]


T1081 = table_zip("T001081", ["人口総数", "男", "女", "世帯総数"],
                  town_rows([[3000, 1500, 1500, 1300], [2000, 1000, 1000, 900], [1000, 500, 500, 400],
                             ["X", "X", "X", "X"], [700, 350, 350, 300]]))
T1082 = table_zip("T001082", ["総数、年齢「不詳」含む", "65歳以上", "75歳以上", "年齢「不詳」", "男65歳以上", "女65歳以上"],
                  town_rows([[3000, 900, 400, 20, 400, 500], [2000, 600, 300, 10, 270, 330],
                             [1000, 300, 100, 10, 130, 170], ["X"] * 6, [700, 300, 150, "-", 140, 160]]))
T1085 = table_zip("T001085", ["一般世帯数", "住宅に住む一般世帯", "主世帯", "持ち家", "公営・都市再生機構・公社の借家",
                              "民営の借家", "給与住宅", "間借り", "住宅以外に住む一般世帯"],
                  town_rows([[1300, 1290, 1280, 900, 50, 300, 30, 10, 10], [900, 895, 890, 600, 40, 230, 20, 5, 5],
                             [400, 395, 390, 300, 10, 70, 10, 5, 5], ["X"] * 9, [300, 300, 300, 100, 150, 50, 0, 0, 0]]))


def boundary_zip():
    fields = [("KEY_CODE", "C", 11, 0), ("S_NAME", "C", 20, 0), ("AREA", "N", 14, 3), ("JINKO", "N", 9, 0),
              ("SETAI", "N", 9, 0)]
    rows = [{"KEY_CODE": "27100000101", "S_NAME": "本町一丁目", "AREA": 250000.0, "JINKO": 1000, "SETAI": 400},
            {"KEY_CODE": "27100000102", "S_NAME": "本町二丁目", "AREA": 100000.0, "JINKO": 0, "SETAI": 0},
            {"KEY_CODE": "27100000201", "S_NAME": "新町", "AREA": 60000.0, "JINKO": 690, "SETAI": 300},
            {"KEY_CODE": "27100000201", "S_NAME": "新町", "AREA": 40000.0, "JINKO": 10, "SETAI": 0}]  # 飛び地
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("r2ka27.dbf", write_dbf(rows, fields))
        zf.writestr("r2ka27.shp", b"\x00" * 100)
    return buf.getvalue()


def routes(*, missing=()):
    d = "https://www.e-stat.go.jp/gis/statmap-search/data?statsId={}&code=27&downloadType=2"
    listing = ("https://www.e-stat.go.jp/gis/statmap-search?page=1&type=1&toukeiCode=00200521&toukeiYear=2020"
               "&aggregateUnit=A&serveyId=A002005212020&statsId=T001085&prefCode=27")
    b11 = ("https://www.e-stat.go.jp/gis/statmap-search/data?dlserveyId=A002005212020&code=27&coordSys=1"
           "&format=shape&downloadType=5&datum=2011")
    b00 = b11.replace("datum=2011", "datum=2000")
    r = {"https://www.e-stat.go.jp/robots.txt": [(404, b"")],
         d.format("T001081"): [(200, T1081)],
         d.format("T001082"): [(200, T1082)],
         d.format("T001085"): [(200, b"<html>error</html>")],  # ZIP 以外 → 一覧ページから探す
         listing: [(200, b"<a href='/gis/statmap-search/data?statsId=T001085&code=27&downloadType=2&x=1'>CSV</a>"
                         b"<a href='/gis/statmap-search/data?statsId=T001085&code=270&downloadType=2'>x</a>")],
         "https://www.e-stat.go.jp/gis/statmap-search/data?statsId=T001085&code=27&downloadType=2&x=1": [(200, T1085)],
         b11: [(404, b"")], b00: [(200, boundary_zip())]}
    for url in missing:
        r.pop(url.format("T001081") if "{}" in url else url, None)
    return r


class FetchCensusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.conn = db(self.dir)
        self.addCleanup(self.conn.close)
        self.raw = self.dir / "raw"

    def run_fetch(self, *, probe, cfg=CFG, r=None):
        return census_fetch.run(self.conn, self.raw, cfg, "27", probe=probe, transport=FakeTransport(r or routes()),
                                sleep=lambda s: None)

    def test_probe_reports_and_writes_nothing(self):
        rep = self.run_fetch(probe=True)
        self.assertIsNone(rep["stopped"])
        t = rep["tables"]["T001081"]
        self.assertEqual((t["town_rows"], t["skipped"]), (3, {"not_town_key": 1, "upper_level": 1}))
        self.assertEqual(t["htksyori_values"], {"0": 1, "1": 1, "2": 1})
        for sid in ("T001081", "T001082", "T001085"):
            self.assertTrue(all(m["status"] == "ok" for m in rep["tables"][sid]["matches"].values()), sid)
        self.assertEqual(rep["tables"]["T001082"]["matches"]["pop_65plus"]["label"], "65歳以上")
        self.assertIn("listing_links=1", [x["result"] for x in rep["tables"]["T001085"]["tried"]])
        self.assertEqual(rep["boundary"]["resolved"]["area_m2"], "AREA")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM regions.town").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM regions.raw_census").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM numbers.fetch_log").fetchone()[0], 0)
        self.assertFalse((self.raw / "census").exists())
        text = census_fetch.render(rep)
        self.assertIn("確認モード", text)
        self.assertIn("pop_65plus: ○ 1列に特定「65歳以上」", text)

    def test_full_import(self):
        rep = self.run_fetch(probe=False)
        self.assertTrue(rep["imported"], rep["stopped"])
        towns = {r["town_key"]: (r["secrecy"], r["merged_into"], r["town_name"])
                 for r in self.conn.execute("SELECT * FROM regions.town")}
        self.assertEqual(towns, {"27100000101": ("none", None, "架空市本町一丁目"),
                                 "27100000102": ("merged_into_other", "27100000201", "架空市本町二丁目"),
                                 "27100000201": ("merge_target", None, "架空市新町")})
        f = {(r["town_key"], r["feature"]): (r["value"], r["state"]) for r in self.conn.execute(
            "SELECT * FROM regions.town_feature")}
        self.assertEqual(f[("27100000101", "pop_total")], (1000.0, "observed"))
        self.assertEqual(f[("27100000101", "pop_age_known")], (990.0, "observed"))   # 総数 − 年齢不詳
        self.assertEqual(f[("27100000201", "pop_age_known")], (700.0, "observed"))   # 不詳 '-' は 0
        self.assertEqual(f[("27100000102", "pop_65plus")], (None, "suppressed"))
        self.assertEqual(f[("27100000101", "hh_owned")], (300.0, "observed"))
        self.assertEqual(f[("27100000101", "area_km2")], (0.25, "observed"))
        self.assertEqual(f[("27100000201", "area_km2")], (0.1, "observed"))           # 飛び地を合計
        self.assertNotIn(("27100000101", "pop_age_total"), f)                         # 計算用の列は保存しない
        self.assertEqual(rep["boundary"]["import"]["population_mismatch_towns"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM regions.raw_census").fetchone()[0], 4)
        self.assertTrue(any((self.raw / "census").rglob("*.zip")))
        src = self.conn.execute("SELECT * FROM regions.source_document WHERE source_id LIKE 'estat:T001081:%'").fetchone()
        self.assertIn("政府標準利用規約", src["license"])
        from support import rules
        from landline_atlas.classify import engine
        run = engine.classify(self.conn, rules(), 2020, "estat-2020")
        st = {r["label_status"] for r in self.conn.execute(
            "SELECT label_status FROM regions.regional_classification WHERE run_id = ?", (run,))}
        self.assertIn("insufficient_data", st)  # 建て方・GIS が無い分類は資料不足（推測で埋めない）

    def test_required_table_missing_stops(self):
        r = routes()
        del r["https://www.e-stat.go.jp/gis/statmap-search/data?statsId=T001081&code=27&downloadType=2"]
        rep = self.run_fetch(probe=False, r=r)
        self.assertIn("T001081", rep["stopped"])
        self.assertFalse(rep["imported"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM regions.town").fetchone()[0], 0)
        self.assertIn("停止しました", census_fetch.render(rep))

    def test_ambiguous_required_column_stops(self):
        cfg = copy.deepcopy(CFG)
        cfg["tables"][0]["features"]["pop_total"] = {"include": ["総数"], "exclude": []}  # 人口総数・世帯総数の両方
        rep = self.run_fetch(probe=False, cfg=cfg)
        self.assertIn("columns not identified", rep["stopped"])
        self.assertEqual(rep["tables"]["T001081"]["matches"]["pop_total"]["status"], "multiple")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM regions.town").fetchone()[0], 0)

    def test_dbf_roundtrip_cp932(self):
        body = write_dbf([{"KEY_CODE": "01101000101", "S_NAME": "大通西一丁目", "AREA": 1234.5}],
                         [("KEY_CODE", "C", 11, 0), ("S_NAME", "C", 30, 0), ("AREA", "N", 12, 1)])
        self.assertEqual(read_dbf(body), [{"KEY_CODE": "01101000101", "S_NAME": "大通西一丁目", "AREA": 1234.5}])


if __name__ == "__main__":
    unittest.main()


class FetchCensusCliTest(unittest.TestCase):
    def test_cli_probe_then_import(self):
        import contextlib
        from unittest import mock
        from landline_atlas.cli import main as atlas
        with tempfile.TemporaryDirectory() as tmp:
            data = str(Path(tmp) / "data")
            fake = lambda: FakeTransport(routes())  # noqa: E731
            with mock.patch("landline_atlas.census_fetch.UrllibTransport", fake), \
                    mock.patch("landline_atlas.acquire.http.Fetcher._wait", lambda self, host: None):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(atlas(["--data-dir", data, "fetch-census", "--pref", "27", "--probe"]), 0)
                self.assertIn("確認だけ行いました", buf.getvalue())
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(atlas(["--data-dir", data, "fetch-census", "--pref", "27"]), 0)
                self.assertIn("atlas classify --census-year 2020 --boundary-version estat-2020", buf.getvalue())
                self.assertEqual(atlas(["--data-dir", data, "classify", "--census-year", "2020",
                                        "--boundary-version", "estat-2020"]), 0)
