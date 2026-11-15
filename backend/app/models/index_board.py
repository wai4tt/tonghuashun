"""指数看板：主要指数的报价、五日均线、三日升幅与中和判断。"""
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .trend import TrendAnalysis


class IndexQuoteItem(BaseModel):
    name: str
    code: str
    status: Literal["ok", "empty", "unavailable"] = "empty"
    latest_close: float | None = None
    change_pct: float | None = None
    turnover_value: float | None = None
    trade_date: date | None = None
    ma5: float | None = None
    three_day_pct: float | None = None
    three_day_height: float | None = None
    verdict: Literal["上行", "偏强震荡", "震荡", "偏弱震荡", "下行", "数据不足"] = "数据不足"
    score: float | None = None
    confidence: float = 0.0
    analysis: TrendAnalysis | None = None
    points: list[float] = Field(default_factory=list)
    dates: list[date] = Field(default_factory=list)
    message: str | None = None


class IndexBoardResponse(BaseModel):
    fetched_at: datetime
    status: Literal["ok", "partial", "empty", "unavailable"] = "empty"
    source_name: str = "同花顺问财"
    benchmark: str | None = "沪深300"
    items: list[IndexQuoteItem] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
