"""Prepared full reports: no skipped gates, no account leakage, bounded delivery."""
import asyncio
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from time import perf_counter
import httpx
import pytest
from backend.app import main
from backend.app.models import (AdvicePackage, AgentResult, ComplianceResult, ComplianceStatus,
    CrossValidationResult, FactRecord, Intent, OrchestrationRequest, TaskNode, TaskPlan, TaskStatus, UserProfile)
from backend.app.models.schemas import DataOverviewResponse, DataOverviewSection
from backend.app.services.evidence_coverage import AGENT_FIELDS
from backend.app.services.prepared_research import (PreparedResearchCache, PreparedResearchRequest,
    board_signature, report_is_ready, task_query)


def full_report():
    now = datetime.now(timezone.utc)
    fields = sorted(set().union(*(set(AGENT_FIELDS[a]) for a in ("market", "industry", "security"))))
    facts = [FactRecord(fact_id=f, entity="SYNTHETIC_ONLY", entity_code="600519", field=f, value=55,
        snapshot_time=now, source_id="SYNTHETIC_TEST_ONLY", quality=.95) for f in fields]
    results = [AgentResult(agent_id=a, opinion="合成测试研判", confidence=.9, status=TaskStatus.COMPLETED,
        score=55, facts_used=list(AGENT_FIELDS[a]), details={"engine":"third_party_llm"})
        for a in ("market", "industry", "security")]
    return AdvicePackage(trace_id="synthetic-origin", intent=Intent.SECURITY_RESEARCH, conclusion="明确标记的合成测试报告",
        confidence=.9, profile_version=1, facts=facts, evidence=fields, agent_results=results,
        compliance=ComplianceResult(status=ComplianceStatus.PASS), cross_validation=CrossValidationResult(status="PASS"),
        task_plan=TaskPlan(trace_id="synthetic-origin", intent=Intent.SECURITY_RESEARCH,
            nodes=[TaskNode(task_id=a, agent_id=a, status=TaskStatus.COMPLETED) for a in ("market","industry","security","fact_verifier","compliance")]),
        model_calls=[{"purpose":a,"status":"completed"} for a in ("market","industry","security","OutputReview")],
        timings_ms={"total":60000})


@pytest.fixture
def prepared_app(monkeypatch):
    profile = UserProfile(confirmed=True, version=1, risk_level="R3")
    report = full_report()
    board = DataOverviewResponse(direction="stock",target="600519",status="ok",fetched_at=datetime.now(timezone.utc),
        sections=[DataOverviewSection(key="overview", title="合成测试行情",status="ok",facts=report.facts)])
    user={"id":1001,"_session_id":"test-session"}
    cache = PreparedResearchCache()
    monkeypatch.setattr(main,"prepared_research_cache",cache)
    monkeypatch.setattr(main.database,"get_profile",lambda uid:{"version":profile.version,"payload":profile.model_dump(mode="json")})
    saved=[]
    monkeypatch.setattr(main.database,"save_exchange",lambda *args:saved.append(args))
    class Pool:
        def submit(self,sid,func,*args):
            future=Future()
            try:future.set_result(func(*args))
            except Exception as e:future.set_exception(e)
            return future
    monkeypatch.setattr(main,"session_thread_pool",Pool())
    async def get(*a):return board
    monkeypatch.setattr(main.overview_cache,"get",get)
    gate=asyncio.Event();calls=[]
    async def analyze(req,owner,*,persist_history=True,pipeline=None):
        assert pipeline.recovery_timeout_seconds == main.preparation_recovery_seconds
        calls.append((req,owner,persist_history))
        await gate.wait()
        return report.model_copy(deep=True)
    monkeypatch.setattr(main,"_analyze_portfolio",analyze)
    main.app.dependency_overrides[main.customer_user]=lambda:user
    yield {"profile":profile,"report":report,"board":board,"user":user,"cache":cache,"saved":saved,"gate":gate,"calls":calls}
    main.app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_preparation_keeps_full_pipeline_and_ten_deliveries_skip_model_but_recheck_gates(prepared_app):
    x=prepared_app;payload={"target":"600519","task":"comprehensive","profile_version":1}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url="http://test") as client:
        for _ in range(3):
            response=await client.post("/api/v1/research/prepared",json=payload)
            assert response.status_code==200 and response.json()["status"]=="preparing"
        assert not x["saved"] and len(x["calls"])==1
        req,owner,persist=x["calls"][0]
        assert req.research_mode=="deep" and req.auto_fetch and persist is False
        assert req.profile.user_id=="1001" and not req.context_messages
        premature=await client.post("/api/v1/research/prepared/open",json=payload)
        assert premature.status_code==409
        x["gate"].set()
        await next(iter(x["cache"].entries.values())).task
        ready=await client.post("/api/v1/research/prepared",json=payload)
        assert ready.json()["status"]=="ready"
        for i in range(10):
            started=perf_counter()
            result=await client.post("/api/v1/research/prepared/open",json={**payload,"conversation_id":str(i)})
            assert perf_counter()-started<3
            assert result.status_code==200,result.text
            body=result.json()
            assert body["response_scope"]=="full" and body["research_mode"]=="deep"
            assert body["compliance"]["status"]=="PASS" and body["cross_validation"]["status"]=="PASS"
            assert body["prepared_report"]["source_trace_id"]=="synthetic-origin"
            assert body["prepared_report"]["generation_timings_ms"]["total"]==60000
            assert not body["model_calls"] and body["history_status"]=="stored"
        assert len(x["calls"])==1 and len(x["saved"])==10
        assert x["saved"][-1][5]["prepared_report"]["reused"] is True
    await x["cache"].aclose()


@pytest.mark.asyncio
async def test_profile_data_account_and_model_changes_reject_existing_report(prepared_app,monkeypatch):
    x=prepared_app;r=PreparedResearchRequest(target="600519",profile_version=1)
    await main.prepare_stock_report(r,x["user"]);x["gate"].set()
    await next(iter(x["cache"].entries.values())).task
    for user in ({"id":1002,"_session_id":"other"},):
        with pytest.raises(main.HTTPException) as exc:await main.open_prepared_stock_report(r,user)
        assert exc.value.status_code==409
    x["profile"].version=2
    with pytest.raises(main.HTTPException):await main.open_prepared_stock_report(r,x["user"])
    x["profile"].version=1
    x["board"].sections[0].facts[0].value=56
    with pytest.raises(main.HTTPException):await main.open_prepared_stock_report(r,x["user"])
    x["board"].sections[0].facts[0].value=55
    monkeypatch.setattr(main,"coordinator",type("Changed",(),{"semantic":type("Semantic",(),{"llm":None})()})())
    with pytest.raises(main.HTTPException):await main.open_prepared_stock_report(r,x["user"])
    assert not x["saved"]
    await x["cache"].aclose()


@pytest.mark.asyncio
async def test_review_not_promoted_and_polling_does_not_repeat_paid_work(prepared_app):
    x=prepared_app;x["report"].compliance.status=ComplianceStatus.REVIEW
    r=PreparedResearchRequest(target="600519",profile_version=1)
    await main.prepare_stock_report(r,x["user"]);x["gate"].set()
    await next(iter(x["cache"].entries.values())).task
    for _ in range(5):assert (await main.prepare_stock_report(r,x["user"]))["status"]=="needs_review"
    with pytest.raises(main.HTTPException):await main.open_prepared_stock_report(r,x["user"])
    assert len(x["calls"])==1 and not x["saved"]
    await x["cache"].aclose()


@pytest.mark.parametrize("change",["expired","missing_parent","fallback","unreviewed","not_full","missing_dimension"])
def test_readiness_rejects_invalid_or_incomplete_evidence(change):
    a=full_report();assert report_is_ready(a)
    if change=="expired":a.facts[0].snapshot_time-=timedelta(days=91)
    elif change=="missing_parent":a.facts[0].derived_from=["unknown"]
    elif change=="fallback":a.agent_results[0].details["engine"]="rule_fallback"
    elif change=="unreviewed":a.model_calls=[]
    elif change=="not_full":a.response_scope="snapshot"
    elif change=="missing_dimension":a.data_acquisition.missing_fields_by_agent={"security":["governance_score"]}
    assert not report_is_ready(a)


def test_signature_ignores_fetch_ids_but_preserves_values_and_source_identity():
    a=full_report();board=DataOverviewResponse(direction="stock",target="600519",status="ok",fetched_at=datetime.now(timezone.utc),
        sections=[DataOverviewSection(key="overview",title="行情",status="ok",facts=a.facts)])
    first=board_signature(board)
    board.sections[0].facts[0].fact_id="newid";board.sections[0].facts[0].snapshot_time+=timedelta(seconds=1)
    assert board_signature(board)==first
    board.sections[0].facts[0].source_id="DIFFERENT"
    assert board_signature(board)!=first


@pytest.mark.asyncio
async def test_queue_bounds_and_shutdown_cancel_background_work():
    cache=PreparedResearchCache(max_entries=2,max_pending=1)
    req=OrchestrationRequest(query="600519",profile={"confirmed":True})
    gate=asyncio.Event()
    async def load():await gate.wait();return full_report()
    assert cache.prepare("a",1,"s",req,load)
    assert cache.prepare("b",2,"other",req,load) is None
    cache.cancel_session("s");await asyncio.sleep(0)
    await cache.aclose();assert not cache.entries


def test_task_queries_are_fixed_and_do_not_reuse_arbitrary_questions():
    assert "经营质量与估值匹配情况" in task_query("600519","valuation")
    assert PreparedResearchRequest(target=" 600519 ",profile_version=1).target=="600519"


@pytest.mark.parametrize("status",["ready","needs_review"])
def test_stock_card_opens_only_ready_report_and_replaces_current_fact_snapshot(status):
    from streamlit.testing.v1 import AppTest
    script = """
from concurrent.futures import Future
from unittest.mock import patch
import streamlit as st
from frontend import streamlit_app as ui
from frontend.api_client import ApiResult
ui.init_session()
st.session_state.auth_token='ui-test'
st.session_state.profile['confirmed']=True
st.session_state.active_research_direction='个股研究'
st.session_state.setdefault('sent_paths',[])
if not st.session_state.conversation:
    st.session_state.facts=[{'fact_id':'old','value':1}]
class Executor:
    def submit(self,fn,client,base,method,path,payload,token):
        st.session_state.sent_paths.append(path)
        f=Future()
        if path.endswith('/open'):
            f.set_result(ApiResult(data={'conclusion':'已审核的合成测试报告','facts':[{'fact_id':'new','value':2}]}))
        else:
            f.set_result(ApiResult(data={'status':STATUS,'query':'固定任务测试','generated_at':'test-time'}))
        return f
with patch.object(ui,'account_read_executor',return_value=Executor()), patch.object(ui,'analysis_executor',side_effect=AssertionError('Ready reports must not use long-running analysis queue')), patch.object(ui,'prepared_delivery_executor',return_value=Executor()), patch.object(ui,'backend_http_client',return_value=object()), patch.object(ui,'invalidate_recent_conversations'):
    ui.finish_chat_analyses()
    ui.render_prepared_stock_report('http://test')
""".replace("STATUS",repr(status))
    app=AppTest.from_string(script).run(timeout=15)
    app.run(timeout=15)
    assert not app.exception
    button=next(b for b in app.button if b.label=='打开已审核的完整报告')
    assert button.disabled==(status!='ready')
    if status=='ready':
        button.click().run(timeout=15)
        assert not app.exception
        assert len(app.session_state['conversation'])==2
        assert app.session_state['facts']==[{'fact_id':'new','value':2}]
        assert app.session_state['conversation'][-1]['content']=='已审核的合成测试报告'
    else:
        assert not app.session_state['conversation']
        assert not any(path.endswith('/open') for path in app.session_state['sent_paths'])


@pytest.mark.asyncio
async def test_data_refresh_does_not_mask_running_or_incomplete_job(prepared_app):
    x=prepared_app;r=PreparedResearchRequest(target="600519",profile_version=1)
    await main.prepare_stock_report(r,x["user"])
    x["board"].status="loading"
    assert (await main.prepare_stock_report(r,x["user"]))["status"]=="preparing"
    x["report"].compliance.status=ComplianceStatus.REVIEW
    x["gate"].set();await next(iter(x["cache"].entries.values())).task
    for _ in range(3):assert (await main.prepare_stock_report(r,x["user"]))["status"]=="needs_review"
    assert len(x["calls"])==1
    await x["cache"].aclose()


@pytest.mark.asyncio
async def test_portfolio_is_preserved_in_full_research_and_delivery_identity(prepared_app):
    x=prepared_app;r=PreparedResearchRequest(target="600519",profile_version=1,portfolio=[{"name":"甲","weight":.7}])
    await main.prepare_stock_report(r,x["user"])
    await asyncio.sleep(0)
    assert x["calls"][0][0].portfolio==r.portfolio
    x["gate"].set();await next(iter(x["cache"].entries.values())).task
    # The same portfolio cannot evade the existing concentration guard.
    with pytest.raises(main.HTTPException):await main.open_prepared_stock_report(r,x["user"])
    # Omitting/changing holdings creates another identity, not a cache hit.
    r.portfolio=[]
    with pytest.raises(main.HTTPException):await main.open_prepared_stock_report(r,x["user"])
    assert not x["saved"]
    await x["cache"].aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("total_label",["成分股总数","成份股总数"])
async def test_actual_breadth_columns_preserve_scope_and_reject_expression_columns(total_label):
    from backend.app.data_provider.iwencai import IwencaiSkillHubProvider
    from backend.app.services.recommendation_recovery import derive_explicit_state
    now=datetime.now(timezone.utc);day=now.astimezone(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    rows=[{"指数简称":"同花顺全A(沪深京)","指数代码":"883957.TI",
        f"指数@上涨家数[{day}]":"4681",f"指数@{total_label}[{day}]":"5576",
        f"{{(}}指数@上涨家数[{day}]{{+}}指数@{total_label}[{day}]{{)}}":"10257"},
        {"指数简称":"其他指数","指数代码":"000300.SH",f"指数@上涨家数[{day}]":"200",f"指数@{total_label}[{day}]":"300"}]
    provider=IwencaiSkillHubProvider("test-only",transport=httpx.MockTransport(lambda req:httpx.Response(200,json={"datas":rows})))
    try:facts=await provider.get_market_breadth("中国宏观经济")
    finally:await provider.aclose()
    assert {f.field:f.value for f in facts}=={"advancing_count":4681,"market_total_count":5576}
    assert all(f.entity=="中国宏观经济" and f.entity_code=="883957.TI" for f in facts)
    derived=derive_explicit_state(facts,now=now)
    assert any(f.field=="market_advancing_ratio" and abs(f.value-4681/5576*100)<.0001 for f in derived)
