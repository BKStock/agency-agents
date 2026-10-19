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
