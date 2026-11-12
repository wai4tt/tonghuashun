"""Runtime boundaries: lightweight imports, routing views and responsive session cleanup."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import ssl
import subprocess
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from backend.app import http_clients, main
from backend.app.model_payload import profile_for_model, profile_for_routing
from backend.app.semantic import SemanticService
from tests.test_model_payload import profile


def test_frontend_import_does_not_load_backend_research_or_trade_processing():
    code = ("import frontend.streamlit_app,sys,json; print(json.dumps([name for name in "
        "('backend.app.models','backend.app.services.trade_history','backend.app.services.research',"
        "'pandas','altair') if name in sys.modules]))")
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout.splitlines()[-1]) == []


def test_routing_profile_keeps_risk_signals_without_modifying_complete_profile():
    original = profile()
    before = original.model_dump(mode="json")
    routed = profile_for_routing(original)
    assert routed["confirmed"] is False
    assert routed["risk_level"] == original.risk_level
    assert routed["constraints"] == ["不得使用杠杆"]
    assert routed["financial_warnings"] == original.financial_warnings
    assert not {"questionnaire_details", "holding_history", "trading_analysis", "user_id"} & routed.keys()
    full = profile_for_model(original)
    assert full["questionnaire_details"] == before["questionnaire_details"]
    assert full["trading_analysis"] == before["trading_analysis"]
    assert original.model_dump(mode="json") == before
    assert len(json.dumps(routed)) < len(json.dumps(full)) / 2


@pytest.mark.asyncio
async def test_final_review_still_receives_full_questionnaire_and_financial_constraints():
    from backend.app.models import OrchestrationRequest
    captured = []
    class Client:
        async def complete_json(self, *, system, payload):
            captured.append(payload)
            return {"confidence": .95, "risk_rules": [], "conflicting_agents": [], "reason": "复核"}
    original = profile()
    await SemanticService(Client()).review(OrchestrationRequest(query="研究", profile=original), [])
    assert captured[0]["profile"]["questionnaire_details"] == original.questionnaire_details
    assert captured[0]["profile"]["constraints"] == ["不得使用杠杆"]
    assert captured[0]["profile"]["trading_analysis"] == original.trading_analysis


def test_tls_reuse_retains_validation_and_reloads_rotated_trust_store(monkeypatch, tmp_path):
    import certifi
    bundle = tmp_path / "ca.pem"
    bundle.write_bytes(Path(certifi.where()).read_bytes())
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    context = http_clients.verified_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert http_clients.verified_ssl_context() is context
    # Adding whitespace changes the bundle version without changing the trust roots.
    bundle.write_bytes(bundle.read_bytes() + b"\n")
    rotated = http_clients.verified_ssl_context()
    assert rotated is not context
    assert rotated.verify_mode == ssl.CERT_REQUIRED and rotated.check_hostname
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    with pytest.raises(OSError):
        http_clients.verified_ssl_context()


@pytest.mark.asyncio
async def test_slow_session_cleanup_does_not_block_other_async_requests(monkeypatch):
    entered, release = Event(), Event()
    def reap():
        entered.set()
        assert release.wait(4)
    monkeypatch.setattr(main, "session_thread_pool", SimpleNamespace(idle_timeout_seconds=2, reap_expired=reap))
    task = asyncio.create_task(main.reap_idle_sessions())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        # This event-loop operation must finish while the database cleanup is blocked.
        await asyncio.wait_for(asyncio.sleep(.001), timeout=.1)
        assert not release.is_set()
    finally:
        release.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_login_reads_start_even_when_research_workers_are_busy(monkeypatch):
    import streamlit as st
    from frontend import account_prefetch, research_board
    from frontend.api_client import ApiResult
    from tests.test_startup_performance import State
    state = State()
    monkeypatch.setattr(st, "session_state", state)
    entered, release = Event(), Event()
    def busy():
        entered.set()
        assert release.wait(3)
    with ThreadPoolExecutor(max_workers=1) as board_pool, ThreadPoolExecutor(max_workers=3) as account_pool:
        blocked = board_pool.submit(busy)
        assert entered.wait(1)
        monkeypatch.setattr(research_board, "board_prefetch_executor", lambda: board_pool)
        monkeypatch.setattr(account_prefetch, "account_read_executor", lambda: account_pool)
        seen = []
        def read(client, base, method, path, payload, token):
            seen.append((path, token))
            return ApiResult(data=[], status=200)
        monkeypatch.setattr(account_prefetch, "request_json", read)
        try:
            account_prefetch.start_account_prefetch("https://test", "captured-token", object())
            for future in state.account_prefetch.values():
                future.result(timeout=1)
            assert not blocked.done()
            assert len(seen) == 3 and {token for _, token in seen} == {"captured-token"}
        finally:
            release.set()
