"""Real localhost HTTP completed-lite probe, including the final stream event.

Starts a private temporary API with lifespan disabled (no database writes),
sends only synthetic evidence, and stops only the process it created.
Requires a useful completed answer; timeouts and clarifications do not pass.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from time import perf_counter, sleep

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_consultation_latency import synthetic_request


def cases():
    market = synthetic_request().model_dump(mode="json")
    market.pop("research_mode")  # Prove the public API default.
    base = {"profile": market["profile"], "auto_fetch": True}
    def fact(fid, entity, field, value, unit=None):
        return {"fact_id": fid, "entity": entity, "field": field, "value": value, "unit": unit,
                "source_id": "SYNTHETIC_TEST_ONLY", "quality": .95,
                "snapshot_time": datetime.now(timezone.utc).isoformat()}
    return [
        ("market", market),
        ("industry", {**market, "research_direction": "行业分析", "query": "已有资料概览"}),
        ("stock", {**base, "research_direction": "个股研究", "query": "请分析合成公司", "facts": [
            fact("price", "合成公司", "close_price", 12.5, "元"),
            fact("roe", "合成公司", "roe", .12, "ratio")]}),
        ("fund", {**base, "research_direction": "基金筛选", "query": "请分析合成基金", "facts": [
            fact("fee", "合成基金", "fee_rate", .005, "ratio")]}),
        ("convertible", {**base, "research_direction": "可转债分析", "query": "请分析合成转债", "facts": [
            fact("bond-price", "合成转债", "bond_price", 110, "元")]}),
        ("portfolio", {**base, "query": "请诊断我的持仓组合", "portfolio": [
            {"name": "合成甲", "weight": .7}, {"name": "合成乙", "weight": .3}]}),
        ("education", {**base, "query": "市盈率是什么"}),
    ]


def completed_lite(body):
    nodes = body.get("task_plan", {}).get("nodes", [])
    scope = body.get("response_scope")
    return (body.get("research_mode") == "lite" and scope in {"snapshot", "education"}
            and bool(body.get("conclusion")) and bool(body.get("scope_notice"))
            and bool(nodes) and all(node["status"] == "completed" for node in nodes)
            and not body.get("model_calls") and not body.get("allocation")
            and not body.get("return_expectation", {}).get("scenarios")
            and body.get("compliance", {}).get("status") == "REVIEW"
            and (scope == "education" or (bool(body.get("evidence"))
                 and body.get("cross_validation", {}).get("status") == "PASS"
                 and all(r.get("details", {}).get("engine") == "local_rules"
                         for r in body.get("agent_results", [])))))


def run(args):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    log_path = args.output.with_suffix(".server.log")
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "response_target_seconds": 3,
              "transport": "localhost_http", "real_model_used": False, "market_provider_used": False,
              "account_storage_used": False, "browser_render_measured": False, "samples": []}
    with log_path.open("w", encoding="utf-8") as log:
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.app.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--lifespan", "off", "--log-level", "warning"],
            cwd=ROOT, stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            with httpx.Client(timeout=10, trust_env=False) as client:
                deadline = perf_counter() + 15
                while True:
                    if server.poll() is not None:
                        raise RuntimeError("Temporary API exited before readiness")
                    try:
                        ready = client.get(f"{base}/api/v1/readiness", timeout=.5)
                        ready.raise_for_status()
                        break
                    except httpx.HTTPError:
                        if perf_counter() >= deadline:
                            raise RuntimeError("Temporary API was not ready in time") from None
                        sleep(.05)
                config = ready.json()
                report["configuration"] = {"mode": config["mode"],
                    "fast_response_timeout_seconds": config["fast_response_timeout_seconds"],
                    "deep_timeout_seconds": config["analysis_timeout_seconds"]}
                for case, payload in cases() * args.samples:
                    for stream in (False, True):
                        start = perf_counter()
                        response = client.post(f"{base}/api/v1/portfolio/analyze" + ("/stream" if stream else ""),
                                               json=payload)
                        if stream:
                            event = json.loads(response.text.splitlines()[-1])
                            body = event.get("advice") or {}
                            status = event.get("status_code", response.status_code)
                            notice = event.get("message", "")
                        else:
                            body = response.json()
                            status = response.status_code
                            notice = body.get("detail", "")
                        elapsed = perf_counter() - start
                        complete = status == 200 and completed_lite(body)
                        sample = {"case": case, "stream": stream, "seconds": round(elapsed, 4), "status": status,
                                  "response_within_3_seconds": elapsed < 3,
                                  "completed_lite_consultation": complete, "response_scope": body.get("response_scope"),
                                  "evidence_count": len(body.get("evidence", [])),
                                  "model_calls": len(body.get("model_calls", [])),
                                  "complete_full_research": False}
                        report["samples"].append(sample)
                        print(json.dumps(sample), flush=True)
            report["completed_lite_target_passed"] = all(s["response_within_3_seconds"]
                and s["completed_lite_consultation"] for s in report["samples"])
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            return 0 if report["completed_lite_target_passed"] else 1
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/performance/lite_consultation_20261006.json")
    raise SystemExit(run(parser.parse_args()))
