"""B0/B1/B2 の比較（標準ライブラリのみ）。

* B0 町丁目の人口・住宅・地域統計
* B1 B0＋番号区画・GIS 由来の地域情報
* B2 B1＋公開施設で観測した市内局番
正解は gold_label（判定者・根拠付き）。分割はグループ単位（既定：市区町村）で、同じ町丁目・
施設が学習と評価の両方に入らない。改善と言えるのは、グループ単位ブートストラップの95%区間の
下限が 0 を超えるときだけ。
"""

from __future__ import annotations

import hashlib
import math
import random
import sqlite3
from collections import Counter, defaultdict

from ..regions.features import derive, load_latest

B0 = ("detached_ratio", "apartment_ratio", "owner_ratio", "rental_ratio", "public_rental_ratio", "aging_rate",
      "pop_change_10y", "pop_density", "estab_per_household", "did_flag")
B1 = B0 + ("agri_settlement_overlap", "fishery_settlement_overlap", "farmland_ratio", "forest_ratio",
           "slope_mean_deg", "zoning_residential_ratio", "zoning_commercial_ratio", "land_price_pref_pct",
           "metro_core_flag", "uea_member", "vacation_home_share_muni")
NUMERIC = {"B0": B0, "B1": B1, "B2": B1}


def samples(conn: sqlite3.Connection, axis: str, baseline: str, year: int, version: str, group_by: str) -> list[dict]:
    labels = defaultdict(set)
    for r in conn.execute("SELECT town_key, class_code FROM regions.gold_label WHERE axis = ? AND census_year = ?"
                          " AND boundary_version = ?", (axis, year, version)):
        labels[r["town_key"]].add(r["class_code"])
    as_of = conn.execute("SELECT COALESCE(MAX(load_id), 0) FROM regions.data_load").fetchone()[0]
    feats = load_latest(conn, year, version, as_of)
    areas, codes = defaultdict(set), defaultdict(set)
    if baseline in ("B1", "B2"):
        for r in conn.execute("SELECT number_area_code, municipality_code FROM numbers.number_area_municipality"):
            areas[r["municipality_code"]].add(f"na:{r['number_area_code']}")
    if baseline == "B2":
        for r in conn.execute("SELECT town_key, area_code, local_code FROM town_observed_code"
                              " WHERE census_year = ? AND boundary_version = ?", (year, version)):
            codes[r["town_key"]].add(f"lc:{r['area_code']}-{r['local_code']}")
    out = []
    for t in conn.execute("SELECT * FROM regions.town WHERE census_year = ? AND boundary_version = ?"
                          " AND secrecy <> 'merged_into_other' ORDER BY town_key", (year, version)):
        lab = labels.get(t["town_key"])
        if not lab or len(lab) > 1:  # 正解なし・判定者間の不一致は除外
            continue
        f = derive(feats.get(t["town_key"], {}))
        num = {n: (math.log1p(f[n].value) if n == "pop_density" else f[n].value)
               for n in NUMERIC[baseline] if n in f and f[n].usable}
        out.append({"key": t["town_key"], "pref": t["prefecture_code"], "label": next(iter(lab)),
                    "group": t["municipality_code"] if group_by == "municipality" else t["town_key"],
                    "num": num, "tok": areas[t["municipality_code"]] | codes[t["town_key"]]})
    return out


class Centroid:
    def fit(self, data: list[dict], names: tuple[str, ...]):
        self.names = names
        self.mu, self.sd = {}, {}
        for n in names:
            v = [d["num"][n] for d in data if n in d["num"]]
            m = sum(v) / len(v) if v else 0.0
            self.mu[n] = m
            self.sd[n] = math.sqrt(sum((x - m) ** 2 for x in v) / len(v)) if v else 1.0
            self.sd[n] = self.sd[n] or 1.0
        self.vocab = sorted({t for d in data for t in d["tok"]})
        groups = defaultdict(list)
        for d in data:
            groups[d["label"]].append(self.vec(d))
        self.c = {k: [sum(x) / len(v) for x in zip(*v)] for k, v in groups.items()}
        return self

    def vec(self, d: dict) -> list[float]:
        out = []
        for n in self.names:
            present = n in d["num"]
            out += [((d["num"][n] if present else self.mu[n]) - self.mu[n]) / self.sd[n], 0.0 if present else 1.0]
        return out + [1.0 if t in d["tok"] else 0.0 for t in self.vocab]

    def rank(self, d: dict) -> list[str]:
        v = self.vec(d)
        dist = {k: sum((a - b) ** 2 for a, b in zip(v, c)) for k, c in self.c.items()}
        return sorted(dist, key=lambda k: (dist[k], k))


def _fold(group: str, k: int) -> int:
    return int(hashlib.sha256(group.encode()).hexdigest(), 16) % k


def predict(data: list[dict], baseline: str, k: int, holdout: tuple[str, ...]) -> list[tuple[dict, list[str]]]:
    names = NUMERIC[baseline]
    if holdout:
        train = [d for d in data if d["pref"] not in holdout]
        model = Centroid().fit(train, names)
        return [(d, model.rank(d)) for d in data if d["pref"] in holdout]
    out = []
    for f in range(k):
        train = [d for d in data if _fold(d["group"], k) != f]
        test = [d for d in data if _fold(d["group"], k) == f]
        if not train or not test:
            continue
        assert not {d["group"] for d in train} & {d["group"] for d in test}, "group leakage"
        model = Centroid().fit(train, names)
        out += [(d, model.rank(d)) for d in test]
    return out


def metrics(preds: list[tuple[dict, list[str]]]) -> dict:
    labels = sorted({d["label"] for d, _ in preds} | {r[0] for _, r in preds})
    conf = {a: Counter() for a in labels}
    for d, r in preds:
        conf[d["label"]][r[0]] += 1
    per = {}
    for a in labels:
        tp = conf[a][a]
        fp = sum(conf[o][a] for o in labels if o != a)
        fn = sum(conf[a][o] for o in labels if o != a)
        p = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        per[a] = {"precision": p, "recall": rc, "f1": 2 * p * rc / (p + rc) if p + rc else 0.0, "support": tp + fn}
    f1s = [v["f1"] for v in per.values() if v["support"]]
    n = len(preds)
    return {"n": n, "top1": sum(r[0] == d["label"] for d, r in preds) / n if n else None,
            "top3": sum(d["label"] in r[:3] for d, r in preds) / n if n else None,
            "macro_f1": sum(f1s) / len(f1s) if f1s else None, "per_class": per,
            "confusion": {a: dict(c) for a, c in conf.items()}}


def _ci(a, b, metric: str, rounds: int, seed: int) -> tuple[float, float]:
    ga, gb = defaultdict(list), defaultdict(list)
    for x in a:
        ga[x[0]["group"]].append(x)
    for x in b:
        gb[x[0]["group"]].append(x)
    groups = sorted(set(ga) & set(gb))
    rng = random.Random(seed)
    deltas = []
    for _ in range(rounds):
        pick = [rng.choice(groups) for _ in groups]
        deltas.append((metrics([x for g in pick for x in gb[g]])[metric] or 0)
                      - (metrics([x for g in pick for x in ga[g]])[metric] or 0))
    deltas.sort()
    return deltas[int(0.025 * rounds)], deltas[int(0.975 * rounds) - 1]


def compare(conn: sqlite3.Connection, axis: str, year: int, version: str, *, k: int = 5,
            group_by: str = "municipality", holdout: tuple[str, ...] = (), rounds: int = 500,
            seed: int = 20261020) -> dict:
    preds, res = {}, {}
    for b in ("B0", "B1", "B2"):
        data = samples(conn, axis, b, year, version, group_by)
        train_labels = {d["label"] for d in data if d["pref"] not in holdout} if holdout else {d["label"] for d in data}
        n_groups = len({d["group"] for d in data})
        n_test = sum(d["pref"] in holdout for d in data) if holdout else len(data)
        if len(train_labels) < 2 or n_test == 0 or (not holdout and n_groups < k):
            return {"status": "not_evaluable",
                    "reason": f"labels={len(train_labels)} groups={n_groups} test={n_test} k={k}"}
        preds[b] = predict(data, b, k, holdout)
        res[b] = metrics(preds[b])
    comp = {}
    for lo, hi in (("B0", "B1"), ("B1", "B2")):
        for m in ("macro_f1", "top1"):
            low, high = _ci(preds[lo], preds[hi], m, rounds, seed)
            comp[f"{hi}_vs_{lo}_{m}"] = {"delta": (res[hi][m] or 0) - (res[lo][m] or 0), "ci95": [low, high],
                                         "verdict": "improved" if low > 0 else
                                         ("worse" if high < 0 else "no_significant_difference")}
    return {"status": "evaluated", "group_by": group_by, "k": k, "holdout": list(holdout),
            "baselines": res, "comparisons": comp}
