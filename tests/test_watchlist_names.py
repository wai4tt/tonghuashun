"""自选名称补全与结构化报告导出的接口回归。"""
from datetime import datetime, timezone

from fastapi.testclient import TestClient

import backend.app.main as main_module
from backend.app.database import Database
from backend.app.models import AdvicePackage, ComplianceResult, CrossValidationResult, FactRecord, Intent, TaskPlan
from backend.app.models.schemas import ReportExportRequest
from backend.app.services.research_report import build_research_report, research_report_markdown

SECRET = "test-secret-that-is-longer-than-thirty-two-characters"


def test_watchlist_stores_and_updates_the_verified_name():
    database = Database("sqlite+pysqlite:///:memory:", SECRET)
    database.initialize()
    user = database.create_user("name-user", "hashed")
    row = database.add_watchlist_item(int(user["id"]), "600519", "股票")
    assert row["name"] is None
    assert database.set_watchlist_name(int(user["id"]), int(row["id"]), "贵州茅台") is True
    assert database.set_watchlist_name(int(user["id"]), int(row["id"]), "   ") is False
    saved = database.list_watchlist(int(user["id"]))[0]
    assert saved["name"] == "贵州茅台" and saved["target"] == "600519"


def test_initialize_adds_the_name_column_to_a_legacy_table():
    """旧库只缺 name 列时，启动迁移补列而不是重建表。"""

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE watchlist_items (id INTEGER PRIMARY KEY, user_id INTEGER, "
            "target VARCHAR(60), asset_type VARCHAR(16), created_at DATETIME)"))
    database = Database("sqlite+pysqlite:///:memory:", SECRET)
    database.engine, database.session_factory = engine, sessionmaker(engine, expire_on_commit=False)
    database.initialize(backfill=False)
    with engine.connect() as connection:
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(watchlist_items)"))}
    assert "name" in columns


def test_adding_a_code_resolves_the_security_name(monkeypatch):
    database = Database("sqlite+pysqlite:///:memory:", SECRET)
    database.initialize()
    user = database.create_user("watch-user", "hashed")

    class Provider:
        async def query(self, query, limit=2, skill_id=None):
            return [FactRecord(fact_id="n1", entity="贵州茅台", entity_code="600519.SH",
                               field="name", value="贵州茅台", period=None,
                               snapshot_time=datetime.now(timezone.utc),
                               source_id="IWENCAI_SKILLHUB", quality=0.9)]

    monkeypatch.setattr(main_module, "database", database)
    monkeypatch.setattr(main_module, "data_provider", Provider())
    client = TestClient(main_module.app)
    main_module.app.dependency_overrides[main_module.authenticated_user] = lambda: {"id": int(user["id"]), "role": None}
    try:
        response = client.post("/api/v1/watchlist", json={"target": "600519", "asset_type": "股票"})
        listed = client.get("/api/v1/watchlist")
    finally:
        main_module.app.dependency_overrides.pop(main_module.authenticated_user, None)
    assert response.status_code == 200
    assert response.json()["name"] == "贵州茅台"
    assert listed.json()[0]["name"] == "贵州茅台"


def test_mismatched_identity_is_not_saved_as_the_name(monkeypatch):
    from backend.app.services.security_names import resolve_security_name

    class Provider:
        async def query(self, query, limit=2, skill_id=None):
            return [FactRecord(fact_id="n2", entity="平安银行", entity_code="000001.SZ",
                               field="name", value="平安银行", period=None,
                               snapshot_time=datetime.now(timezone.utc),
                               source_id="IWENCAI_SKILLHUB", quality=0.9)]

    import asyncio
    assert asyncio.run(resolve_security_name(Provider(), "600519", "股票")) is None
    assert asyncio.run(resolve_security_name(None, "600519", "股票")) is None


def fact(fid, field="change", value=2, period="2026-06-30"):
    return FactRecord(fact_id=fid, entity="测试公司", field=field, value=value, unit="percent",
                      period=period, source_id="SYNTHETIC_TEST_ONLY", quality=0.95,
                      snapshot_time=datetime.now(timezone.utc))


def advice(rows):
    return AdvicePackage(
        trace_id="t", intent=Intent.SECURITY_RESEARCH,
        task_plan=TaskPlan(trace_id="t", intent=Intent.SECURITY_RESEARCH),
        compliance=ComplianceResult(status="PASS"), conclusion="有限结论", confidence=0.6,
        facts=rows, evidence=[row.fact_id for row in rows],
        cross_validation=CrossValidationResult(status="PASS"), research_mode="deep",
    )


def test_markdown_export_contains_conclusion_coverage_and_sources():
    report = build_research_report(advice([fact("a"), fact("b", "net_profit_growth", -3)]))
    markdown = research_report_markdown(report, question="分析测试公司")
    assert "# " in markdown and "## 一、结论" in markdown
    assert "测试公司" in markdown and "分析测试公司" in markdown
    assert "数据覆盖" in markdown and "分维度证据" in markdown
    assert "SYNTHETIC_TEST_ONLY" in markdown


def test_markdown_endpoint_requires_login_and_returns_text(monkeypatch):
    report = build_research_report(advice([fact("a")]))
    client = TestClient(main_module.app, raise_server_exceptions=False)
    payload = ReportExportRequest(report=report, question="导出测试").model_dump(mode="json")
    assert client.post("/api/v1/research/report/markdown", json=payload).status_code == 401
    main_module.app.dependency_overrides[main_module.authenticated_user] = lambda: {"id": 1}
    try:
        response = client.post("/api/v1/research/report/markdown", json=payload)
    finally:
        main_module.app.dependency_overrides.pop(main_module.authenticated_user, None)
    assert response.status_code == 200
    body = response.json()
    assert body["filename"].endswith(".md") and "## 一、结论" in body["markdown"]
