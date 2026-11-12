"""Latency contracts: provider parameters, exact-input reuse, final review and progress."""
import json

import httpx
import pytest

from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM
from backend.app.services.model_telemetry import analysis_telemetry
from frontend.api_client import AnalysisProgress, request_analysis


@pytest.mark.asyncio
async def test_recovery_reuses_unchanged_models_and_reviews_only_final_evidence():
    from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
    from backend.app.agents.llm_agents import make_investment_agents
    from backend.app.models import Intent
    from backend.app.semantic import RequestUnderstanding, SemanticService
    from tests.test_agent_data_recovery import complete_request
    requests = []
    def handler(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        purpose = (payload.get("required_output") or {}).get("agent_id") or payload["required_schema"]["title"]
        requests.append((purpose, payload))
        if purpose == "OutputReview":
            answer = {"confidence": .95, "risk_rules": [], "conflicting_agents": [], "reason": "通过"}
        else:
            baseline = payload["rule_baseline"]
            business = any(f["fact_id"] == "new-business" for f in payload["authorized_facts"])
            answer = {"opinion": baseline["opinion"], "score": baseline["score"], "confidence": .95,
                "facts_used": baseline["facts_used"], "details": baseline.get("details", {}),
                "data_requirements": ["company_operations"] if purpose == "security" and not business else []}
            if purpose == "security" and business:
                answer["facts_used"] = [*answer["facts_used"], "new-business"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})
    llm = OpenAICompatibleLLM(LLMConfig("https://model.test", "fake", "test"),
                              transport=httpx.MockTransport(handler))
    req = complete_request()
    stages, verified_counts = [], []
    async def verify(results, facts):
        verified_counts.append(len(facts))
        return await verify_facts(results, facts)
    async def recover(provisional):
        assert provisional.compliance.status == "REVIEW"
        assert provisional.agent_results[-1].data_requirements == ["company_operations"]
        new = req.facts[-1].model_copy(update={"fact_id": "new-business", "field": "main_business", "value": "制造业务"})
        return req.model_copy(update={"facts": [*req.facts, new]})
    agents, _ = make_investment_agents(llm)
    coordinator = CoordinatorAgent(agents, verify, basic_compliance_check, semantic=SemanticService(llm))
    collector = {"calls": [], "mode": "deep"}
    token = analysis_telemetry.set(collector)
    try:
        output = await coordinator.run(req, understanding=RequestUnderstanding(intent=Intent.SECURITY_RESEARCH,
            confidence=.95, risk_rules=[], reason="研究"), recovery_hook=recover, progress_sink=stages.append)
        assert len(verified_counts) == 2 and verified_counts[1] == verified_counts[0] + 1
        assert [purpose for purpose, _ in requests].count("market") == 1
        assert [purpose for purpose, _ in requests].count("industry") == 1
        assert [purpose for purpose, _ in requests].count("security") == 2
        reviews = [payload for purpose, payload in requests if purpose == "OutputReview"]
        assert len(reviews) == 1
        assert "new-business" in {f["fact_id"] for f in reviews[0]["authorized_facts"]}
        assert "new-business" in output.evidence and output.compliance.status == "PASS"
        assert stages.count("风险检查") == 1
    finally:
        analysis_telemetry.reset(token)
        await llm.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("model,mode,expected", [
    ("qwen3.5-plus", "auto", False),
    ("qwen3.5-plus-2026-02-15", "disabled", False),
    ("qwen3.5-flash", "enabled", True),
    ("qwen3.5-plus", "omit", None),
    ("other-model", "auto", None),
])
async def test_qwen_uses_its_own_thinking_protocol(model, mode, expected):
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
    llm = OpenAICompatibleLLM(LLMConfig("https://model.test", "fake", model,
        thinking_mode=mode), transport=httpx.MockTransport(handler))
    try:
        await llm.complete_json(system="s", payload={})
        assert sent[0].get("enable_thinking") is expected
        assert "thinking" not in sent[0]
    finally:
        await llm.aclose()


@pytest.mark.asyncio
async def test_request_cache_reuses_only_exact_inputs_and_isolates_requests():
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"values":[1]}'}}]})
    llm = OpenAICompatibleLLM(LLMConfig("https://model.test", "fake", "test"),
                              transport=httpx.MockTransport(handler))
    collector = {"calls": [], "mode": "fast"}
    token = analysis_telemetry.set(collector)
    try:
        payload = {"authorized_facts": [{"fact_id": "same", "value": 1}], "profile": {"risk_level": "R3"}}
        result = await llm.complete_json(system="s", payload=payload)
        result["values"].clear()
        assert (await llm.complete_json(system="s", payload=payload))["values"] == [1]
        assert len(sent) == 1
        assert collector["calls"][-1]["estimated_cost_usd"] == 0
        assert collector["calls"][-1]["request_cache_hit"]
        # Same fact ID with a revised value cannot reuse an earlier judgment.
        await llm.complete_json(system="s", payload={**payload, "authorized_facts": [{"fact_id": "same", "value": 2}]})
        await llm.complete_json(system="s", payload={**payload, "profile": {"risk_level": "R1"}})
        collector["mode"] = "deep"
        await llm.complete_json(system="s", payload=payload)
        assert len(sent) == 4
        other = analysis_telemetry.set({"calls": [], "mode": "fast"})
        try:
            await llm.complete_json(system="s", payload=payload)
        finally:
            analysis_telemetry.reset(other)
        assert len(sent) == 5
    finally:
        analysis_telemetry.reset(token)
        await llm.aclose()


@pytest.mark.asyncio
async def test_disabling_cache_also_disables_request_local_reuse():
    sent = []
    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
    llm = OpenAICompatibleLLM(LLMConfig("https://model.test", "fake", "test", cache_ttl_seconds=0),
                              transport=httpx.MockTransport(handler))
    token = analysis_telemetry.set({"calls": [], "mode": "fast"})
    try:
        for _ in range(2):
            await llm.complete_json(system="s", payload={})
        assert len(sent) == 2
    finally:
        analysis_telemetry.reset(token)
        await llm.aclose()


def test_invalid_or_unauthorized_model_results_are_not_cacheable():
    payload = {"required_output": {"agent_id": "market"}, "authorized_facts": [{"fact_id": "allowed"}]}
    valid = {"opinion": "观察", "facts_used": ["allowed"], "data_requirements": []}
    assert OpenAICompatibleLLM._cacheable(valid, payload)
    assert not OpenAICompatibleLLM._cacheable({**valid, "facts_used": ["other"]}, payload)
    assert not OpenAICompatibleLLM._cacheable({**valid, "data_requirements": ["company_operations"]}, payload)
    assert not OpenAICompatibleLLM._cacheable({**valid, "confidence": "invalid"}, payload)


@pytest.mark.parametrize("ending", ["result", "error", "disconnect", "invalid"])
def test_background_stream_reports_stages_and_never_replays_a_request(ending):
    calls = []
    def handler(request):
        calls.append(request)
        events = [{"type": "progress", "stage": "事实核验"}]
        if ending == "result":
            events.append({"type": "result", "advice": {"compliance": {"status": "REVIEW"}}})
        elif ending == "error":
            events.append({"type": "error", "message": "风险测评已过期"})
        content = "\n".join(json.dumps(e, ensure_ascii=False) for e in events)
        if ending == "invalid":
            content += "\n{broken"
        return httpx.Response(200, text=content)
    progress = AnalysisProgress()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = request_analysis(client, "https://backend.test", "POST", "/portfolio/analyze", {}, "fake", progress)
    assert len(calls) == 1
    assert calls[0].url.path == "/portfolio/analyze/stream"
    assert calls[0].headers["Authorization"] == "Bearer fake"
    assert progress.snapshot()[0] == "事实核验"
    if ending == "result":
        assert result.data["compliance"]["status"] == "REVIEW"
    else:
        assert result.data is None and result.detail


@pytest.mark.asyncio
async def test_fast_model_preserves_protocol_and_isolates_cached_answers_and_prices():
    sent = []
    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 10}})
    llm = OpenAICompatibleLLM(LLMConfig("https://model.test", "fake", "deepseek-v4-pro",
        fast_model="qwen3.5-flash", input_price_per_million=1, output_price_per_million=2),
        transport=httpx.MockTransport(handler))
    collector = {"calls": [], "mode": "fast", "cache_scope": "account-a"}
    token = analysis_telemetry.set(collector)
    try:
        await llm.complete_json(system="s", payload={})
        assert sent[0]["model"] == "qwen3.5-flash"
        assert sent[0]["enable_thinking"] is False and "thinking" not in sent[0]
        assert collector["calls"][-1]["model"] == "qwen3.5-flash"
        # The primary model's price must not be attributed to another model.
        assert collector["calls"][-1]["estimated_cost_usd"] is None
        collector["mode"] = "deep"
        await llm.complete_json(system="s", payload={})
        assert sent[1]["model"] == "deepseek-v4-pro"
        assert sent[1]["thinking"] == {"type": "disabled"}
        assert collector["calls"][-1]["estimated_cost_usd"] == .00012
        collector["mode"] = "fast"
        await llm.complete_json(system="s", payload={})
        assert len(sent) == 2 and collector["calls"][-1]["cache_hit"]
    finally:
        analysis_telemetry.reset(token)
        await llm.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [False, True])
async def test_stream_delivers_completion_without_polling_delay(monkeypatch, error):
    import asyncio
    from fastapi import HTTPException
    from backend.app import main
    from scripts.verify_consultation_latency import synthetic_request
    from backend.app.models import AdvicePackage, ComplianceResult, ComplianceStatus, Intent, TaskPlan

    async def analyze(request, user):
        await asyncio.sleep(.02)
        if error:
            raise HTTPException(429, detail="繁忙", headers={"Retry-After": "2"})
        return AdvicePackage(trace_id="stream-test", intent=Intent.UNKNOWN,
            conclusion="需要补充信息", confidence=0, compliance=ComplianceResult(status=ComplianceStatus.REVIEW),
            task_plan=TaskPlan(trace_id="stream-test", intent=Intent.UNKNOWN))
    monkeypatch.setattr(main, "analyze_portfolio", analyze)
    response = await main.stream_portfolio_analysis(synthetic_request(), None)
    # The previous 250ms polling loop cannot satisfy this completion boundary.
    events = await asyncio.wait_for(_collect_events(response.body_iterator), .15)
    assert len(events) == 1
    assert events[0]["type"] == ("error" if error else "result")
    if error:
        assert events[0]["status_code"] == 429 and events[0]["retry_after"] == "2"


async def _collect_events(iterator):
    return [json.loads(event) async for event in iterator]


@pytest.mark.asyncio
async def test_stream_disconnect_cancels_analysis_and_pending_progress_read(monkeypatch):
    import asyncio
    from backend.app import main
    from scripts.verify_consultation_latency import synthetic_request
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def analyze(request, user):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
    monkeypatch.setattr(main, "analyze_portfolio", analyze)
    response = await main.stream_portfolio_analysis(synthetic_request(), None)
    reader = asyncio.create_task(anext(response.body_iterator))
    await asyncio.wait_for(entered.wait(), .5)
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader
    assert cancelled.is_set()


def test_latency_target_never_passes_for_early_exit_or_failed_review():
    from scripts.verify_consultation_latency import complete_sample, summarize
    assert not complete_sample({"intent": "education", "compliance": {"status": "PASS"}})
    assert not summarize([{"seconds": .1, "complete": False}], 3)["sla_met"]
    assert not summarize([{"seconds": 3.01, "complete": True}], 3)["sla_met"]
    assert summarize([{"seconds": 2.9, "complete": True}], 3)["sla_met"]
