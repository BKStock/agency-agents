"""GIS 結合（geopandas / shapely / pyproj / rasterio は任意依存）。

* 保存座標系は JGD2011 地理座標（EPSG:6668）に統一する
* 面積は楕円体（GRS80）上の測地線面積で計算するので、平面直角座標系の
  系番号の選択や系をまたぐ都道府県（北海道・東京島しょ・沖縄・鹿児島）の
  扱いに依存しない
* 対象レイヤの取得範囲（coverage）外の町丁目は 0 ではなく missing にする
* 面積 0 の町丁目（海域・境界外）は undefined にする

結果は (town_key, feature_name, Feature) の列で返し、store.put_features で保存する。
エンジン本体は GIS ライブラリに依存しない。
"""

from __future__ import annotations

from typing import Iterable, Mapping

from .features import Feature

STORAGE_CRS = "EPSG:6668"


def _require():
    try:
        import geopandas as gpd
        from pyproj import Geod
    except ImportError as exc:  # pragma: no cover - 依存がない環境では使えないことを明示
        raise ImportError("gis features need geopandas, shapely and pyproj (pip install geopandas)") from exc
    return gpd, Geod(ellps="GRS80")


def geodesic_area_m2(geom) -> float:
    _, geod = _require()
    if geom is None or geom.is_empty:
        return 0.0
    area, _ = geod.geometry_area_perimeter(geom)
    return abs(area)


def load_layer(path: str, *, layer: str | None = None, key_field: str | None = None):
    """レイヤを読み EPSG:6668 に揃える。key_field は文字列として保持する（先頭ゼロ保護）。"""
    gpd, _ = _require()
    gdf = gpd.read_file(path, layer=layer)
    if gdf.crs is None:
        raise ValueError(f"{path}: CRS is not defined; set it explicitly before joining")
    if key_field:
        if gdf[key_field].dtype.kind in "fi":
            raise ValueError(f"{path}: {key_field} was read as a number; leading zeros may be lost")
        gdf[key_field] = gdf[key_field].astype(str)
    return gdf.to_crs(STORAGE_CRS)


def _prepare(towns, key_field: str):
    if str(towns.crs) != STORAGE_CRS and towns.crs.to_epsg() != 6668:
        towns = towns.to_crs(STORAGE_CRS)
    return towns[[key_field, "geometry"]].copy()


def overlap_ratio(towns, polygons, feature_name: str, *, key_field: str = "town_key",
                  coverage=None, source_id: str | None = None) -> list[tuple[str, str, Feature]]:
    """町丁目面積に対する polygons（集落境界など）との重複面積の比。"""
    gpd, _ = _require()
    towns = _prepare(towns, key_field)
    polys = polygons.to_crs(STORAGE_CRS)
    merged = polys.geometry.union_all() if hasattr(polys.geometry, "union_all") else polys.unary_union
    cover = None
    if coverage is not None:
        cover_gs = coverage.to_crs(STORAGE_CRS).geometry
        cover = cover_gs.union_all() if hasattr(cover_gs, "union_all") else cover_gs.unary_union
    out = []
    for key, geom in zip(towns[key_field], towns.geometry):
        out.append((key, feature_name, _ratio_feature(geom, merged, cover, source_id)))
    return out


def _ratio_feature(geom, target, cover, source_id) -> Feature:
    total = geodesic_area_m2(geom)
    if total == 0:
        return Feature(None, "undefined", source_id)
    if cover is not None and not cover.covers(geom):
        # 取得範囲外が含まれる → 重複 0 と未取得を区別できない
        return Feature(None, "missing", source_id)
    inter = geodesic_area_m2(geom.intersection(target))
    value = round(inter / total, 6)
    return Feature(value, "zero" if value == 0 else "observed", source_id)


def category_area_ratios(towns, landuse, class_field: str, categories: Mapping[str, Iterable[str]], *,
                         key_field: str = "town_key", coverage=None,
                         source_id: str | None = None) -> list[tuple[str, str, Feature]]:
    """土地利用メッシュ等の区分別面積比。categories は {特徴量名: [区分コード, ...]}。"""
    _require()
    lu = landuse.to_crs(STORAGE_CRS)
    lu[class_field] = lu[class_field].astype(str)
    out = []
    for feature_name, codes in categories.items():
        subset = lu[lu[class_field].isin([str(c) for c in codes])]
        out.extend(overlap_ratio(towns, subset, feature_name, key_field=key_field,
                                 coverage=coverage if coverage is not None else lu,
                                 source_id=source_id))
    return out


def dem_stats(towns, dem_path: str, *, key_field: str = "town_key",
              source_id: str | None = None) -> list[tuple[str, str, Feature]]:
    """DEM（GeoTIFF に変換済み）から平均標高と平均傾斜（度）。有効画素がなければ missing。"""
    import math

    import numpy as np
    import rasterio
    from rasterio.mask import mask

    towns = _prepare(towns, key_field)
    out = []
    with rasterio.open(dem_path) as src:
        towns = towns.to_crs(src.crs)
        geographic = src.crs.is_geographic
        for key, geom in zip(towns[key_field], towns.geometry):
            try:
                data, transform = mask(src, [geom], crop=True, filled=False)
            except ValueError:  # 範囲外
                out += [(key, "elevation_mean_m", Feature(None, "missing", source_id)),
                        (key, "slope_mean_deg", Feature(None, "missing", source_id))]
                continue
            band = data[0].astype("float64")
            if band.count() == 0:
                out += [(key, "elevation_mean_m", Feature(None, "missing", source_id)),
                        (key, "slope_mean_deg", Feature(None, "missing", source_id))]
                continue
            dx, dy = abs(transform.a), abs(transform.e)
            if geographic:
                lat = math.radians(geom.centroid.y)
                dx, dy = dx * 111_320 * math.cos(lat), dy * 110_574
            filled = band.filled(np.nan)
            gy, gx = np.gradient(filled, dy, dx)
            slope = np.degrees(np.arctan(np.hypot(gx, gy)))
            valid = ~np.isnan(slope) & ~band.mask
            elev = float(band.mean())
            out.append((key, "elevation_mean_m", Feature(elev, "zero" if elev == 0 else "observed", source_id)))
            if valid.any():
                s = float(np.mean(slope[valid]))
                out.append((key, "slope_mean_deg", Feature(s, "zero" if s == 0 else "observed", source_id)))
            else:
                out.append((key, "slope_mean_deg", Feature(None, "missing", source_id)))
    return out
