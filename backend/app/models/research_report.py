"""Structured, evidence-linked research output shared by API, history and export."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ResearchEvidence(BaseModel):
    observation: str
    interpretation: str
    stance: Literal["favorable", "caution", "neutral"] = "neutral"
    fact_ids: list[str] = Field(default_factory=list)
    period: str | None = None
    source: str
    source_url: str | None = None


class ResearchSection(BaseModel):
    entity: str
    title: str
    evidence: list[ResearchEvidence] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    change_conditions: list[str] = Field(default_factory=list)


class ResearchFinding(BaseModel):
    """从分维度证据中提炼的一条关键信息，保留可回查的 fact_id。"""

    title: str
    detail: str
    stance: Literal["favorable", "caution", "neutral"] = "neutral"
    fact_ids: list[str] = Field(default_factory=list)


class ReportCoverage(BaseModel):
    """报告的可核对程度：引用了多少、原始资料多少、覆盖哪些维度与来源。"""

    cited: int = 0
    available: int = 0
    dated: int = 0
    dimensions: list[str] = Field(default_factory=list)
    missing_dimensions: list[str] = Field(default_factory=list)
    sources: dict[str, int] = Field(default_factory=dict)


class ResearchReport(BaseModel):
    version: int = 2
    title: str | None = None
    generated_at: datetime | None = None
    targets: list[str] = Field(default_factory=list)
    status: Literal["limited", "reviewed", "needs_review"] = "limited"
    summary: str
    method: str = "依据已引用数据整理支持因素与不利因素，不代表额外执行了多轮模型辩论。"
    conclusion: str | None = None
    key_findings: list[ResearchFinding] = Field(default_factory=list)
    coverage: ReportCoverage = Field(default_factory=ReportCoverage)
    sections: list[ResearchSection] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
