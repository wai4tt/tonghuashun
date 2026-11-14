"""主要指数看板：固定模板取数、逐指数降级与短期缓存。"""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import backend.app.main as main_module
from backend.app.models import FactRecord
from backend.app.services.index_board import INDEXES, IndexBoardCache, fetch_index_board

START = date(2026, 6, 1)


def fact(entity, code, field, value, period=None):
    return FactRecord(fact_id=f"{code}-{field}-{period}", entity=entity, entity_code=code,
                      field=field, value=value, period=period,
                      snapshot_time=datetime.now(timezone.utc), source_id="IWENCAI_SKILLHUB",
                      quality=0.9)


def closes(count=20, base=3000.0, step=6.0):
    return [base + step * index for index in range(count)]


class Provider:
    def __init__(self, failing=()):
        self.calls = 0
        self.failing = set(failing)

    async def query(self, query, limit=6):
        self.calls += 1
        return [fact(name, code, "close_price", 3200.0 + index, START.isoformat())
                for index, (name, code) in enumerate(INDEXES)] + [
            fact(name, code, "change", 0.5 + index * 0.1, START.isoformat())
            for index, (name, code) in enumerate(INDEXES)]

    async def get_price_history(self, target, asset_type, *, limit):
        assert asset_type == "指数"
        if target in self.failing:
            raise RuntimeError("问财暂时不可用")
        return [fact(target, dict(INDEXES)[target], "close_price", value,
                     (START + timedelta(days=index)).isoformat())
                for index, value in enumerate(closes())]


@pytest.mark.asyncio
async def test_index_board_returns_quotes_ma5_and_three_day_rise():
    board = await fetch_index_board(Provider())
    assert board.status == "ok"
    assert len(board.items) == len(INDEXES)
    first = board.items[0]
    assert first.status == "ok" and first.ma5 is not None
    assert first.three_day_pct is not None and first.three_day_height is not None
    assert first.verdict in {"上行", "偏强震荡", "震荡", "偏弱震荡", "下行"}
    assert first.analysis is not None and len(first.analysis.ma5_series) == 16
    assert len(first.points) == 20 and len(first.dates) == 20


@pytest.mark.asyncio
async def test_one_failed_index_does_not_break_the_board():
    board = await fetch_index_board(Provider(failing={"创业板指"}))
    assert board.status == "partial"
    failed = next(item for item in board.items if item.name == "创业板指")
    # 报价仍在，但历史点位缺失，因此没有走势判断，卡片标记为信息不完整。
    assert failed.status == "empty" and failed.analysis is None
    assert any("创业板指" in note for note in board.notes)


@pytest.mark.asyncio
async def test_unconfigured_provider_is_explicit():
    board = await fetch_index_board(None)
    assert board.status == "unavailable"
    assert all(item.status == "unavailable" for item in board.items)


@pytest.mark.asyncio
async def test_broken_provider_degrades_to_unavailable_instead_of_raising():
    class Broken:
        async def query(self, *_args, **_kwargs):
            raise RuntimeError("问财暂时不可用")

        async def get_price_history(self, *_args, **_kwargs):
            raise RuntimeError("问财暂时不可用")

    board = await fetch_index_board(Broken())
    assert board.status == "unavailable"
    assert all(item.status == "unavailable" for item in board.items)
    cache = IndexBoardCache()
    try:
        cached = await cache.get(Broken())
        assert cached.status == "unavailable"
    finally:
        await cache.aclose()


@pytest.mark.asyncio
async def test_cache_reuses_the_same_result_within_ttl():
    provider = Provider()
    cache = IndexBoardCache(ttl=60)
    try:
        first = await cache.get(provider)
        second = await cache.get(provider)
        assert first.items[0].ma5 == second.items[0].ma5
        assert provider.calls == 1
        await cache.get(provider, refresh=True)
        assert provider.calls == 2
    finally:
        await cache.aclose()


def test_endpoint_requires_login_and_serves_board(monkeypatch):
    client = TestClient(main_module.app)
    unauthenticated = client.post("/api/v1/market/index-board", json={})
    assert unauthenticated.status_code == 401
    monkeypatch.setattr(main_module, "data_provider", Provider())
    monkeypatch.setattr(main_module, "index_board_cache", IndexBoardCache())
    main_module.app.dependency_overrides[main_module.authenticated_user] = lambda: {"id": 1}
    try:
        response = client.post("/api/v1/market/index-board", json={"refresh": True})
    finally:
        main_module.app.dependency_overrides.pop(main_module.authenticated_user, None)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["items"][0]["ma5"] is not None
    assert "analysis" in body["items"][0]
