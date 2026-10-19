"""B0/B1/B2 ベースライン比較（標準ライブラリのみ）。

* B0: 町丁目の人口・住宅・地域統計
* B1: B0 + 番号区画・GIS 由来の地域情報
* B2: B1 + 公開施設で観測した市内局番

正解は gold_label（判定者・根拠付き）だけを使う。分割はグループ単位
（既定は市区町村、最低でも町丁目）で、同じ町丁目・施設が学習と評価の
両方に入らない。B2 が B1 より良いと言えるのは、グループ単位ブートストラップ
の95%区間が 0 を含まない場合だけ。
"""

from __future__ import annotations

import hashlib
import math
import random
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass

from .engine import load_inputs
from .features import derive

B0_FEATURES = ("detached_ratio", "apartment_ratio", "owner_ratio", "rental_ratio", "public_rental_ratio",
               "aging_rate", "pop_change_10y", "pop_density", "estab_per_household", "did_flag")
B1_EXTRA = ("agri_settlement_overlap", "fishery_settlement_overlap", "farmland_ratio", "forest_ratio",
            "slope_mean_deg", "elevation_mean_m", "zoning_residential_ratio", "zoning_commercial_ratio",
            "land_price_pref_pct", "metro_core_flag", "uea_member", "vacation_home_share_muni")
BASELINES = ("B0", "B1", "B2")


@dataclass
class Sample:
    town_key: str
    group: str
    prefecture: str
    label: str
    numeric: dict[str, float]
    tokens: frozenset[str]


def load_samples(conn: sqlite3.Connection, axis: str, baseline: str, census_year: int,
                 boundary_version: str, *, group_by: str = "municipality") -> tuple[list[Sample], dict]:
    if baseline not in BASELINES:
        raise ValueError(baseline)
    labels: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute("SELECT town_key, class_code FROM gold_label WHERE axis = ? AND census_year = ?"
                          " AND boundary_version = ?", (axis, census_year, boundary_version)):
        labels[r["town_key"]].add(r["class_code"])
    disagreements = sorted(k for k, v in labels.items() if len(v) > 1)

    areas: dict[str, set[str]] = defaultdict(set)
    codes: dict[str, set[str]] = defaultdict(set)
    if baseline in ("B1", "B2"):
        for r in conn.execute("SELECT DISTINCT municipality_code, number_area_code FROM number_area_municipality"):
            areas[r["municipality_code"]].add(f"na:{r['number_area_code']}")
    if baseline == "B2":
        for r in conn.execute("SELECT town_key, area_code, local_code FROM town_observed_code"
                              " WHERE census_year = ? AND boundary_version = ?", (census_year, boundary_version)):
            codes[r["town_key"]].add(f"lc:{r['area_code']}-{r['local_code']}")

    names = B0_FEATURES + (B1_EXTRA if baseline != "B0" else ())
    samples = []
    for t in load_inputs(conn, census_year, boundary_version):
        key = t.town.town_key
        if key not in labels or key in disagreements or t.town.secrecy_state == "merged_into_other":
            continue
        feats = derive(t.features)
        numeric = {}
        for n in names:
            f = feats.get(n)
            if f is not None and f.usable:
                numeric[n] = math.log1p(f.value) if n == "pop_density" else f.value
        tokens = frozenset(areas.get(t.town.municipality_code, set()) | codes.get(key, set()))
        group = t.town.municipality_code if group_by == "municipality" else key
        samples.append(Sample(key, group, t.town.prefecture_code, next(iter(labels[key])), numeric, tokens))
    return samples, {"gold_towns": len(labels), "excluded_disagreement": len(disagreements)}


class NearestCentroid:
    """標準化した数値特徴＋0/1トークンでの最近傍重心。欠損は学習平均で補い欠損指標を加える。"""

    def fit(self, samples: list[Sample], feature_names: tuple[str, ...]):
        self.names = feature_names
        self.mean, self.std = {}, {}
        for n in feature_names:
            vals = [s.numeric[n] for s in samples if n in s.numeric]
            m = sum(vals) / len(vals) if vals else 0.0
            var = sum((v - m) ** 2 for v in vals) / len(vals) if vals else 0.0
            self.mean[n], self.std[n] = m, math.sqrt(var) or 1.0
        self.vocab = sorted({tok for s in samples for tok in s.tokens})
        by_label = defaultdict(list)
        for s in samples:
            by_label[s.label].append(self._vector(s))
        self.centroids = {lab: [sum(col) / len(vecs) for col in zip(*vecs)] for lab, vecs in by_label.items()}
        return self

    def _vector(self, s: Sample) -> list[float]:
        vec = []
        for n in self.names:
            present = n in s.numeric
            vec.append(((s.numeric[n] if present else self.mean[n]) - self.mean[n]) / self.std[n])
            vec.append(0.0 if present else 1.0)
        vec.extend(1.0 if tok in s.tokens else 0.0 for tok in self.vocab)
        return vec

    def rank(self, s: Sample) -> list[str]:
        v = self._vector(s)
        dist = {lab: sum((a - b) ** 2 for a, b in zip(v, c)) for lab, c in self.centroids.items()}
        return sorted(dist, key=lambda lab: (dist[lab], lab))


def _fold(group: str, k: int) -> int:
    return int(hashlib.sha256(group.encode()).hexdigest(), 16) % k


def cross_validate(samples: list[Sample], baseline: str, *, k: int = 5,
                   holdout_prefectures: tuple[str, ...] = ()) -> list[tuple[Sample, list[str]]]:
    """out-of-fold の順位付き予測を返す。holdout 指定時はその都道府県だけを評価に使う。"""
    names = B0_FEATURES + (B1_EXTRA if baseline != "B0" else ())
    if holdout_prefectures:
        train = [s for s in samples if s.prefecture not in holdout_prefectures]
        test = [s for s in samples if s.prefecture in holdout_prefectures]
        model = NearestCentroid().fit(train, names)
        return [(s, model.rank(s)) for s in test]
    preds = []
    for fold in range(k):
        train = [s for s in samples if _fold(s.group, k) != fold]
        test = [s for s in samples if _fold(s.group, k) == fold]
        if not train or not test:
            continue
        assert not {s.group for s in train} & {s.group for s in test}
        model = NearestCentroid().fit(train, names)
        preds.extend((s, model.rank(s)) for s in test)
    return preds


def metrics(preds: list[tuple[Sample, list[str]]]) -> dict:
    labels = sorted({s.label for s, _ in preds} | {r[0] for _, r in preds if r})
    confusion = {a: Counter() for a in labels}
    top1 = top3 = 0
    for s, ranking in preds:
        confusion[s.label][ranking[0]] += 1
        top1 += ranking[0] == s.label
        top3 += s.label in ranking[:3]
    per_class = {}
    for lab in labels:
        tp = confusion[lab][lab]
        fp = sum(confusion[o][lab] for o in labels if o != lab)
        fn = sum(confusion[lab][o] for o in labels if o != lab)
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        per_class[lab] = {"precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0.0,
                          "support": tp + fn}
    supported = [v["f1"] for v in per_class.values() if v["support"]]
    n = len(preds)
    return {
        "n": n,
        "top1_accuracy": top1 / n if n else None,
        "top3_accuracy": top3 / n if n else None,
        "macro_f1": sum(supported) / len(supported) if supported else None,
        "per_class": per_class,
        "confusion": {a: dict(c) for a, c in confusion.items()},
    }


def _bootstrap_delta(a, b, metric: str, rounds: int, seed: int) -> tuple[float, float]:
    """グループ単位で再標本化した b - a の95%区間。a, b は同じ標本に対する予測。"""
    by_group_a, by_group_b = defaultdict(list), defaultdict(list)
    for (s, r) in a:
        by_group_a[s.group].append((s, r))
    for (s, r) in b:
        by_group_b[s.group].append((s, r))
    groups = sorted(set(by_group_a) & set(by_group_b))
    rng = random.Random(seed)
    deltas = []
    for _ in range(rounds):
        pick = [rng.choice(groups) for _ in groups]
        pa = [x for g in pick for x in by_group_a[g]]
        pb = [x for g in pick for x in by_group_b[g]]
        ma, mb = metrics(pa)[metric], metrics(pb)[metric]
        deltas.append((mb or 0.0) - (ma or 0.0))
    deltas.sort()
    return deltas[int(0.025 * rounds)], deltas[int(0.975 * rounds) - 1]


def compare_baselines(conn: sqlite3.Connection, axis: str, census_year: int, boundary_version: str, *,
                      k: int = 5, group_by: str = "municipality", holdout_prefectures: tuple[str, ...] = (),
                      bootstrap_rounds: int = 500, seed: int = 20261020) -> dict:
    results, preds = {}, {}
    for b in BASELINES:
        samples, info = load_samples(conn, axis, b, census_year, boundary_version, group_by=group_by)
        groups = {s.group for s in samples}
        labels = {s.label for s in samples}
        if holdout_prefectures:
            train_labels = {s.label for s in samples if s.prefecture not in holdout_prefectures}
            n_test = sum(1 for s in samples if s.prefecture in holdout_prefectures)
            if len(train_labels) < 2 or n_test == 0:
                return {"status": "not_evaluable",
                        "reason": f"holdout split empty (train_labels={len(train_labels)}, test={n_test})", **info}
        elif len(labels) < 2 or len(groups) < k:
            return {"status": "not_evaluable",
                    "reason": f"gold labels insufficient (labels={len(labels)}, groups={len(groups)}, k={k})",
                    **info}
        preds[b] = cross_validate(samples, b, k=k, holdout_prefectures=holdout_prefectures)
        results[b] = metrics(preds[b])
    comparisons = {}
    for lo, hi in (("B0", "B1"), ("B1", "B2")):
        for m in ("macro_f1", "top1_accuracy"):
            low, high = _bootstrap_delta(preds[lo], preds[hi], m, bootstrap_rounds, seed)
            point = (results[hi][m] or 0.0) - (results[lo][m] or 0.0)
            comparisons[f"{hi}_vs_{lo}_{m}"] = {
                "delta": point, "ci95": [low, high],
                "verdict": "improved" if low > 0 else ("worse" if high < 0 else "no_significant_difference"),
            }
    return {"status": "evaluated", "group_by": group_by, "k": k, "holdout_prefectures": list(holdout_prefectures),
            "baselines": results, "comparisons": comparisons}
