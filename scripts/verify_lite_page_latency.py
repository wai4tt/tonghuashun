"""Measure completed local consultation and warm Streamlit result rendering via AppTest.

This is not a browser/network or authenticated database benchmark.
"""
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from streamlit.testing.v1 import AppTest


def run():
    app = AppTest.from_string('''
import asyncio
import streamlit as st
from backend.app import main
from frontend import streamlit_app as ui
from frontend.result_views import render_advice
from scripts.verify_consultation_latency import synthetic_request
ui.init_session()
req = synthetic_request().model_copy(update={'research_mode': 'lite'})
advice = asyncio.run(main.analyze_portfolio(req, None))
st.session_state.probe_advice = advice.model_dump(mode='json')
render_advice(st.session_state.probe_advice, question=req.query)
''')
    start = perf_counter()
    app.run(timeout=30)
    bootstrap = perf_counter() - start
    if app.exception:
        raise RuntimeError("Initial AppTest rendering failed")
    samples = []
    for _ in range(2):
        start = perf_counter()
        app.run(timeout=15)
        seconds = perf_counter() - start
        body = app.session_state["probe_advice"]
        samples.append({"seconds": round(seconds, 4), "completed_snapshot": bool(
            not app.exception and body["response_scope"] == "snapshot" and len(body["evidence"]) == 9
            and all(n["status"] == "completed" for n in body["task_plan"]["nodes"])
            and any("精简咨询已完成" in c.value for c in app.caption)),
            "evidence_count": len(body["evidence"]), "model_calls": len(body["model_calls"])})
    report = {"transport": "direct_handler_plus_streamlit_apptest", "browser_measured": False,
              "authentication_measured": False, "initial_script_bootstrap_seconds": round(bootstrap, 4),
              "scope": "consultation_on_already_initialized_page", "samples": samples,
              "target_passed": all(s["seconds"] < 3 and s["completed_snapshot"] for s in samples)}
    path = ROOT / "outputs/performance/lite_page_latency_20261006.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["target_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(run())
