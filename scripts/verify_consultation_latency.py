"""Measure the complete ASGI consultation, including routing and final review.

Synthetic evidence only; no account, market provider or fixed-intent bypass.
The three-second target passes only when all samples finish every planned gate.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
from time import perf_counter

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from backend.app import main
from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM, make_investment_agents
from backend.app.models import FactRecord, OrchestrationRequest
from backend.app.semantic import SemanticService


def synthetic_request():
    fields = {"growth_score": 55, "inflation_score": 50, "liquidity_score": 60,
              "policy_score": 55, "risk_appetite_score": 50, "prosperity_score": 55,
              "valuation_score": 50, "capital_flow_score": 55, "crowding_score": 45}
    return OrchestrationRequest(
        query="请根据给定的宏观及行业评分研判市场环境，给出市场判断、行业观察、主要依据和风险。",
        research_direction="市场解读",
        profile={"user_id": "SYNTHETIC_LATENCY_ONLY", "confirmed": True, "risk_level": "R3"},
        auto_fetch=False, research_mode="deep", facts=[FactRecord(
            fact_id=f"SYNTHETIC-{field}", entity="合成测试市场", field=field, value=value,
            snapshot_time=datetime.now(timezone.utc), source_id="SYNTHETIC_TEST_ONLY", quality=.95,
        ) for field, value in fields.items()],
    )


def complete_sample(body):
    nodes = body.get("task_plan", {}).get("nodes", [])
    calls = body.get("model_calls", [])
    return (body.get("intent") == "market_analysis"
            and body.get("compliance", {}).get("status") == "PASS"
            and len(body.get("evidence", [])) == 9
            and {n["agent_id"] for n in nodes} == {"market", "industry", "fact_verifier", "compliance"}
            and all(n["status"] == "completed" for n in nodes)
            and len(body.get("agent_results", [])) == 2
            and all(r.get("details", {}).get("engine") == "third_party_llm" for r in body["agent_results"])
            and {c["purpose"] for c in calls} == {"RequestUnderstanding", "market", "industry", "OutputReview"}
            and all(c["status"] == "completed" for c in calls))


async def probe(config, payload):
    llm = OpenAICompatibleLLM(config)
    original, enabled = main.coordinator, main.llm_enabled
    agents, _ = make_investment_agents(llm)
    main.coordinator = CoordinatorAgent(agents, verify_facts, basic_compliance_check,
                                        semantic=SemanticService(llm))
    main.llm_enabled = True
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            start = perf_counter()
            response = await client.post("/api/v1/portfolio/analyze", json=payload)
            seconds = perf_counter() - start
        body = response.json()
        return {"model": config.model, "seconds": round(seconds, 4), "http_status": response.status_code,
                "complete": response.status_code == 200 and complete_sample(body),
                "compliance": body.get("compliance", {}).get("status"), "intent": body.get("intent"),
                "evidence_count": len(body.get("evidence", [])), "timings_ms": body.get("timings_ms", {}),
                "calls": body.get("model_calls", []),
                "matched_rules": body.get("compliance", {}).get("matched_rules", []),
                "agent_results": [{"agent_id": r.get("agent_id"), "status": r.get("status"),
                                   "engine": r.get("details", {}).get("engine"),
                                   "facts_used": r.get("facts_used", []),
                                   "data_requirements": r.get("data_requirements", []),
                                   "confidence_reasons": r.get("confidence_reasons", [])}
                                  for r in body.get("agent_results", [])],
                "nodes": body.get("task_plan", {}).get("nodes", [])}
    finally:
        main.coordinator, main.llm_enabled = original, enabled
        await llm.aclose()


def summarize(samples, sla_seconds):
    complete = bool(samples) and all(s["complete"] for s in samples)
    return {"complete": complete, "sla_met": complete and all(s["seconds"] <= sla_seconds for s in samples),
            "median_seconds": statistics.median(s["seconds"] for s in samples) if samples else None,
            "max_seconds": max(s["seconds"] for s in samples) if samples else None}


async def run(args):
    config = LLMConfig.from_env()
    if config is None:
        print("Model is not configured; no requests sent.")
        return 2
    # Baseline must not inherit the locally enabled fast-model override.
    config = replace(config, fast_model=None)
    candidates = [config]
    if args.candidate_model and args.candidate_model != config.model:
        candidates.append(replace(config, model=args.candidate_model,
                                  input_price_per_million=None, output_price_per_million=None))
    payload = synthetic_request().model_copy(update={"research_mode": args.mode}).model_dump(mode="json")
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "sla_seconds": args.sla_seconds,
              "measurement": "complete_asgi_response", "market_provider_used": False,
              "account_storage_used": False, "browser_render_measured": False, "intent_fixed": False,
              "cache_scope": None, "research_mode": args.mode, "samples": [], "summary": {}}
    for _ in range(args.samples):
        for settings in candidates:
            sample = await probe(settings, payload)
            report["samples"].append(sample)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({k: sample[k] for k in ("model", "seconds", "complete", "compliance", "intent")}), flush=True)
    report["summary"] = {c.model: summarize([s for s in report["samples"] if s["model"] == c.model],
                                            args.sla_seconds) for c in candidates}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"]), flush=True)
    return 0 if any(s["sla_met"] for s in report["summary"].values()) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--candidate-model")
    parser.add_argument("--mode", choices=["fast", "deep"], default="deep",
                        help="Deep measures full work; fast exercises the 3-second response deadline.")
    parser.add_argument("--sla-seconds", type=float, default=3)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/performance/consultation_latency_20261006.json")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
