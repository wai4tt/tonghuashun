"""自选/对比的确定性投资价值比较结果。

得分是同一组标的之间的相对档位，外加可解释的绝对区间分，不是预期收益、
不是上涨概率，也不构成买入建议。
"""
from typing import Literal

from pydantic import BaseModel, Field


class DecisionDimension(BaseModel):
    key: str
    label: str
    weight: float = Field(gt=0, le=1)
    unit: str | None = None
    # 本次实际参与比较的字段；同一维度内所有标的必须一致。
    field: str | None = None
    field_label: str | None = None
    direction: Literal["higher_better", "lower_better"] = "higher_better"
    note: str | None = None


class DecisionCandidate(BaseModel):
    target: str
    name: str | None = None
    code: str | None = None
    rank: int = Field(ge=1)
    score: float = Field(ge=0, le=100)
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    coverage: float = Field(default=0.0, ge=0, le=1)


class ComparisonDecision(BaseModel):
    status: Literal["ok", "insufficient"] = "insufficient"
    method: str = "同一组标的的相对档位分（50%）与绝对区间分（50%）加权，指标权重固定。"
    weights: dict[str, float] = Field(default_factory=dict)
    dimensions: list[DecisionDimension] = Field(default_factory=list)
    candidates: list[DecisionCandidate] = Field(default_factory=list)
    preferred: str | None = None
    preferred_name: str | None = None
    conclusion: str = ""
    caveats: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
