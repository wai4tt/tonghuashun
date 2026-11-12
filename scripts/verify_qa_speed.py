"""Alternating live baseline/optimized Q&A probes with fixed synthetic evidence.

No accounts or market providers are called. Intent is fixed to measure the same
specialists and final review in both cases; this is not an end-to-end SLA.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from backend.app.agents import llm_agents
from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM, make_investment_agents
from backend.app.models import FactRecord, Intent, OrchestrationRequest
from backend.app.semantic import RequestUnderstanding, SemanticService
from backend.app.services.model_telemetry import analysis_telemetry


class BaselineClient(OpenAICompatibleLLM):
    async def complete_json(self, *, system, payload):
        if payload.get("required_output"):
            payload = {**payload, "required_output": {**payload["required_output"],
                "confidence_reasons": ["最多3项"],
                "details": {"note": "只放本分析维度必要的结构化补充"}}}
            system = system.split("本次为快速研究：")[0] + (
                "本次为快速研究：opinion控制在80字内；风险与缺口必须保留，其余列表最多2项。")
        return await super().complete_json(system=system, payload=payload)


async def probe(config, request, baseline):
    original = llm_agents.compact_fact_references
    if baseline:
        llm_agents.compact_fact_references = lambda payload: (payload, {})
    client = (BaselineClient if baseline else OpenAICompatibleLLM)(config)
    collector = {"calls": [], "mode": "fast"}
    token = analysis_telemetry.set(collector)
    try:
        agents, _ = make_investment_agents(client)
        coordinator = CoordinatorAgent(agents, verify_facts, basic_compliance_check,
                                       semantic=SemanticService(client))
        start = perf_counter()
        result = await coordinator.run(request, understanding=RequestUnderstanding(
            intent=Intent.MARKET_ANALYSIS, confidence=.95, risk_rules=[], reason="固定合成测试意图"))
        return {"baseline": baseline, "seconds": round(perf_counter() - start, 3),
            "compliance": result.compliance.status.value,
            "evidence_count": len(result.evidence),
            "engines": [r.details.get("engine") for r in result.agent_results],
            "calls": collector["calls"]}
    finally:
        analysis_telemetry.reset(token)
        await client.aclose()
        llm_agents.compact_fact_references = original


async def run(args):
    load_dotenv(ROOT / ".env")
    config = LLMConfig.from_env()
    if config is None:
        raise RuntimeError("Model is not configured")
    fields = {"growth_score": 55, "inflation_score": 50, "liquidity_score": 60,
        "policy_score": 55, "risk_appetite_score": 50, "prosperity_score": 55,
        "valuation_score": 50, "capital_flow_score": 55, "crowding_score": 45}
    request = OrchestrationRequest(query="请根据给定的合成评分研判测试市场环境，解释主要依据与风险。",
        profile={"user_id": "SYNTHETIC_SPEED_ONLY", "confirmed": True, "risk_level": "R3"},
        auto_fetch=False, research_mode="fast", facts=[FactRecord(
            fact_id=f"SYNTHETIC-EVIDENCE-{i:016X}", entity="合成测试市场", field=field,
            value=value, snapshot_time=datetime.now(timezone.utc),
            source_id="SYNTHETIC_TEST_ONLY", quality=.95) for i, (field, value) in enumerate(fields.items())])
    report = {"synthetic_inputs_only": True, "market_provider_used": False,
              "intent_fixed": True, "cache_scope": None, "samples": []}
    for baseline in [True, False] * args.pairs:
        result = await probe(config, request, baseline)
        report["samples"].append(result)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in result.items() if k != "calls"}), flush=True)
    report["passed"] = all(s["compliance"] == "PASS" and s["evidence_count"] == 9
        and s["engines"] == ["third_party_llm"] * 2
        and all(c["status"] == "completed" for c in s["calls"]) for s in report["samples"])
    report["median_seconds"] = {name: statistics.median(s["seconds"] for s in report["samples"]
        if s["baseline"] == baseline) for name, baseline in [("baseline", True), ("optimized", False)]}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "median_seconds": report["median_seconds"]}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/performance/qa_speed_20261005.json")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
