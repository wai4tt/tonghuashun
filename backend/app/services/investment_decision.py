"""自选横向比较之后的投资价值判断（确定性规则，不调用模型）。

比较对象必须是同一资产类型；每个维度先算组内相对档位，再叠加可解释的绝对
区间分。缺失指标不按 0 分参与，而是让该维度整体退出并在 ``missing`` 中列出，
避免“数据缺失”被误读成“最差”。
"""
from __future__ import annotations

import math
from typing import Any

from backend.app.models.decision import (
    ComparisonDecision,
    DecisionCandidate,
    DecisionDimension,
)

# 维度定义：字段、方向、权重与绝对区间打分。
DIMENSIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "valuation", "label": "估值", "weight": 0.25, "unit": "倍",
        "fields": ("pe_ttm", "pb"), "direction": "lower_better",
        "bands": {"pe_ttm": ((10, 90), (20, 70), (35, 50), (60, 30), (math.inf, 15)),
                  "pb": ((1, 90), (2, 70), (4, 50), (8, 30), (math.inf, 15))},
        "note": "PE 为负表示亏损，不参与估值排序；不同行业估值中枢不同，只做同组比较。",
    },
    {
        "key": "growth", "label": "成长", "weight": 0.25, "unit": "%",
        "fields": ("revenue_growth", "net_profit_growth"), "direction": "higher_better",
        "bands": {"revenue_growth": ((-math.inf, 25), (0, 45), (5, 60), (15, 75), (30, 90)),
                  "net_profit_growth": ((-math.inf, 25), (0, 45), (5, 60), (15, 75), (30, 90))},
        "note": "增长率取自最新报告期，单季波动与基数效应会让同比读数失真。",
    },
    {
        "key": "quality", "label": "盈利质量", "weight": 0.20, "unit": "%",
        "fields": ("roe", "roe_weighted"), "direction": "higher_better",
        "bands": {"roe": ((-math.inf, 10), (0, 30), (4, 45), (8, 60), (12, 75), (20, 90)),
                  "roe_weighted": ((-math.inf, 10), (0, 30), (4, 45), (8, 60), (12, 75), (20, 90))},
        "note": "ROE 为报告期口径，未剔除杠杆与一次性损益，需与现金流一并核对。",
    },
    {
        "key": "capital", "label": "资金", "weight": 0.15, "unit": "元",
        "fields": ("capital_flow",), "direction": "higher_better",
        "bands": {"capital_flow": ((-math.inf, 25), (0, 50), (math.inf, 75))},
        "note": "单日主力净流入不代表持续增配，需结合连续交易日与成交额。",
    },
    {
        "key": "momentum", "label": "当日表现", "weight": 0.15, "unit": "%",
        "fields": ("change",), "direction": "higher_better",
        "bands": {"change": ((-math.inf, 25), (-1, 40), (0, 50), (1, 60), (3, 75), (math.inf, 90))},
        "note": "单日涨跌幅噪声很大，只作为当期表现的描述，不预测后续走势。",
    },
)
CORE_DIMENSIONS = {"valuation", "growth", "quality"}
FIELD_LABELS = {"pe_ttm": "市盈率 TTM", "pb": "市净率", "revenue_growth": "营收同比",
                "net_profit_growth": "净利润同比", "roe": "ROE", "roe_weighted": "加权 ROE",
                "capital_flow": "主力资金净流入", "change": "当日涨跌幅"}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _latest(facts: list[Any], field: str) -> tuple[float | None, str | None]:
    """取该字段最新期间的数值；同字段不同口径（如 PE 静态/动态）不混用。"""

    candidates: list[tuple[str, float]] = []
    for fact in facts:
        if getattr(fact, "field", None) != field:
            continue
        value = _number(getattr(fact, "value", None))
        if value is None:
            continue
        candidates.append((str(getattr(fact, "period", "") or ""), value))
    if not candidates:
        return None, None
    period, value = max(candidates, key=lambda item: item[0])
    return value, period or None


def _band_score(field: str, value: float | None, bands) -> float | None:
    if value is None:
        return None
    if field in {"pe_ttm", "pb"} and value <= 0:
        return None
    for limit, score in bands:
        if value <= limit:
            return float(score)
    return 25.0


def _absolute(dimension: dict[str, Any], metrics: dict[str, float]) -> float | None:
    scores = [_band_score(field, metrics.get(field), dimension["bands"][field])
              for field in dimension["fields"] if field in metrics]
    scores = [score for score in scores if score is not None]
    return sum(scores) / len(scores) if scores else None


def _relative(values: dict[str, float], direction: str) -> dict[str, float]:
    if len(values) < 2:
        return {key: 50.0 for key in values}
    ordered = sorted(values.items(), key=lambda item: item[1], reverse=direction == "higher_better")
    if math.isclose(max(values.values()), min(values.values()), rel_tol=1e-9, abs_tol=1e-12):
        return {key: 50.0 for key in values}
    count = len(ordered)
    return {key: round(100 * (count - 1 - index) / (count - 1), 2) for index, (key, _value) in enumerate(ordered)}


def _metric_text(field: str, value: float, unit: str | None) -> str:
    label = FIELD_LABELS.get(field, field)
    if field in {"pe_ttm", "pb"}:
        return f"{label} {value:.2f} 倍"
    if field == "capital_flow":
        if abs(value) >= 1e8:
            return f"{label} {value / 1e8:,.2f} 亿元"
        if abs(value) >= 1e4:
            return f"{label} {value / 1e4:,.2f} 万元"
        return f"{label} {value:,.2f} 元"
    if unit == "%":
        return f"{label} {value:+.2f}%"
    return f"{label} {value:,.2f}"


def _collect(items: list[Any]) -> list[dict[str, Any]]:
    records = []
    for item in items:
        facts = list(getattr(item, "facts", []) or [])
        metrics: dict[str, float] = {}
        periods: dict[str, str] = {}
        for dimension in DIMENSIONS:
            for field in dimension["fields"]:
                if field in metrics:
                    continue
                value, period = _latest(facts, field)
                if value is not None:
                    metrics[field] = value
                    if period:
                        periods[field] = period
        records.append({
            "target": getattr(item, "target", ""),
            "name": getattr(item, "entity", None),
            "code": getattr(item, "entity_code", None),
            "facts": facts, "metrics": metrics, "periods": periods,
        })
    return records


def _valid(field: str, value: float | None) -> bool:
    if value is None:
        return False
    # 亏损公司的负 PE/PB 没有估值含义，不能当作“便宜”。
    return value > 0 if field in {"pe_ttm", "pb"} else True


def build_decision(items: list[Any], *, asset_type: str = "股票") -> ComparisonDecision:
    """对 2–4 个同类型标的给出相对投资价值排序与理由。"""

    if len(items) < 2:
        return ComparisonDecision(status="insufficient", conclusion="至少需要两个同类型标的才能比较。")
    records = _collect(items)
    dimensions: list[DecisionDimension] = []
    weights: dict[str, float] = {}
    missing: list[str] = []
    dimension_values: dict[str, dict[str, float]] = {}
    dimension_fields: dict[str, str] = {}
    for dimension in DIMENSIONS:
        # 同一维度内必须所有标的都用同一个字段，口径不一致时整体不比较。
        field = next((name for name in dimension["fields"]
                      if all(name in record["metrics"] and _valid(name, record["metrics"][name])
                             for record in records)), None)
        if field is None:
            names = "、".join(record["name"] or record["target"] for record in records)
            missing.append(f"{dimension['label']}（{names} 没有同口径且有效的资料）")
            continue
        dimension_values[dimension["key"]] = {record["target"]: record["metrics"][field]
                                              for record in records}
        dimension_fields[dimension["key"]] = field
        weights[dimension["key"]] = dimension["weight"]
        dimensions.append(DecisionDimension(
            key=dimension["key"], label=dimension["label"], weight=dimension["weight"],
            unit=dimension["unit"], field=field, field_label=FIELD_LABELS.get(field, field),
            direction=dimension["direction"], note=dimension["note"],
        ))
    available_core = CORE_DIMENSIONS & set(dimension_values)
    if len(dimension_values) < 2 or len(available_core) < 2:
        return ComparisonDecision(
            status="insufficient", dimensions=dimensions, weights=weights,
            conclusion=("现有资料不足以比较投资价值：至少需要估值、成长、盈利质量中的两项，"
                        "且每个标的都要有同口径数值。请补齐资料后再比较。"),
            caveats=["缺失指标不按 0 分处理，也不会用其他标的的数值替代。"],
            missing=missing,
        )

    total_weight = sum(weights.values())
    scores: dict[str, float] = {record["target"]: 0.0 for record in records}
    dimension_scores: dict[str, dict[str, float]] = {}
    for key, values in dimension_values.items():
        dimension = next(item for item in DIMENSIONS if item["key"] == key)
        relative = _relative(values, dimension["direction"])
        field = dimension_fields[key]
        for record in records:
            target = record["target"]
            absolute = _band_score(field, record["metrics"][field], dimension["bands"][field])
            absolute = 50.0 if absolute is None else absolute
            score = 0.5 * relative[target] + 0.5 * absolute
            dimension_scores.setdefault(target, {})[key] = round(score, 1)
            scores[target] += score * weights[key] / total_weight

    ordered = sorted(records, key=lambda record: (-scores[record["target"]], record["target"]))
    candidates: list[DecisionCandidate] = []
    for rank, record in enumerate(ordered, start=1):
        target = record["target"]
        dims = dimension_scores.get(target, {})
        strengths, weaknesses = [], []
        for key, value in sorted(dims.items(), key=lambda item: -item[1]):
            label = next(item["label"] for item in DIMENSIONS if item["key"] == key)
            unit = next(item["unit"] for item in DIMENSIONS if item["key"] == key)
            field = dimension_fields.get(key)
            text = f"{label} {value:.0f} 分"
            if field and field in record["metrics"]:
                text += "（" + _metric_text(field, record["metrics"][field], unit) + "）"
            if value >= 60 and len(strengths) < 2:
                strengths.append(text)
            elif value < 45 and len(weaknesses) < 2:
                weaknesses.append(text)
        coverage = len(record["metrics"]) / max(1, sum(len(item["fields"]) for item in DIMENSIONS))
        candidates.append(DecisionCandidate(
            target=target, name=record["name"], code=record["code"], rank=rank,
            score=round(scores[target], 1), dimension_scores=dims,
            metrics={key: round(value, 4) for key, value in record["metrics"].items()},
            strengths=strengths, weaknesses=weaknesses, coverage=round(coverage, 2),
        ))

    best = candidates[0]
    second = candidates[1] if len(candidates) > 1 else None
    name = best.name or best.target
    title = f"{name}（{best.code}）" if best.code else name
    lead = (f"综合 {len(dimensions)} 个维度、{len(records)} 个标的的相对与区间档位，"
            f"{title} 得分 {best.score:.1f} 分，排名第 1")
    if second is not None:
        lead += f"，相对第 2 名 {second.name or second.target}（{second.score:.1f} 分）高 {best.score - second.score:.1f} 分"
    lead += "，在现有资料范围内相对更具投资价值。"
    detail = []
    if best.strengths:
        detail.append("主要优势：" + "；".join(best.strengths) + "。")
    if best.weaknesses:
        detail.append("主要不足：" + "；".join(best.weaknesses) + "。")
    conclusion = lead + "".join(detail) + "该排序只比较现有资料的可比口径，不构成买入建议。"
    caveats = [
        "得分是同组相对档位与绝对区间的加权，不是预期收益、上涨概率或目标价。",
        "不同资产类型（股票/基金/可转债/行业）的估值口径不同，不能跨类型比较。",
        "资料期不一致时以各自最新期间为准，不强行对齐，也不把缺失补成 0。",
    ]
    if asset_type != "股票":
        caveats.append(f"当前比较对象为{asset_type}，部分股票专有指标不参与评分。"
                       if asset_type != "股票" else "")
        caveats = [item for item in caveats if item]
    return ComparisonDecision(
        status="ok", weights=weights, dimensions=dimensions, candidates=candidates,
        preferred=best.target, preferred_name=best.name,
        conclusion=conclusion, caveats=caveats, missing=missing,
    )
