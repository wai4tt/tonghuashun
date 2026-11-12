"""Three-second response contract; an incomplete response is never full advice."""
import asyncio
import json
from time import perf_counter

import httpx
import pytest

from backend.app import main
from backend.app.models import AdvicePackage, ComplianceResult, ComplianceStatus, Intent, TaskPlan
from backend.app.services.model_response_cache import ModelResponseCache
from frontend.api_client import AnalysisProgress, request_analysis
from tests.test_semantic import request


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_legacy_fast_consultation_ends_before_three_seconds_with_explicit_incomplete_notice(monkeypatch, stream):
    cancelled = asyncio.Event()
    async def slow(req):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()
    monkeypatch.setattr(main.coordinator, "understand_request", slow)
    # Exercise the actual configured default, including admission and cleanup.
    assert 0 < main.fast_response_timeout_seconds <= 2.6
    payload = request().model_dump(mode="json")
    payload["research_mode"] = "fast"
    path = "/api/v1/portfolio/analyze" + ("/stream" if stream else "")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        start = perf_counter()
        response = await client.post(path, json=payload)
        assert perf_counter() - start < 3
    if stream:
        events = [json.loads(line) for line in response.text.splitlines()]
        assert not any(e["type"] == "result" for e in events)
        last = events[-1]
        assert last["type"] == "error" and last["status_code"] == 504
        detail = last["message"]
    else:
        assert response.status_code == 504 and "advice" not in response.json()
        detail = response.json()["detail"]
    assert "完整分析或审核尚未完成" in detail and "深度思考" in detail
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_deep_research_has_no_global_deadline(monkeypatch):
    monkeypatch.setattr(main, "fast_response_timeout_seconds", .01)
    monkeypatch.setattr(main, "analysis_timeout_seconds", .01)
    async def complete(req, user):
        await asyncio.sleep(.04)
        return AdvicePackage(trace_id="deep-test", intent=Intent.UNKNOWN, conclusion="需复核", confidence=0,
            compliance=ComplianceResult(status=ComplianceStatus.REVIEW),
            task_plan=TaskPlan(trace_id="deep-test", intent=Intent.UNKNOWN))
    monkeypatch.setattr(main, "_analyze_portfolio", complete)
    result = await main.analyze_portfolio(request().model_copy(update={"research_mode": "deep"}), None)
    assert result.trace_id == "deep-test"


def test_deep_stream_has_no_read_timeout():
    observed = []
    def handler(request):
        observed.append(request.extensions["timeout"])
        return httpx.Response(200, text='{"type":"result","advice":{}}\n')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = request_analysis(client, "https://test", "POST", "/portfolio/analyze",
                                  {"research_mode": "deep"}, None, AnalysisProgress())
    assert result.data == {}
    assert observed[0]["read"] is None and observed[0]["connect"] == 10


@pytest.mark.asyncio
async def test_deep_stream_sends_heartbeats_while_waiting(monkeypatch):
    monkeypatch.setattr(main, "DEEP_STREAM_HEARTBEAT_SECONDS", .01)
    async def complete(req, user):
        await asyncio.sleep(.04)
        return AdvicePackage(trace_id="deep-heartbeat", intent=Intent.UNKNOWN, conclusion="需复核", confidence=0,
            compliance=ComplianceResult(status=ComplianceStatus.REVIEW),
            task_plan=TaskPlan(trace_id="deep-heartbeat", intent=Intent.UNKNOWN))
    monkeypatch.setattr(main, "analyze_portfolio", complete)
    response = await main.stream_portfolio_analysis(request().model_copy(update={"research_mode": "deep"}), None)
    events = [json.loads(chunk) async for chunk in response.body_iterator]
    assert any(event["type"] == "heartbeat" for event in events)
    assert events[-1]["type"] == "result"


@pytest.mark.parametrize("mode", ["fast", "lite"])
def test_fast_overload_response_never_sleeps_or_retries(monkeypatch, mode):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(429, headers={"Retry-After": "2"}, json={"detail": "繁忙"})
    monkeypatch.setattr("frontend.api_client.sleep", lambda _: pytest.fail("3-second mode cannot back off and replay"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = request_analysis(client, "https://test", "POST", "/portfolio/analyze",
                                  {"research_mode": mode}, None, AnalysisProgress())
    assert len(calls) == 1 and result.status == 429


@pytest.mark.asyncio
async def test_bounded_model_cache_cancels_only_after_last_waiter_leaves():
    cache = ModelResponseCache()
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def load():
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
    first = asyncio.create_task(cache.get("same", load, cancel_if_unused=True))
    await started.wait()
    second = asyncio.create_task(cache.get("same", load, cancel_if_unused=True))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not cancelled.is_set()
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    await asyncio.wait_for(cancelled.wait(), .2)
    assert not cache.pending and not cache.waiters
    await cache.aclose()
