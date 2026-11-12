"""Reproduced comprehensive QA failures: numeric boundaries, scope and races."""
import asyncio
from datetime import datetime, timezone
import json

import httpx
import pytest

from backend.app import main
from backend.app.models import FactRecord, OrchestrationRequest
from backend.app.numeric import finite_number
from backend.app.fact_units import percentage_points
from backend.app.services.local_consultation import observation_value, select_scope
from backend.app.services.instant_research import InstantResearchService
from backend.app.services.model_response_cache import ModelResponseCache
from backend.app.services.research_cache import ResearchFactCache


def source(name="甲公司", code="600519", value=10, field="close_price"):
    return FactRecord(fact_id=code+field, entity=name, entity_code=code+".SH", field=field,
        value=value, unit="CNY" if field=="close_price" else None, quality=.9,
        source_id="SYNTHETIC_COMPREHENSIVE_QA", snapshot_time=datetime.now(timezone.utc))


def req(query="分析当前标的", **kwargs):
    return OrchestrationRequest(query=query, research_mode="instant", research_direction="个股研究",
        profile={"confirmed":True,"risk_level":"R3"}, facts=kwargs.pop("facts",[
            source(), source("乙公司","600036",20)]), **kwargs)


@pytest.mark.parametrize("bad", [True, False, None, {}, [], 10**400, -(10**400), float("inf"), float("nan"),
                                     "1e309", "1,2", "1,,000", "1,00,000", "123元", "9"*300])
def test_nonfinite_and_ambiguous_numeric_values_are_unusable(bad):
    assert finite_number(bad,allow_text=True) is None
    assert observation_value(source(value=bad)) is None


@pytest.mark.parametrize("text,expected", [("1,446.44",1446.44),("9.53644797473E9",9536447974.73),
                                           ("-.5",-.5),("0",0),("1,234,567",1234567)])
def test_normal_provider_numeric_text_remains_supported(text,expected):
    assert finite_number(text,allow_text=True)==expected


@pytest.mark.parametrize("value,unit", [(10**400,"percent"),(1e308,"ratio"),("1e309%",None)])
def test_percentage_overflow_never_produces_infinity_or_exception(value,unit):
    assert percentage_points(value,unit) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,unit", [("close_price",10**400,"CNY"),("roe",10**400,"percent"),
                                              ("roe",1e308,"ratio"),("fundamental_score",10**400,None)])
async def test_extreme_user_facts_return_explicit_missing_data_instead_of_500(field,value,unit):
    row=source(value=value,field=field).model_dump(mode="json")
    row["unit"]=unit
    payload=req("分析甲公司",facts=[]).model_dump(mode="json")
    payload["facts"]=[row]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url="http://test") as client:
        response=await client.post("/api/v1/portfolio/analyze",json=payload)
    assert response.status_code==200
    body=response.json()
    assert body["response_scope"]=="clarification" and not body["evidence"]
    assert not body["model_calls"]


@pytest.mark.asyncio
async def test_generic_target_without_selection_never_combines_multiple_companies():
    request=req()
    prepared,_,advice=await InstantResearchService().run(request,select_scope(request))
    assert advice.response_scope=="clarification" and not advice.evidence and not prepared.facts
    assert "明确名称或代码" in advice.conclusion


@pytest.mark.asyncio
async def test_generic_target_uses_page_selection_and_explicit_question_overrides_it():
    request=req(research_target="600519")
    prepared,_,advice=await InstantResearchService().run(request,select_scope(request))
    assert advice.response_scope=="evidence_review" and "甲公司" in advice.conclusion
    assert "乙公司" not in advice.conclusion
    assert {f.entity for f in prepared.facts}=={"甲公司"}
    explicit=request.model_copy(update={"query":"分析乙公司的价格"})
    prepared,_,advice=await InstantResearchService().run(explicit,select_scope(explicit))
    assert "乙公司" in advice.conclusion and "甲公司" not in advice.conclusion
    assert {f.entity for f in prepared.facts}=={"乙公司"}


@pytest.mark.asyncio
async def test_selected_target_not_loaded_does_not_fall_back_to_another_stock():
    request=req(research_target="000001")
    _,_,advice=await InstantResearchService().run(request,select_scope(request))
    assert advice.response_scope=="clarification" and not advice.evidence


@pytest.mark.asyncio
async def test_generic_single_company_snapshot_keeps_backward_compatibility():
    request=req(facts=[source()])
    _,_,advice=await InstantResearchService().run(request,select_scope(request))
    assert advice.response_scope=="evidence_review" and "甲公司" in advice.conclusion


@pytest.mark.asyncio
async def test_cancelled_model_loader_cannot_overwrite_retry_result():
    cache=ModelResponseCache()
    entered,release=asyncio.Event(),asyncio.Event()
    async def old():
        entered.set()
        try:await asyncio.Future()
        except asyncio.CancelledError:
            await release.wait()
            return {"version":"cancelled-old"}
    waiter=asyncio.create_task(cache.get("key",old,cancel_if_unused=True))
    await entered.wait()
    old_loader=cache.pending["key"]
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):await waiter
    async def fresh():return {"version":"fresh"}
    assert (await cache.get("key",fresh))[0]=={"version":"fresh"}
    release.set()
    await old_loader
    assert (await cache.get("key",fresh))==({"version":"fresh"},True)
    assert not cache.pending and not cache.waiters
    await cache.aclose()


@pytest.mark.asyncio
async def test_rebound_public_cache_cannot_publish_previous_generation_result():
    cache=ResearchFactCache()
    entered,release=asyncio.Event(),asyncio.Event()
    async def old():entered.set();await release.wait();return [source(value=1)]
    pending=asyncio.create_task(cache.get("key",old,lambda:datetime.now(timezone.utc)))
    await entered.wait()
    # Represents a new lifecycle binding while an old loader is still unwinding.
    cache.loop=None
    async def fresh():return [source(value=2)]
    await cache.get("key",fresh,lambda:datetime.now(timezone.utc))
    release.set();await pending
    rows,hit=await cache.get("key",fresh,lambda:datetime.now(timezone.utc))
    assert hit and rows[0].value==2
    await cache.aclose()


@pytest.mark.asyncio
async def test_concurrent_model_callers_share_one_load_and_receive_separate_copies():
    cache=ModelResponseCache()
    calls=0
    async def load():
        nonlocal calls
        calls+=1
        await asyncio.sleep(.01)
        return {"values":[1,2]}
    results=await asyncio.gather(*(cache.get("shared",load) for _ in range(40)))
    assert calls==1 and sum(not hit for _,hit in results)==1
    results[0][0]["values"].append(999)
    assert all(item["values"]==[1,2] for item,_ in results[1:])
    assert (await cache.get("shared",load))[0]["values"]==[1,2]
    await cache.aclose()


def test_ui_captures_selected_target_with_request(monkeypatch):
    from concurrent.futures import Future
    import streamlit as st
    from frontend import streamlit_app as ui
    class State(dict):
        def __getattr__(self,key):return self[key]
        def __setattr__(self,key,value):self[key]=value
    class Executor:
        def submit(self,*args):state["sent"]=args[5];return Future()
    state=State(profile={"confirmed":True,"version":1},facts=[],portfolio=[],auth_token=None,
        conversation_id="scope-test",conversation=[],active_research_direction="个股研究",research_mode="instant",
        board_target_stock="600036")
    monkeypatch.setattr(st,"session_state",state)
    monkeypatch.setattr(ui,"profile_ready",lambda:True)
    monkeypatch.setattr(ui,"preview_executor",lambda:Executor())
    monkeypatch.setattr(ui,"backend_http_client",lambda _:object())
    monkeypatch.setattr(ui,"save_research_session",lambda:None)
    ui.submit_chat_analysis("http://test","个股研究","分析当前标的")
    assert state["sent"]["research_target"]=="600036"
    state["board_target_stock"]="600519"
    assert state["sent"]["research_target"]=="600036"


@pytest.mark.asyncio
@pytest.mark.parametrize("direction,field,names", [
    ("行业分析","prosperity_score",("甲行业","乙行业")),
    ("基金筛选","fund_score",("甲基金","乙基金")),
    ("可转债分析","bond_price",("甲转债","乙转债")),
])
@pytest.mark.parametrize("question",["explicit","selected","missing","comparison"])
async def test_entity_scope_is_consistent_across_other_research_pages(direction,field,names,question):
    rows=[source(names[0],"600519",20,field),source(names[1],"600036",80,field)]
    query={"explicit":"请分析"+names[1],"selected":"分析当前标的","missing":"分析当前标的",
           "comparison":"比较"+names[0]+"和"+names[1]}[question]
    target="600036" if question=="selected" else "000001" if question=="missing" else None
    request=req(query,facts=rows,research_target=target).model_copy(update={"research_direction":direction})
    prepared,_,advice=await InstantResearchService().run(request,select_scope(request))
    if question=="missing":
        assert advice.response_scope=="clarification" and not advice.evidence
    elif question=="comparison":
        assert advice.response_scope=="evidence_review" and all(name in advice.conclusion for name in names)
    else:
        assert advice.response_scope=="evidence_review" and names[1] in advice.conclusion
        assert names[0] not in advice.conclusion and {f.entity for f in prepared.facts}=={names[1]}
