"""分類ルールの読込と不変条件の検証。

ルールファイルでは外せない条件:
* 12分類すべてが定義されている
* R3 / R4 / L5 は確定に外部証拠を要する（国勢調査の周辺集計・世帯数だけでは確定不可）
* L6 は属性（role=attribute）で、集落性の文脈 context_any を持つ
* approved=true にできるのは、すべての閾値の出所が approved の分類だけ
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

CODES = ("R1", "R2", "R3", "R4", "R5", "R6", "L1", "L2", "L3", "L4", "L5", "L6")
OPS = (">=", "<=", ">", "<", "==")
EVIDENCE_REQUIRED = ("R3", "R4", "L5")
NOT_EVIDENCE = frozenset({"census_tenure_marginal", "census_structure_marginal", "general_household_count",
                          "vacancy_rate"})


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Cond:
    feature: str
    op: str
    value: float
    origin: str


@dataclass(frozen=True)
class ClassRule:
    code: str
    axis: str
    role: str
    name: str
    approved: bool
    conds: tuple[Cond, ...]
    excludes: tuple[str, ...]
    context_classes: tuple[str, ...]
    context_conds: tuple[Cond, ...]
    confirm_evidence: tuple[str, ...]


@dataclass(frozen=True)
class Rules:
    version: str
    classes: dict[str, ClassRule]
    order: tuple[str, ...]
    min_margin: float
    tolerance: float
    grade_order: tuple[str, ...]
    text: str
    sha256: str


def _cond(code: str, item, origin_required: bool = True) -> Cond:
    if not isinstance(item, list) or len(item) not in (3, 4):
        raise RuleError(f"{code}: condition must be [feature, op, value, origin]")
    feature, op, value, *rest = item
    origin = rest[0] if rest else ("approved" if not origin_required else None)
    if op not in OPS:
        raise RuleError(f"{code}: bad operator {op!r}")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuleError(f"{code}: value must be numeric")
    if origin not in ("user", "proposed", "approved"):
        raise RuleError(f"{code}: origin must be user / proposed / approved")
    return Cond(feature, op, float(value), origin)


def parse(data: dict) -> Rules:
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    cls = data.get("classes", {})
    if set(cls) != set(CODES):
        raise RuleError(f"classes must be exactly {CODES}")
    rules = {}
    for code in CODES:
        spec = cls[code]
        if spec.get("axis") != code[0]:
            raise RuleError(f"{code}: axis must be {code[0]}")
        role = spec.get("role", "main")
        conds = tuple(_cond(code, c) for c in spec.get("all", []))
        if not conds:
            raise RuleError(f"{code}: needs conditions")
        evidence = tuple(spec.get("confirm_evidence", []))
        if code in EVIDENCE_REQUIRED and (not evidence or NOT_EVIDENCE & set(evidence)):
            raise RuleError(f"{code}: confirm_evidence must list external evidence types")
        ctx = spec.get("context_any", [])
        ctx_classes = tuple(c["class"] for c in ctx if "class" in c)
        ctx_conds = tuple(_cond(code, c["feature"], origin_required=False) for c in ctx if "feature" in c)
        if code == "L6" and (role != "attribute" or not ctx):
            raise RuleError("L6 must be role=attribute with context_any")
        approved = spec.get("approved") is True
        if approved and any(c.origin != "approved" for c in conds):
            raise RuleError(f"{code}: approved requires every threshold origin to be 'approved'")
        excludes = tuple(spec.get("excludes", []))
        for dep in excludes + ctx_classes:
            if dep not in CODES or dep == code:
                raise RuleError(f"{code}: bad class reference {dep}")
        rules[code] = ClassRule(code, code[0], role, spec.get("name", code), approved, conds, excludes,
                                ctx_classes, ctx_conds, evidence)
    grade_order = tuple(data.get("grade_order", ("A", "B", "C")))
    if sorted(grade_order) != ["A", "B", "C"]:
        raise RuleError("grade_order must be a permutation of A, B, C")
    return Rules(str(data["rule_version"]), rules, _order(rules), float(data.get("min_margin", 0.1)),
                 float(data.get("near_tolerance", 0.03)), grade_order, text,
                 hashlib.sha256(text.encode()).hexdigest())


def _order(rules: dict[str, ClassRule]) -> tuple[str, ...]:
    done: list[str] = []
    active: set[str] = set()

    def visit(c: str) -> None:
        if c in done:
            return
        if c in active:
            raise RuleError(f"circular dependency at {c}")
        active.add(c)
        for d in rules[c].excludes + rules[c].context_classes:
            visit(d)
        active.discard(c)
        done.append(c)

    for c in CODES:
        visit(c)
    return tuple(done)


def load(path: str | Path) -> Rules:
    return parse(json.loads(Path(path).read_text(encoding="utf-8")))
