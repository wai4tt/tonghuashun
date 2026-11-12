"""One authenticated localhost consultation with real configured provider and database.

Creates a clearly named QA account with a fixture questionnaire, never a real
person's assumed profile. No credentials are printed or written to the report.
Browser rendering is not measured. The private normal API is stopped on exit.
"""
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
from backend.app.risk_questionnaire import QUESTIONNAIRE_VERSION, QUESTIONS


def run():
    output = ROOT / "outputs/performance/lite_live_consultation_20261006.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}/api/v1"
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "transport": "localhost_http",
              "normal_api_lifespan": True, "browser_measured": False,
              "profile_is_explicit_test_fixture": True, "market_facts_are_synthetic": False,
              "target": "600519", "timings_seconds": {}, "passed": False,
              "pass_scope": "authenticated_completed_lite_consultation_after_data_loaded"}
    with output.with_suffix(".server.log").open("w", encoding="utf-8") as log:
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host",
            "127.0.0.1", "--port", str(port), "--log-level", "warning"], cwd=ROOT,
            stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        headers = None
        try:
            with httpx.Client(timeout=60, trust_env=False) as client:
                started = perf_counter()
                deadline = started + 35
                while True:
                    if server.poll() is not None:
                        raise RuntimeError("Temporary normal API exited before readiness")
                    try:
                        ready = client.get(base + "/readiness", timeout=.5)
                        ready.raise_for_status()
                        break
                    except httpx.HTTPError:
                        if perf_counter() > deadline:
                            raise RuntimeError("API startup exceeded 35 seconds") from None
                        sleep(.05)
                report["timings_seconds"]["api_startup"] = round(perf_counter() - started, 4)
                readiness = ready.json()
                report["readiness"] = {key: readiness.get(key) for key in
                    ("status", "mode", "mysql_configured", "mysql_ready", "default_consultation_mode")}
                if not readiness.get("mysql_ready"):
                    raise RuntimeError("Configured database is not ready")

                def post(path, payload):
                    response = client.post(base + path, json=payload, headers=headers)
                    response.raise_for_status()
                    return response.json()

                credentials = {"username": "qa3s_" + datetime.now().strftime("%m%d_%H%M%S_") + secrets.token_hex(2),
                               "password": secrets.token_urlsafe(24)}
                registered = post("/auth/register", credentials)
                headers = {"Authorization": "Bearer " + registered["access_token"]}
                report["test_account"] = credentials["username"]
                post("/auth/logout", {})
                headers = None
                started = perf_counter()
                logged_in = post("/auth/login", credentials)
                report["timings_seconds"]["login"] = round(perf_counter() - started, 4)
                headers = {"Authorization": "Bearer " + logged_in["access_token"]}
                questionnaire = dict(zip((q.id for q in QUESTIONS),
                    "ACABDC CACCB BCCBC CDB CADAECDC".replace(" ", ""), strict=True))
                profile = post("/profile/confirm", {"profile": {"questionnaire_version": QUESTIONNAIRE_VERSION,
                    "risk_answers": questionnaire, "version": 1, "confirmed": False}})
                report["profile"] = {key: profile.get(key) for key in
                    ("version", "confirmed", "questionnaire_version", "risk_level", "valid_until")}
                started = perf_counter()
                fetched = post("/data/fetch", {"kind": "quote", "target": "600519", "filters": {}})
                report["timings_seconds"]["load_real_quote"] = round(perf_counter() - started, 4)
                facts = fetched.get("facts", [])
                report["provider"] = {"status": fetched.get("status"), "name": fetched.get("provider"),
                    "fact_count": len(facts), "source_ids": sorted({f["source_id"] for f in facts}),
                    "observation_dates": sorted({str(f["observation_date"]) for f in facts if f.get("observation_date")}),
                    "snapshot_times": sorted({f["snapshot_time"] for f in facts}),
                    "capture_time_is_not_exchange_timestamp": True}
                print(json.dumps({"stage": "real_quote_loaded", **report["provider"],
                                  "seconds": report["timings_seconds"]["load_real_quote"]}, ensure_ascii=False), flush=True)
                if fetched.get("status") != "ok" or not facts:
                    raise RuntimeError("Actual provider did not return usable facts")
                conversation_id = "qa-three-seconds-" + uuid4().hex
                payload = {"query": "请分析贵州茅台（600519）的已有资料概要", "research_direction": "个股研究",
                           "profile": {"version": profile["version"], "confirmed": True}, "facts": facts,
                           "auto_fetch": True, "conversation_id": conversation_id}
                events = []
                started = perf_counter()
                with client.stream("POST", base + "/portfolio/analyze/stream", json=payload, headers=headers) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if line:
                            event = json.loads(line)
                            events.append(event)
                            if event["type"] in {"result", "error"}:
                                finished = perf_counter()
                elapsed = finished - started
                report["timings_seconds"]["authenticated_consultation_to_final_event"] = round(elapsed, 4)
                combined = report["timings_seconds"]["load_real_quote"] + elapsed
                report["timings_seconds"]["sum_separate_fetch_and_consultation"] = round(combined, 4)
                report["first_fetch_plus_consultation_within_3_seconds"] = combined < 3
                final = events[-1]
                advice = final.get("advice", {})
                complete = bool(final.get("type") == "result" and advice.get("response_scope") == "snapshot"
                    and advice.get("evidence") and not advice.get("model_calls")
                    and all(n["status"] == "completed" for n in advice.get("task_plan", {}).get("nodes", [])))
                report["consultation"] = {"completed": complete, "within_3_seconds": elapsed < 3,
                    "event_types": [e["type"] for e in events], "research_mode": advice.get("research_mode"),
                    "response_scope": advice.get("response_scope"), "trace_id": advice.get("trace_id"),
                    "evidence_count": len(advice.get("evidence", [])), "model_calls": len(advice.get("model_calls", [])),
                    "compliance": advice.get("compliance", {}).get("status"),
                    "cross_validation": advice.get("cross_validation", {}).get("status"),
                    "history_status_at_response": advice.get("history_status")}
                report["advice"] = advice
                started = perf_counter()
                persisted = None
                while perf_counter() - started < 5:
                    history = client.get(base + "/history/" + conversation_id, headers=headers)
                    if history.status_code == 200:
                        messages = history.json().get("messages", [])
                        persisted = next((m for m in messages if m["role"] == "assistant"), None)
                        if persisted:
                            break
                    sleep(.05)
                stored_advice = (persisted or {}).get("payload", {})
                report["history"] = {"persisted": bool(persisted),
                    "history_status": stored_advice.get("history_status"),
                    "trace_matches": stored_advice.get("trace_id") == advice.get("trace_id"),
                    "additional_check_seconds": round(perf_counter() - started, 4)}
                report["passed"] = complete and elapsed < 3 and bool(persisted) and report["history"]["trace_matches"]
                post("/auth/logout", {})
                headers = None
        except Exception as exc:
            report["error_type"] = type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError):
                report["error_http_status"] = exc.response.status_code
            print(json.dumps({"passed": False, "error_type": report["error_type"]}), flush=True)
        finally:
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            server.terminate()
            try:
                server.wait(timeout=8)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
    print(json.dumps({key: value for key, value in report.items() if key != "advice"}, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(run())
