"""从汇总型交易报告提取画像。仅读取单元格，不执行公式或文档指令。"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import date
from pathlib import Path

METRICS = {
    "总成交笔数": "trade_count", "交易日数": "active_days", "涉及股票数": "security_count",
    "日均成交笔数": "daily_trade_count", "年周转率（总买入/本金）": "reported_turnover",
    "单笔成交额中位数": "median_trade_amount", "个股持仓时间中位数": "median_holding_days",
    "当日买卖同日占比": "same_day_trading_percent", "买卖总金额": "reported_trade_amounts",
    "交易总费用": "reported_total_cost",
}
TABLE_HEADERS = {"月份", "区间", "板块", "题材方向", "股票", "项目"}


def _text(value):
    return str(value if value is not None else "").strip()[:2000]


def _numeric(value):
    if isinstance(value, bool):
        return None
    text = _text(value).replace(",", "").replace("，", "")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        return None
    number = float(text)
    return number if math.isfinite(number) and 0 <= number <= 1e14 else None


def read_trade_report(*, raw, names, rows, file_name):
    # Small reports only; transactional files continue through the normal parser.
    sheets = {}
    for name in names:
        values = []
        for n, row in enumerate(rows(name), 1):
            if n > 2000:
                return None
            values.append(tuple(row))
        sheets[name] = values
    metrics, sections, notes, limitations, source_notes, dates = [], [], [], [], [], []
    recognized = {}
    report_title = False
    for name, values in sheets.items():
        section = None
        metric_table = False
        heading = ""
        for n, row in enumerate(values, 1):
            padded = (*row, None, None, None)
            first, second, third = (_text(v) for v in padded[:3])
            if not first:
                section = None
                metric_table = False
                continue
            if n <= 2 and "分析" in first and ("交易" in first or "投资偏好" in first):
                report_title = True
            if "期间" in first or "数据来源" in first:
                dates.extend(re.findall(r"20\d{2}-\d{2}-\d{2}", first))
            if first == "指标" and second == "数值":
                metric_table = True
                section = None
                continue
            if metric_table and first in METRICS:
                if second.startswith(("=", "#")):
                    limitations.append(f"{name} 第 {n} 行指标含公式或错误，未导入其数值。")
                    continue
                metric = {"label": first, "value": second, "unit": third,
                          "sheet": name, "row": n, "origin": "reported"}
                metrics.append(metric)
                number = _numeric(padded[1])
                if METRICS[first] in recognized:
                    raise ValueError("报告中的关键指标重复，请保留一份明确的汇总数据。")
                recognized[METRICS[first]] = number
                continue
            if first in TABLE_HEADERS and second in {"成交笔数", "笔数", "成交金额(元)", "估算成交金额(元)", "金额(元)"}:
                section = {"title": heading or name, "sheet": name, "header_row": n,
                           "columns": [first, second, third or "说明"], "rows": [], "reported_total": None}
                sections.append(section)
                metric_table = False
                continue
            if section:
                number = _numeric(padded[1])
                if number is not None and first != "合计":
                    section["rows"].append({"label": first, "value": number,
                                           "detail": third if not third.startswith("=") else "公式未执行",
                                           "row": n})
                elif first == "合计" and number is not None:
                    section["reported_total"] = number
                elif first != "合计":
                    limitations.append(f"{name} 第 {n} 行未取得有效数值，未纳入本表合计。")
                continue
            if first[0] in "①②③④⑤⑥⑦⑧⑨" and second:
                notes.append(f"原报告表述：{first}：{second}")
            elif re.match(r"^[1-9]\.\s", first):
                source_notes.append({"text": first, "sheet": name, "row": n})
            if not second:
                heading = first
    if not report_title or not {"trade_count", "security_count"} <= recognized.keys():
        return None
    for field in ("trade_count", "security_count", "active_days"):
        number = recognized.get(field)
        if field in recognized and (number is None or number <= 0 or not number.is_integer()):
            raise ValueError("报告成交笔数、股票数及交易日数必须为有效正整数。")
    period = sorted(set(dates))
    start, end = (period[0], period[-1]) if len(period) >= 2 else (None, None)
    if start and end:
        span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        if span > 366:
            limitations.append(f"报告期间为 {start} 至 {end}，共 {span} 天，超过一年；已保留原报告全期口径，未裁剪成近一年。")
    else:
        limitations.append("报告未提供可识别的完整起止日期，未推断分析期间。")
    monthly = []
    checks = []
    cost = None
    for section in sections:
        total = sum(row["value"] for row in section["rows"])
        section["calculated_total"] = total
        if section["reported_total"] is not None:
            checks.append({"label": section["title"] + "的合计", "reported": section["reported_total"],
                           "calculated": total, "matches": abs(total - section["reported_total"]) < .005})
        # Only additive amount/count tables receive recomputed shares.
        for row in section["rows"]:
            row["calculated_share"] = row["value"] / total if total else None
        if section["columns"][0] == "月份":
            for row in section["rows"]:
                if re.fullmatch(r"20\d{2}-\d{2}", row["label"]):
                    monthly.append({"month": row["label"], "trade_count": row["value"]})
            actual = sum(row["trade_count"] for row in monthly)
            checks.append({"label": "月度成交笔数与报告总笔数", "reported": recognized["trade_count"],
                           "calculated": actual, "matches": actual == recognized["trade_count"]})
        if section["columns"][0] == "区间":
            checks.append({"label": "金额区间笔数与报告总笔数", "reported": recognized["trade_count"],
                           "calculated": total, "matches": total == recognized["trade_count"]})
        if section["columns"][0] == "项目" and "成本" in section["title"]:
            cost = total
    limitations[:0] = [
        "这是汇总分析报告，导入值来自报告单元格，尚未用逐笔成交记录独立验证。公式未执行，表内合计和占比由已读取的数值重新计算。",
        "报告中的历史交易偏好只补充行为画像；风险承受能力仍以已确认的风险问卷为准。",
        "报告持仓时间中位数与逐笔先进先出平均持有时长口径不同，未相互替代；缺少明细，未生成盈亏、年化收益或最大回撤。",
        "成交额排名、单笔金额及题材占比不代表当前持仓权重或实际风险上限；题材分类和叙述性结论保留为原报告表述。",
    ]
    for check in checks:
        if not check["matches"]:
            limitations.append(f"数据不一致：{check['label']}，表内合计 {check['calculated']:g}，报告值 {check['reported']:g}。")
    return {"schema_version": "stock-report-v1", "input_kind": "analysis_report", "confirmed": False,
            "source": {"file_name": Path(file_name).name, "sha256": hashlib.sha256(raw).hexdigest(),
                       "sheet_names": names}, "window_start": start, "window_end": end,
            "trade_count": int(recognized["trade_count"]), "security_count": int(recognized["security_count"]),
            "active_days": recognized.get("active_days"), "reported_median_holding_days": recognized.get("median_holding_days"),
            "average_matched_holding_days": None, "matched_sell_gross_pnl": None,
            "total_cost": cost, "report_metrics": metrics, "report_sections": sections,
            "monthly": monthly, "behavioral_notes": notes, "limitations": limitations,
            "source_notes": source_notes, "reconciliation": checks}
