"""从会话上下文找回本轮省略的研究标的。

精简/即时研究不调用模型，只能按已加载资料回答。用户追问「它的市净率呢」
时，问题本身没有点名标的，这里按最近一轮提到的标的补齐，避免退回澄清。
只做确定性的字面匹配：不推断新标的，也不把不同领域的问题串在一起。
"""
from __future__ import annotations

import re
from typing import Any

from backend.app.models import FactRecord

# 明确换话题的说法：出现这些词时不再沿用上一轮标的。
OTHER_SCOPE = re.compile(r"市场|大盘|指数|板块|宏观|行业|基金|ETF|可转债|转债|持仓|组合", re.I)


def _entity_index(facts: list[FactRecord]) -> dict[str, str]:
    """代码与名称都映射到资料里的标准实体名，长键优先匹配。"""

    index: dict[str, str] = {}
    for fact in facts:
        entity = str(getattr(fact, "entity", "") or "").strip()
        if not entity:
            continue
        index.setdefault(entity.casefold(), entity)
        code = str(getattr(fact, "entity_code", "") or "").split(".")[0].strip()
        if code:
            index.setdefault(code.casefold(), entity)
    return index


def _last_entity_in(content: str, index: dict[str, str]) -> str | None:
    text = content.casefold()
    for key in sorted(index, key=len, reverse=True):
        if key and key in text:
            return index[key]
    return None


def carry_context_entity(request: Any, facts: list[FactRecord]) -> str | None:
    """返回上一轮提到的标的；本轮已点名其他领域或没有上下文时返回 None。"""

    query = str(getattr(request, "query", "") or "")
    if OTHER_SCOPE.search(query):
        return None
    index = _entity_index(facts)
    if not index:
        return None
    turns = list(getattr(request, "context_messages", ()) or ())
    current = query.strip()
    for turn in reversed(turns):
        if getattr(turn, "role", "") != "user":
            continue
        content = str(getattr(turn, "content", "") or "").strip()
        if not content or content == current:
            continue
        found = _last_entity_in(content, index)
        if found:
            return found
    target = str(getattr(request, "research_target", "") or "").strip().casefold()
    if target:
        return index.get(target) or index.get(target.split(".")[0])
    return None
