"""Bounded account-scoped preparation of fully reviewed stock reports."""
from __future__ import annotations
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from time import monotonic
from typing import Literal, Any
from pydantic import BaseModel, Field, field_validator
from backend.app.fact_taxonomy import fact_is_current
from backend.app.models import AdvicePackage, Intent
from backend.app.services.history import used_fact_ids_of

TASKS = {"comprehensive": "经营情况、估值及主要风险", "valuation": "经营质量与估值匹配情况", "risk": "经营、财务与估值的主要风险"}

class PreparedResearchRequest(BaseModel):
    target: str = Field(min_length=1, max_length=60)
    task: Literal["comprehensive", "valuation", "risk"] = "comprehensive"
    profile_version: int = Field(ge=1)
    conversation_id: str | None = Field(default=None, max_length=128)
    retry: bool = False
    portfolio: list[dict[str, Any]] = Field(default_factory=list, max_length=100)

    @field_validator("target")
    @classmethod
    def clean_target(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("标的不能为空")
        return value

def task_query(target, task):
    return f"个股研究：请深入研究{target}股票的{TASKS[task]}，不提供买卖指令或收益保证。"

def board_signature(board):
    # Fetch timestamps and random fact ids may change without source data changing.
    # Keep all meaningful source values, identity, dates, quality and units.
    fields = ("entity", "entity_code", "field", "value", "unit", "source_id", "source_url", "quality", "period", "observation_date", "source_field", "produced_by")
    rows = [json.dumps({k: fact.model_dump(mode="json").get(k) for k in fields},
            ensure_ascii=False, sort_keys=True, default=str)
            for section in board.sections if section.status == "ok" for fact in section.facts]
    return hashlib.sha256(json.dumps(sorted(rows), ensure_ascii=False).encode()).hexdigest()

def report_is_ready(advice, now=None):
    now = now or datetime.now(timezone.utc)
    if (advice.intent is not Intent.SECURITY_RESEARCH or advice.response_scope != "full" or advice.research_mode != "deep"
            or advice.compliance.status.value != "PASS" or advice.cross_validation.status.value != "PASS"
            or advice.omitted_features or not advice.agent_results or not advice.evidence
            or not advice.task_plan.nodes or advice.task_plan.clarification_question
            or not {"market", "industry", "security"} <= {a.agent_id for a in advice.agent_results}
            or advice.data_acquisition.missing_fields_by_agent
            or any(n.status.value != "completed" for n in advice.task_plan.nodes)
            or any((a.details or {}).get("engine") != "third_party_llm" for a in advice.agent_results)
            or not any(c.get("purpose") == "OutputReview" and c.get("status") == "completed" for c in advice.model_calls)):
        return False
    used = used_fact_ids_of(advice.model_dump(mode="json"))
    facts = {f.fact_id: f for f in advice.facts}
    return bool(used) and all(fid in facts and fact_is_current(facts[fid], now) for fid in used)

@dataclass
class Entry:
    owner: str
    session_id: str
    request: object
    started: float
    source_signature: str | None = None
    task: asyncio.Task | None = None
    advice: AdvicePackage | None = None
    finished: float | None = None
    generated_at: str | None = None
    error: bool = False
    error_type: str | None = None

class PreparedResearchCache:
    def __init__(self, max_entries=16, max_pending=4, ttl_seconds=120):
        self.max_entries, self.max_pending, self.ttl_seconds = max_entries, max_pending, ttl_seconds
        self.entries = OrderedDict()

    @staticmethod
    def key(owner, profile, target, task, runtime, portfolio=None):
        value = [str(owner), profile.model_dump(mode="json"), target, task, runtime, portfolio or []]
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()

    def state(self, entry):
        if entry.task is not None and not entry.task.done():
            return "preparing"
        if entry.error or entry.advice is None:
            return "failed"
        return "ready" if (entry.advice.profile_version == entry.request.profile.version and report_is_ready(entry.advice)) else "needs_review"

    def peek(self, key):
        entry = self.entries.get(key)
        if entry is None:
            return None
        # An incomplete/failed run remains visible until an explicit retry;
        # polling must never trigger endless paid model retries.
        if entry.finished is not None and self.state(entry) == "ready" and monotonic()-entry.finished >= self.ttl_seconds:
            self.entries.pop(key, None)
            return None
        self.entries.move_to_end(key)
        return entry

    def prepare(self, key, owner, session_id, request, loader, *, retry=False, source_signature=None):
        entry = self.peek(key)
        if entry is not None and not (retry and self.state(entry) in {"failed", "needs_review"} and monotonic()-entry.started >= 10):
            return entry
        active = [e for e in self.entries.values() if e.task is not None and not e.task.done()]
        if len(active) >= self.max_pending or any(e.owner == str(owner) for e in active):
            return None
        if entry is not None:
            self.entries.pop(key, None)
        while len(self.entries) >= self.max_entries:
            victim = next((k for k,e in self.entries.items() if e.task is None or e.task.done()), None)
            if victim is None:
                return None
            self.entries.pop(victim)
        entry = Entry(str(owner), str(session_id), request.model_copy(deep=True), monotonic(), source_signature=source_signature)
        self.entries[key] = entry
        async def run():
            try:
                entry.advice = await loader()
                entry.generated_at = datetime.now(timezone.utc).isoformat()
            except asyncio.CancelledError:
                entry.error = True
                entry.error_type = "CancelledError"
                raise
            except Exception as exc:
                entry.error = True
                entry.error_type = type(exc).__name__
            finally:
                entry.finished = monotonic()
        entry.task = asyncio.create_task(run())
        return entry

    def cancel_session(self, session_id):
        for entry in list(self.entries.values()):
            if entry.session_id == str(session_id) and entry.task is not None and not entry.task.done():
                entry.task.get_loop().call_soon_threadsafe(entry.task.cancel)

    async def aclose(self):
        tasks = [e.task for e in self.entries.values() if e.task is not None and not e.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.entries.clear()
