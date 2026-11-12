"""Compare real model latency using synthetic inputs; never calls market/account services."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM, make_investment_agents
from backend.app.models import FactRecord, OrchestrationRequest
from backend.app.semantic import SemanticService
from backend.app.services.model_telemetry import analysis_telemetry


async def probe(config, *, pipeline=False):
    llm = OpenAICompatibleLLM(config)
    collector = {"calls": [], "mode": "fast"}
    token = analysis_telemetry.set(collector)
    started = perf_counter()
    try:
        semantic = SemanticService(llm)
        request = OrchestrationRequest(query="请解释市盈率的含义，不推荐股票。",
            profile={"user_id": "SYNTHETIC_LATENCY_ONLY", "confirmed": True, "risk_level": "R3"},
            auto_fetch=False, research_mode="fast")
        if pipeline:
            now = datetime.now(timezone.utc)
            fields = {"growth_score": 55, "inflation_score": 50, "liquidity_score": 60,
                "policy_score": 55, "risk_appetite_score": 50, "prosperity_score": 55,
                "valuation_score": 50, "capital_flow_score": 55, "crowding_score": 45}
            request = request.model_copy(update={"query": "请只根据提供的合成宏观评分分析测试市场的环境，"
                "解释各维度作用；这不是实际市场数据，不推荐具体产品，不作收益承诺。",
                "facts": [FactRecord(fact_id="SYNTHETIC-" + field, entity="合成测试市场", field=field,
                    value=value, snapshot_time=now, source_id="SYNTHETIC_TEST_ONLY", quality=.95)
                    for field, value in fields.items()]})
            agents, _ = make_investment_agents(llm)
            advice = await CoordinatorAgent(agents, verify_facts, basic_compliance_check,
                semantic=semantic).run(request)
            result = {"intent": advice.intent, "compliance": advice.compliance.status,
                "engines": [r.details.get("engine") for r in advice.agent_results],
                "evidence_count": len(advice.evidence)}
        else:
            judged = await semantic.understand(request)
            result = {"intent": judged.intent, "reason": judged.reason}
        return {"seconds": round(perf_counter() - started, 3), "result": result, "calls": collector["calls"]}
    finally:
        analysis_telemetry.reset(token)
        await llm.aclose()


async def run(args):
    load_dotenv(ROOT / ".env")
    config = LLMConfig.from_env()
    if config is None:
        print("Model not configured; no requests sent.")
        return 2
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "model": config.model,
              "synthetic_inputs_only": True, "market_provider_used": False, "checks": {}}
    checks = [("baseline_default_thinking", replace(config, thinking_mode="omit"), False)] if args.baseline else []
    checks += [("optimized_understanding", config, False), ("optimized_pipeline", config, True)]
    for name, settings, pipeline in checks:
        report["checks"][name] = await probe(settings, pipeline=pipeline)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        item = report["checks"][name]
        print(json.dumps({"check": name, "seconds": item["seconds"], "result": item["result"],
                          "call_status": [c["status"] for c in item["calls"]]}, ensure_ascii=True), flush=True)
    full = report["checks"]["optimized_pipeline"]["result"]
    report["passed"] = (report["checks"]["optimized_understanding"]["result"]["intent"] == "education"
        and full.get("compliance") == "PASS" and full.get("engines") == ["third_party_llm"] * 2
        and all(c["status"] == "completed" for name, check in report["checks"].items()
                if name.startswith("optimized") for c in check["calls"]))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true", help="Also send a call using provider default thinking")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/performance/analysis_latency_20261004.json")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
