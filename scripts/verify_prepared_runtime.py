"""Exercise real background preparation and instant requests on one isolated API.

A needs-review report is reported as such; rejecting its delivery is a separate
safety check, never a claim that a complete online report was delivered in 3s.
"""
from datetime import datetime, timezone
import argparse
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

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.app.risk_questionnaire import QUESTIONS, QUESTIONNAIRE_VERSION


def run(output):
    output=output.resolve();output.mkdir(parents=True,exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1",0));port=listener.getsockname()[1]
    base=f"http://127.0.0.1:{port}/api/v1"
    env=os.environ.copy()
    env["WENCE_SESSION_STORE_PATH"]=str(output/"sessions.sqlite3")
    env["WENCE_RESEARCH_CACHE_PATH"]=str(output/"research.sqlite3")
    report={"created_at":datetime.now(timezone.utc).isoformat(),"real_model":True,"real_provider":True,
            "real_database":True,"profile_is_explicit_test_fixture":True,"polls":[],"instant_samples":[],"passed":False}
    def save():(output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    headers=None
    with (output/"server.log").open("w",encoding="utf-8") as log:
        server=subprocess.Popen([sys.executable,"-m","uvicorn","backend.app.main:app","--host","127.0.0.1","--port",str(port),"--log-level","warning"],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
        try:
            with httpx.Client(timeout=15,trust_env=False) as client:
                deadline=perf_counter()+35
                while True:
                    try:
                        response=client.get(base+"/readiness",timeout=.5);response.raise_for_status();break
                    except httpx.HTTPError:
                        if server.poll() is not None or perf_counter()>deadline:raise RuntimeError("QA startup failed") from None
                        sleep(.1)
                config=response.json()
                report["configuration"]={k:config.get(k) for k in ("mysql_ready","third_party_llm_configured","iwencai_skillhub_configured")}
                if not all(report["configuration"].values()):raise RuntimeError("Real dependencies not configured")
                def post(path,payload):
                    response=client.post(base+path,json=payload,headers=headers);response.raise_for_status();return response.json()
                creds={"username":"qa_runtime_"+uuid4().hex[:16],"password":secrets.token_urlsafe(24)}
                session=post("/auth/register",creds)
                headers={"Authorization":"Bearer "+session["access_token"]}
                report["test_account"]=creds["username"]
                answers=dict(zip((q.id for q in QUESTIONS),"ACABDC CACCB BCCBC CDB CADAECDC".replace(" ",""),strict=True))
                profile=post("/profile/confirm",{"profile":{"version":1,"questionnaire_version":QUESTIONNAIRE_VERSION,"risk_answers":answers,"confirmed":False}})
                task={"target":"600519","task":"comprehensive","profile_version":profile["version"]}
                started=perf_counter();previous=None;status={}
                while perf_counter()-started<205:
                    stamp=perf_counter();status=post("/research/prepared",task)
                    report["polls"].append({"elapsed":round(perf_counter()-started,3),"seconds":round(perf_counter()-stamp,4),"status":status["status"]})
                    if status["status"]!=previous:
                        print(json.dumps({"status":status["status"],"elapsed":report["polls"][-1]["elapsed"]}),flush=True);previous=status["status"]
                    board=post("/data/overview",{"direction":"stock","target":"600519","wait":False})
                    facts={f["fact_id"]:f for section in board.get("sections",[]) if section.get("status")=="ok" for f in section.get("facts",[])}
                    stamp=perf_counter()
                    instant=post("/portfolio/analyze",{"query":"请分析600519的估值与风险","research_direction":"个股研究","research_target":"600519","research_mode":"instant","profile":{"version":profile["version"],"confirmed":True},"facts":list(facts.values()),"auto_fetch":False,"preview_only":True})
                    report["instant_samples"].append({"seconds":round(perf_counter()-stamp,4),"response_scope":instant["response_scope"],"evidence_count":len(instant.get("evidence",[])),"model_calls":len(instant.get("model_calls",[]))})
                    save()
                    if status["status"] in {"ready","needs_review","failed"}:break
                    sleep(2)
                report["preparation_seconds"]=round(perf_counter()-started,4)
                report["final_preparation"]=status
                stamp=perf_counter();response=client.post(base+"/research/prepared/open",json={**task,"conversation_id":"qa-runtime-"+uuid4().hex},headers=headers)
                body=response.json()
                report["delivery"]={"seconds":round(perf_counter()-stamp,4),"http_status":response.status_code,"response_scope":body.get("response_scope"),"compliance":body.get("compliance",{}).get("status"),"detail":body.get("detail")}
                report["full_report_delivered"]=response.status_code==200 and body.get("response_scope")=="full" and body.get("compliance",{}).get("status")=="PASS"
                report["unready_delivery_rejected"]=status.get("status")!="ready" and response.status_code==409
                report["instant_target_met"]=bool(report["instant_samples"]) and all(s["seconds"]<3 and s["model_calls"]==0 for s in report["instant_samples"])
                report["passed"]=status.get("status") in {"ready","needs_review"} and report["instant_target_met"] and (report["full_report_delivered"] or report["unready_delivery_rejected"])
                post("/auth/logout",{});headers=None;report["test_session_closed"]=True
        except Exception as exc:
            report["error_type"]=type(exc).__name__
            if isinstance(exc,httpx.HTTPStatusError):report["error_http_status"]=exc.response.status_code
        finally:
            if headers:
                try:
                    with httpx.Client(timeout=2,trust_env=False) as client:report["test_session_closed"]=client.post(base+"/auth/logout",json={},headers=headers).status_code==200
                except httpx.HTTPError:report["test_session_closed"]=False
            server.terminate()
            try:server.wait(timeout=8)
            except subprocess.TimeoutExpired:server.kill();server.wait(timeout=5)
            report["server_stopped"]=server.poll() is not None;save()
    print(json.dumps({k:report.get(k) for k in ("passed","preparation_seconds","full_report_delivered","unready_delivery_rejected","instant_target_met","error_type")}),flush=True)
    return 0 if report["passed"] else 1


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=ROOT/"outputs/testing/20261104-comprehensive/prepared-runtime")
    raise SystemExit(run(parser.parse_args().output))
