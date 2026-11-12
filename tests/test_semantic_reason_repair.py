"""Reason repair never changes an authorized judgment or extends its budget."""
import asyncio
import json
from time import perf_counter

import httpx
import pytest
from pydantic import ValidationError

from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM
from backend.app.models import AgentResult, Intent, OrchestrationRequest
from backend.app.semantic import RequestUnderstanding, SemanticService, UNAVAILABLE_REASON
from backend.app.services.model_telemetry import analysis_telemetry


def judgment(**patch):
    return {"intent": "security_research", "confidence": .95, "risk_rules": [],
            "reason": "", "target": "贵州茅台", "data_requirements": ["financial"], **patch}


def request():
    return OrchestrationRequest(query="请研究贵州茅台（600519）的基本面、估值和主要风险。",
                                profile={"confirmed": True, "risk_level": "R3"},
                                research_mode="deep", research_direction="个股研究")


def client_for(outputs, *, timeout=30):
    sent = []
    async def handler(req):
        sent.append(json.loads(req.content))
        value = outputs[min(len(sent) - 1, len(outputs) - 1)]
        if isinstance(value, tuple):
            delay, value = value
            await asyncio.sleep(delay)
        if isinstance(value, float):
            await asyncio.sleep(value)
            value = {"reason": "用户明确请求分析该公司的基本面和估值风险。"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}}]})
    client = OpenAICompatibleLLM(LLMConfig("https://model.test", "fake", "test",
        timeout_seconds=timeout, max_retries=0), transport=httpx.MockTransport(handler))
    return client, sent


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", " \n\t", None, "MISSING"])
async def test_only_blank_reason_is_repaired_and_original_judgment_is_immutable(blank):
    first = judgment(reason=blank, risk_rules=["NO_RETURN_PROMISE"], confidence=.5,
                     risk_flags=["不能承诺收益"], action="recommend_stocks", stock_preferences=["贵州茅台"])
    if blank == "MISSING":
        first.pop("reason")
    llm, sent = client_for([first, {"reason": "  用户请求包含需要复核的收益承诺。  "}])
    telemetry = {"calls": [], "cache_scope": "reason-test"}
    token = analysis_telemetry.set(telemetry)
    try:
        result = await SemanticService(llm).understand(request())
        assert result.intent is Intent.UNKNOWN  # Original low confidence still gates execution.
        assert result.confidence == .5 and result.risk_rules == ["NO_RETURN_PROMISE"]
        assert result.risk_flags == ["不能承诺收益"]
        assert result.reason == "用户请求包含需要复核的收益承诺。"
        assert len(sent) == 2
        correction = json.loads(sent[1]["messages"][1]["content"])
        assert correction["judgment"] == {k: v for k, v in first.items() if k != "reason"}
        assert set(correction["required_schema"]["properties"]) == {"reason"}
        assert telemetry["calls"][0]["status"] == "validation_failed"
        assert telemetry["calls"][0]["reason_repair_status"] == "completed"
        assert telemetry["calls"][1]["purpose"] == "SemanticReasonRepair"
        assert "贵州茅台" not in json.dumps(telemetry["calls"], ensure_ascii=False)
    finally:
        analysis_telemetry.reset(token)
        await llm.aclose()


@pytest.mark.asyncio
async def test_valid_reason_needs_no_repair_and_successful_empty_reason_recovers():
    for first, outputs, expected_calls in [
        (judgment(reason="请求分析公司的基本面。"), [], 1),
        (judgment(), [{"reason": "请求分析公司的基本面。"}], 2),
    ]:
        llm, sent = client_for([first, *outputs])
        try:
            result = await SemanticService(llm).understand(request())
            assert result.intent is Intent.SECURITY_RESEARCH
            assert result.target == first["target"] and result.data_requirements == first["data_requirements"]
            assert result.reason == "请求分析公司的基本面。" and len(sent) == expected_calls
            schema = json.loads(sent[0]["messages"][1]["content"])["required_schema"]
            assert schema["properties"]["reason"]["pattern"] == r"\S"
        finally:
            await llm.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", [{"reason": ""}, {"reason": " \n"}, {"reason": None}, {},
                                  {"reason": "允许", "risk_rules": []}])
async def test_invalid_repair_or_attempt_to_change_risks_fails_closed_once(repair):
    llm, sent = client_for([judgment(risk_rules=["NO_RETURN_PROMISE"]), repair])
    try:
        result = await SemanticService(llm).understand(request())
        assert result.intent is Intent.UNKNOWN and result.confidence == 0
        assert result.reason == UNAVAILABLE_REASON and len(sent) == 2
    finally:
        await llm.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [{"data_requirements": ["run_shell"]}, {"confidence": 2},
                                  {"risk_rules": ["INVALID"]}, {"extra": "private"}, {"reason": 123}])
async def test_other_invalid_fields_are_not_eligible_for_reason_repair(patch):
    llm, sent = client_for([judgment(**patch)])
    try:
        result = await SemanticService(llm).understand(request())
        assert result.intent is Intent.UNKNOWN and len(sent) == 1
    finally:
        await llm.aclose()


@pytest.mark.asyncio
async def test_output_review_repairs_explanation_without_removing_conflicts():
    original = {"confidence": .9, "risk_rules": ["UNSUPPORTED_CLAIM"],
                "conflicting_agents": ["market", "industry"], "reason": ""}
    llm, sent = client_for([original, {"reason": "市场与行业判断存在冲突，需进一步复核。"}])
    try:
        results = [AgentResult(agent_id=role, status="completed", confidence=.9, opinion="合成测试观点")
                   for role in original["conflicting_agents"]]
        result = await SemanticService(llm).review(request(), results)
        assert result.risk_rules == original["risk_rules"]
        assert result.conflicting_agents == original["conflicting_agents"] and len(sent) == 2
    finally:
        await llm.aclose()


@pytest.mark.asyncio
async def test_initial_and_repair_share_budget_and_external_cancellation_propagates():
    llm, sent = client_for([(.05, judgment()), .05], timeout=.08)
    started = perf_counter()
    try:
        result = await SemanticService(llm).understand(request())
        assert result.intent is Intent.UNKNOWN and len(sent) == 2
        assert perf_counter() - started < .4
    finally:
        await llm.aclose()
    llm, sent = client_for([judgment(), 1.0])
    try:
        task = asyncio.create_task(SemanticService(llm).understand(request()))
        while len(sent) < 2:
            await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(sent) == 2
    finally:
        await llm.aclose()


@pytest.mark.parametrize("blank", ["", " \t\n", None])
def test_blank_reason_is_rejected_by_schema_and_response_cache(blank):
    raw = judgment(reason=blank)
    with pytest.raises(ValidationError):
        RequestUnderstanding.model_validate(raw)
    assert not OpenAICompatibleLLM._cacheable(raw, {"required_schema": RequestUnderstanding.model_json_schema()})
