"""軸（R/L）ごとの主分類・副分類・重複フラグの決定。

優先順位の固定表は持たない。並べ方は (確定度, 根拠等級, 根拠スコア) で、
上位2件が同じ確定度・同じ等級でスコア差が min_margin 未満なら主分類を
決めずに保留（on_hold）にする。属性クラス（L6）は主分類にならない。
"""

from __future__ import annotations

from dataclasses import dataclass

from .rules import RuleSet

MATCHED = ("confirmed", "candidate")
_STATUS_RANK = {"confirmed": 0, "candidate": 1}


@dataclass
class ClassResult:
    code: str
    status: str
    score: float | None
    grade: str | None
    reason: str
    evidence: dict
    primary: bool = False
    secondary: bool = False
    overlap: bool = False


@dataclass(frozen=True)
class AxisResult:
    axis: str
    primary_class: str | None
    state: str
    reason: str


def assign(results: dict[str, ClassResult], rules: RuleSet) -> list[AxisResult]:
    grade_rank = {g: i for i, g in enumerate(rules.grade_order)}
    matched_total = sum(1 for r in results.values() if r.status in MATCHED)
    for r in results.values():
        r.overlap = r.status in MATCHED and matched_total >= 2

    axes = []
    for axis in ("R", "L"):
        members = [results[c] for c, rule in rules.classes.items()
                   if rule.axis == axis and rule.kind == "primary"]
        matched = sorted(
            (r for r in members if r.status in MATCHED),
            key=lambda r: (_STATUS_RANK[r.status], grade_rank.get(r.grade or "C", 99), -(r.score or 0.0), r.code),
        )
        if not matched:
            axes.append(_unmatched_axis(axis, members))
            continue
        top = matched[0]
        if len(matched) > 1:
            second = matched[1]
            tied = (second.status == top.status and second.grade == top.grade
                    and (top.score or 0.0) - (second.score or 0.0) < rules.min_margin)
            if tied:
                for r in matched:
                    r.secondary = True
                axes.append(AxisResult(axis, None, "on_hold",
                                       f"tie:{top.code},{second.code} margin<{rules.min_margin}"))
                continue
        top.primary = True
        for r in matched[1:]:
            r.secondary = True
        axes.append(AxisResult(axis, top.code, "decided", f"ranked_first_of:{len(matched)}"))
    return axes


def _unmatched_axis(axis: str, members: list[ClassResult]) -> AxisResult:
    statuses = {r.status for r in members}
    if "insufficient_data" in statuses:
        missing = sorted(r.code for r in members if r.status == "insufficient_data")
        return AxisResult(axis, None, "insufficient_data", "insufficient:" + ",".join(missing))
    if "on_hold" in statuses:
        held = sorted(r.code for r in members if r.status == "on_hold")
        return AxisResult(axis, None, "on_hold", "on_hold:" + ",".join(held))
    if statuses == {"undeterminable"}:
        return AxisResult(axis, None, "undeterminable", members[0].reason)
    return AxisResult(axis, None, "none", "no_class_matched")
