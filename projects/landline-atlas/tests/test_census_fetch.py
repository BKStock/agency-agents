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
from landline_atlas.ingest import estat_tables
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


# 実データ（2020 年・大阪府）の形に合わせた架空の表。
# 人口総数の表（T001081）は秘匿が無く、ほかの表で合算がある。HTKSYORI は 1=合算先（GASSAN に合算元）、
# 2=秘匿して他へ合算（値は X、HTKSAKI は市区町村内のコード）。本町二丁目は新町へ合算される。
def town_rows(values, *, secret=True):
    base = [["27100", 1, "架空市", "", 0, "", ""],
            ["271000001", 2, "架空市", "本町", 0, "", ""],
            ["27100000101", 3, "架空市", "本町一丁目", 0, "", ""],
            ["27100000102", 3, "架空市", "本町二丁目", 2 if secret else 0, "000201" if secret else "", ""],
            ["27100000201", 3, "架空市", "新町", 1 if secret else 0, "", "000102" if secret else ""]]
    return [r + v for r, v in zip(base, values)]


T1081 = table_zip("T001081", ["人口総数", "男", "女", "世帯総数"],
                  town_rows([[3000, 1500, 1500, 1300], [2000, 1000, 1000, 900], [1000, 500, 500, 400],
                             [300, 150, 150, 120], [700, 350, 350, 300]], secret=False))
T1082 = table_zip("T001082", ["総数、年齢「不詳」含む", "総数15歳未満", "総数15～64歳", "総数65歳以上", "総数75歳以上",
                              "男の総数、年齢「不詳」含む", "男65歳以上", "女65歳以上"],
                  town_rows([[3000, 300, 1780, 900, 400, 1500, 400, 500], [2000, 200, 1190, 600, 300, 1000, 270, 330],
                             [1000, 100, 590, 300, 100, 500, 130, 170], ["X"] * 8,
                             [1000, 100, 600, 300, 150, 500, 140, 160]]))
T1083 = table_zip("T001083", ["一般世帯数（世帯人員６人以上含む）", "世帯人員１人", "一般世帯人員", "１世帯当たり人員"],
                  town_rows([[1290, 400, 2950, 2.29], [895, 300, 1980, 2.21], [395, 100, 970, 2.46], ["X"] * 4,
                             [420, 120, 990, 2.36]]))
T1085 = table_zip("T001085", ["住宅に住む一般世帯", "持ち家", "民営借家"],
                  town_rows([[1290, 900, 300], [895, 600, 230], [395, 300, 70], ["X"] * 3, [420, 100, 50]]))
T1086 = table_zip("T001086", ["主世帯数", "一戸建", "長屋建", "共同住宅", "共同住宅１・２階建", "共同住宅３～５階建",
                              "共同住宅６～１０階建", "共同住宅１１階建以上", "その他"],
                  town_rows([[1280, 700, 30, 540, 100, 200, 140, 100, 10], [890, 500, 20, 365, 65, 150, 100, 50, 5],
                             [390, 200, 10, 175, 35, 50, 40, 50, 5], ["X"] * 9, [415, 300, "-", 115, 115, "-", "-", "-", "-"]]))


def boundary_zip():
    fields = [("KEY_CODE", "C", 11, 0), ("S_NAME", "C", 20, 0), ("AREA", "N", 14, 3), ("JINKO", "N", 9, 0),
              ("SETAI", "N", 9, 0)]
    rows = [{"KEY_CODE": "27100000101", "S_NAME": "本町一丁目", "AREA": 250000.0, "JINKO": 1000, "SETAI": 400},
            {"KEY_CODE": "27100000102", "S_NAME": "本町二丁目", "AREA": 100000.0, "JINKO": 300, "SETAI": 120},
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
         d.format("T001083"): [(200, T1083)],
         d.format("T001086"): [(200, T1086)],
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
        self.assertEqual(t["htksyori_values"], {"0": 3})
        self.assertEqual(rep["tables"]["T001086"]["htksyori_values"], {"0": 1, "1": 1, "2": 1})
        self.assertEqual(rep["secrecy"], {"none": 1, "merged_into_other": 1, "merge_target": 1,
                                          "merged_into_upper_level": 0, "conflicts": []})
        for sid in ("T001081", "T001082", "T001083", "T001085", "T001086"):
            self.assertTrue(all(m["status"] == "ok" for m in rep["tables"][sid]["matches"].values()), sid)
        self.assertEqual(rep["tables"]["T001082"]["matches"]["pop_65plus"]["label"], "総数65歳以上")
        self.assertEqual(rep["tables"]["T001086"]["matches"]["hh_apartment"]["label"], "共同住宅")
        self.assertIn("listing_links=1", [x["result"] for x in rep["tables"]["T001085"]["tried"]])
        self.assertEqual(rep["boundary"]["resolved"]["area_m2"], "AREA")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM regions.town").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM regions.raw_census").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM numbers.fetch_log").fetchone()[0], 0)
        self.assertFalse((self.raw / "census").exists())
        text = census_fetch.render(rep)
        self.assertIn("確認モード", text)
        self.assertIn("pop_65plus: ○ 1列に特定「総数65歳以上」", text)

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
        self.assertEqual(f[("27100000101", "pop_age_known")], (990.0, "observed"))   # 15歳未満＋15～64歳＋65歳以上
        self.assertEqual(f[("27100000201", "pop_age_known")], (1000.0, "observed"))  # 合算先は合算元を含む
        self.assertEqual(f[("27100000102", "pop_65plus")], (None, "suppressed"))
        self.assertEqual(f[("27100000102", "pop_total")], (300.0, "observed"))      # 人口総数の表は秘匿なし
        self.assertEqual(f[("27100000101", "hh_owned")], (300.0, "observed"))
        self.assertEqual(f[("27100000101", "hh_main")], (390.0, "observed"))
        self.assertEqual(f[("27100000101", "hh_detached")], (200.0, "observed"))
        self.assertEqual(f[("27100000101", "hh_apartment")], (175.0, "observed"))
        self.assertEqual(f[("27100000201", "hh_row_house")], (0.0, "zero"))
        self.assertEqual(f[("27100000101", "households_general")], (395.0, "observed"))
        for absent in ("hh_rented_public", "hh_company"):  # 町丁・字等の表には無い。推測で埋めない
            self.assertNotIn(("27100000101", absent), f)
        self.assertEqual(f[("27100000101", "area_km2")], (0.25, "observed"))
        self.assertEqual(f[("27100000201", "area_km2")], (0.1, "observed"))           # 飛び地を合計
        for inp in ("pop_under15", "pop_15to64"):                                       # 計算用の列は保存しない
            self.assertNotIn(("27100000101", inp), f)
        self.assertEqual(rep["boundary"]["import"]["population_mismatch_towns"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM regions.raw_census").fetchone()[0], 6)
        self.assertTrue(any((self.raw / "census").rglob("*.zip")))
        src = self.conn.execute("SELECT * FROM regions.source_document WHERE source_id LIKE 'estat:T001081:%'").fetchone()
        self.assertIn("政府標準利用規約", src["license"])
        from support import rules
        from landline_atlas.classify import engine
        run = engine.classify(self.conn, rules(), 2020, "estat-2020")
        st = {r["label_status"] for r in self.conn.execute(
            "SELECT label_status FROM regions.regional_classification WHERE run_id = ?", (run,))}
        self.assertIn("insufficient_data", st)  # GIS・借家の内訳が無い分類は資料不足（推測で埋めない）
        merged = {r["label_status"] for r in self.conn.execute(
            "SELECT label_status FROM regions.regional_classification WHERE run_id = ? AND town_key = '27100000102'",
            (run,))}
        self.assertEqual(merged, {"undeterminable"})

    def test_merged_into_upper_level_and_conflict(self):
        # 実データ：平林北一・二丁目（HTKSYORI=2、HTKSAKI=0190）は親の大字「平林北」（271250190）へ合算される
        rows = [["27125", 1, "住之江区", "", 0, "", "", 10], ["271250190", 3, "住之江区", "平林北", 1, "", "019001;019002", 10],
                ["27125019001", 4, "住之江区", "平林北一丁目", 2, "0190", "", "X"],
                ["27125019002", 4, "住之江区", "平林北二丁目", 2, "0190", "", "X"]]
        t = estat_tables.parse_table_zip(table_zip("T001086", ["主世帯数"], rows))
        sec = estat_tables.secrecy_by_town([t])
        self.assertEqual(sec.states["27125019001"], ("merged_into_other", "271250190"))
        self.assertEqual(sec.into_upper_level, 2)
        other = [r[:4] + [0, "", "", 5] for r in rows]
        sec2 = estat_tables.secrecy_by_town([t, estat_tables.parse_table_zip(table_zip("T001085", ["持ち家"], other))])
        self.assertEqual(sec2.conflicts, [])  # 秘匿なし（0）は他の表の秘匿と矛盾しない
        bad = [r[:4] + [1, "", "x", 5] if r[0] == "27125019001" else r[:4] + [0, "", "", 5] for r in rows]
        sec3 = estat_tables.secrecy_by_town([t, estat_tables.parse_table_zip(table_zip("T001085", ["持ち家"], bad))])
        self.assertEqual([c["town_key"] for c in sec3.conflicts], ["27125019001"])

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
