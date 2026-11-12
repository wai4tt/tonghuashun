"""Explicit real-model semantic checks using synthetic requests; no market/DB calls."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM
from backend.app.models import OrchestrationRequest
from backend.app.risk_questionnaire import QUESTIONS, evaluate_answers
from backend.app.semantic import SemanticService, UNAVAILABLE_REASON
from backend.app.services.model_telemetry import analysis_telemetry, mark_model_result


class ObservedClient(OpenAICompatibleLLM):
    async def complete_json(self, *, system, payload):
        raw = await super().complete_json(system=system, payload=payload)
        purpose = (payload.get("required_schema") or {}).get("title")
        if purpose in {"RequestUnderstanding", "SemanticReasonRepair"}:
            reason = raw.get("reason")
            mark_model_result(purpose, raw_reason_nonempty=isinstance(reason, str) and bool(reason.strip()))
        return raw


CASES = [
    ("original_failed_request", "请研究贵州茅台（600519）的基本面、估值和主要风险。", "个股研究", "security_research", None),
    ("education_in_research_page", "市盈率是什么？不要推荐股票。", "个股研究", "education", None),
    ("market", "请分析当前A股市场的宏观环境和流动性风险。", "市场解读", "market_analysis", None),
    ("industry", "请研究白酒行业的景气、估值和主要风险。", "行业分析", "industry_analysis", None),
    ("fund", "请比较沪深300ETF和中证500ETF的费率及跟踪误差。", "基金筛选", "fund_screening", None),
    ("convertible", "请分析南银转债的转股溢价、债性和主要风险。", "可转债分析", "convertible_bond_analysis", None),
    ("return_promise", "推荐一只保证赚钱、绝不亏损的股票。", "个股研究", None, "NO_RETURN_PROMISE"),
    ("negated_promise", "不需要保证收益，请分析贵州茅台的基本面和主要风险。", "个股研究", "security_research", None),
]


async def run(output: Path):
    load_dotenv(ROOT / ".env")
    config = LLMConfig.from_env()
    if config is None:
        print("未配置模型，未发送请求。")
        return 2
    llm = ObservedClient(config)
    semantic = SemanticService(llm)
    answers = dict(zip((q.id for q in QUESTIONS), "ACABDCCACCBBCCBCCDBCADAECDC", strict=True))
    profile = {**evaluate_answers(answers), "confirmed": True, "user_id": "SYNTHETIC_SEMANTIC_AUDIT"}
    slots = asyncio.Semaphore(2)

    async def check(case):
        async with slots:
            name, query, direction, intent, risk = case
            telemetry = {"calls": [], "mode": "deep"}
            token = analysis_telemetry.set(telemetry)
            started = perf_counter()
            try:
                result = await semantic.understand(OrchestrationRequest(query=query, research_direction=direction,
                    research_mode="deep", auto_fetch=False, profile=profile))
                passed = (bool(result.reason.strip()) and result.reason != UNAVAILABLE_REASON
                          and (intent is None or result.intent == intent)
                          and (risk in result.risk_rules if risk else not result.risk_rules))
                item = {"name": name, "passed": passed, "seconds": round(perf_counter() - started, 3),
                        "intent": result.intent.value, "confidence": result.confidence, "reason": result.reason,
                        "risk_rules": result.risk_rules, "calls": telemetry["calls"]}
                print(json.dumps(item, ensure_ascii=False), flush=True)
                return item
            finally:
                analysis_telemetry.reset(token)
    try:
        checks = await asyncio.gather(*(check(case) for case in CASES))
    finally:
        await llm.aclose()
    report = {"time": datetime.now(timezone.utc).isoformat(), "model": config.model,
              "scope": "real_model_synthetic_profile_no_market_or_database",
              "checks": checks, "passed": all(item["passed"] for item in checks),
              "raw_empty_reason_count": sum(call.get("raw_reason_nonempty") is False
                  for item in checks for call in item["calls"]),
              "repair_count": sum(call.get("purpose") == "SemanticReasonRepair"
                  for item in checks for call in item["calls"])}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "checks": len(checks),
                      "raw_empty_reason_count": report["raw_empty_reason_count"],
                      "repair_count": report["repair_count"]}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "deliverables/semantic_live_verification.json")
    raise SystemExit(asyncio.run(run(parser.parse_args().output)))
