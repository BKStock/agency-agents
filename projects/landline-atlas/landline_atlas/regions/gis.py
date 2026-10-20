"""GIS 特徴量（geopandas / shapely / pyproj / rasterio は任意依存）。

* 保存座標系は JGD2011（EPSG:6668）
* 面積は GRS80 楕円体上の測地線面積。平面直角座標系の系番号に依存しない
* 対象レイヤの取得範囲（coverage）の外にかかる町丁目は 0 ではなく missing
* 面積 0（海域・境界外）は undefined
結果は (town_key, feature, Feature) の列で返し、features.put で保存する。
"""

from __future__ import annotations

import math
from typing import Iterable, Mapping

from .features import Feature

CRS = "EPSG:6668"


def _geod():
    from pyproj import Geod
    return Geod(ellps="GRS80")


def area_m2(geom) -> float:
    if geom is None or geom.is_empty:
        return 0.0
    return abs(_geod().geometry_area_perimeter(geom)[0])


def _union(gdf):
    gs = gdf.to_crs(CRS).geometry
    return gs.union_all() if hasattr(gs, "union_all") else gs.unary_union


def read_layer(path: str, *, key_field: str | None = None, layer: str | None = None):
    import geopandas as gpd
    gdf = gpd.read_file(path, layer=layer)
    if gdf.crs is None:
        raise ValueError(f"{path}: CRS undefined")
    if key_field is not None:
        if gdf[key_field].dtype.kind in "fi":
            raise ValueError(f"{path}: {key_field} was read as a number (leading zeros may be lost)")
        gdf[key_field] = gdf[key_field].astype(str)
    return gdf.to_crs(CRS)


def overlap(towns, polygons, feature: str, *, key_field: str = "town_key", coverage=None,
            source_id: str | None = None) -> list[tuple[str, str, Feature]]:
    target = _union(polygons) if len(polygons) else None
    cover = _union(coverage) if coverage is not None else None
    towns = towns.to_crs(CRS)
    out = []
    for key, geom in zip(towns[key_field], towns.geometry):
        total = area_m2(geom)
        if total == 0:
            f = Feature(None, "undefined", source_id)
        elif cover is not None and not cover.covers(geom):
            f = Feature(None, "missing", source_id)
        else:
            inter = area_m2(geom.intersection(target)) if target is not None else 0.0
            f = Feature.of(round(inter / total, 6), source_id)
        out.append((key, feature, f))
    return out


def category_ratios(towns, landuse, class_field: str, categories: Mapping[str, Iterable[str]], *,
                    key_field: str = "town_key", source_id: str | None = None):
    lu = landuse.to_crs(CRS)
    lu[class_field] = lu[class_field].astype(str)
    out = []
    for feature, codes in categories.items():
        subset = lu[lu[class_field].isin([str(c) for c in codes])]
        out += overlap(towns, subset, feature, key_field=key_field, coverage=lu, source_id=source_id)
    return out


def dem_stats(towns, dem_path: str, *, key_field: str = "town_key", source_id: str | None = None):
    """GeoTIFF の DEM から平均標高・平均傾斜（度）。有効画素がなければ missing。"""
    import numpy as np
    import rasterio
    from rasterio.mask import mask

    out = []
    with rasterio.open(dem_path) as src:
        towns = towns.to_crs(src.crs)
        for key, geom in zip(towns[key_field], towns.geometry):
            try:
                data, tf = mask(src, [geom], crop=True, filled=False)
            except ValueError:
                data = None
            band = None if data is None else data[0].astype("float64")
            if band is None or band.count() == 0:
                out += [(key, "elevation_mean_m", Feature(None, "missing", source_id)),
                        (key, "slope_mean_deg", Feature(None, "missing", source_id))]
                continue
            dx, dy = abs(tf.a), abs(tf.e)
            if src.crs.is_geographic:
                lat = math.radians(geom.centroid.y)
                dx, dy = dx * 111_320 * math.cos(lat), dy * 110_574
            gy, gx = np.gradient(band.filled(np.nan), dy, dx)
            slope = np.degrees(np.arctan(np.hypot(gx, gy)))
            valid = ~np.isnan(slope) & ~band.mask
            out.append((key, "elevation_mean_m", Feature.of(float(band.mean()), source_id)))
            out.append((key, "slope_mean_deg", Feature.of(float(slope[valid].mean()), source_id) if valid.any()
                        else Feature(None, "missing", source_id)))
    return out


# ---------------------------------------------------------------------------
# 町丁目 × 大きなレイヤ（数十万セルのメッシュなど）の集計。
# 上の overlap は対象レイヤを1つに union するため、L03-b（1次メッシュあたり 58 万セル）では終わらない。
# ここでは overlay（空間索引つき）で町丁目ごとの断片に切り、断片の測地線面積を足し上げる。
# 判定の約束（missing / undefined / 0 を混同しない）は overlap と同じ。

def geodesic_areas(geoms) -> list[float]:
    g = _geod()
    return [0.0 if x is None or x.is_empty else abs(g.geometry_area_perimeter(x)[0]) for x in geoms]


def town_areas(towns, *, key_field: str = "town_key") -> dict[str, float]:
    t = towns.to_crs(CRS)
    return dict(zip(t[key_field], geodesic_areas(t.geometry)))


def class_areas(towns, layer, class_field: str, *, key_field: str = "town_key",
                into: dict | None = None) -> dict[str, dict[str, float]]:
    """町丁目ごと・区分ごとの重なり面積（m²）。レイヤの図形同士は重ならない前提（呼び出し側で dissolve する）。

    メッシュの区画ごとに呼ぶときは into に足し上げる。"""
    import geopandas as gpd
    out = {} if into is None else into
    t = towns[[key_field, "geometry"]].to_crs(CRS)
    lay = layer[[class_field, "geometry"]].to_crs(CRS)
    if not len(t) or not len(lay):
        return out
    pieces = gpd.overlay(t, lay, how="intersection", keep_geom_type=True)
    for key, cls, a in zip(pieces[key_field], pieces[class_field].astype(str), geodesic_areas(pieces.geometry)):
        d = out.setdefault(key, {})
        d[cls] = d.get(cls, 0.0) + a
    return out


def share(total: float, part: float, known: float, min_coverage: float,
          source_id: str | None = None) -> Feature:
    """part / total。町丁目の面積のうち値が分かっている割合（known / total）が min_coverage 未満なら missing。"""
    if total <= 0:
        return Feature(None, "undefined", source_id)
    if known / total < min_coverage:
        return Feature(None, "missing", source_id)
    return Feature.of(round(min(part / total, 1.0), 6), source_id)


def value_area_sums(towns, cells, value_fields: Iterable[str], *, key_field: str = "town_key",
                    into: dict | None = None) -> dict[str, dict[str, list[float]]]:
    """町丁目ごと・値ごとの [Σ値×面積, Σ面積]。値が NaN のセル（無効）は足さない。

    メッシュの区画ごとに呼び、into に足し上げる（町丁目が区画をまたいでも1つにまとまる）。"""
    import geopandas as gpd
    fields = list(value_fields)
    out = {} if into is None else into
    t = towns[[key_field, "geometry"]].to_crs(CRS)
    if not len(t) or not len(cells):
        return out
    pieces = gpd.overlay(t, cells[fields + ["geometry"]].to_crs(CRS), how="intersection", keep_geom_type=True)
    areas = geodesic_areas(pieces.geometry)
    for name in fields:
        for key, v, a in zip(pieces[key_field], pieces[name], areas):
            if v != v:  # NaN
                continue
            s = out.setdefault(key, {}).setdefault(name, [0.0, 0.0])
            s[0] += float(v) * a
            s[1] += a
    return out


def mean_feature(total: float, sums: list[float] | None, min_coverage: float,
                 source_id: str | None = None) -> Feature:
    """面積加重平均。有効なセルの被覆（Σ面積 / 町丁目の面積）が min_coverage 未満なら missing。"""
    num, den = sums or (0.0, 0.0)
    if total <= 0:
        return Feature(None, "undefined", source_id)
    if den == 0 or den / total < min_coverage:
        return Feature(None, "missing", source_id)
    return Feature.of(round(num / den, 6), source_id)


def points_median(towns, points, value_field: str, *, key_field: str = "town_key") -> tuple[dict[str, float], int]:
    """町丁目に入る地点の値の中央値。戻り値の2つ目はどの町丁目にも入らなかった地点の数。"""
    import geopandas as gpd
    t = towns[[key_field, "geometry"]].to_crs(CRS)
    p = points[[value_field, "geometry"]].to_crs(CRS)
    joined = gpd.sjoin(p, t, how="left", predicate="within")
    outside = int(joined[key_field].isna().sum())
    med = joined.dropna(subset=[key_field]).groupby(key_field)[value_field].median()
    return {k: float(v) for k, v in med.items()}, outside


def percentile_rank(values: Mapping[str, float]) -> dict[str, float]:
    """値の順位を 0〜1 にする（最小 0・最大 1、同値は平均順位）。地点が1つの町丁目しかなければ順位は付けない。"""
    keys = sorted(values, key=lambda k: values[k])
    n = len(keys)
    if n < 2:
        return {}
    out, i = {}, 0
    while i < n:
        j = i
        while j + 1 < n and values[keys[j + 1]] == values[keys[i]]:
            j += 1
        r = ((i + j) / 2) / (n - 1)
        for k in keys[i:j + 1]:
            out[k] = round(r, 6)
        i = j + 1
    return out
