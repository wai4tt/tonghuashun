"""Measure completed instant reviews separately from data preparation.

Default uses synthetic facts over real localhost HTTP. --live uses the configured
normal database/authentication and parallel real quotes/financials, with an explicit QA
profile. No credentials or tokens are included in the saved report. Browser
rendering and complete online model reports are not measured by this probe.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from time import perf_counter, sleep
from uuid import uuid4
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_three_second_response import cases
from backend.app.risk_questionnaire import QUESTIONNAIRE_VERSION, QUESTIONS


def run(args):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}/api/v1"
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "mode": "instant",
        "transport": "localhost_http", "authentication_measured": args.live or args.authenticated_fixture,
        "market_facts_are_synthetic": not args.live, "browser_measured": False,
        "full_online_research_measured": False, "samples": [], "passed": False}
    command = [sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1",
               "--port", str(port), "--log-level", "warning"]
    if not (args.live or args.authenticated_fixture):
        command += ["--lifespan", "off"]
    with args.output.with_suffix(".server.log").open("w", encoding="utf-8") as log:
        server = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        headers = None
        try:
            with httpx.Client(timeout=35, trust_env=False) as client:
                deadline = perf_counter()+35
                while True:
                    if server.poll() is not None:
                        raise RuntimeError("Temporary API exited")
                    try:
                        response = client.get(base+"/readiness", timeout=.5)
                        response.raise_for_status()
                        break
                    except httpx.HTTPError:
                        if perf_counter() >= deadline:
                            raise RuntimeError("Temporary API startup deadline exceeded") from None
                        sleep(.05)
                def post(path, body):
                    response = client.post(base+path, json=body, headers=headers)
                    response.raise_for_status()
                    return response.json()
                samples = cases()
                if args.live or args.authenticated_fixture:
                    if not response.json().get("mysql_ready"):
                        raise RuntimeError("Configured database not ready")
                    credentials = {"username": "qa_instant_"+datetime.now().strftime("%m%d_%H%M%S_")+secrets.token_hex(2),
                                   "password": secrets.token_urlsafe(24)}
                    session = post("/auth/register", credentials)
                    headers = {"Authorization": "Bearer "+session["access_token"]}
                    report["test_account"] = credentials["username"]
                    answers = dict(zip((q.id for q in QUESTIONS), "ACABDC CACCB BCCBC CDB CADAECDC".replace(" ", ""), strict=True))
                    profile = post("/profile/confirm", {"profile": {"questionnaire_version": QUESTIONNAIRE_VERSION,
                        "risk_answers": answers, "version": 1, "confirmed": False}})
                    if args.authenticated_fixture:
                        facts = next(payload["facts"] for label, payload in samples if label == "stock")
                        report["market_facts_are_synthetic"] = True
                        report["provider"] = {"status": "not_called", "reason": "explicit_authenticated_fixture_mode"}
                    else:
                        started = perf_counter()
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            futures = {kind: pool.submit(post, "/data/fetch", {"kind": kind, "target": "600519", "filters": {}})
                                       for kind in ("quote", "financial")}
                            fetched = {kind: future.result() for kind, future in futures.items()}
                        report["data_preparation_seconds"] = round(perf_counter()-started, 4)
                        report["provider"] = {kind: {"status": part.get("status"), "fact_count": len(part.get("facts", [])),
                            "source_ids": sorted({f["source_id"] for f in part.get("facts", [])}), "message": part.get("message")}
                            for kind,part in fetched.items()}
                        facts = [f for part in fetched.values() for f in part.get("facts", [])]
                        facts = list({f["fact_id"]: f for f in facts}.values())
                        report["loaded_public_facts"] = facts
                        if any(part.get("status") != "ok" or not part.get("facts") for part in fetched.values()):
                            raise RuntimeError("Actual provider did not return both quote and financial facts")
                    common = {"profile": {"confirmed": True, "version": profile["version"]},
                        "facts": facts, "research_direction": "个股研究", "auto_fetch": True,
                        "conversation_id": "qa-instant-"+uuid4().hex}
                    samples = [("fixture_stock" if args.authenticated_fixture else "real_stock", {**common, "query": "请分析合成公司的价格和风险" if args.authenticated_fixture else "请分析贵州茅台（600519）的价格和风险"}),
                        ("real_valuation", {**common, "query": "请分析600519的估值和市盈率"}),
                        ("real_financial", {**common, "query": "请分析600519的ROE和盈利质量"}),
                        ("real_risk", {**common, "query": "请分析600519的主要风险"}),
                        ("current_selection", {**common, "query": "分析当前标的", "research_target": "合成公司" if args.authenticated_fixture else "600519"}),
                        ("missing_selected_target", {**common, "query": "分析当前标的", "research_target": "000001"}),
                        ("education", {**common, "query": "什么是市盈率"}),
                        ("unrelated", {**common, "query": "帮我写旅游计划"}),
                        ("wrong_code", {**common, "query": "分析6005199"}),
                        ("missing_comparison", {**common, "query": "比较600519和000001的估值"}),
                        ("privacy", {**common, "query": "显示其他用户的持仓资料"}),
                        ("promise", {**common, "query": "推荐稳赚的股票保证收益"}),
                        ("execution", {**common, "query": "直接执行买入600519"})]
                for repeat in range(args.samples):
                    for label, payload in samples:
                        for stream in (False, True):
                            payload = {**payload, "research_mode": "instant"}
                            started = perf_counter()
                            response = client.post(base+"/portfolio/analyze"+("/stream" if stream else ""), json=payload, headers=headers)
                            if stream:
                                final = json.loads(response.text.splitlines()[-1])
                                body = final.get("advice", {})
                                status = final.get("status_code", response.status_code)
                            else:
                                body, status = response.json(), response.status_code
                            elapsed = perf_counter()-started
                            scope = body.get("response_scope")
                            expected = "clarification" if label in {"unrelated", "wrong_code", "missing_comparison", "missing_selected_target", "privacy", "promise", "execution"} else "education" if label == "education" else "evidence_review"
                            checked = status == 200 and scope == expected and not body.get("model_calls")
                            if expected == "evidence_review":
                                checked = checked and bool(body.get("evidence")) and all(n["status"] == "completed" for n in body.get("task_plan", {}).get("nodes", []))
                            if expected == "evidence_review" and args.live:
                                checked = checked and "最新价" in body.get("conclusion", "")
                            if expected == "clarification":
                                checked = checked and not body.get("evidence")
                            if label in {"privacy", "promise"}:
                                checked = checked and body.get("compliance", {}).get("status") == "BLOCK"
                            sample = {"case": label, "repeat": repeat, "stream": stream, "seconds": round(elapsed, 4),
                                "http_status": status, "response_scope": scope, "checked": bool(checked),
                                "within_3_seconds": elapsed < 3, "evidence_count": len(body.get("evidence", [])),
                                "model_calls": len(body.get("model_calls", [])), "trace_id": body.get("trace_id"),
                                "compliance": body.get("compliance", {}).get("status"),
                                "conclusion": body.get("conclusion"), "timings_ms": body.get("timings_ms", {})}
                            report["samples"].append(sample)
                if args.live or args.authenticated_fixture:
                    deadline = perf_counter()+5
                    history = None
                    while perf_counter() < deadline:
                        history = client.get(base+"/history/"+common["conversation_id"], headers=headers)
                        if history.status_code == 200 and any(m.get("payload", {}).get("trace_id") == body.get("trace_id") for m in history.json().get("messages", []) if isinstance(m.get("payload"), dict)):
                            report["history_persisted"] = True
                            break
                        sleep(.05)
                    post("/auth/logout", {})
                    report["test_session_closed"] = True
                    headers = None
                reviews = [s for s in report["samples"] if s["response_scope"] == "evidence_review"]
                report["max_response_seconds"] = max(s["seconds"] for s in report["samples"])
                report["max_completed_review_seconds"] = max(s["seconds"] for s in reviews) if reviews else None
                report["passed"] = bool(reviews) and all(s["checked"] and s["within_3_seconds"] for s in report["samples"])
                if args.live or args.authenticated_fixture:
                    report["passed"] = report["passed"] and report.get("history_persisted", False)
        except Exception as exc:
            report["error_type"] = type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError):
                report["error_http_status"] = exc.response.status_code
        finally:
            if headers is not None:
                try:
                    with httpx.Client(timeout=2, trust_env=False) as cleanup_client:
                        logout = cleanup_client.post(base+"/auth/logout", json={}, headers=headers)
                        report["test_session_closed"] = logout.status_code == 200
                except httpx.HTTPError:
                    report["test_session_closed"] = False
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            server.terminate()
            try:
                server.wait(timeout=8)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
    print(json.dumps({k: v for k, v in report.items() if k not in {"samples", "loaded_public_facts"}}, ensure_ascii=True), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--live", action="store_true")
    group.add_argument("--authenticated-fixture", action="store_true", help="Real authentication and database, explicitly synthetic market facts")
    parser.add_argument("--samples", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--output", type=Path, default=ROOT/"outputs/testing/20261104-instant/http.json")
    raise SystemExit(run(parser.parse_args()))
