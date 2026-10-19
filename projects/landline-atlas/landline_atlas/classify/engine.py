"""12分類判定エンジン。町丁目ごとに12分類すべてを評価し、全行を追記する。

状態: confirmed / candidate / not_applicable / on_hold / insufficient_data / undeterminable
* 必要な特徴量が missing・suppressed → insufficient_data（0 で埋めない）
* 分母 0 など → undeterminable。合算元の町丁目 → undeterminable（合算先にだけ計上）
* 外れた条件がすべて閾値近傍 → on_hold
* 未承認ルール、外部証拠（等級A/B）不足、排他・文脈の未解決 → 最大でも candidate
主分類は R軸・L軸それぞれで (確定度, 根拠等級, 根拠スコア) の順に決め、拮抗すれば on_hold。
L6（属性）は主分類にならない。
"""

from __future__ import annotations

import hashlib
import json
import operator
import sqlite3
from dataclasses import dataclass, field

from .. import __version__
from ..db.connection import utc_now
from ..regions.features import MISSING, derive, load_latest
from .rules import CODES, ClassRule, Cond, Rules, parse

OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt, "==": operator.eq}
MATCHED = ("confirmed", "candidate")
UNRESOLVED = ("insufficient_data", "on_hold", "undeterminable")


@dataclass
class Result:
    code: str
    status: str
    reason: str
    score: float | None = None
    grade: str | None = None
    evidence: dict = field(default_factory=dict)
    primary: bool = False
    secondary: bool = False
    overlap: bool = False


@dataclass(frozen=True)
class TownInput:
    key: str
    pref: str
    muni: str
    secrecy: str
    merged_into: str | None
    features: dict
    evidence: tuple


def _canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_inputs(conn: sqlite3.Connection, year: int, version: str, *, prefecture: str | None = None,
                as_of_load: int) -> list[TownInput]:
    feats = load_latest(conn, year, version, as_of_load)
    ev: dict[str, list] = {}
    for r in conn.execute("SELECT * FROM regions.external_evidence WHERE census_year = ? AND boundary_version = ?"
                          " AND load_id <= ? ORDER BY evidence_id", (year, version, as_of_load)):
        ev.setdefault(r["town_key"], []).append(
            (r["evidence_id"], r["evidence_type"], r["supports_class"], r["grade"], r["source_id"]))
    sql = "SELECT * FROM regions.town WHERE census_year = ? AND boundary_version = ?"
    params: list = [year, version]
    if prefecture:
        sql += " AND prefecture_code = ?"
        params.append(prefecture)
    return [TownInput(t["town_key"], t["prefecture_code"], t["municipality_code"], t["secrecy"], t["merged_into"],
                      feats.get(t["town_key"], {}), tuple(ev.get(t["town_key"], ())))
            for t in conn.execute(sql + " ORDER BY town_key", params)]


def input_hash(inputs: list[TownInput]) -> str:
    return hashlib.sha256(_canon([
        [t.key, t.secrecy, t.merged_into, sorted((k, f.value, f.state, f.source_id) for k, f in t.features.items()),
         list(t.evidence)] for t in inputs]).encode()).hexdigest()


def _near(c: Cond, v: float, tol: float) -> bool:
    return c.op != "==" and abs(v - c.value) <= tol * max(1.0, abs(c.value))


def _check(rule: ClassRule, feats, tol: float):
    detail, lacking, undefined = [], [], []
    for c in rule.conds:
        f = feats.get(c.feature, MISSING)
        detail.append({"feature": c.feature, "op": c.op, "threshold": c.value, "origin": c.origin,
                       "value": f.value, "state": f.state, "source_id": f.source_id})
        if f.state in ("missing", "suppressed"):
            lacking.append(c.feature)
        elif not f.usable:
            undefined.append(c.feature)
    if lacking:
        return "insufficient_data", "lacking:" + ",".join(lacking), None, detail
    if undefined:
        return "undeterminable", "undefined:" + ",".join(undefined), None, detail
    ok = 0
    near_only = True
    for c, d in zip(rule.conds, detail):
        d["satisfied"] = OPS[c.op](d["value"], c.value)
        ok += d["satisfied"]
        near_only &= d["satisfied"] or _near(c, d["value"], tol)
    score = round(ok / len(rule.conds), 4)
    if ok == len(rule.conds):
        return None, "conditions_met", score, detail
    return ("on_hold", "near_threshold", score, detail) if near_only else \
           ("not_applicable", "conditions_not_met", score, detail)


def _evaluate(rule: ClassRule, feats, evidence, done: dict[str, Result], rules: Rules) -> Result:
    status, reason, score, detail = _check(rule, feats, rules.tolerance)
    info = {"conditions": detail}
    if status:
        return Result(rule.code, status, reason, score, None, info)
    caps = []
    for other in rule.excludes:
        if done[other].status in MATCHED:
            return Result(rule.code, "not_applicable", f"excluded_by:{other}", score, None, info)
        if done[other].status in UNRESOLVED:
            caps.append(f"exclusion_unresolved:{other}")
    if rule.context_classes or rule.context_conds:
        hit = next((c for c in rule.context_classes if done[c].status in MATCHED), None)
        if hit is None:
            for c in rule.context_conds:
                f = feats.get(c.feature, MISSING)
                if f.usable and OPS[c.op](f.value, c.value):
                    hit = c.feature
                    break
        unresolved = any(done[c].status in UNRESOLVED for c in rule.context_classes) or any(
            not feats.get(c.feature, MISSING).usable for c in rule.context_conds)
        info["context"] = hit
        if hit is None:
            return Result(rule.code, "on_hold" if unresolved else "not_applicable",
                          "settlement_context_unresolved" if unresolved else "no_settlement_context",
                          score, None, info)
    support = sorted((e for e in evidence if e[2] == rule.code and e[1] in rule.confirm_evidence
                      and e[3] in ("A", "B")), key=lambda e: (e[3], e[0]))
    info["external_evidence"] = [{"evidence_id": e[0], "type": e[1], "grade": e[3], "source_id": e[4]}
                                 for e in support]
    if rule.confirm_evidence and not support:
        caps.append("evidence_missing")
        grade = "C"
    else:
        grade = support[0][3] if support else "B"
    if not rule.approved:
        caps.append("thresholds_unapproved")
    if caps:
        return Result(rule.code, "candidate", ";".join(caps), score, grade, info)
    return Result(rule.code, "confirmed", "conditions_met;evidence_ok", score, grade, info)


def _axes(results: dict[str, Result], rules: Rules) -> list[tuple[str, str | None, str, str]]:
    rank = {g: i for i, g in enumerate(rules.grade_order)}
    matched_all = [r for r in results.values() if r.status in MATCHED]
    for r in matched_all:
        r.overlap = len(matched_all) >= 2
    out = []
    for axis in ("R", "L"):
        members = [results[c] for c in CODES if c[0] == axis and rules.classes[c].role == "main"]
        m = sorted((r for r in members if r.status in MATCHED),
                   key=lambda r: (MATCHED.index(r.status), rank[r.grade or "C"], -(r.score or 0), r.code))
        if not m:
            states = {r.status for r in members}
            if "insufficient_data" in states:
                out.append((axis, None, "insufficient_data",
                            ",".join(r.code for r in members if r.status == "insufficient_data")))
            elif "on_hold" in states:
                out.append((axis, None, "on_hold", ",".join(r.code for r in members if r.status == "on_hold")))
            elif states == {"undeterminable"}:
                out.append((axis, None, "undeterminable", members[0].reason))
            else:
                out.append((axis, None, "none", "no_class_matched"))
            continue
        if len(m) > 1 and (m[0].status, m[0].grade) == (m[1].status, m[1].grade) \
                and (m[0].score or 0) - (m[1].score or 0) < rules.min_margin:
            for r in m:
                r.secondary = True
            out.append((axis, None, "on_hold", f"tie:{m[0].code},{m[1].code}"))
            continue
        m[0].primary = True
        for r in m[1:]:
            r.secondary = True
        out.append((axis, m[0].code, "decided", f"first_of:{len(m)}"))
    return out


def evaluate_town(t: TownInput, rules: Rules):
    if t.secrecy in ("merged_into_other", "suppressed"):
        status, reason = (("undeterminable", f"merged_into:{t.merged_into}") if t.secrecy == "merged_into_other"
                          else ("insufficient_data", "suppressed_area"))
        res = {c: Result(c, status, reason, evidence={"secrecy": t.secrecy}) for c in CODES}
        return res, _axes(res, rules)
    feats = derive(t.features)
    done: dict[str, Result] = {}
    for code in rules.order:
        done[code] = _evaluate(rules.classes[code], feats, t.evidence, done, rules)
    res = {c: done[c] for c in CODES}
    if t.secrecy == "merge_target":
        for r in res.values():
            r.evidence["note"] = "includes_merged_areas"
    return res, _axes(res, rules)


def _max_load(conn) -> int:
    return conn.execute("SELECT COALESCE(MAX(load_id), 0) FROM regions.data_load").fetchone()[0]


def classify(conn: sqlite3.Connection, rules: Rules, year: int, version: str, *, prefecture: str | None = None,
             as_of_load: int | None = None, evaluated_at: str | None = None) -> int:
    as_of = _max_load(conn) if as_of_load is None else as_of_load
    inputs = load_inputs(conn, year, version, prefecture=prefecture, as_of_load=as_of)
    when = evaluated_at or utc_now()
    sources = {r["source_id"]: (r["source_year"], r["url"]) for r in conn.execute("SELECT * FROM regions.source_document")}
    run_id = conn.execute(
        "INSERT INTO regions.class_run (rule_version, rule_sha256, rule_json, input_sha256, max_load_id, code_version,"
        " census_year, boundary_version, scope, evaluated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rules.version, rules.sha256, rules.text, input_hash(inputs), as_of, __version__, year, version,
         prefecture or "ALL", when)).lastrowid
    for t in inputs:
        res, axes = evaluate_town(t, rules)
        for code, r in res.items():
            src = next((sources[d["source_id"]] for d in r.evidence.get("conditions", ())
                        if d.get("source_id") in sources), (None, None))
            conn.execute("INSERT INTO regions.regional_classification VALUES"
                         " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (run_id, t.pref, t.muni, t.key, year, version, code, code[0], int(r.primary),
                          int(r.secondary), int(r.overlap), r.score, r.grade, r.status, r.reason, rules.version,
                          src[0], src[1], _canon(r.evidence), when))
        for axis, primary, state, reason in axes:
            conn.execute("INSERT INTO regions.axis_result VALUES (?,?,?,?,?,?,?,?)",
                         (run_id, t.key, year, version, axis, primary, state, reason))
    return int(run_id)


def reproduce(conn: sqlite3.Connection, run_id: int) -> dict:
    """保存したルール本文と入力時点で再計算し、保存結果と全行照合する（書き込みなし）。"""
    run = conn.execute("SELECT * FROM regions.class_run WHERE run_id = ?", (run_id,)).fetchone()
    rules = parse(json.loads(run["rule_json"]))
    inputs = load_inputs(conn, run["census_year"], run["boundary_version"],
                         prefecture=None if run["scope"] == "ALL" else run["scope"], as_of_load=run["max_load_id"])
    again = {}
    for t in inputs:
        res, _ = evaluate_town(t, rules)
        for c, r in res.items():
            again[(t.key, c)] = (r.status, r.reason, r.score, r.grade, int(r.primary), int(r.secondary),
                                 int(r.overlap), _canon(r.evidence))
    stored = {(r["town_key"], r["class_code"]): (r["label_status"], r["reason"], r["score"], r["evidence_grade"],
                                                 r["primary_flag"], r["secondary_flag"], r["overlap_flag"],
                                                 r["evidence_json"])
              for r in conn.execute("SELECT * FROM regions.regional_classification WHERE run_id = ?", (run_id,))}
    diffs = sorted(k for k in set(again) | set(stored) if again.get(k) != stored.get(k))
    same_input = input_hash(inputs) == run["input_sha256"]
    return {"run_id": run_id, "rows": len(stored), "rule_sha256_ok": rules.sha256 == run["rule_sha256"],
            "input_sha256_ok": same_input, "differences": diffs, "reproduced": same_input and not diffs}
