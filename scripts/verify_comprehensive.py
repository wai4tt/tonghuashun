"""Repeat full isolated regressions, then optionally real authenticated probes.

Results distinguish test failures, expected business clarification, HTTP failure
and completed evidence review. Each round writes independent logs and JUnit XML.
No credentials/tokens are copied into this report, and no third-party dependency
is installed or upgraded by this script.
"""
from __future__ import annotations
import argparse
import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]


def junit_counts(path):
    cases=list(ET.parse(path).getroot().iter("testcase"))
    failed=sum(case.find("failure") is not None for case in cases)
    errors=sum(case.find("error") is not None for case in cases)
    skipped=sum(case.find("skipped") is not None for case in cases)
    return {"total":len(cases),"passed":len(cases)-failed-errors-skipped,
            "failed":failed,"errors":errors,"skipped":skipped}


def execute(command, log_path, timeout):
    started=perf_counter()
    with log_path.open("w",encoding="utf-8") as log:
        try:
            process=subprocess.run(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=timeout)
            status=process.returncode
        except subprocess.TimeoutExpired:
            status=124
    return {"exit_code":status,"seconds":round(perf_counter()-started,3),"log":log_path.name}


def run(args):
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=True)
    report={"created_at":datetime.now(timezone.utc).isoformat(),"test_rounds":[],"live_rounds":[],"passed":False}
    report_path=output/"comprehensive.json"
    def save():report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    for index in range(1,args.rounds+1):
        xml=output/f"round-{index}.xml"
        row=execute([sys.executable,"-m","pytest","-q","-p","no:cacheprovider",
            "-W","error::RuntimeWarning","-W","error::pytest.PytestUnhandledThreadExceptionWarning",
            "--junitxml="+str(xml)],output/f"round-{index}.log",300)
        row["round"]=index
        if xml.exists():row.update(junit_counts(xml))
        row["passed_round"]=row["exit_code"]==0 and row.get("passed",0)>0 and row.get("failed",0)==row.get("errors",0)==0
        report["test_rounds"].append(row)
        save()
        print(json.dumps({"phase":"full_regression",**row}),flush=True)
    syntax_errors=[]
    syntax_count=0
    for folder in ("backend","frontend","scripts","tests"):
        for path in (ROOT/folder).rglob("*.py"):
            if "__pycache__" in path.parts:continue
            syntax_count+=1
            try:ast.parse(path.read_text(encoding="utf-8-sig"))
            except (SyntaxError,UnicodeError) as exc:syntax_errors.append({"file":str(path.relative_to(ROOT)),"error_type":type(exc).__name__})
    report["syntax"]={"files_checked":syntax_count,"errors":syntax_errors}
    report["dependencies"]=execute([sys.executable,"-m","pip","check"],output/"dependencies.log",60)
    report["diff_check"]=execute(["git","diff","--check"],output/"diff-check.log",30)
    save()
    if args.live:
        for index in range(1,args.live_rounds+1):
            target=output/f"live-round-{index}.json"
            row=execute([sys.executable,str(ROOT/"scripts/verify_instant_research.py"),"--live","--samples","3","--output",str(target)],output/f"live-round-{index}.log",180)
            row["round"]=index
            if target.exists():
                live=json.loads(target.read_text(encoding="utf-8"))
                samples=live.get("samples",[])
                reviewed=[s for s in samples if s.get("response_scope")=="evidence_review"]
                row.update(verified=live.get("passed",False),responses=len(samples),evidence_reviews=len(reviewed),
                    max_response_seconds=live.get("max_response_seconds"),max_review_seconds=live.get("max_completed_review_seconds"),
                    prefetch_seconds=live.get("data_preparation_seconds"),test_session_closed=live.get("test_session_closed",False),
                    http_500_count=sum(s.get("http_status",0)>=500 for s in samples))
            log=target.with_suffix(".server.log")
            lines=log.read_text(encoding="utf-8",errors="replace").splitlines() if log.exists() else []
            markers=("Traceback (most recent call last)","Exception in ASGI application","Task exception was never retrieved","RuntimeWarning:")
            row["uncaught_error_markers"]=sum(any(marker in line for marker in markers) for line in lines)
            row["passed_round"]=row["exit_code"]==0 and row.get("verified",False) and row.get("test_session_closed",False) and row["uncaught_error_markers"]==0
            report["live_rounds"].append(row)
            save()
            print(json.dumps({"phase":"real_authenticated_http",**row}),flush=True)
    report["passed"]=all(row["passed_round"] for row in report["test_rounds"]+report["live_rounds"]) and not syntax_errors and report["dependencies"]["exit_code"]==report["diff_check"]["exit_code"]==0
    save()
    print(json.dumps({"passed":report["passed"],"report":str(report_path)}),flush=True)
    return 0 if report["passed"] else 1


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds",type=int,choices=range(1,6),default=3)
    parser.add_argument("--live",action="store_true")
    parser.add_argument("--live-rounds",type=int,choices=range(1,6),default=3)
    parser.add_argument("--output",type=Path,default=ROOT/"outputs/testing/20261104-comprehensive/final")
    raise SystemExit(run(parser.parse_args()))
