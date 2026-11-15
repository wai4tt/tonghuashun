"""横向比较的投资价值排序：同类型、同口径、缺失不补零。"""
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

import backend.app.main as main_module
from backend.app.models import FactRecord
from backend.app.models.schemas import (
    ComparisonItem,
    DataOverviewResponse,
    DataOverviewSection,
)
from backend.app.services.investment_decision import build_decision

NOW = datetime.now(timezone.utc)


def facts(code, name, **metrics):
    return [FactRecord(fact_id=f"{code}-{field}", entity=name, entity_code=code, field=field,
                       value=value, period="2026-06-30", snapshot_time=NOW,
                       source_id="IWENCAI_SKILLHUB", quality=0.9)
            for field, value in metrics.items()]


def item(target, name, code, **metrics):
    return ComparisonItem(target=target, asset_type="股票", entity=name, entity_code=code,
                          display_name=name, status="ok", facts=facts(code, name, **metrics),
                          fetched_at=NOW)


def test_decision_ranks_the_cheaper_growing_company_first():
    decision = build_decision([
        item("600519", "公司甲", "600519.SH", pe_ttm=12, pb=1.5, roe=18,
             revenue_growth=20, net_profit_growth=25, change=2),
        item("000858", "公司乙", "000858.SZ", pe_ttm=45, pb=5, roe=6,
             revenue_growth=-5, net_profit_growth=-8, change=-2),
    ])
    assert decision.status == "ok"
    assert decision.preferred == "600519"
    assert decision.preferred_name == "公司甲"
    assert [candidate.target for candidate in decision.candidates] == ["600519", "000858"]
    assert decision.candidates[0].score > decision.candidates[1].score
    assert "公司甲" in decision.conclusion and "不构成买入建议" in decision.conclusion
    assert decision.candidates[0].strengths
    assert any("预期收益" in caveat for caveat in decision.caveats)
    assert decision.weights and decision.dimensions


def test_decision_is_insufficient_when_only_one_dimension_exists():
    decision = build_decision([
        item("600519", "公司甲", "600519.SH", pe_ttm=12),
        item("000858", "公司乙", "000858.SZ", pe_ttm=20),
    ])
    assert decision.status == "insufficient"
    assert decision.preferred is None
    assert "不足以比较" in decision.conclusion
    assert decision.missing


def test_negative_pe_falls_back_to_pb_instead_of_treating_it_as_cheap():
    decision = build_decision([
        item("600519", "公司甲", "600519.SH", pe_ttm=-8, pb=1.2, roe=15,
             revenue_growth=10, change=1),
        item("000858", "公司乙", "000858.SZ", pe_ttm=25, pb=3, roe=10,
             revenue_growth=5, change=0),
    ])
    valuation = next(dimension for dimension in decision.dimensions if dimension.key == "valuation")
    # 亏损公司的负 PE 不能当作“便宜”，同一维度改用双方都有效的市净率。
    assert valuation.field == "pb"
    assert valuation.field_label == "市净率"
    assert decision.candidates[0].metrics["pe_ttm"] == -8
    assert decision.status == "ok"


class FakeCache:
    async def get(self, provider, request, *, jobs=None, namespace=None):
        return DataOverviewResponse(
            direction=request.direction, target=request.target, fetched_at=NOW, status="ok",
            sections=[DataOverviewSection(key="overview", title="行情与指标", status="ok",
                                          facts=FACTS[request.target], fetched_at=NOW)],
        )


FACTS = {
    "600519": facts("600519.SH", "公司甲", pe_ttm=12, pb=1.5, roe=18, revenue_growth=20,
                    net_profit_growth=25, change=2),
    "000858": facts("000858.SZ", "公司乙", pe_ttm=45, pb=5, roe=6, revenue_growth=-5,
                    net_profit_growth=-8, change=-2),
}


def test_compare_endpoint_returns_names_and_a_decision(monkeypatch):
    client = TestClient(main_module.app)
    monkeypatch.setattr(main_module, "overview_cache", FakeCache())
    main_module.app.dependency_overrides[main_module.authenticated_user] = lambda: {"id": 1}
    try:
        response = client.post("/api/v1/data/compare",
                               json={"asset_type": "股票", "targets": ["600519", "000858"]})
    finally:
        main_module.app.dependency_overrides.pop(main_module.authenticated_user, None)
    assert response.status_code == 200
    body = response.json()
    assert [item["display_name"] for item in body["items"]] == ["公司甲", "公司乙"]
    assert body["decision"]["status"] == "ok"
    assert body["decision"]["preferred"] == "600519"
    assert body["decision"]["candidates"][0]["rank"] == 1
