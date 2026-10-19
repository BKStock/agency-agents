"""分類ルールの読込と検証。ルールはデータ（JSON）として版管理する。

ルールファイルで変更できるのは閾値・条件・承認状態だけで、次の不変条件は
ここで検証して破れば読込を拒否する。

* 12分類すべてが定義されている
* R3/R4/L5 は確定に外部証拠を必須とする（国勢調査の周辺集計だけでは確定不可）
* L6 は属性クラスで、集落性の文脈条件（context_any）を持つ
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

CLASS_CODES = ("R1", "R2", "R3", "R4", "R5", "R6", "L1", "L2", "L3", "L4", "L5", "L6")
OPS = (">=", "<=", ">", "<", "==")
THRESHOLD_ORIGINS = ("user_specified", "proposed", "approved")

# 確定に外部証拠が必須の分類
EVIDENCE_REQUIRED_CLASSES = ("R3", "R4", "L5")
# 証拠として数えてはいけない種別（国勢調査の周辺集計・一般世帯数など）
MARGINAL_ONLY_EVIDENCE = frozenset({
    "census_tenure_marginal", "census_structure_marginal",
    "general_household_count", "vacancy_rate",
})


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Condition:
    feature: str
    op: str
    value: float
    threshold_origin: str = "proposed"


@dataclass(frozen=True)
class ContextItem:
    class_code: str | None = None
    condition: Condition | None = None


@dataclass(frozen=True)
class ClassRule:
    code: str
    axis: str
    kind: str
    name: str
    approved: bool
    conditions: tuple[Condition, ...]
    confirm_requires_evidence: tuple[str, ...] = ()
    exclude_if_class: tuple[str, ...] = ()
    context_any: tuple[ContextItem, ...] = ()

    @property
    def depends_on(self) -> tuple[str, ...]:
        return self.exclude_if_class + tuple(c.class_code for c in self.context_any if c.class_code)


@dataclass(frozen=True)
class RuleSet:
    rule_version: str
    classes: dict[str, ClassRule]
    min_margin: float
    grade_order: tuple[str, ...]
    near_threshold_tolerance: float
    raw_json: str
    sha256: str
    evaluation_order: tuple[str, ...] = field(default=())


def canonical_json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_rules(path: str | Path) -> RuleSet:
    return parse_rules(json.loads(Path(path).read_text(encoding="utf-8")))


def parse_rules(data: dict) -> RuleSet:
    raw = canonical_json(data)
    classes_data = data.get("classes") or {}
    missing = [c for c in CLASS_CODES if c not in classes_data]
    extra = [c for c in classes_data if c not in CLASS_CODES]
    if missing or extra:
        raise RuleError(f"classes must be exactly R1-R6/L1-L6 (missing={missing}, unknown={extra})")

    classes = {code: _parse_class(code, classes_data[code]) for code in CLASS_CODES}
    for rule in classes.values():
        for dep in rule.depends_on:
            if dep not in classes or dep == rule.code:
                raise RuleError(f"{rule.code}: invalid class reference {dep!r}")

    selection = data.get("primary_selection") or {}
    grade_order = tuple(selection.get("grade_order", ("A", "B", "C")))
    if sorted(grade_order) != ["A", "B", "C"]:
        raise RuleError("primary_selection.grade_order must be a permutation of A, B, C")
    return RuleSet(
        rule_version=str(data["rule_version"]),
        classes=classes,
        min_margin=float(selection.get("min_margin", 0.10)),
        grade_order=grade_order,
        near_threshold_tolerance=float(data.get("near_threshold_tolerance", 0.03)),
        raw_json=raw,
        sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        evaluation_order=_topological_order(classes),
    )


def _parse_condition(code: str, item: dict) -> Condition:
    op = item.get("op")
    if op not in OPS:
        raise RuleError(f"{code}: unsupported operator {op!r}")
    origin = item.get("threshold_origin", "proposed")
    if origin not in THRESHOLD_ORIGINS:
        raise RuleError(f"{code}: unknown threshold_origin {origin!r}")
    if not isinstance(item.get("value"), (int, float)) or isinstance(item.get("value"), bool):
        raise RuleError(f"{code}: condition value must be numeric")
    return Condition(str(item["feature"]), op, float(item["value"]), origin)


def _parse_class(code: str, spec: dict) -> ClassRule:
    axis = spec.get("axis")
    if axis != code[0]:
        raise RuleError(f"{code}: axis must be {code[0]!r}")
    kind = spec.get("kind", "primary")
    if kind not in ("primary", "attribute"):
        raise RuleError(f"{code}: kind must be primary or attribute")
    conditions = tuple(_parse_condition(code, c) for c in spec.get("conditions", ()))
    if not conditions:
        raise RuleError(f"{code}: at least one condition is required")
    evidence = tuple(spec.get("confirm_requires_evidence", ()))
    if code in EVIDENCE_REQUIRED_CLASSES:
        if not evidence:
            raise RuleError(f"{code}: confirm_requires_evidence must not be empty")
        bad = MARGINAL_ONLY_EVIDENCE.intersection(evidence)
        if bad:
            raise RuleError(f"{code}: {sorted(bad)} cannot confirm this class")
    context = tuple(
        ContextItem(class_code=c["class"]) if "class" in c else ContextItem(condition=_parse_condition(code, c))
        for c in spec.get("context_any", ())
    )
    if code == "L6" and (kind != "attribute" or not context):
        raise RuleError("L6 must be kind=attribute with a non-empty context_any")
    approved = spec.get("approved", False)
    if approved is True and any(c.threshold_origin != "approved" for c in conditions):
        raise RuleError(f"{code}: approved=true requires every threshold_origin to be 'approved'")
    return ClassRule(
        code=code, axis=axis, kind=kind, name=str(spec.get("name", code)),
        approved=approved is True, conditions=conditions,
        confirm_requires_evidence=evidence,
        exclude_if_class=tuple(spec.get("exclude_if_class", ())),
        context_any=context,
    )


def _topological_order(classes: dict[str, ClassRule]) -> tuple[str, ...]:
    order: list[str] = []
    state: dict[str, str] = {}

    def visit(code: str) -> None:
        if state.get(code) == "done":
            return
        if state.get(code) == "active":
            raise RuleError(f"circular class dependency at {code}")
        state[code] = "active"
        for dep in classes[code].depends_on:
            visit(dep)
        state[code] = "done"
        order.append(code)

    for code in CLASS_CODES:
        visit(code)
    return tuple(order)
