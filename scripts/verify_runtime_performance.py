"""Measure fresh-process imports and optionally real routing latency with a synthetic profile."""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import subprocess
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def imports():
    results = {}
    for module in ("frontend.streamlit_app", "backend.app.main"):
        samples = []
        loaded = []
        for _ in range(3):
            code = ("import time,json,sys; start=time.perf_counter(); import " + module +
                "; print(json.dumps({'seconds':time.perf_counter()-start,'loaded':"
                "[m for m in ('backend.app.services.trade_history','backend.app.models',"
                "'backend.app.services.research','pandas','altair') if m in sys.modules]}))")
            result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                                    text=True, check=True)
            data = json.loads(result.stdout.splitlines()[-1])
            samples.append(round(data["seconds"], 4))
            loaded = data["loaded"]
        results[module] = {"seconds": samples, "median_seconds": round(statistics.median(samples), 4),
                           "loaded_optional_modules": loaded}
    return results


async def routing():
    from dotenv import load_dotenv
    from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM
    from backend.app.models import OrchestrationRequest, UserProfile
    from backend.app.risk_questionnaire import QUESTIONS, evaluate_answers
    from backend.app.semantic import SemanticService
    from backend.app.services.model_telemetry import analysis_telemetry
    load_dotenv(ROOT / ".env")
    config = LLMConfig.from_env()
    if config is None:
        return {"status": "not_configured"}
    answers = dict(zip((q.id for q in QUESTIONS), "ACABDC CACCB BCCBC CDB CADAECDC".replace(" ", ""), strict=True))
    profile = UserProfile(**evaluate_answers(answers), user_id="SYNTHETIC_RUNTIME_ONLY", confirmed=True,
        constraints=["不得使用杠杆"], holding_history=[{"entity": "合成示例", "weight": 0}],
        trading_analysis={"warnings": ["合成测试数据"], "trade_count": 0})
    client = OpenAICompatibleLLM(config)
    collector = {"calls": [], "mode": "fast"}
    token = analysis_telemetry.set(collector)
    try:
        start = perf_counter()
        result = await SemanticService(client).understand(OrchestrationRequest(query="请诊断我的持仓组合",
            profile=profile, research_mode="fast", auto_fetch=False))
        return {"seconds": round(perf_counter() - start, 3), "intent": result.intent,
                "calls": collector["calls"], "synthetic_profile_only": True,
                "market_provider_used": False, "passed": result.intent == "portfolio_review"}
    finally:
        analysis_telemetry.reset(token)
        await client.aclose()


def main(args):
    report = json.loads(args.output.read_text(encoding="utf-8")) if args.output.exists() else {}
    measured = {**report.get(args.phase, {}), "time": datetime.now(timezone.utc).isoformat(), "imports": imports()}
    print(json.dumps({"phase": args.phase, "imports": measured["imports"]}), flush=True)
    report[args.phase] = measured
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.live:
        measured["routing"] = asyncio.run(routing())
        measured["routing"]["measured_at"] = datetime.now(timezone.utc).isoformat()
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"phase": args.phase, "routing": measured["routing"]}, ensure_ascii=True), flush=True)
        return 0 if measured["routing"].get("passed") else 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["before", "after"], required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/performance/runtime_20261005.json")
    raise SystemExit(main(parser.parse_args()))
