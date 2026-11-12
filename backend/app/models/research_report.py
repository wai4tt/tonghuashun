"""Structured, evidence-linked research output shared by API, history and export."""
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


class ResearchReport(BaseModel):
    version: int = 1
    status: Literal["limited", "reviewed", "needs_review"] = "limited"
    summary: str
    method: str = "依据已引用数据整理支持因素与不利因素，不代表额外执行了多轮模型辩论。"
    sections: list[ResearchSection] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
