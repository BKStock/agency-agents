"""特徴量の導出。値は常に Feature(value, state) で扱い、欠損を 0 にしない。

生特徴量（国勢調査・経済センサス・GIS が出す値）の名前は RAW_FEATURES に、
導出特徴量の式は DERIVED に定義する。導出の入力のどれかが欠損・秘匿なら、
結果も同じ状態になる（最も悪い状態を伝播させる）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

# 状態の悪さ。複数入力の導出では最も悪い状態を採用する
_STATE_RANK = {"observed": 0, "zero": 0, "undefined": 1, "merged": 2, "suppressed": 3, "missing": 4}


@dataclass(frozen=True)
class Feature:
    value: float | None
    state: str
    source_id: str | None = None

    @property
    def usable(self) -> bool:
        return self.state in ("observed", "zero")


MISSING = Feature(None, "missing")

RAW_FEATURES = {
    # 国勢調査（町丁・字等別）
    "pop_total": "総人口",
    "pop_age_known": "年齢判明人口",
    "pop_65plus": "65歳以上人口",
    "pop_2010": "2010年総人口（同一境界に組み替え済み）",
    "pop_2015": "2015年総人口（同一境界に組み替え済み）",
    "households_general": "一般世帯数",
    "hh_in_housing": "住宅に住む一般世帯数",
    "hh_detached": "一戸建に住む世帯数",
    "hh_apartment": "共同住宅に住む世帯数",
    "hh_owned": "持ち家世帯数",
    "hh_rented_public": "公営・都市再生機構・公社の借家世帯数",
    "hh_rented_private": "民営の借家世帯数",
    "hh_company_housing": "給与住宅世帯数",
    "area_km2": "可住地を含む町丁目面積（km2）",
    "did_flag": "人口集中地区（DID）内=1",
    # 経済センサス
    "establishments": "事業所数",
    # GIS・地価・地域区分
    "agri_settlement_overlap": "農業集落ポリゴンとの重複率",
    "fishery_settlement_overlap": "漁業集落（または漁港勢力圏）との重複率",
    "farmland_ratio": "田・その他農用地の面積比",
    "forest_ratio": "森林の面積比",
    "slope_mean_deg": "平均傾斜（度）",
    "elevation_mean_m": "平均標高（m）",
    "zoning_residential_ratio": "住居系用途地域の面積比",
    "zoning_commercial_ratio": "商業系用途地域の面積比",
    "land_price_pref_pct": "地価の都道府県内パーセンタイル（0-1）",
    "metro_core_flag": "三大都市圏の中心市・周辺=1",
    "uea_member": "都市雇用圏に属する=1",
    "vacation_home_share_muni": "市区町村の二次的住宅比率（住宅・土地統計調査）",
}


def _ratio(num: str, den: str) -> Callable[[Mapping[str, Feature]], Feature]:
    def compute(f: Mapping[str, Feature]) -> Feature:
        a, b = f.get(num, MISSING), f.get(den, MISSING)
        bad = _worst(a, b)
        if bad:
            return bad
        if b.value == 0:
            return Feature(None, "undefined")
        return _value(a.value / b.value, a.source_id)
    return compute


def _sum_ratio(nums: tuple[str, ...], den: str) -> Callable[[Mapping[str, Feature]], Feature]:
    def compute(f: Mapping[str, Feature]) -> Feature:
        parts = [f.get(n, MISSING) for n in nums]
        b = f.get(den, MISSING)
        bad = _worst(*parts, b)
        if bad:
            return bad
        if b.value == 0:
            return Feature(None, "undefined")
        return _value(sum(p.value for p in parts) / b.value, parts[0].source_id)
    return compute


def _change(now: str, before: str) -> Callable[[Mapping[str, Feature]], Feature]:
    def compute(f: Mapping[str, Feature]) -> Feature:
        a, b = f.get(now, MISSING), f.get(before, MISSING)
        bad = _worst(a, b)
        if bad:
            return bad
        if b.value == 0:
            return Feature(None, "undefined")
        return _value((a.value - b.value) / b.value, a.source_id)
    return compute


def _worst(*features: Feature) -> Feature | None:
    worst = max(features, key=lambda x: _STATE_RANK[x.state])
    return None if worst.usable else Feature(None, worst.state)


def _value(v: float, source_id: str | None) -> Feature:
    return Feature(v, "zero" if v == 0 else "observed", source_id)


DERIVED: dict[str, Callable[[Mapping[str, Feature]], Feature]] = {
    "detached_ratio": _ratio("hh_detached", "hh_in_housing"),
    "apartment_ratio": _ratio("hh_apartment", "hh_in_housing"),
    "owner_ratio": _ratio("hh_owned", "hh_in_housing"),
    "rental_ratio": _sum_ratio(("hh_rented_public", "hh_rented_private", "hh_company_housing"), "hh_in_housing"),
    "public_rental_ratio": _ratio("hh_rented_public", "hh_in_housing"),
    "aging_rate": _ratio("pop_65plus", "pop_age_known"),
    "pop_change_10y": _change("pop_total", "pop_2010"),
    "pop_change_5y": _change("pop_total", "pop_2015"),
    "pop_density": _ratio("pop_total", "area_km2"),
    "estab_per_household": _ratio("establishments", "households_general"),
}


def derive(raw: Mapping[str, Feature]) -> dict[str, Feature]:
    """生特徴量に導出特徴量を加えた辞書を返す。生特徴量は上書きしない。"""
    out = dict(raw)
    for name, fn in DERIVED.items():
        if name not in out:
            out[name] = fn(raw)
    return out
