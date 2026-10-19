"""12分類判定エンジン。

町丁目ごとに R1〜R6・L1〜L6 の全分類を評価し、全分類の行を保存する。
判定状態は confirmed / candidate / not_applicable / on_hold /
insufficient_data / undeterminable の6値。

ルールファイルでは外せない不変条件:
* 必要な特徴量が missing / suppressed → insufficient_data（0 で埋めない）
* 未承認ルール（approved=false）は confirmed を出さない
* confirm_requires_evidence の外部証拠（等級 A/B）がなければ confirmed を出さない
* 合算元の町丁目は分類せず undeterminable（合算先にだけ計上する）
"""

from __future__ import annotations

import hashlib
import json
import operator
import sqlite3
from dataclasses import dataclass
from typing import Mapping

from . import __version__
from .features import MISSING, Feature, derive
from .primary import MATCHED, AxisResult, ClassResult, assign
from .rules import CLASS_CODES, ClassRule, Condition, RuleSet, canonical_json, parse_rules
from .store import max_load_id, utc_now

_OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt, "==": operator.eq}
_GRADE_ORDER = ("A", "B", "C")


@dataclass(frozen=True)
class Town:
    town_key: str
    census_year: int
    boundary_version: str
    prefecture_code: str
    municipality_code: str
    secrecy_state: str
    merged_into: str | None


@dataclass(frozen=True)
class Evidence:
    evidence_id: int
    evidence_type: str
    supports_class: str
    grade: str
    source_id: str


@dataclass(frozen=True)
class TownInputs:
    town: Town
    features: dict[str, Feature]
    evidence: tuple[Evidence, ...]


# ---------------------------------------------------------------- 入力の読込

def load_inputs(conn: sqlite3.Connection, census_year: int, boundary_version: str, *,
                prefecture: str | None = None, as_of_load: int | None = None) -> list[TownInputs]:
    as_of = max_load_id(conn) if as_of_load is None else as_of_load
    params: list = [census_year, boundary_version]
    where = "census_year = ? AND boundary_version = ?"
    if prefecture:
        where += " AND prefecture_code = ?"
        params.append(prefecture)
    towns = [Town(r["town_key"], r["census_year"], r["boundary_version"], r["prefecture_code"],
                  r["municipality_code"], r["secrecy_state"], r["merged_into"])
             for r in conn.execute(f"SELECT * FROM town_master WHERE {where} ORDER BY town_key", params)]

    feats: dict[str, dict[str, Feature]] = {t.town_key: {} for t in towns}
    for r in conn.execute(
        """SELECT tf.town_key, tf.feature, tf.value, tf.state, tf.source_id
           FROM town_feature tf
           JOIN (SELECT town_key, feature, MAX(load_id) AS load_id FROM town_feature
                 WHERE census_year = ? AND boundary_version = ? AND load_id <= ?
                 GROUP BY town_key, feature) latest
             ON latest.town_key = tf.town_key AND latest.feature = tf.feature AND latest.load_id = tf.load_id
           WHERE tf.census_year = ? AND tf.boundary_version = ?""",
        (census_year, boundary_version, as_of, census_year, boundary_version),
    ):
        if r["town_key"] in feats:
            feats[r["town_key"]][r["feature"]] = Feature(r["value"], r["state"], r["source_id"])

    evidence: dict[str, list[Evidence]] = {t.town_key: [] for t in towns}
    for r in conn.execute(
        """SELECT * FROM external_evidence WHERE census_year = ? AND boundary_version = ? AND load_id <= ?
           ORDER BY evidence_id""",
        (census_year, boundary_version, as_of),
    ):
        if r["town_key"] in evidence:
            evidence[r["town_key"]].append(
                Evidence(r["evidence_id"], r["evidence_type"], r["supports_class"], r["grade"], r["source_id"]))

    return [TownInputs(t, feats[t.town_key], tuple(evidence[t.town_key])) for t in towns]


def input_hash(inputs: list[TownInputs]) -> str:
    payload = [
        {
            "town": t.town.__dict__,
            "features": {k: [f.value, f.state, f.source_id] for k, f in sorted(t.features.items())},
            "evidence": [e.__dict__ for e in t.evidence],
        }
        for t in inputs
    ]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 判定

def _near(cond: Condition, value: float, tolerance: float) -> bool:
    if cond.op == "==":
        return False
    scale = abs(cond.value) if abs(cond.value) > 1 else 1.0
    return abs(value - cond.value) <= tolerance * scale


def _check_conditions(rule: ClassRule, feats: Mapping[str, Feature], tolerance: float):
    """戻り値: (status or None, score, reason, details)。status が None なら条件を満たした。"""
    details = []
    unusable: dict[str, list[str]] = {}
    for cond in rule.conditions:
        f = feats.get(cond.feature, MISSING)
        details.append({"feature": cond.feature, "op": cond.op, "threshold": cond.value,
                        "threshold_origin": cond.threshold_origin, "value": f.value,
                        "state": f.state, "source_id": f.source_id})
        if not f.usable:
            unusable.setdefault(f.state, []).append(cond.feature)
    lacking = unusable.get("missing", []) + unusable.get("suppressed", [])
    if lacking:
        return "insufficient_data", None, "lacking:" + ",".join(lacking), details
    if unusable:
        names = [n for v in unusable.values() for n in v]
        return "undeterminable", None, "undefined:" + ",".join(names), details

    satisfied = 0
    failing_near_only = True
    for cond, d in zip(rule.conditions, details):
        ok = _OPS[cond.op](d["value"], cond.value)
        d["satisfied"] = ok
        if ok:
            satisfied += 1
        elif not _near(cond, d["value"], tolerance):
            failing_near_only = False
    score = round(satisfied / len(rule.conditions), 4)
    if satisfied == len(rule.conditions):
        return None, score, "conditions_met", details
    if failing_near_only:
        return "on_hold", score, "near_threshold", details
    return "not_applicable", score, "conditions_not_met", details


def evaluate_town(inputs: TownInputs, rules: RuleSet) -> tuple[dict[str, ClassResult], list[AxisResult]]:
    town = inputs.town
    if town.secrecy_state in ("merged_into_other", "suppressed"):
        status, reason = (("undeterminable", f"merged_into:{town.merged_into}")
                          if town.secrecy_state == "merged_into_other"
                          else ("insufficient_data", "suppressed_area"))
        results = {c: ClassResult(c, status, None, None, reason, {"secrecy_state": town.secrecy_state})
                   for c in CLASS_CODES}
        return results, assign(results, rules)

    feats = derive(inputs.features)
    results: dict[str, ClassResult] = {}
    for code in rules.evaluation_order:
        results[code] = _evaluate_class(rules.classes[code], feats, inputs.evidence, results, rules)
    ordered = {c: results[c] for c in CLASS_CODES}
    if town.secrecy_state == "merge_target":
        for r in ordered.values():
            r.evidence["note"] = "includes_merged_areas"
    return ordered, assign(ordered, rules)


def _evaluate_class(rule: ClassRule, feats: Mapping[str, Feature], evidence: tuple[Evidence, ...],
                    done: Mapping[str, ClassResult], rules: RuleSet) -> ClassResult:
    status, score, reason, details = _check_conditions(rule, feats, rules.near_threshold_tolerance)
    ev_info: dict = {"conditions": details}
    if status is not None:
        return ClassResult(rule.code, status, score, None, reason, ev_info)

    caps: list[str] = []
    for other in rule.exclude_if_class:
        other_status = done[other].status
        if other_status in MATCHED:
            return ClassResult(rule.code, "not_applicable", score, None, f"excluded_by:{other}", ev_info)
        if other_status in ("insufficient_data", "on_hold", "undeterminable"):
            caps.append(f"exclusion_unresolved:{other}")

    if rule.context_any:
        context_state = _context(rule, feats, done)
        ev_info["context"] = context_state
        if context_state == "unresolved":
            return ClassResult(rule.code, "on_hold", score, None, "settlement_context_unresolved", ev_info)
        if context_state == "absent":
            return ClassResult(rule.code, "not_applicable", score, None, "no_settlement_context", ev_info)

    supporting = sorted(
        (e for e in evidence
         if e.supports_class == rule.code and e.evidence_type in rule.confirm_requires_evidence
         and e.grade in ("A", "B")),
        key=lambda e: (_GRADE_ORDER.index(e.grade), e.evidence_id),
    )
    ev_info["external_evidence"] = [e.__dict__ for e in supporting]
    if rule.confirm_requires_evidence and not supporting:
        caps.append("evidence_missing:" + "|".join(rule.confirm_requires_evidence))
        grade = "C"
    else:
        grade = supporting[0].grade if supporting else "B"
    if not rule.approved:
        caps.append("thresholds_unapproved")

    if caps:
        return ClassResult(rule.code, "candidate", score, grade, ";".join(caps), ev_info)
    return ClassResult(rule.code, "confirmed", score, grade, "conditions_met;evidence_ok", ev_info)


def _context(rule: ClassRule, feats: Mapping[str, Feature], done: Mapping[str, ClassResult]) -> str:
    unresolved = False
    for item in rule.context_any:
        if item.class_code:
            status = done[item.class_code].status
            if status in MATCHED:
                return f"satisfied_by:{item.class_code}"
            if status in ("insufficient_data", "on_hold", "undeterminable"):
                unresolved = True
        else:
            cond = item.condition
            f = feats.get(cond.feature, MISSING)
            if not f.usable:
                unresolved = True
            elif _OPS[cond.op](f.value, cond.value):
                return f"satisfied_by:{cond.feature}"
    return "unresolved" if unresolved else "absent"


# ---------------------------------------------------------------- run の保存

def _source_of(result: ClassResult, sources: Mapping[str, sqlite3.Row]) -> tuple[int | None, str | None]:
    for d in result.evidence.get("conditions", ()):
        src = sources.get(d.get("source_id") or "")
        if src is not None:
            return src["source_year"], src["url"]
    return None, None


def evaluate_all(inputs: list[TownInputs], rules: RuleSet):
    return [(t, *evaluate_town(t, rules)) for t in inputs]


def classify(conn: sqlite3.Connection, rules: RuleSet, census_year: int, boundary_version: str, *,
             prefecture: str | None = None, as_of_load: int | None = None,
             evaluated_at: str | None = None, note: str | None = None) -> int:
    """全町丁目×12分類を判定し、新しい run として追記する。run_id を返す。"""
    as_of = max_load_id(conn) if as_of_load is None else as_of_load
    inputs = load_inputs(conn, census_year, boundary_version, prefecture=prefecture, as_of_load=as_of)
    evaluated_at = evaluated_at or utc_now()
    sources = {r["source_id"]: r for r in conn.execute("SELECT * FROM source_document")}
    with conn:
        run_id = conn.execute(
            """INSERT INTO classification_run (rule_version, rule_sha256, rule_json, input_sha256, max_load_id,
               code_version, census_year, boundary_version, scope, evaluated_at, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (rules.rule_version, rules.sha256, rules.raw_json, input_hash(inputs), as_of, __version__,
             census_year, boundary_version, prefecture or "ALL", evaluated_at, note),
        ).lastrowid
        for t, results, axes in evaluate_all(inputs, rules):
            for code, r in results.items():
                year, url = _source_of(r, sources)
                conn.execute(
                    """INSERT INTO regional_classification VALUES
                       (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, t.town.prefecture_code, t.town.municipality_code, t.town.town_key,
                     census_year, boundary_version, code, code[0],
                     int(r.primary), int(r.secondary), int(r.overlap), r.score, r.grade, r.status,
                     r.reason, rules.rule_version, year, url,
                     canonical_json(r.evidence), evaluated_at),
                )
            for a in axes:
                conn.execute("INSERT INTO town_axis_result VALUES (?,?,?,?,?,?,?,?)",
                             (run_id, t.town.town_key, census_year, boundary_version,
                              a.axis, a.primary_class, a.state, a.reason))
    return int(run_id)


_COMPARE_COLS = ("town_key", "class_code", "primary_flag", "secondary_flag", "overlap_flag", "score",
                 "evidence_grade", "label_status", "reason", "evidence_json")


def reproduce(conn: sqlite3.Connection, run_id: int) -> dict:
    """保存済み run を、保存したルール本文と入力時点で再計算して照合する（書き込みなし）。"""
    run = conn.execute("SELECT * FROM classification_run WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise KeyError(run_id)
    rules = parse_rules(json.loads(run["rule_json"]))
    scope = None if run["scope"] == "ALL" else run["scope"]
    inputs = load_inputs(conn, run["census_year"], run["boundary_version"],
                         prefecture=scope, as_of_load=run["max_load_id"])
    recomputed = {}
    for t, results, _axes in evaluate_all(inputs, rules):
        for code, r in results.items():
            recomputed[(t.town.town_key, code)] = (
                t.town.town_key, code, int(r.primary), int(r.secondary), int(r.overlap), r.score,
                r.grade, r.status, r.reason, canonical_json(r.evidence))
    stored = {
        (row["town_key"], row["class_code"]): tuple(row[c] for c in _COMPARE_COLS)
        for row in conn.execute(f"SELECT {', '.join(_COMPARE_COLS)} FROM regional_classification WHERE run_id = ?",
                                (run_id,))
    }
    diffs = sorted(k for k in set(stored) | set(recomputed) if stored.get(k) != recomputed.get(k))
    return {
        "run_id": run_id,
        "rule_sha256_ok": rules.sha256 == run["rule_sha256"],
        "input_sha256_ok": input_hash(inputs) == run["input_sha256"],
        "rows": len(stored),
        "differences": diffs,
        "reproduced": not diffs and input_hash(inputs) == run["input_sha256"],
    }
