"""确定性组合诊断与再平衡建议。

设计约束：

* 只用标准库（math / statistics），不引入 numpy、scipy 或任何求解器，
  保证在无科学计算环境的部署机上也能跑出同样的小数位；
* 全程不调用大模型：权重、风险贡献、相关性都由价格序列直接算得，
  可被逐项复算，避免"权重是模型编出来的"这类不可审计输出；
* 数据不足时显式降级（``basis`` 字段说明依据），绝不补造历史收益。

对外主入口是 :func:`build_portfolio_plan`，返回可直接 JSON 化的字典。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from statistics import fmean
from typing import Any, Iterable, Sequence

TRADING_DAYS = 252
EPS = 1e-12
MIN_WEIGHT_TOTAL = 1e-6
MAX_SERIES_LENGTH = 750

# 风险等级到"更看重收益还是更看重分散"的映射；未知等级按稳健处理。
_AGGRESSIVE_LEVELS = frozenset({"C4", "C5", "积极型", "进取型", "激进型", "激进"})
_CONSERVATIVE_LEVELS = frozenset({"C1", "C2", "保守型", "稳健型", "保守"})


@dataclass(frozen=True)
class Holding:
    """一个持仓条目；weight 为占总资产比例（0-1）。"""

    symbol: str
    weight: float
    name: str = ""
    industry: str | None = None
    volatility: float | None = None

    @property
    def label(self) -> str:
        return self.name or self.symbol


@dataclass(frozen=True)
class OptimizationConstraints:
    """与画像中的单票/行业上限保持一致；默认值与 UserProfile 相同。"""

    single_limit: float = 0.20
    industry_limit: float = 0.30
    rebalance_threshold: float = 0.02

    def clamped(self) -> "OptimizationConstraints":
        single = min(max(self.single_limit, 0.01), 1.0)
        industry = min(max(self.industry_limit, single), 1.0)
        threshold = min(max(self.rebalance_threshold, 0.0), 0.25)
        return OptimizationConstraints(single, industry, threshold)


# ---------------------------------------------------------------------------
# 序列统计
# ---------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def normalize_prices(prices: Iterable[Any]) -> list[float]:
    """把价格序列清洗成正数列表，保持顺序并按需截断尾部（最近 N 个点）。"""

    cleaned: list[float] = []
    for item in prices or []:
        number = _finite(item)
        if number is None or number <= 0:
            continue
        cleaned.append(number)
    if len(cleaned) > MAX_SERIES_LENGTH:
        cleaned = cleaned[-MAX_SERIES_LENGTH:]
    return cleaned


def simple_returns(prices: Sequence[float]) -> list[float]:
    """简单收益率序列；输入不足两个点或价格非正时返回空列表。"""

    values = normalize_prices(prices)
    if len(values) < 2:
        return []
    returns: list[float] = []
    for previous, current in zip(values, values[1:]):
        if previous <= 0:
            continue
        returns.append(current / previous - 1.0)
    return returns


def _stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = fmean(values)
    variance = sum((value - average) ** 2 for value in values) / (len(values) - 1)
    return sqrt(max(variance, 0.0))


def annualized_volatility(returns: Sequence[float]) -> float | None:
    if len(returns) < 2:
        return None
    return _stdev(returns) * sqrt(TRADING_DAYS)


def _aligned(series: Sequence[Sequence[float]]) -> list[list[float]]:
    """按尾部对齐多条收益率序列，长度取最短者（不同上市日期也能算相关性）。"""

    usable = [list(item) for item in series if len(item) >= 3]
    if len(usable) < 2:
        return []
    length = min(len(item) for item in usable)
    return [item[-length:] for item in usable]


def correlation_matrix(series: Sequence[Sequence[float]]) -> list[list[float]]:
    """皮尔逊相关系数矩阵；无法计算的位置用 0 占位（并在结果里说明）。"""

    aligned = _aligned(series)
    if not aligned:
        return []
    means = [fmean(item) for item in aligned]
    deviations = [[value - mean for value in item] for item, mean in zip(aligned, means)]
    scales = [sqrt(sum(value ** 2 for value in item)) for item in deviations]
    size = len(aligned)
    matrix = [[0.0 for _ in range(size)] for _ in range(size)]
    for i in range(size):
        matrix[i][i] = 1.0
        for j in range(i + 1, size):
            if scales[i] <= EPS or scales[j] <= EPS:
                value = 0.0
            else:
                value = sum(a * b for a, b in zip(deviations[i], deviations[j])) / (scales[i] * scales[j])
            matrix[i][j] = round(max(-1.0, min(1.0, value)), 4)
            matrix[j][i] = matrix[i][j]
    return matrix


def covariance_matrix(series: Sequence[Sequence[float]]) -> list[list[float]]:
    """年化协方差矩阵；序列不足时返回空矩阵。"""

    aligned = _aligned(series)
    if not aligned:
        return []
    size = len(aligned)
    means = [fmean(item) for item in aligned]
    centered = [[value - mean for value in item] for item, mean in zip(aligned, means)]
    dof = len(aligned[0]) - 1
    if dof < 1:
        return []
    matrix = [[0.0 for _ in range(size)] for _ in range(size)]
    for i in range(size):
        for j in range(i, size):
            value = sum(a * b for a, b in zip(centered[i], centered[j])) / dof * TRADING_DAYS
            matrix[i][j] = value
            matrix[j][i] = value
    return matrix


# ---------------------------------------------------------------------------
# 组合度量
# ---------------------------------------------------------------------------


def normalize_weights(weights: Sequence[float]) -> list[float]:
    cleaned = [max(0.0, _finite(weight) or 0.0) for weight in weights]
    total = sum(cleaned)
    if total <= MIN_WEIGHT_TOTAL:
        if not cleaned:
            return []
        return [1.0 / len(cleaned)] * len(cleaned)
    return [weight / total for weight in cleaned]


def effective_n(weights: Sequence[float]) -> float:
    """有效持仓数 1/Σw²；越接近 1 说明越集中。"""

    normalized = normalize_weights(weights)
    total = sum(weight ** 2 for weight in normalized)
    if total <= EPS:
        return 0.0
    return round(1.0 / total, 3)


def concentration_hhi(weights: Sequence[float]) -> float:
    normalized = normalize_weights(weights)
    return round(sum(weight ** 2 for weight in normalized), 4)


def portfolio_volatility(weights: Sequence[float], cov: Sequence[Sequence[float]]) -> float | None:
    size = len(cov)
    if size == 0 or len(weights) != size:
        return None
    normalized = normalize_weights(weights)
    variance = 0.0
    for i in range(size):
        for j in range(size):
            variance += normalized[i] * normalized[j] * cov[i][j]
    if variance < 0:
        variance = 0.0
    return sqrt(variance)


def risk_contributions(weights: Sequence[float], cov: Sequence[Sequence[float]]) -> list[float]:
    """各资产对组合方差的贡献占比（合计为 1）。"""

    size = len(cov)
    if size == 0 or len(weights) != size:
        return []
    normalized = normalize_weights(weights)
    marginal = []
    for i in range(size):
        total = 0.0
        for j in range(size):
            total += cov[i][j] * normalized[j]
        marginal.append(total)
    contributions = [normalized[i] * marginal[i] for i in range(size)]
    overall = sum(contributions)
    if overall <= EPS:
        return [1.0 / size] * size
    return [round(value / overall, 4) for value in contributions]


def diversification_ratio(weights: Sequence[float], volatilities: Sequence[float]) -> float | None:
    """分散化比率 = Σwᵢσᵢ / σ_p；需要协方差才能算 σ_p，这里由调用方传入。"""

    normalized = normalize_weights(weights)
    if len(normalized) != len(volatilities):
        return None
    weighted = sum(weight * vol for weight, vol in zip(normalized, volatilities) if vol > 0)
    return round(weighted, 6) if weighted > 0 else None


# ---------------------------------------------------------------------------
# 约束投影与权重方案
# ---------------------------------------------------------------------------


def _project(weights: Sequence[float], caps: Sequence[float]) -> list[float]:
    """按逐资产上限做"削峰填谷"，返回归一化且不越过上限的权重。"""

    size = len(weights)
    if size == 0:
        return []
    safe_caps = [min(max(_finite(cap) or 0.0, 0.0), 1.0) for cap in caps]
    capacity = sum(safe_caps)
    if capacity <= EPS:
        return [0.0] * size
    normalized = normalize_weights(weights)
    result = [min(normalized[i], safe_caps[i]) for i in range(size)]
    for _ in range(64):
        deficit = 1.0 - sum(result)
        if deficit <= 1e-9:
            break
        room = [i for i in range(size) if result[i] < safe_caps[i] - 1e-12]
        if not room:
            break
        free = sum(safe_caps[i] - result[i] for i in room)
        if free <= EPS:
            break
        for i in room:
            result[i] = min(safe_caps[i], result[i] + deficit * (safe_caps[i] - result[i]) / free)
    total = sum(result)
    if total <= EPS:
        return [0.0] * size
    if abs(total - 1.0) > 1e-9:
        result = [value / total for value in result]
    return [min(max(value, 0.0), 1.0) for value in result]


def _cap_by_industry(
    weights: Sequence[float],
    industries: Sequence[str | None],
    cap: float,
) -> list[float]:
    """行业暴露超过上限时，按比例削减该行业内的权重并把差额还给其他行业。"""

    if not weights:
        return []
    result = normalize_weights(weights)
    if cap >= 1.0:
        return result
    groups: dict[str, list[int]] = {}
    for index, industry in enumerate(industries):
        key = (industry or "").strip() or "未分类"
        groups.setdefault(key, []).append(index)
    for _ in range(8):
        exposure = {key: sum(result[i] for i in members) for key, members in groups.items()}
        overfull = {key: value for key, value in exposure.items() if value > cap + 1e-9}
        if not overfull:
            break
        released = 0.0
        for key, value in overfull.items():
            scale = cap / value
            for index in groups[key]:
                before = result[index]
                result[index] = before * scale
                released += before - result[index]
        receivers = [index for key, members in groups.items() if key not in overfull for index in members]
        if not receivers or released <= EPS:
            break
        room_total = sum(1.0 - sum(result[i] for i in members) for key, members in groups.items() if key not in overfull)
        for index in receivers:
            key = (industries[index] or "").strip() or "未分类"
            room_key = max(cap - sum(result[i] for i in groups[key]), 0.0)
            share = (cap - sum(result[i] for i in groups[key])) / room_total if room_total > EPS else 0.0
            addition = released * share
            addition = min(addition, room_key)
            result[index] += addition
    total = sum(result)
    if total <= EPS:
        return normalize_weights(weights)
    return [value / total for value in result]


def equal_weight(n: int, caps: Sequence[float] | None = None) -> list[float]:
    if n <= 0:
        return []
    base = [1.0 / n] * n
    return _project(base, caps) if caps else normalize_weights(base)


def inverse_volatility_weights(
    volatilities: Sequence[float],
    caps: Sequence[float] | None = None,
) -> list[float]:
    usable = [vol if vol and vol > 0 else 0.0 for vol in volatilities]
    positive = [vol for vol in usable if vol > 0]
    if not positive:
        return equal_weight(len(usable), caps)
    floor = max(min(positive) * 0.25, 1e-4)
    raw = [1.0 / (vol if vol > 0 else floor) for vol in usable]
    return _project(raw, caps) if caps else normalize_weights(raw)


def risk_parity_weights(
    cov: Sequence[Sequence[float]],
    caps: Sequence[float] | None = None,
    *,
    max_iterations: int = 400,
    tolerance: float = 1e-6,
) -> list[float]:
    """风险平价：让每个资产的风险贡献相等，用乘法不动点迭代求解。

    迭代失败时退回逆波动率权重——两者在同一数量级，不会给出离谱的数字。
    """

    size = len(cov)
    if size == 0:
        return []
    diagonal = [max(cov[i][i], 0.0) for i in range(size)]
    volatilities = [sqrt(value) for value in diagonal]
    fallback = inverse_volatility_weights(volatilities, caps)
    if all(value <= EPS for value in volatilities):
        return fallback
    weights = equal_weight(size, caps)
    for _ in range(max_iterations):
        marginal = []
        for i in range(size):
            total = 0.0
            for j in range(size):
                total += cov[i][j] * weights[j]
            marginal.append(total)
        variance = sum(weights[i] * marginal[i] for i in range(size))
        if variance <= EPS:
            return fallback
        sigma = sqrt(variance)
        target = sigma / size if size else 0.0
        updated = []
        for i in range(size):
            contribution = weights[i] * marginal[i] / sigma
            if contribution <= EPS:
                updated.append(weights[i] + 1e-6)
            else:
                updated.append(weights[i] * sqrt(target / contribution))
        candidate = _project(updated, caps) if caps else normalize_weights(updated)
        shift = max(abs(candidate[i] - weights[i]) for i in range(size))
        weights = candidate
        if shift <= tolerance:
            break
    if not weights or abs(sum(weights) - 1.0) > 0.01:
        return fallback
    return weights


# ---------------------------------------------------------------------------
# 报告组装
# ---------------------------------------------------------------------------


def _round_list(values: Sequence[float], digits: int = 4) -> list[float]:
    return [round(value, digits) for value in values]


def _risk_profile_target(risk_level: str | None) -> str:
    level = (risk_level or "").strip()
    if level in _AGGRESSIVE_LEVELS:
        return "risk_parity"
    if level in _CONSERVATIVE_LEVELS:
        return "inverse_volatility"
    return "risk_parity"


def _plan_warnings(
    *,
    basis: str,
    holdings: Sequence[Holding],
    vol_estimates: Sequence[float],
    constraints: OptimizationConstraints,
    single_violations: list[str],
    industry_exposure: dict[str, float],
    imputed_correlation: Sequence[str] = (),
    relaxations: Sequence[str] = (),
) -> list[str]:
    warnings: list[str] = list(relaxations)
    if basis != "live_returns":
        warnings.append("缺少足够的历史价格序列，波动率按可得数据近似，风险贡献仅作方向参考。")
    if len(holdings) < 3:
        warnings.append("持仓数量少于 3 个，分散化结论不具代表性。")
    if single_violations:
        warnings.append("当前持仓已越过单票上限：" + "、".join(single_violations) + "。")
    heavy = [name for name, exposure in industry_exposure.items() if exposure > constraints.industry_limit + 1e-9]
    if heavy:
        warnings.append("行业暴露超过上限：" + "、".join(heavy) + "。")
    missing_vol = [holding.label for holding, vol in zip(holdings, vol_estimates) if vol <= 0]
    if missing_vol:
        warnings.append("以下标的没有可用波动率数据，按等风险处理：" + "、".join(missing_vol) + "。")
    if imputed_correlation:
        warnings.append(
            "以下标的没有足够历史序列，相关性按 0 处理（不假装知道它们的联动）："
            + "、".join(imputed_correlation) + "。"
        )
    return warnings


def build_portfolio_plan(
    holdings: Sequence[Holding | dict[str, Any]],
    *,
    price_series: dict[str, Sequence[Any]] | None = None,
    constraints: OptimizationConstraints | None = None,
    risk_level: str | None = None,
    total_value: float | None = None,
) -> dict[str, Any]:
    """生成组合诊断 + 再平衡建议。

    ``price_series`` 形如 ``{"600519": [1680.0, 1695.5, ...]}``，按时间升序；
    缺失的标的会退回持仓自带波动率，再退回等风险假设，并由 ``basis`` 说明。
    """

    limits = (constraints or OptimizationConstraints()).clamped()
    items: list[Holding] = []
    for raw in holdings or []:
        if isinstance(raw, Holding):
            items.append(raw)
            continue
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol") or raw.get("code") or raw.get("target") or "").strip()
        weight = _finite(raw.get("weight"))
        if weight is None:
            weight = _finite(raw.get("ratio"))
        if not symbol or weight is None or weight < 0:
            continue
        items.append(
            Holding(
                symbol=symbol,
                weight=weight,
                name=str(raw.get("name") or "").strip(),
                industry=(str(raw.get("industry")).strip() or None) if raw.get("industry") else None,
                volatility=_finite(raw.get("volatility")),
            )
        )
    if not items:
        return {
            "status": "empty",
            "basis": "no_holdings",
            "message": "没有可分析的持仓，无法给出组合与再平衡建议。",
            "holdings": [],
            "candidates": {},
            "rebalance": [],
            "warnings": ["未提供持仓明细。"],
        }

    # 上限本身可能不可行：3 条持仓配 20% 单票上限时，满仓需要 33.3%。这时按"最小可行
    # 上限"放宽并明确写进 warnings/relaxations，既不偷偷超限，也不留出无法解释的现金。
    relaxations: list[str] = []
    floor_single = 1.0 / max(len(items), 1)
    if limits.single_limit + 1e-9 < floor_single:
        relaxations.append(
            f"单票上限 {limits.single_limit:.0%} 在 {len(items)} 条持仓上不可行，已放宽到 {floor_single:.2%}。")
        effective_single = floor_single
    else:
        effective_single = limits.single_limit
    labelled_industries = {(item.industry or "").strip() for item in items if (item.industry or "").strip()}
    if not labelled_industries:
        # 没填行业就没有行业约束可言，静默按 30% 削权重只会给出无法解释的仓位。
        relaxations.append("未提供行业归属，本轮不执行行业上限。")
        effective_industry = 1.0
    else:
        floor_industry = 1.0 / len(labelled_industries)
        if limits.industry_limit + 1e-9 < floor_industry:
            relaxations.append(
                f"行业上限 {limits.industry_limit:.0%} 在 {len(labelled_industries)} 个已标注行业上不可行，"
                f"已放宽到 {floor_industry:.2%}。")
            effective_industry = floor_industry
        else:
            effective_industry = limits.industry_limit
    if relaxations:
        limits = OptimizationConstraints(
            single_limit=min(effective_single, 1.0),
            industry_limit=min(effective_industry, 1.0),
            rebalance_threshold=limits.rebalance_threshold,
        )
    symbols = [item.symbol for item in items]
    industries = [item.industry for item in items]
    provided_series = price_series or {}
    series_map: dict[str, list[float]] = {}
    for symbol in symbols:
        returns = simple_returns(provided_series.get(symbol, []))
        if returns:
            series_map[symbol] = returns

    vol_estimates: list[float] = []
    explicit_only = 0
    for item in items:
        from_series = annualized_volatility(series_map.get(item.symbol, []))
        if from_series is not None and from_series > 0:
            vol_estimates.append(from_series)
        elif item.volatility is not None and item.volatility > 0:
            vol_estimates.append(float(item.volatility))
            explicit_only += 1
        else:
            vol_estimates.append(0.0)
    if len(series_map) == len(items):
        basis = "live_returns"
    elif series_map:
        basis = "mixed"
    elif explicit_only:
        basis = "volatility_only"
    else:
        basis = "equal_risk"

    # 协方差按"波动率 × 相关系数"重建，维度始终与持仓对齐：没有历史序列的标的
    # 只能拿到 0 相关系数，这一点会在 warnings 里明说，而不是假装知道相关性。
    size = len(items)
    correlation = [[1.0 if i == j else 0.0 for j in range(size)] for i in range(size)]
    correlation_symbols = [item.symbol for item in items]
    ordered_series = [series_map[item.symbol] for item in items if item.symbol in series_map]
    if len(ordered_series) >= 2:
        partial = correlation_matrix(ordered_series)
        positions = [index for index, item in enumerate(items) if item.symbol in series_map]
        for a, row in enumerate(partial):
            for b, value in enumerate(row):
                correlation[positions[a]][positions[b]] = value
    if any(vol > 0 for vol in vol_estimates):
        cov = [
            [correlation[i][j] * vol_estimates[i] * vol_estimates[j] for j in range(size)]
            for i in range(size)
        ]
    else:
        cov = []

    current = normalize_weights([item.weight for item in items])
    caps = [limits.single_limit] * len(items)
    single_violations = [
        item.label for item, weight in zip(items, current) if weight > limits.single_limit + 1e-9
    ]
    industry_exposure: dict[str, float] = {}
    for item, weight in zip(items, current):
        key = (item.industry or "").strip() or "未分类"
        industry_exposure[key] = round(industry_exposure.get(key, 0.0) + weight, 4)

    candidates: dict[str, list[float]] = {
        "equal_weight": equal_weight(len(items), caps),
        "inverse_volatility": inverse_volatility_weights(vol_estimates, caps),
    }
    if cov:
        candidates["risk_parity"] = _cap_by_industry(
            risk_parity_weights(cov, caps), industries, limits.industry_limit
        )
    else:
        candidates["risk_parity"] = candidates["inverse_volatility"]
    for key in list(candidates):
        candidates[key] = _cap_by_industry(candidates[key], industries, limits.industry_limit)

    recommended_key = _risk_profile_target(risk_level)
    if not cov and recommended_key == "risk_parity":
        recommended_key = "inverse_volatility"
    target = candidates.get(recommended_key) or candidates["inverse_volatility"]

    def metrics_of(weights: Sequence[float]) -> dict[str, Any]:
        volatility = portfolio_volatility(weights, cov) if cov else None
        if volatility is None:
            volatility = sum(w * vol for w, vol in zip(normalize_weights(weights), vol_estimates)) or None
        return {
            "volatility": round(volatility, 4) if volatility else None,
            "effective_n": effective_n(weights),
            "concentration_hhi": concentration_hhi(weights),
            "max_weight": round(max(normalize_weights(weights)) if weights else 0.0, 4),
            "risk_contribution": risk_contributions(weights, cov) if cov else [],
        }

    rebalance = []
    turnover = 0.0
    for item, current_weight, target_weight in zip(items, current, target):
        delta = target_weight - current_weight
        turnover += abs(delta)
        if abs(delta) < limits.rebalance_threshold:
            action = "hold"
        elif delta > 0:
            action = "increase"
        else:
            action = "reduce"
        amount = None
        if total_value and total_value > 0:
            amount = round(delta * total_value, 2)
        rebalance.append({
            "symbol": item.symbol,
            "name": item.label,
            "industry": item.industry,
            "current_weight": round(current_weight, 4),
            "target_weight": round(target_weight, 4),
            "delta": round(delta, 4),
            "action": action,
            "amount": amount,
        })

    planned_metrics = metrics_of(target)
    current_metrics = metrics_of(current)
    notes = [
        "权重为算术解，不含交易成本、税费与最小交易单位，落地前请按实际可成交金额取整。",
        f"约束：单票上限 {limits.single_limit:.0%}，行业上限 {limits.industry_limit:.0%}"
        + ("（原上限不可行，已放宽，见 warnings）。" if relaxations else "。"),
    ]
    if basis != "live_returns":
        notes.append("波动率来自画像/调用方估值或等风险假设，非历史序列回测结果。")

    return {
        "status": "ok",
        "basis": basis,
        "risk_level": risk_level,
        "recommended_strategy": recommended_key,
        "holdings": [
            {
                "symbol": item.symbol,
                "name": item.label,
                "industry": item.industry,
                "input_weight": round(item.weight, 6),
                "normalized_weight": round(current[i], 6),
                "annualized_volatility": round(vol_estimates[i], 6) if vol_estimates[i] else None,
                "has_price_history": item.symbol in series_map,
            }
            for i, item in enumerate(items)
        ],
        "current": current_metrics,
        "target": planned_metrics,
        "candidates": {key: _round_list(value) for key, value in candidates.items()},
        "correlation": {"symbols": correlation_symbols, "matrix": correlation},
        "industry_exposure": dict(sorted(industry_exposure.items(), key=lambda kv: kv[1], reverse=True)),
        "rebalance": rebalance,
        "turnover": round(turnover / 2, 4),
        "constraint_violations": {
            "single_security": single_violations,
            "industry": [
                name for name, exposure in industry_exposure.items()
                if exposure > limits.industry_limit + 1e-9
            ],
            "relaxed": relaxations,
        },
        "warnings": _plan_warnings(
            basis=basis,
            holdings=items,
            vol_estimates=vol_estimates,
            constraints=limits,
            single_violations=single_violations,
            industry_exposure=industry_exposure,
            imputed_correlation=[item.label for item in items if item.symbol not in series_map],
            relaxations=relaxations,
        ),
        "notes": notes,
        "method": "deterministic_arithmetic_no_llm",
    }


def price_series_from_points(points: Iterable[Any]) -> list[float]:
    """把 ``PriceHistoryPoint`` / ``{"date","value"}`` 转成升序价格序列。"""

    collected: list[tuple[Any, float]] = []
    for item in points or []:
        if isinstance(item, dict):
            key, raw = item.get("date"), item.get("value")
        else:
            key, raw = getattr(item, "date", None), getattr(item, "value", None)
        if isinstance(raw, str):
            # 上游偶尔返回 "1,680.00 元" 这类带千分位/单位的文本。
            raw = raw.replace(",", "").replace("，", "").replace("元", "").strip()
        number = _finite(raw)
        if number is None or number <= 0:
            continue
        collected.append((key, number))
    try:
        collected.sort(key=lambda pair: pair[0])
    except TypeError:
        pass
    return [value for _, value in collected]