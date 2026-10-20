"""atlas fetch-gis：国土数値情報・農業集落境界から町丁目の GIS 特徴量を作って取り込む。

町丁目の形は fetch-census が保存した e-Stat 小地域境界を使う（先に fetch-census が必要）。
--probe では取得・属性の確認・計算までを行い、DB にもディスクにも書かない。
属性名・コード・座標系・年が config/gis_sources.json と違えば、どのデータも取り込まずに停止する。
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

from .acquire import estat
from .acquire.http import Fetcher, UrllibTransport
from .acquire.sources import make_logger
from .db.connection import utc_now
from .ingest import estat_tables
from .normalize import CodeError, nfkc, prefecture_code, town_key
from .regions import gis
from .regions.features import Feature, new_load, put

STATES = ("observed", "zero", "missing", "undefined")


class Stop(Exception):
    """属性・コード・取得の想定違い。推測で続けずに停止する。"""


def load_config(config_dir: Path) -> dict:
    return json.loads((config_dir / "gis_sources.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 町丁目の形

def load_towns(conn, raw_dir: Path, cfg: dict, pref: str):
    """(町丁目の GeoDataFrame[town_key, geometry], 情報)。境界の原本が無ければ停止する。"""
    import geopandas as gpd
    row = conn.execute("SELECT stored_path, sha256 FROM regions.raw_census WHERE item = 'boundary' AND prefecture = ?"
                       " ORDER BY fetched_at DESC LIMIT 1", (pref,)).fetchone()
    if row is None:
        raise Stop(f"no e-Stat boundary for {pref} in raw/census (run: atlas fetch-census --pref {pref})")
    path = raw_dir / row[0]
    if hashlib.sha256(path.read_bytes()).hexdigest() != row[1]:
        raise Stop(f"stored boundary {path} does not match its SHA-256")
    gdf = _read_member(path, _one_member(path, r"\.shp$"), cfg["towns"].get("encoding"))
    keys, skipped = [], Counter()
    for raw in gdf[cfg["towns"]["key_field"]].astype(str):
        try:
            keys.append(town_key(nfkc(raw)))
        except CodeError:
            keys.append(None)
            skipped[raw] += 1
    gdf = gdf.assign(town_key=keys).dropna(subset=["town_key"])
    name_f = cfg["towns"].get("municipality_name_field")
    names = {} if not name_f else {k[:5]: str(n) for k, n in zip(gdf.town_key, gdf[name_f]) if n == n}
    gdf = gdf[["town_key", "geometry"]]
    towns = gdf.to_crs(gis.CRS).dissolve(by="town_key", as_index=False)
    year, version = cfg["census_year"], cfg["boundary_version"]
    known = {r[0] for r in conn.execute("SELECT town_key FROM regions.town WHERE prefecture_code = ?"
                                        " AND census_year = ? AND boundary_version = ?", (pref, year, version))}
    info = {"boundary_sha256": row[1], "polygons_without_town": sum(skipped.values()),
            "towns_in_boundary": len(towns), "towns_in_master": len(known),
            "master_without_polygon": len(known - set(towns.town_key)),
            "polygon_without_master": len(set(towns.town_key) - known)}
    towns = towns[towns.town_key.isin(known)].reset_index(drop=True)
    info["municipality_names"] = names
    return gpd.GeoDataFrame(towns, crs=gis.CRS), info


def _one_member(path: Path, pattern: str) -> str:
    names = [n for n in zipfile.ZipFile(path).namelist() if re.search(pattern, n)]
    if len(names) != 1:
        raise Stop(f"{path.name}: expected one member matching {pattern}, found {names}")
    return names[0]


def _base(name: str) -> str:
    return re.split(r"[\\/]", name)[-1]


def resolve_member(path: Path, member: str) -> str:
    """ZIP 内で、ファイル名（フォルダを除く）が member と同じものを1つ探す。

    配布物によってはフォルダ付き・区切りが「\\」で入っている（L03-b-21 の一部のメッシュ）。"""
    names = zipfile.ZipFile(path).namelist()
    if member in names:
        return member
    hits = [n for n in names if _base(n) == _base(member)]
    if len(hits) != 1:
        raise Stop(f"{path.name}: expected one {_base(member)}, found {hits} among {names[:10]}")
    return hits[0]


def _read_member(path: Path, member: str, encoding: str | None = None):
    """Shapefile を同じ名前の付属ファイル（.dbf .shx .prj .cpg）ごと取り出して読む。"""
    import geopandas as gpd
    member = resolve_member(path, member)
    stem = member[:-4]
    kw = {"encoding": encoding} if encoding else {}
    with zipfile.ZipFile(path) as zf, tempfile.TemporaryDirectory() as tmp:
        for n in zf.namelist():
            if n[:-4] == stem and n[-4:].lower() in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                (Path(tmp) / f"x{n[-4:].lower()}").write_bytes(zf.read(n))
        return gpd.read_file(Path(tmp) / "x.shp", **kw)


def mesh1_codes(towns) -> set[str]:
    """町丁目に重なりうる1次メッシュ（緯度 40 分 × 経度 1 度）のコード。"""
    out = set()
    for minx, miny, maxx, maxy in towns.geometry.bounds.itertuples(index=False):
        for la in range(math.floor(miny * 1.5), math.floor(maxy * 1.5) + 1):
            for lo in range(math.floor(minx) - 100, math.floor(maxx) - 100 + 1):
                out.add(f"{la:02d}{lo:02d}")
    return out


# ---------------------------------------------------------------------------
# 取得

class RawCache:
    """保管済みの原本を URL で探して再利用する（--refresh のときは使わない）。

    取込で保管した原本は regions.raw_gis に、--probe で取得した原本は raw/gis/probe_index.json に載る。
    --probe は DB に書かないので、原本のファイルと索引だけをディスクに残し、続く取込で同じ URL を取り直さない。
    ファイルの SHA-256 が記録と合わなければ使わずに取り直す。"""

    def __init__(self, conn, raw_dir: Path, *, refresh: bool, probe: bool):
        self.conn, self.raw_dir, self.refresh, self.probe = conn, raw_dir, refresh, probe
        self.index_path = raw_dir / "gis" / "probe_index.json"
        self.fetched_at: dict[str, str] = {}
        self.reused: list[str] = []
        self.memo: dict[str, estat.Fetched] = {}   # 同じ実行の中で2つのデータセットが同じ URL を使うとき

    def _index(self) -> dict:
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _candidates(self, url: str) -> list[tuple[str, str, str]]:
        rows = self.conn.execute("SELECT sha256, stored_path, fetched_at FROM regions.raw_gis WHERE url = ?"
                                 " ORDER BY fetched_at DESC", (url,)).fetchall()
        out = [(r[0], r[1], r[2]) for r in rows]
        e = self._index().get(url)
        if e:
            out.append((e["sha256"], e["stored_path"], e["fetched_at"]))
        return out

    def lookup(self, url: str, item: str) -> estat.Fetched | None:
        if url in self.memo:
            return self.memo[url]
        if self.refresh:
            return None
        for sha, stored, at in self._candidates(url):
            path = self.raw_dir / stored
            if not path.exists():
                continue
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != sha:
                continue
            self.fetched_at[url] = at
            self.reused.append(url)
            self.memo[url] = estat.Fetched(item, True, url, sha, body, [{"url": url, "result": f"reused {stored}"}])
            return self.memo[url]
        return None

    def remember(self, fetched: estat.Fetched) -> None:
        """取得した時刻を覚える。--probe では原本を保管して索引に載せる（DB には書かない）。"""
        self.fetched_at[fetched.url] = utc_now()
        self.memo[fetched.url] = fetched
        if not self.probe:
            return
        stored = _write_raw(self.raw_dir, fetched.sha256, fetched.body)
        index = self._index()
        index[fetched.url] = {"sha256": fetched.sha256, "stored_path": stored, "fetched_at": self.fetched_at[fetched.url]}
        fd, tmp = tempfile.mkstemp(dir=self.index_path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, self.index_path)


class Ctx:
    """1回の実行で共有するもの。files はデータセットごとの取得結果 (unit, Fetched, 一時ファイル)。"""

    def __init__(self, fetcher: Fetcher, towns, cfg: dict, pref: str, tmp: Path, cache: RawCache):
        self.fetcher, self.towns, self.cfg, self.pref, self.tmp, self.cache = fetcher, towns, cfg, pref, tmp, cache
        self.areas = gis.town_areas(towns)
        self.municipality_names: dict[str, str] = {}
        self.files: dict[str, list[tuple[str, estat.Fetched, Path]]] = {}

    def min_coverage(self, spec: dict) -> float:
        return float(spec.get("min_coverage", self.cfg["min_coverage"]["default"]))


def _fetch_zip(ctx: Ctx, url: str, item: str) -> estat.Fetched:
    hit = ctx.cache.lookup(url, item)
    if hit:
        return hit
    out = estat.Fetched(item, False)
    body = estat._try(ctx.fetcher, url, out.tried)
    if body:
        out.ok, out.url, out.body, out.sha256 = True, url, body, hashlib.sha256(body).hexdigest()
        ctx.cache.remember(out)
    return out


def _keep(ctx: Ctx, key: str, unit: str, f: estat.Fetched) -> Path:
    path = ctx.tmp / f"{key}_{unit}{_ext(f.body)}"
    path.write_bytes(f.body)
    ctx.files.setdefault(key, []).append((unit, f, path))
    return path


def fetch_url(ctx: Ctx, key: str, unit: str, url: str) -> Path:
    f = _fetch_zip(ctx, url, f"{key}:{unit}")
    if not f.ok:
        raise Stop(f"{key}: download failed {f.tried}")
    return _keep(ctx, key, unit, f)


def fetch_pref(ctx: Ctx, key: str, spec: dict) -> Path:
    return fetch_url(ctx, key, ctx.pref, spec["url_template"].format(pref=ctx.pref))


def fetch_meshes(ctx: Ctx, key: str, spec: dict, entry: dict) -> list[tuple[str, Path]]:
    """配布ページに載っている1次メッシュのうち、町丁目に重なりうるものを取る。"""
    resp = ctx.fetcher.get(spec["page"])
    if resp is None:
        raise Stop(f"{key}: listing page {spec['page']} could not be fetched")
    listed = set(re.findall(spec["mesh_regex"], resp.body.decode("utf-8", "replace")))
    if not listed:
        raise Stop(f"{key}: no mesh links matching {spec['mesh_regex']} on {spec['page']}")
    needed = mesh1_codes(ctx.towns)
    entry["meshes"] = {"needed": len(needed), "listed_and_needed": sorted(needed & listed),
                       "not_listed": sorted(needed - listed)}
    return [(mesh, fetch_url(ctx, key, mesh, spec["url_template"].format(mesh=mesh)))
            for mesh in sorted(needed & listed)]


# ---------------------------------------------------------------------------
# 読み込みと確認

def read_checked(path: Path, member: str, spec: dict, key: str):
    gdf = _read_member(path, member, spec.get("encoding"))
    if gdf.crs is None:
        if not spec.get("crs_if_missing"):
            raise Stop(f"{key}: {member} has no CRS")
        gdf = gdf.set_crs(spec["crs_if_missing"])
    elif spec.get("expect_crs") and f"EPSG:{gdf.crs.to_epsg()}" != spec["expect_crs"]:
        raise Stop(f"{key}: {member} CRS is {gdf.crs.to_string()}, expected {spec['expect_crs']}")
    missing = [f for f in spec["fields"] if f not in gdf.columns]
    if missing:
        raise Stop(f"{key}: {member} lacks fields {missing} (has {list(gdf.columns)})")
    for field, want in spec.get("must_equal", {}).items():
        got = Counter(gdf[field].astype(str))
        if set(got) != {want}:
            raise Stop(f"{key}: {member} {field} should be {want}, got {dict(got)}")
    return gdf


def check_codes(gdf, field: str, allowed: set[str], key: str) -> Counter:
    got = Counter(gdf[field].astype(str))
    extra = {c: n for c, n in got.items() if c not in allowed}
    if extra:
        raise Stop(f"{key}: unexpected {field} codes {extra} (allowed {sorted(allowed)})")
    return got


def numeric(gdf, field: str, invalid: set[str], key: str):
    """数値の列にする。invalid の値は NaN、それ以外で数値にならない値があれば停止する。"""
    vals, bad = [], Counter()
    for v in gdf[field].astype(str):
        if v in invalid:
            vals.append(float("nan"))
            continue
        try:
            vals.append(float(v))
        except ValueError:
            vals.append(float("nan"))
            bad[v] += 1
    if bad:
        raise Stop(f"{key}: {field} has non-numeric values {dict(bad.most_common(5))}")
    return vals


# ---------------------------------------------------------------------------
# データセットごとの計算。戻り値は {特徴量: {town_key: Feature}}

def did(ctx: Ctx, spec: dict, entry: dict):
    path = fetch_pref(ctx, "did", spec)
    gdf = read_checked(path, spec["member"].format(pref=ctx.pref), spec, "did")
    entry["polygons"] = len(gdf)
    layer = gdf.assign(cls="did").dissolve(by="cls", as_index=False)
    areas = gis.class_areas(ctx.towns, layer, "cls")
    cut = spec["features"]["did_flag"]["min_share"]
    out = {}
    for key, total in ctx.areas.items():
        s = gis.share(total, areas.get(key, {}).get("did", 0.0), total, 0.0)
        out[key] = s if not s.usable else Feature.of(1.0 if s.value >= cut else 0.0)
    return {"did_flag": out}


def zoning(ctx: Ctx, spec: dict, entry: dict):
    import pandas as pd
    path = fetch_pref(ctx, "zoning", spec)
    members = [n for n in zipfile.ZipFile(path).namelist() if re.search(spec["member_regex"], n)]
    if not members:
        raise Stop(f"zoning: no member matching {spec['member_regex']}")
    code_to_group = {c: g for g, cs in spec["codes"].items() for c in cs}
    parts, munis, codes, file_names = [], set(), Counter(), {}
    for m in members:
        muni = re.search(spec["member_regex"], m).group(1)
        gdf = read_checked(path, m, spec, "zoning")
        attr = set(gdf[spec["municipality_field"]].astype(str))
        if attr != {muni}:
            raise Stop(f"zoning: {m} holds municipality codes {sorted(attr)}, file name says {muni}")
        city = set(gdf[spec["municipality_name_field"]].astype(str))
        if len(city) != 1:
            raise Stop(f"zoning: {m} holds municipality names {sorted(city)}")
        file_names[muni] = city.pop()
        codes += check_codes(gdf, spec["class_field"], set(code_to_group), "zoning")
        munis.add(muni)
        parts.append(gdf.to_crs(gis.CRS).assign(grp=gdf[spec["class_field"]].astype(str).map(code_to_group)))
    wards = ward_coverage(file_names, ctx.municipality_names)
    munis |= set(wards)
    entry.update(municipalities_with_file=len(file_names), wards_covered_by_city_file=wards,
                 codes=dict(sorted(codes.items(), key=lambda x: int(x[0]))))
    import geopandas as gpd
    layer = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=gis.CRS).dissolve(by="grp", as_index=False)
    areas = gis.class_areas(ctx.towns, layer, "grp")
    entry["towns_outside_municipalities_with_file"] = sum(1 for k in ctx.areas if k[:5] not in munis)
    out = {}
    for feat, fs in spec["features"].items():
        out[feat] = {}
        for key, total in ctx.areas.items():
            a = areas.get(key, {})
            if key[:5] not in munis:
                out[feat][key] = Feature(None, "missing")
                continue
            out[feat][key] = gis.share(total, a.get(fs["codes"], 0.0), total - a.get("unknown", 0.0),
                                       ctx.min_coverage(spec))
    return out


def ward_coverage(file_names: dict[str, str], town_names: dict[str, str]) -> dict[str, str]:
    """{区のコード: 市のコード}。政令指定都市の用途地域は市の単位（例 27140 堺市）で配布され、町丁目は区の単位
    （27141 堺市堺区）なので、自分のファイルが無く、名前が「その市の名前＋…区」の区は市のファイルで覆われる。"""
    out = {}
    for ward, name in town_names.items():
        if ward in file_names or not name.endswith("区"):
            continue
        cities = [c for c, n in file_names.items() if c[:2] == ward[:2] and name.startswith(n) and len(name) > len(n)]
        if len(cities) == 1:
            out[ward] = cities[0]
    return dict(sorted(out.items()))


def landuse(ctx: Ctx, spec: dict, entry: dict):
    allowed = {c for cs in spec["codes"].values() for c in cs}
    areas: dict = {}
    codes = Counter()
    for mesh, path in fetch_meshes(ctx, "landuse", spec, entry):
        gdf = read_checked(path, spec["member"].format(mesh=mesh), spec, "landuse")
        codes += check_codes(gdf, spec["class_field"], allowed, "landuse")
        minx, miny, maxx, maxy = gdf.total_bounds
        sub = ctx.towns.cx[minx:maxx, miny:maxy]
        gis.class_areas(sub, gdf, spec["class_field"], into=areas)
    entry["cells_by_code"] = dict(sorted(codes.items()))
    out = {}
    for feat, fs in spec["features"].items():
        want = set(spec["codes"][fs["codes"]])
        out[feat] = {}
        for key, total in ctx.areas.items():
            a = areas.get(key, {})
            out[feat][key] = gis.share(total, sum(v for c, v in a.items() if c in want), sum(a.values()),
                                       ctx.min_coverage(spec))
    return out


def land_price(ctx: Ctx, spec: dict, entry: dict):
    path = fetch_pref(ctx, "land_price", spec)
    gdf = read_checked(path, spec["member"].format(pref=ctx.pref), spec, "land_price")
    vals = numeric(gdf, spec["value_field"], set(), "land_price")
    if any(not v > 0 for v in vals):
        raise Stop("land_price: a posted price is not positive")
    gdf = gdf.assign(price=vals)
    medians, outside = gis.points_median(ctx.towns, gdf, "price")
    ranks = gis.percentile_rank(medians)
    entry.update(points=len(gdf), points_outside_towns=outside, towns_with_points=len(medians))
    return {"land_price_pref_pct": {k: (Feature.of(ranks[k]) if k in ranks else Feature(None, "missing"))
                                    for k in ctx.areas}}


def agri_polygons(ctx: Ctx, spec: dict, entry: dict, key: str):
    """農業集落境界を読み、図形を recognized / not_recognized / uncertain に分ける（列 grp）。"""
    path = fetch_url(ctx, key, "boundary" if key != "agri_settlement" else ctx.pref,
                     spec["url_template"].format(pref=ctx.pref))
    gdf = read_checked(path, spec["member"].format(pref=ctx.pref), spec, key)
    check_codes(gdf, spec["flag_field"], set(spec["flag_values"]), key)
    rcom, flag = gdf[spec["settlement_field"]].astype(str), gdf[spec["flag_field"]].astype(str)
    notrec = rcom.isin(spec["not_recognized_codes"])
    if (notrec & (flag == "0")).any():
        raise Stop(f"{key}: {int((notrec & (flag == '0')).sum())} polygons have RCOM 999/-99 "
                   f"but {spec['flag_field']}=0")
    grp = notrec.map({True: "not_recognized", False: "recognized"}).where(~(~notrec & (flag != "0")), "uncertain")
    gdf = gdf.assign(grp=grp)
    entry["polygons"] = dict(Counter(grp))
    unc = gdf[grp == "uncertain"]
    entry["uncertain"] = {"rcom": dict(Counter(unc[spec["settlement_field"]].astype(str))),
                          "names": sorted({str(n) for n in unc.get("RCOM_NAME", []) if n == n})[:10],
                          "area_km2": round(sum(gis.geodesic_areas(unc.to_crs(gis.CRS).geometry)) / 1e6, 3)}
    return gdf


def agri_settlement(ctx: Ctx, spec: dict, entry: dict):
    gdf = agri_polygons(ctx, spec, entry, "agri_settlement")
    areas = gis.class_areas(ctx.towns, gdf.to_crs(gis.CRS).dissolve(by="grp", as_index=False), "grp")
    out = {}
    for key, total in ctx.areas.items():
        a = areas.get(key, {})
        out[key] = gis.share(total, a.get("recognized", 0.0), a.get("recognized", 0.0) + a.get("not_recognized", 0.0),
                             ctx.min_coverage(spec))
    return {"agri_settlement_overlap": out}


def _xlsx_rows(path: Path) -> list[tuple]:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return [tuple(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    finally:
        wb.close()


def repair_polygons(gdf, key_field: str, tolerance: float, entry: dict, key: str):
    """不正な図形（自己交差など）を make_valid で直し、面の部分だけ残す。直した図形のキーを entry に記録する。

    dissolve が位相の矛盾で落ちるのを防ぐためで、面積が相対 tolerance を超えて変わるなら直さずに停止する。"""
    import shapely
    bad = ~gdf.geometry.is_valid
    if not bad.any():
        return gdf
    fixed, repaired = gdf.geometry.copy(), []
    for i in gdf.index[bad]:
        g = gdf.geometry[i]
        parts = [p for p in shapely.get_parts(shapely.make_valid(g)) if p.geom_type in ("Polygon", "MultiPolygon")]
        new = shapely.union_all(parts) if parts else None
        change = abs(new.area - g.area) / g.area if new is not None and g.area > 0 else math.inf
        if change > tolerance:
            raise Stop(f"{key}: repairing {gdf[key_field][i]} changes its area by {change:.2e} (> {tolerance})")
        fixed[i] = new
        repaired.append(str(gdf[key_field][i]))
    entry["repaired_polygons"] = repaired
    return gdf.set_geometry(fixed)


def settlement_types(path: Path, spec: dict) -> dict[str, str]:
    """{農業集落コード: 農業地域類型（1次分類）}。見出しや値が定義と違えば停止する。"""
    rows = _xlsx_rows(path)
    if not rows or [str(c) for c in rows[0]] != spec["types_header"]:
        raise Stop(f"rural_settlement: type table header {rows[0] if rows else None} != {spec['types_header']}")
    ki, ti, ri = (spec["types_header"].index(spec[f]) for f in ("types_key", "types_field", "types_rcom"))
    allowed = {c for cs in spec["type_codes"].values() for c in cs} | set(spec["type_not_applicable"])
    out, bad = {}, Counter()
    for r in rows[1:]:
        if r[ri] is None or str(r[ri]) == "000":   # 都道府県・市区町村・旧市区町村の行
            continue
        v = str(r[ti])
        if v not in allowed:
            bad[v] += 1
        out[str(r[ki])] = v
    if bad:
        raise Stop(f"rural_settlement: unexpected {spec['types_field']} values {dict(bad)}")
    return out


def rural_settlement(ctx: Ctx, spec: dict, entry: dict):
    """農業地域類型が「都市的地域」でない（平地・中間・山間）農業集落との重なり。"""
    bspec = ctx.cfg["datasets"][spec["boundary_from"]]
    gdf = agri_polygons(ctx, bspec, entry, "rural_settlement")
    types = settlement_types(fetch_pref(ctx, "rural_settlement", spec), spec)
    code_grp = {c: g for g, cs in spec["type_codes"].items() for c in cs}
    rec = gdf.grp == "recognized"
    keys = gdf[bspec["key_field"]].astype(str)
    untyped = sorted(set(keys[rec & ~keys.map(types).isin(code_grp)]))
    if untyped:
        raise Stop(f"rural_settlement: {len(untyped)} recognized settlements have no type, e.g. {untyped[:5]}")
    gdf = gdf.assign(grp=gdf.grp.where(~rec, keys.map(types).map(code_grp)))
    entry["polygons_by_type"] = dict(Counter(gdf.grp))
    areas = gis.class_areas(ctx.towns, gdf.to_crs(gis.CRS).dissolve(by="grp", as_index=False), "grp")
    out = {}
    for key, total in ctx.areas.items():
        a = areas.get(key, {})
        known = a.get("rural", 0.0) + a.get("urban", 0.0) + a.get("not_recognized", 0.0)
        out[key] = gis.share(total, a.get("rural", 0.0), known, ctx.min_coverage(spec))
    return {"rural_settlement_overlap": out}


def fishery_settlement(ctx: Ctx, spec: dict, entry: dict):
    """漁業集落との重なり。漁業集落は沿海市区町村の漁業地区の中にだけ設定される（漁業センサスの定義）。"""
    resp = ctx.fetcher.get(spec["page"])
    if resp is None:
        raise Stop(f"fishery_settlement: listing page {spec['page']} could not be fetched")
    listed = set(re.findall(spec["pref_regex"], resp.body.decode("utf-8", "replace")))
    if len(listed) < spec["min_listed_prefectures"]:
        raise Stop(f"fishery_settlement: only {len(listed)} prefectures listed on {spec['page']}")
    page = estat.Fetched("fishery_settlement:listing", True, spec["page"], hashlib.sha256(resp.body).hexdigest(),
                         resp.body)
    if ctx.pref not in listed:
        _keep(ctx, "fishery_settlement", "listing", page)
        entry["coastal"] = f"都道府県 {ctx.pref} の漁業集落境界は配布されていない（沿海市区町村なし）"
        return {"fishery_settlement_overlap": {k: Feature.of(0.0) for k in ctx.areas}}
    gdf = read_checked(fetch_pref(ctx, "fishery_settlement", spec), spec["member"].format(pref=ctx.pref), spec,
                       "fishery_settlement")
    other = gdf[spec["settlement_field"]].astype(str) == spec["other_code"]
    names = Counter(gdf.loc[other, spec["settlement_name_field"]].astype(str))
    if set(names) - {spec["other_name"]}:
        raise Stop(f"fishery_settlement: {spec['settlement_field']}={spec['other_code']} named {dict(names)}")
    gdf = repair_polygons(gdf, spec["key_field"], spec["repair_area_tolerance"], entry, "fishery_settlement")
    gdf = gdf.assign(grp=other.map({True: "other", False: "settlement"}))
    files = {ctx.pref + str(c): str(n) for c, n in zip(gdf[spec["city_field"]], gdf[spec["city_name_field"]])}
    covered = set(files) | set(ward_coverage(files, ctx.municipality_names))
    entry.update(polygons=dict(Counter(gdf.grp)), coastal_municipalities=len(files),
                 towns_in_coastal_municipalities=sum(1 for k in ctx.areas if k[:5] in covered))
    areas = gis.class_areas(ctx.towns, gdf.to_crs(gis.CRS).dissolve(by="grp", as_index=False), "grp")
    out, stray = {}, 0
    for key, total in ctx.areas.items():
        a = areas.get(key, {})
        s = a.get("settlement", 0.0)
        if key[:5] in covered:
            out[key] = gis.share(total, s, s + a.get("other", 0.0), ctx.min_coverage(spec))
        elif total > 0 and s / total < spec["outside_tolerance"]:
            out[key] = Feature.of(0.0)
        else:
            out[key], stray = Feature(None, "missing" if total > 0 else "undefined"), stray + (total > 0)
    entry["outside_coastal_but_overlapping"] = stray
    return {"fishery_settlement_overlap": out}


def metro(ctx: Ctx, spec: dict, entry: dict):
    """三大都市圏（関東・中京・近畿大都市圏）の中心市・周辺市町村なら 1（令和2年国勢調査の定義）。"""
    rows = _xlsx_rows(fetch_pref(ctx, "metro", spec))
    hdr = [str(c) for c in rows[spec["header_row"]]]
    missing = [c for c in [spec["code_column"], spec["flag_column"], *spec["areas"]] if c not in hdr]
    if missing:
        raise Stop(f"metro: columns {missing} not in header {hdr}")
    ci, fi = hdr.index(spec["code_column"]), hdr.index(spec["flag_column"])
    ai = [hdr.index(c) for c in spec["areas"]]
    table = {}
    for r in rows[spec["header_row"] + 1:]:
        code = str(r[ci]) if r[ci] is not None else ""
        if not re.fullmatch(r"\d{5}", code):
            continue
        if r[fi] not in (None, *spec["flag_values"]):
            raise Stop(f"metro: {code} has {spec['flag_column']}={r[fi]!r}")
        inside = any(r[i] for i in ai)
        if inside and r[fi] is None:
            raise Stop(f"metro: {code} is in {spec['areas']} but has no {spec['flag_column']}")
        table[code] = 1.0 if inside else 0.0
    munis = {k[:5] for k in ctx.areas}
    entry.update(municipalities_in_table=len(table), municipalities_not_in_table=sorted(munis - set(table)),
                 municipalities_in_three_metros=sum(1 for m in munis if table.get(m) == 1.0))
    return {"metro_core_flag": {k: Feature.of(table[k[:5]]) if k[:5] in table else Feature(None, "missing")
                                for k in ctx.areas}}


def _housing_table(rows: list[tuple], spec: dict) -> dict[str, tuple]:
    """{市区町村コード: (総数, 二次的住宅)}。「-」（該当数値なし又は数字が得られない）は None。"""
    title = " ".join(str(c) for r in rows[:3] for c in r if c)
    if any(t not in title for t in spec["title_must_contain"]):
        raise Stop(f"vacation_home: title {title!r} lacks {spec['title_must_contain']}")
    hi = next((i for i, r in enumerate(rows) if spec["total_label"] in r and spec["part_label"] in r), None)
    if hi is None:
        raise Stop(f"vacation_home: no header row with {spec['total_label']} and {spec['part_label']}")
    ti, pi = rows[hi].index(spec["total_label"]), rows[hi].index(spec["part_label"])
    out = {}
    for r in rows[hi + 1:]:
        m = re.match(r"(\d{5})_", str(r[spec["area_column"]] or ""))
        if not m:
            continue
        vals = []
        for v in (r[ti], r[pi]):
            if v == spec["unknown_symbol"]:
                vals.append(None)
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                vals.append(float(v))
            else:
                raise Stop(f"vacation_home: {m.group(1)} has value {v!r}")
        out[m.group(1)] = tuple(vals)
    return out


def vacation_home(ctx: Ctx, spec: dict, entry: dict):
    """市区町村の総住宅数に占める二次的住宅の割合を、その市区町村の町丁目に付ける。"""
    table = _housing_table(_xlsx_rows(fetch_pref(ctx, "vacation_home", spec)), spec)
    munis = {k[:5] for k in ctx.areas}
    per = {}
    for m in munis & set(table):
        total, part = table[m]
        if total is None or part is None:
            per[m] = Feature(None, "missing")
        elif total == 0:
            per[m] = Feature(None, "undefined")
        else:
            per[m] = Feature.of(round(part / total, 6))
    entry.update(municipalities_published=len(munis & set(table)),
                 municipalities_not_published=sorted(munis - set(table)),
                 municipalities_with_symbol=sorted(m for m, f in per.items() if f.state == "missing"))
    return {"vacation_home_share_muni": {k: per.get(k[:5], Feature(None, "missing")) for k in ctx.areas}}


def establishment_counts(path: Path, spec: dict, member: str) -> dict[str, int]:
    """{KEY_CODE: 事業所数}。1行目が列コード、2行目が見出し。「-」は 0（境界データの事業所数と照合する）。"""
    import csv
    with zipfile.ZipFile(path) as zf:
        lines = zf.read(resolve_member(path, member)).decode(spec["encoding"]).splitlines()
    rows = list(csv.reader(lines))
    codes, labels = rows[0], rows[1]
    if spec["value_column"] not in codes or labels[codes.index(spec["value_column"])] != spec["value_label"]:
        raise Stop(f"establishments: column {spec['value_column']} ({spec['value_label']}) not found in {codes[:8]}")
    ki, vi = codes.index(spec["key_column"]), codes.index(spec["value_column"])
    out, bad = {}, Counter()
    for r in rows[2:]:
        v = r[vi]
        if v.isdigit():
            out[r[ki]] = int(v)
        elif v == spec["zero_symbol"]:
            out[r[ki]] = 0
        else:
            bad[v] += 1
    if bad:
        raise Stop(f"establishments: unexpected values {dict(bad)}")
    return out


def establishments(ctx: Ctx, spec: dict, entry: dict):
    """経済センサスの町丁・大字の事業所数を、境界が国勢調査の町丁目と1対1で重なる所だけ移す。"""
    counts = establishment_counts(fetch_url(ctx, "establishments", "table",
                                            spec["table_url_template"].format(pref=ctx.pref)), spec,
                                  spec["table_member"].format(pref=ctx.pref))
    bpath = fetch_url(ctx, "establishments", "boundary", spec["boundary_url_template"].format(pref=ctx.pref))
    bnd = read_checked(bpath, spec["boundary_member"].format(pref=ctx.pref), spec["boundary"], "establishments")
    kf = spec["boundary"]["key_field"]
    bnd = bnd.assign(**{kf: bnd[kf].astype(str)})
    bnd = bnd[bnd[kf].str.fullmatch(r"\d+")]
    check = bnd.groupby(kf)[spec["boundary"]["check_field"]].sum()
    wrong = {k: (int(v), counts.get(k)) for k, v in check.items() if counts.get(k) != int(v)}
    if wrong:
        raise Stop(f"establishments: table and boundary disagree for {len(wrong)} areas, e.g. {list(wrong.items())[:5]}")
    other = {k: v for k, v in counts.items() if k[5:].strip("9") == "" and v > 0}
    matched = gis.match_units(ctx.towns, bnd.dissolve(by=kf, as_index=False), kf, spec["min_match_share"])
    entry.update(areas_in_table=len(counts), areas_with_polygon=len(check),
                 areas_without_polygon=len(set(counts) - set(check.index)),
                 unlocated_establishments={k[:5]: v for k, v in sorted(other.items())},
                 towns_matched=len(matched), towns_not_matched=len(ctx.areas) - len(matched),
                 areas_not_matched=len(set(check.index) - set(matched.values())))
    return {"establishments": {t: Feature.of(counts[u]) for t, u in matched.items()}}


def elevation(ctx: Ctx, spec: dict, entry: dict):
    fields = {feat: fs["value_field"] for feat, fs in spec["features"].items()}
    invalid = set(spec.get("invalid_values", []))
    sums: dict = {}
    invalid_cells = Counter()
    for mesh, path in fetch_meshes(ctx, "elevation", spec, entry):
        gdf = read_checked(path, spec["member"].format(mesh=mesh), spec, "elevation")
        for f in fields.values():
            invalid_cells[f] += int(gdf[f].astype(str).isin(invalid).sum())
            gdf[f] = numeric(gdf, f, invalid, "elevation")
        minx, miny, maxx, maxy = gdf.to_crs(gis.CRS).total_bounds
        sub = ctx.towns.cx[minx:maxx, miny:maxy]
        gis.value_area_sums(sub, gdf, fields.values(), into=sums)
    entry["invalid_cells"] = dict(invalid_cells)
    return {feat: {key: gis.mean_feature(total, sums.get(key, {}).get(f), ctx.min_coverage(spec))
                   for key, total in ctx.areas.items()}
            for feat, f in fields.items()}


HANDLERS = {"did": did, "zoning": zoning, "landuse": landuse, "land_price": land_price,
            "agri_settlement": agri_settlement, "elevation": elevation, "rural_settlement": rural_settlement,
            "fishery_settlement": fishery_settlement, "metro": metro, "vacation_home": vacation_home,
            "establishments": establishments}


# ---------------------------------------------------------------------------
# 実行

def run(conn, raw_dir: Path, cfg: dict, pref: str, *, probe: bool, transport=None, sleep=None,
        user_agent: str = "landline-atlas/1.0", datasets: list[str] | None = None, refresh: bool = False) -> dict:
    pref = prefecture_code(pref)
    keys = datasets or list(cfg["datasets"])
    unknown = [k for k in keys if k not in HANDLERS or k not in cfg["datasets"]]
    report: dict = {"prefecture": pref, "census_year": cfg["census_year"], "boundary_version": cfg["boundary_version"],
                    "probe": probe, "datasets": {}, "towns": None, "imported": False, "stopped": None}
    if unknown:
        report["stopped"] = f"unknown datasets {unknown}"
        return report
    try:
        towns, report["towns"] = load_towns(conn, raw_dir, cfg, pref)
    except Stop as exc:
        report["stopped"] = str(exc)
        return report
    kw = {"sleep": sleep} if sleep else {}
    fetcher = Fetcher(transport or UrllibTransport(timeout=300.0), user_agent=user_agent, min_interval=3.0,
                      log=None if probe else make_logger(conn, "official"), **kw)
    with tempfile.TemporaryDirectory(prefix="atlas-gis-") as tmp:
        cache = RawCache(conn, raw_dir, refresh=refresh, probe=probe)
        ctx = Ctx(fetcher, towns, cfg, pref, Path(tmp), cache)
        ctx.municipality_names = report["towns"].pop("municipality_names")
        results = {}
        for key in keys:
            entry = report["datasets"].setdefault(key, {"name": cfg["datasets"][key]["name"]})
            try:
                results[key] = HANDLERS[key](ctx, cfg["datasets"][key], entry)
            except Stop as exc:
                entry["stopped"] = str(exc)
                report["stopped"] = str(exc)
                break
            entry["features"] = {f: dict(Counter(x.state for x in v.values())) for f, v in results[key].items()}
        report["reused_raw_files"] = len(cache.reused)
        if probe or report["stopped"]:
            return report
        for key, feats in results.items():
            entry = report["datasets"][key]
            source_id = _register(conn, raw_dir, ctx, key, cfg["datasets"][key])
            load_id = new_load(conn, "gis", source_id, note=f"fetch-gis {key} {pref}")
            rows = [(k, feat, Feature(f.value, f.state, source_id)) for feat, v in feats.items() for k, f in v.items()]
            entry["written"] = put(conn, load_id, cfg["census_year"], cfg["boundary_version"], rows)
    report["imported"] = True
    return report


def _register(conn, raw_dir: Path, ctx: Ctx, key: str, spec: dict) -> str:
    """原本を保管して出典を登録する。複数ファイル（メッシュ）は束ねた出典を1つ作り、特徴量はそれを指す。"""
    ids, shas = [], []
    for unit, f, _ in ctx.files[key]:
        store(conn, raw_dir, f, key, unit, ctx.pref, ctx.cache.fetched_at.get(f.url))
        sid = f"gis:{key}:{unit}:{f.sha256[:12]}"
        estat_tables.register(conn, sid, f"{spec['name']}（{unit}）", f.url, spec["source_year"], spec["license"],
                              f.sha256)
        ids.append(sid)
        shas.append(f.sha256)
    if len(ids) == 1:
        return ids[0]
    combined = hashlib.sha256("\n".join(sorted(shas)).encode()).hexdigest()
    sid = f"gis:{key}:{ctx.pref}:{combined[:12]}"
    estat_tables.register(conn, sid, f"{spec['name']}（都道府県 {ctx.pref} で使った {len(ids)} ファイル："
                          f"{', '.join(u for u, _, _ in ctx.files[key])}）", spec["page"], spec["source_year"],
                          spec["license"], combined)
    return sid


def _ext(body: bytes) -> str:
    if not estat.is_zip(body):
        return ".html" if body.lstrip()[:1] == b"<" else ".bin"
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        return ".xlsx" if "[Content_Types].xml" in zf.namelist() else ".zip"


def _write_raw(raw_dir: Path, sha: str, body: bytes) -> str:
    """原本を raw/gis/<sha の先頭2文字>/<sha><拡張子> に置く（同じ内容は1つ）。raw_dir からの相対パスを返す。"""
    path = raw_dir / "gis" / sha[:2] / f"{sha}{_ext(body)}"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent)
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        os.replace(tmp, path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        raise RuntimeError(f"stored GIS file {path} does not match {sha}")
    return str(path.relative_to(raw_dir))


def store(conn, raw_dir: Path, fetched: estat.Fetched, dataset: str, unit: str, pref: str,
          fetched_at: str | None = None) -> str:
    """原本を raw/gis/ に保存し、regions.raw_gis に記録する。同じ内容は1つだけ保存する。"""
    stored = _write_raw(raw_dir, fetched.sha256, fetched.body)
    conn.execute("INSERT OR IGNORE INTO regions.raw_gis VALUES (?,?,?,?,?,?,?)",
                 (fetched.sha256, fetched.url, stored, dataset, unit, pref, fetched_at or utc_now()))
    return fetched.sha256


STATE_JA = {"observed": "値あり", "zero": "0", "missing": "資料なし(missing)", "undefined": "面積0(undefined)"}


def render(report: dict) -> str:
    lines = [f"GIS 特徴量 {report['census_year']}年境界・都道府県 {report['prefecture']}"
             f"（{'確認モード：DBには書き込みません' if report['probe'] else '取込'}）", ""]
    t = report.get("towns")
    if t:
        lines.append(f"■ 町丁目の形：e-Stat 境界 {t['towns_in_boundary']:,} 町丁目（町丁目マスタ {t['towns_in_master']:,}、"
                     f"形の無いマスタ {t['master_without_polygon']:,}、マスタに無い形 {t['polygon_without_master']:,}、"
                     f"町丁目に属さない図形 {t['polygons_without_town']:,}）")
        lines.append("")
    for key, d in report["datasets"].items():
        lines.append(f"■ {key}：{d['name']}")
        for k, v in d.items():
            if k not in ("name", "features"):
                lines.append(f"  {k}: {v}")
        for feat, counts in d.get("features", {}).items():
            lines.append(f"  {feat}: " + "、".join(f"{STATE_JA[s]} {counts.get(s, 0):,}" for s in STATES))
        lines.append("")
    if report.get("reused_raw_files"):
        lines.append(f"保管済みの原本を再利用: {report['reused_raw_files']} ファイル（取り直すには --refresh）")
    if report["stopped"]:
        lines.append(f"停止しました（取り込んでいません）: {report['stopped']}")
    elif report["probe"]:
        lines.append("確認だけ行いました（DB には書いていません。取得した原本は raw/gis に残し、取込で再利用します）。")
    else:
        lines.append(f"取り込みました。次は: atlas classify --census-year {report['census_year']}"
                     f" --boundary-version {report['boundary_version']}")
    return "\n".join(lines)
