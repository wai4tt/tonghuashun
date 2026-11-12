"""一键自检：把 2026-10-22 新增/改动的能力完整跑一遍。

不含外部依赖（不调用真实大模型、不访问问财），用固定语义夹具 + 内存 SQLite +
ASGI 传输，因此可以在任何机器上复现。覆盖：

1. 健康检查 / 就绪检查；
2. 深度投顾链路（规则模式）仍然返回可解释建议包，并带上数值对账结果；
3. 新增的组合诊断接口（风险平价 + 约束投影）；
4. 新增的数值溯源接口；
5. Prometheus 观测出口与 JSON 观测出口的一致性；
6. 登录失败限流的 429 + Retry-After；
7. 并发突发下的完成率与 P95（本机自检，不等于生产容量承诺）。

用法::

    python scripts/verify_optimizations.py
    python scripts/verify_optimizations.py --burst 100 --json-out docs/optimization-report.json

退出码 0 表示全部检查通过；非 0 时``failures``会列出失败项。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx

from backend.app import main
from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
from backend.app.agents.rule_agents import make_rule_agents
from backend.app.auth import hash_password
from backend.app.database import Database
from backend.app.models import FactRecord, OrchestrationRequest
from backend.app.semantic import SemanticService
from backend.app.services.login_guard import LoginThrottle
from backend.app.services.monitoring import ServiceMetrics
from backend.app.services.research import AutomatedResearchPipeline
from backend.app.services.research_admission import ResearchAdmission
from backend.app.session_pool import SessionThreadPool


class FixedSemanticLLM:
    """固定语义夹具：让验收只检验编排与契约，不检验第三方模型水平。"""

    async def complete_json(self, *, system, payload):
        schema = payload["required_schema"]["title"]
        if schema == "OutputReview":
            return {"confidence": 0.95, "risk_rules": [], "conflicting_agents": [], "reason": "自检复核通过"}
        cases = {
            "市场演示": "market_analysis",
            "行业演示": "industry_analysis",
            "个股演示": "security_research",
            "基金演示": "fund_screening",
            "组合演示": "portfolio_review",
            "保证收益演示": "portfolio_review",
        }
        return {
            "intent": cases.get(payload.get("query", ""), "portfolio_review"),
            "confidence": 0.95,
            "risk_rules": ["NO_RETURN_PROMISE"] if payload.get("query") == "保证收益演示" else [],
            "reason": "固定合成语义，仅供自检",
        }


def sample_request(query: str = "组合演示", *, research_mode: str = "deep") -> OrchestrationRequest:
    values = {
        "growth_score": 55, "inflation_score": 50, "liquidity_score": 60, "policy_score": 55,
        "risk_appetite_score": 50, "prosperity_score": 55, "valuation_score": 55,
        "capital_flow_score": 55, "crowding_score": 45, "fundamental_score": 60,
        "technical_score": 55, "event_score": 55, "governance_score": 60,
        "fund_risk_level": "R2", "fund_score": 65, "fee_rate": 0.005,
        "tracking_error": 0.01, "weight": 0.15,
    }
    return OrchestrationRequest(
        query=query, auto_fetch=False, research_mode=research_mode,
        profile={"risk_level": "R3", "confirmed": True, "horizon_months": 24,
                 "max_drawdown": 0.15, "liquidity_need": "中"},
        facts=[
            FactRecord(
                fact_id="DEMO-" + field, entity="自检ETF", field=field, value=value,
                snapshot_time=datetime.now(timezone.utc),
                source_id="SYNTHETIC_SELF_CHECK_ONLY", quality=0.95,
            )
            for field, value in values.items()
        ],
    )


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return round(ordered[index], 2)


class Checker:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def record(self, name: str, passed: bool, detail: Any = None) -> None:
        self.checks.append({"name": name, "passed": bool(passed), "detail": detail})

    def expect(self, name: str, condition: bool, detail: Any = None) -> None:
        self.record(name, condition, detail)

    @property
    def failures(self) -> list[dict[str, Any]]:
        return [item for item in self.checks if not item["passed"]]


async def run(burst: int, timeout_seconds: float) -> dict[str, Any]:
    check = Checker()
    coordinator = CoordinatorAgent(
        make_rule_agents(), verify_facts, basic_compliance_check,
        semantic=SemanticService(FixedSemanticLLM()),
    )
    originals = {key: getattr(main, key) for key in
                 ("database", "session_thread_pool", "coordinator", "research_pipeline",
                  "service_metrics", "llm_enabled", "data_provider", "login_throttle",
                  "local_research_admission")}
    pool = SessionThreadPool(max_workers=max(32, min(burst, 128)))
    report: dict[str, Any] = {"generated_at": datetime.now(timezone.utc).isoformat()}

    with TemporaryDirectory(prefix="wence-optimization-") as directory:
        database = Database("sqlite+pysqlite:///" + (Path(directory) / "selfcheck.sqlite").as_posix(),
                            "isolated-selfcheck-secret-not-production-20261022")
        database.initialize()
        throttle = LoginThrottle(max_failures=5, window_seconds=600, lockout_seconds=30)
        try:
            main.database, main.session_thread_pool = database, pool
            main.coordinator, main.research_pipeline = coordinator, AutomatedResearchPipeline(None)
            main.service_metrics, main.llm_enabled = ServiceMetrics(), False
            main.data_provider, main.login_throttle = None, throttle
            # 线上精简闸门是 16+0.05s 排队；自检要观察整条链路而不是排队策略，
            # 因此把闸门放宽到突发数，429 由专门的登录限流用例覆盖。
            main.local_research_admission = ResearchAdmission(
                limit=max(32, burst), queue_seconds=0.5)
            report["admission_limit"] = main.local_research_admission.limit

            password_hash = hash_password("SyntheticSelfCheckPassword")
            user = database.create_user("selfcheck-user", password_hash)
            database.save_profile(int(user["id"]), sample_request().profile.model_dump(mode="json"), 1)
            headers = {"Authorization": "Bearer " + main.auth_response_for(user).access_token}

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                         base_url="http://selfcheck", timeout=timeout_seconds) as client:
                # 1) 存活与就绪
                health = await client.get("/api/v1/health")
                check.expect("健康检查可用", health.status_code == 200, health.status_code)
                readiness = await client.get("/api/v1/readiness")
                check.expect("就绪检查可用", readiness.status_code in {200, 503}, readiness.status_code)

                # 2) 深度投顾链路（含多智能体协同 + 数值对账）
                deep = await client.post("/api/v1/portfolio/analyze", headers=headers,
                                         json=sample_request().model_dump(mode="json"))
                payload = deep.json() if deep.status_code == 200 else {}
                check.expect("深度分析返回 200", deep.status_code == 200, deep.status_code)
                check.expect("合规状态可用",
                             payload.get("compliance", {}).get("status") in {"PASS", "REVIEW", "BLOCK"},
                             payload.get("compliance", {}).get("status"))
                grounding = payload.get("numeric_grounding") or {}
                check.expect("建议包带数值对账结果", isinstance(grounding.get("claim_count"), int), grounding.get("status"))
                check.expect("有智能体协同结果", bool(payload.get("agent_results")), len(payload.get("agent_results") or []))
                check.expect("有阶段耗时", "total" in (payload.get("timings_ms") or {}),
                             sorted((payload.get("timings_ms") or {}).keys())[:6])
                deep_latency = payload.get("timings_ms", {}).get("total")

                # 3) 组合诊断（确定性算术，无模型调用）
                plan_response = await client.post("/api/v1/portfolio/plan", headers=headers, json={
                    "holdings": [
                        {"symbol": "600519", "name": "自检白酒", "weight": 0.2, "industry": "白酒", "volatility": 0.25},
                        {"symbol": "000001", "name": "自检银行", "weight": 0.2, "industry": "银行", "volatility": 0.18},
                        {"symbol": "300750", "name": "自检电池", "weight": 0.2, "industry": "电池", "volatility": 0.40},
                        {"symbol": "601318", "name": "自检保险", "weight": 0.2, "industry": "保险", "volatility": 0.22},
                        {"symbol": "000858", "name": "自检食品", "weight": 0.2, "industry": "食品", "volatility": 0.30},
                    ],
                    "include_price_history": False,
                    "total_value": 200000,
                })
                plan = plan_response.json() if plan_response.status_code == 200 else {}
                check.expect("组合诊断返回 200", plan_response.status_code == 200, plan_response.status_code)
                candidates = plan.get("candidates") or {}
                check.expect("候选权重合计为 1",
                             bool(candidates) and all(abs(sum(v) - 1) < 1e-3 for v in candidates.values()),
                             {k: round(sum(v), 6) for k, v in candidates.items()})
                check.expect("单票上限被遵守",
                             bool(candidates) and all(max(v) <= 0.2 + 1e-6 for v in candidates.values()),
                             {k: max(v) for k, v in candidates.items()})
                check.expect("可行约束无需放宽",
                             (plan.get("constraint_violations") or {}).get("relaxed") == [],
                             (plan.get("constraint_violations") or {}).get("relaxed"))
                check.expect("组合诊断不含模型调用", plan.get("method") == "deterministic_arithmetic_no_llm",
                             plan.get("method"))

                # 4) 数值溯源
                grounding_response = await client.post("/api/v1/research/grounding-check", json={
                    "text": "自检ETF的费率为0.5%，另有9.99%的编造数字。",
                    "facts": [{
                        "fact_id": "DEMO-fee_rate", "entity": "自检ETF", "field": "fee_rate",
                        "value": 0.005, "unit": None,
                        "snapshot_time": datetime.now(timezone.utc).isoformat(),
                        "source_id": "SYNTHETIC_SELF_CHECK_ONLY", "quality": 0.95,
                    }],
                })
                grounding_payload = grounding_response.json() if grounding_response.status_code == 200 else {}
                check.expect("数值溯源接口可用", grounding_response.status_code == 200, grounding_response.status_code)
                check.expect("能抓出无出处数字", grounding_payload.get("ungrounded_count", 0) >= 1,
                             grounding_payload.get("ungrounded_count"))

                # 5) 观测出口
                metrics = await client.get("/api/v1/metrics")
                prometheus = await client.get("/api/v1/metrics/prometheus")
                check.expect("JSON 观测可用", metrics.status_code == 200, metrics.status_code)
                check.expect("观测含登录限流", "login_throttle" in (metrics.json() if metrics.status_code == 200 else {}), None)
                check.expect("Prometheus 观测可用",
                             prometheus.status_code == 200 and "text/plain" in prometheus.headers.get("content-type", ""),
                             prometheus.headers.get("content-type"))
                check.expect("Prometheus 指标齐全",
                             "wence_analysis_latency_ms" in prometheus.text and "wence_requests_total" in prometheus.text,
                             None)

                # 6) 登录失败限流
                for _ in range(4):
                    attempt = await client.post("/api/v1/auth/login",
                                                json={"username": "selfcheck-user", "password": "wrong-pass"})
                    check.expect("错误口令返回 401", attempt.status_code == 401, attempt.status_code)
                locked = await client.post("/api/v1/auth/login",
                                           json={"username": "selfcheck-user", "password": "wrong-pass"})
                check.expect("连续失败后锁定", locked.status_code == 429, locked.status_code)
                check.expect("锁定带 Retry-After", bool(locked.headers.get("Retry-After")), locked.headers.get("Retry-After"))
                throttle.reset()
                allowed = await client.post("/api/v1/auth/login",
                                            json={"username": "selfcheck-user", "password": "SyntheticSelfCheckPassword"})
                check.expect("正确口令仍可登录", allowed.status_code == 200, allowed.status_code)

                # 7) 并发突发（本机自检口径）
                burst_results = await burst_lite(client, headers, burst)
                latencies = [item["latency_ms"] for item in burst_results if item["status"] == 200]
                completed = len(latencies)
                check.expect("突发全部完成", completed == burst, {"completed": completed, "total": burst})
                check.expect("P95 在 3 秒内", percentile(latencies, 0.95) <= 3000.0, percentile(latencies, 0.95))
                report["burst"] = {
                    "requests": burst, "completed": completed,
                    "rejected": len([item for item in burst_results if item["status"] == 429]),
                    "failed": len([item for item in burst_results if item["status"] not in {200, 429}]),
                    "p50_ms": percentile(latencies, 0.5), "p95_ms": percentile(latencies, 0.95),
                    "max_ms": percentile(latencies, 1.0),
                }
                report["deep_analysis_total_ms"] = deep_latency
        finally:
            for key, value in originals.items():
                setattr(main, key, value)
            pool.shutdown()
            if database.engine is not None:
                database.engine.dispose()

    report["checks"] = check.checks
    report["failures"] = check.failures
    report["passed"] = not check.failures
    return report


async def burst_lite(client: httpx.AsyncClient, headers: dict[str, str], count: int) -> list[dict[str, Any]]:
    start = asyncio.Event()

    async def one() -> dict[str, Any]:
        await start.wait()
        request = sample_request("请诊断我的持仓组合", research_mode="lite")
        started = perf_counter()
        response = await client.post("/api/v1/portfolio/analyze", json=request.model_dump(mode="json"),
                                     headers=headers)
        return {"status": response.status_code,
                "latency_ms": round((perf_counter() - started) * 1000, 2),
                "retry_after": response.headers.get("Retry-After")}

    tasks = [asyncio.create_task(one()) for _ in range(count)]
    start.set()
    return await asyncio.gather(*tasks)


def main_entry() -> int:
    parser = argparse.ArgumentParser(description="问策智投 2026-10-22 优化项自检")
    parser.add_argument("--burst", type=int, default=20, help="并发突发请求数（默认 20）")
    parser.add_argument("--timeout", type=float, default=30.0, help="单请求超时秒数")
    parser.add_argument("--json-out", type=Path, default=None, help="把结果写入 JSON 文件")
    args = parser.parse_args()

    report = asyncio.run(run(max(1, args.burst), max(1.0, args.timeout)))
    for item in report["checks"]:
        print(("PASS " if item["passed"] else "FAIL ") + item["name"],
              "" if item["detail"] is None else f" -> {item['detail']}")
    if "burst" in report:
        print("突发口径:", json.dumps(report["burst"], ensure_ascii=False))
    print("深度分析总耗时(ms):", report.get("deep_analysis_total_ms"))
    print("结论:", "全部通过" if report["passed"] else f"{len(report['failures'])} 项未通过")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("结果已写入:", args.json_out)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main_entry())