"""数值级幻觉检测：把回答里的数字拉回事实表逐一对账。

大模型最容易编造的不是结构，而是数字。这个模块只做一件事：

1. 从中文回答里抽出带单位的数值（百分比、倍数、金额、评分）；
2. 用同一条回答实际引用的事实（FactRecord）去匹配这些数值；
3. 对不上的数值连同上下文一起列出来，交给界面/人工复核。

它不是合规闸门，也不改结论：只在"数字找不到出处"时降低可信度和提示复核，
避免把一次措辞不当升级成一次误拦截。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

_TOLERANCE_ABS = 0.02
_TOLERANCE_REL = 0.01
_CONTEXT_CHARS = 24
_MAX_LISTED = 8

_PERCENT = re.compile(r"(\d{1,7}(?:\.\d{1,4})?)\s*(?:%|％|个百分点)")
_MULTIPLE = re.compile(r"(\d{1,5}(?:\.\d{1,4})?)\s*倍")
_MONEY = re.compile(r"(\d{1,9}(?:\.\d{1,4})?)\s*(亿元|亿|万元|万|元)")
_SCORE = re.compile(r"(\d{1,3}(?:\.\d{1,2})?)\s*分(?!钟|位|之|析|类|享|红)")

# 明显是日期/代码/年份的片段不参与对账，否则"2024""600519""1.5年"都会误报。
_LOOKS_LIKE_YEAR = re.compile(r"^(19|20)\d{2}$")


@dataclass(frozen=True)
class NumericClaim:
    raw: str
    value: float
    kind: str  # percent / multiple / money / score
    unit: str
    position: int
    context: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "value": self.value,
            "kind": self.kind,
            "unit": self.unit,
            "context": self.context,
        }


@dataclass(frozen=True)
class _FactNumber:
    value: float
    fact_id: str
    field: str
    unit: str


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "")
        match = re.fullmatch(r"-?\d+(?:\.\d+)?", text)
        if not match:
            return None
        number = float(text)
    else:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def extract_numeric_claims(text: str) -> list[NumericClaim]:
    """抽取带单位的数值；同一位置只保留最长的一次匹配。"""

    if not text:
        return []
    claims: list[NumericClaim] = []
    taken: list[tuple[int, int]] = []

    def add(match: re.Match[str], kind: str, unit: str) -> None:
        start, end = match.span()
        for left, right in taken:
            if start < right and left < end:
                return
        number = _to_float(match.group(1))
        if number is None:
            return
        if kind == "score" and not 0 <= number <= 100:
            return
        if kind == "percent" and not 0 <= number <= 1000:
            return
        if _LOOKS_LIKE_YEAR.match(match.group(1)) and kind == "money":
            return
        taken.append((start, end))
        left_context = max(0, start - _CONTEXT_CHARS)
        right_context = min(len(text), end + _CONTEXT_CHARS)
        claims.append(
            NumericClaim(
                raw=match.group(0).strip(),
                value=number,
                kind=kind,
                unit=unit,
                position=start,
                context=text[left_context:right_context].replace("\n", " ").strip(),
            )
        )

    for match in _MONEY.finditer(text):
        unit = match.group(2)
        add(match, "money", unit)
    for match in _PERCENT.finditer(text):
        add(match, "percent", "%")
    for match in _MULTIPLE.finditer(text):
        add(match, "multiple", "倍")
    for match in _SCORE.finditer(text):
        add(match, "score", "分")
    claims.sort(key=lambda claim: claim.position)
    return claims


def _fact_numbers(facts: Iterable[Any]) -> list[_FactNumber]:
    numbers: list[_FactNumber] = []
    for fact in facts or []:
        fact_id = str(getattr(fact, "fact_id", "") or "")
        field = str(getattr(fact, "field", "") or "")
        unit = str(getattr(fact, "unit", "") or "")
        for candidate in (getattr(fact, "normalized_value", None), getattr(fact, "value", None)):
            number = _to_float(candidate)
            if number is None:
                continue
            numbers.append(_FactNumber(value=number, fact_id=fact_id, field=field, unit=unit))
        raw = getattr(fact, "value", None)
        if isinstance(raw, str):
            for match in re.finditer(r"-?\d+(?:\.\d+)?", raw.replace(",", "")):
                number = _to_float(match.group(0))
                if number is not None:
                    numbers.append(_FactNumber(value=number, fact_id=fact_id, field=field, unit=unit))
    return numbers


def _close(claim: float, fact: float) -> bool:
    return abs(claim - fact) <= max(_TOLERANCE_ABS, _TOLERANCE_REL * abs(claim))


def _match_claim(claim: NumericClaim, pool: Sequence[_FactNumber]) -> str | None:
    """返回命中的 fact_id；对不上返回 None。"""

    for item in pool:
        if claim.kind == "money":
            scale = {"亿元": 1e8, "亿": 1e8, "万元": 1e4, "万": 1e4, "元": 1.0}[claim.unit]
            if _close(claim.value * scale, item.value):
                return item.fact_id
            # 事实侧也可能以"亿元"为单位直接给出 3.2 这种数字。
            if _close(claim.value, item.value) and item.unit in {"亿元", "亿", "万元", "万", "元"}:
                return item.fact_id
            continue
        if claim.kind == "percent":
            if _close(claim.value, item.value):
                return item.fact_id
            # 事实常以 0.2 的小数形式存比例。
            if 0 <= item.value <= 1 and _close(claim.value / 100.0, item.value):
                return item.fact_id
            continue
        if _close(claim.value, item.value):
            return item.fact_id
    return None


def check_text_grounding(
    text: str,
    facts: Iterable[Any],
    *,
    max_listed: int = _MAX_LISTED,
) -> dict[str, Any]:
    """单段文本的数值对账结果。facts 为空时不判罚，只标记为不可核验。"""

    claims = extract_numeric_claims(text)
    pool = _fact_numbers(facts)
    ungrounded: list[dict[str, Any]] = []
    matched = 0
    for claim in claims:
        fact_id = _match_claim(claim, pool) if pool else None
        if fact_id is not None:
            matched += 1
            continue
        entry = claim.as_dict()
        entry["matched_fact_id"] = None
        ungrounded.append(entry)
    total = len(claims)
    ratio = round(matched / total, 4) if total else None
    if not pool:
        risk = "unverifiable"
    elif total == 0:
        risk = "none"
    elif ratio is not None and ratio >= 0.9:
        risk = "low"
    elif ratio is not None and ratio >= 0.5:
        risk = "medium"
    else:
        risk = "high"
    return {
        "claim_count": total,
        "grounded_count": matched,
        "grounded_ratio": ratio,
        "risk": risk,
        "ungrounded": ungrounded[:max_listed],
        "ungrounded_count": len(ungrounded),
    }


def summarize_grounding(
    agent_results: Iterable[Any],
    facts: Iterable[Any],
    *,
    max_listed: int = _MAX_LISTED,
) -> dict[str, Any]:
    """按智能体汇总数值对账结果；用于建议包里的 ``numeric_grounding`` 字段。"""

    fact_list = list(facts or [])
    by_agent: dict[str, dict[str, Any]] = {}
    total_claims = 0
    total_grounded = 0
    all_ungrounded: list[dict[str, Any]] = []
    for result in agent_results or []:
        agent_id = str(getattr(result, "agent_id", "") or "")
        opinion = str(getattr(result, "opinion", "") or "")
        if not opinion:
            continue
        report = check_text_grounding(opinion, fact_list, max_listed=max_listed)
        if report["claim_count"] == 0:
            continue
        by_agent[agent_id] = report
        total_claims += report["claim_count"]
        total_grounded += report["grounded_count"]
        for item in report["ungrounded"]:
            entry = dict(item)
            entry["agent_id"] = agent_id
            all_ungrounded.append(entry)
    ratio = round(total_grounded / total_claims, 4) if total_claims else None
    if not fact_list:
        risk = "unverifiable"
    elif total_claims == 0:
        risk = "none"
    elif ratio is not None and ratio >= 0.9:
        risk = "low"
    elif ratio is not None and ratio >= 0.5:
        risk = "medium"
    else:
        risk = "high"
    return {
        "status": "checked" if fact_list else "no_facts",
        "claim_count": total_claims,
        "grounded_count": total_grounded,
        "grounded_ratio": ratio,
        "risk": risk,
        "ungrounded_count": len(all_ungrounded),
        "ungrounded": all_ungrounded[:max_listed],
        "by_agent": by_agent,
        "method": "regex_extraction_against_cited_facts",
    }