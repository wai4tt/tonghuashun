"""热门板块：把问财的板块涨跌幅、成交额与换手率整理成量能／走势描述。

只做数据描述与分档，不构成推荐、评级或买卖建议。供应商返回的板块集合由
问财「今日热门板块」口径决定，本模块不自行挑选或补造标的。
"""
from __future__ import annotations

from time import monotonic
from typing import Any, Callable

import streamlit as st

CACHE_TTL = 60
HOT_SECTOR_QUERY = "今日热门板块 涨跌幅 成交额 换手率"
MAX_SECTORS = 6

# 平台分档口径：涨跌幅决定走势，换手率决定量能。阈值是固定规则，不是预测。
TREND_BANDS = ((2.0, "强势"), (0.5, "偏强"), (-0.5, "震荡"), (-2.0, "偏弱"))
VOLUME_BANDS = ((6.0, "明显放量"), (3.0, "温和活跃"), (0.0, "缩量"))

DESCRIPTIONS = {
    ("强势", "明显放量"): "放量上攻，短线资金关注度高，追高需留意波动放大。",
    ("强势", "温和活跃"): "随量上行，情绪回暖，能否延续要看后续量能。",
    ("强势", "缩量"): "缩量上涨，承接资金有限，持续性待观察。",
    ("偏强", "明显放量"): "放量走强，资金参与度提升，注意节奏变化。",
    ("偏强", "温和活跃"): "温和放量上行，属于常见的轮动走强形态。",
    ("偏强", "缩量"): "缩量抬升，上涨基础不牢，容易反复。",
    ("震荡", "明显放量"): "放量震荡，多空分歧加大，方向尚未选择。",
    ("震荡", "温和活跃"): "量能平稳，区间震荡为主。",
    ("震荡", "缩量"): "缩量整理，观望情绪偏浓。",
    ("偏弱", "明显放量"): "放量走弱，抛压有所增加。",
    ("偏弱", "温和活跃"): "温和回落，短线资金分歧加大。",
    ("偏弱", "缩量"): "缩量回调，杀跌意愿不强但也缺少承接。",
    ("弱势", "明显放量"): "放量下跌，短线风险释放中，不宜盲目抄底。",
    ("弱势", "温和活跃"): "走弱且量能不减，弱势格局延续。",
    ("弱势", "缩量"): "缩量下跌，承接力度不足。",
}


def _as_float(value: Any) -> float | None:
    """问财的数值有时是字符串或带千分位，统一转成浮点。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def format_amount(value: float | None) -> str:
    """成交额按中文习惯显示为亿／万亿，缺失时用破折号。"""

    if value is None:
        return "—"
    if value >= 1e12:
        return f"{value / 1e12:.2f} 万亿"
    if value >= 1e8:
        return f"{value / 1e8:.1f} 亿"
    if value >= 1e4:
        return f"{value / 1e4:.0f} 万"
    return f"{value:.0f}"


def trend_label(change: float | None) -> str:
    if change is None:
        return "数据不足"
    for threshold, label in TREND_BANDS:
        if change >= threshold:
            return label
    return "弱势"


def volume_label(rate: float | None) -> str:
    if rate is None:
        return "数据不足"
    for threshold, label in VOLUME_BANDS:
        if rate >= threshold:
            return label
    return "缩量"


def parse_sector_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把扁平的事实列表按板块聚合成一行一个板块。"""

    grouped: dict[str, dict[str, Any]] = {}
    for fact in facts:
        entity = fact.get("entity")
        field = fact.get("field")
        if not entity or not field:
            continue
        row = grouped.setdefault(str(entity), {"name": str(entity), "code": None, "date": None})
        if field == "change":
            row["change"] = _as_float(fact.get("value"))
        elif field == "turnover_value":
            row["turnover"] = _as_float(fact.get("value"))
        elif field == "turnover_rate":
            row["rate"] = _as_float(fact.get("value"))
        elif field == "指数代码":
            row["code"] = str(fact.get("value") or "") or None
        observed = fact.get("observation_date") or fact.get("period")
        if observed and not row["date"]:
            row["date"] = str(observed)[:10]
    rows = [row for row in grouped.values() if row.get("change") is not None]
    rows.sort(key=lambda row: row.get("turnover") or 0.0, reverse=True)
    return rows[:MAX_SECTORS]


def describe(row: dict[str, Any]) -> tuple[str, str, str]:
    trend = trend_label(row.get("change"))
    volume = volume_label(row.get("rate"))
    return trend, volume, DESCRIPTIONS.get((trend, volume), "数据有限，暂不做进一步描述。")


def load_hot_sectors(api_base: str, api_request: Callable, *, refresh: bool = False) -> dict[str, Any]:
    """会话内缓存 60 秒；刷新按钮显式跳过缓存。"""

    cache = st.session_state.setdefault("hot_sector_cache", {})
    entry = cache.get(api_base)
    if entry and not refresh and monotonic() - entry["loaded_at"] < CACHE_TTL:
        return entry["result"]
    raw = api_request(api_base, "POST", "/data/fetch",
                      {"kind": "sector_screen", "target": HOT_SECTOR_QUERY})
    if not isinstance(raw, dict):
        result = {"status": "unavailable", "rows": [], "date": None,
                  "message": "热门板块暂不可用，请确认已登录且数据源已配置。"}
    else:
        rows = parse_sector_facts(raw.get("facts") or [])
        result = {
            "status": raw.get("status") or "empty",
            "rows": rows,
            "date": next((row["date"] for row in rows if row.get("date")), None),
            "message": raw.get("message"),
        }
    cache[api_base] = {"loaded_at": monotonic(), "result": result}
    while len(cache) > 4:
        cache.pop(next(iter(cache)))
    return result


def _render_sector(row: dict[str, Any]) -> None:
    change = row.get("change")
    trend, volume, sentence = describe(row)
    color = "red" if (change or 0) >= 0 else "green"
    code = f" `{row['code']}`" if row.get("code") else ""
    quote = f":{color}[{change:+.2f}%]" if change is not None else "—"
    parts = [quote, f"成交额 {format_amount(row.get('turnover'))}"]
    if row.get("rate") is not None:
        parts.append(f"换手率 {row['rate']:.2f}%")
    st.markdown(f"**{row['name']}**{code}")
    st.markdown("　".join(parts))
    st.markdown(f"量能：**{volume}** ｜ 走势：**{trend}**")
    st.caption(sentence)


def render_hot_sectors(api_base: str, api_request: Callable, *, key: str = "market") -> None:
    """在「相关资讯」之后展示问财口径的热门板块观察列表。"""

    with st.container(border=True, key="hot-sectors-box"):
        header = st.container(horizontal=True, vertical_alignment="center")
        with header:
            st.markdown("**热门板块 · 量能与走势**")
            st.space("stretch")
            refresh = st.button("刷新板块", key=f"hot_sectors_refresh_{key}", icon=":material/refresh:")
        result = load_hot_sectors(api_base, api_request, refresh=refresh)
        rows = result.get("rows") or []
        if not rows:
            st.caption(result.get("message") or "问财本次未返回热门板块数据。")
            return
        ups = sum(1 for row in rows if (row.get("change") or 0) > 0)
        downs = sum(1 for row in rows if (row.get("change") or 0) < 0)
        heavy = sum(1 for row in rows if volume_label(row.get("rate")) != "缩量")
        date = f" · 数据日期 {result['date']}" if result.get("date") else ""
        st.caption(f"共 {len(rows)} 个板块，{ups} 涨 {downs} 跌，量能活跃 {heavy} 个{date}。按成交额从高到低排列。")
        columns = st.columns(3)
        for index, row in enumerate(rows):
            with columns[index % 3]:
                _render_sector(row)
        st.caption("量能按板块换手率分档（≥6% 明显放量、3%–6% 温和活跃、<3% 缩量），走势按当日涨跌幅分档"
                   "（≥2% 强势、0.5%–2% 偏强、±0.5% 内震荡、-2%–-0.5% 偏弱、≤-2% 弱势）。"
                   "板块集合由问财「今日热门板块」口径给出，本页不构成推荐或买卖建议。")
