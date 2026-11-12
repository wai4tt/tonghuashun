"""Benchmark exact evidence reuse; distinguish primitive latency from end-to-end research."""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.models import FactRecord, OrchestrationRequest
from backend.app.services import disclosure_reader as reader
from backend.app.services.model_telemetry import analysis_telemetry


async def measure(loader):
    start = perf_counter()
    value = await loader()
    return value, round(perf_counter() - start, 6)


async def pdf_check(source=None):
    if source:
        # Current publisher bytes are fetched once, separately from extraction timing.
        data, download_seconds = await measure(lambda: reader._download_pdf(source.source_url))
        scope = "real_official_PDF_same_downloaded_bytes_extraction_only"
    else:
        from reportlab.pdfgen.canvas import Canvas
        data = BytesIO()
        canvas = Canvas(data)
        for page in range(20):
            canvas.drawString(40, 780, f"Synthetic issuer 600001 - Page {page + 1}")
            canvas.drawString(40, 740, "Synthetic evidence for extraction equivalence testing.")
            canvas.showPage()
        canvas.save()
        data, download_seconds = data.getvalue(), 0
        scope = "synthetic_PDF_real_worker_same_bytes_extraction_only"
    await reader.close_document_caches()
    uncached, baseline_seconds = await measure(lambda: reader._extract_pdf(data))
    cold, cold_seconds = await measure(lambda: reader.extract_pdf(data))
    warm, warm_seconds = await measure(lambda: reader.extract_pdf(data))
    return {"scope": scope, "bytes": len(data), "download_seconds": download_seconds,
            "uncached_extraction_seconds": baseline_seconds, "cold_seconds": cold_seconds,
            "warm_seconds": warm_seconds, "same_output": uncached == cold == warm,
            "page_excerpts": len(warm.get("pages", [])), "passed": bool(warm.get("pages")) and uncached == cold == warm}


async def model_check():
    from dotenv import load_dotenv
    from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM
    from backend.app.semantic import SemanticService
    load_dotenv(ROOT / ".env")
    config = LLMConfig.from_env()
    if config is None:
        return {"passed": False, "status": "not_configured"}
    llm = OpenAICompatibleLLM(config)
    semantic = SemanticService(llm)
    # Clearly marked synthetic original text, no investment claim or current-data claim.
    fact = FactRecord(fact_id="SYNTHETIC-EVENT", entity="合成测试公司", field="announcement_excerpt",
        value="合成测试原文：公司已完成股份回购计划。本材料只用于软件验证，不对应真实证券。",
        source_id="SYNTHETIC_SPEED_TEST", snapshot_time=datetime.now(timezone.utc), quality=.9,
        source_url="https://example.test/synthetic-report", period="synthetic")
    request = OrchestrationRequest(query="仅对给定合成原文评估事件，不提供投资建议。",
        profile={"confirmed": True, "risk_level": "R3"}, research_mode="deep", auto_fetch=False)
    targets = [{"entity": fact.entity, "dimension": "event"}]
    runs = []
    outputs = []
    try:
        for name in ("cold", "warm"):
            collector = {"calls": [], "cache_scope": "SYNTHETIC_EXACT_EVIDENCE_BENCHMARK", "mode": "deep"}
            token = analysis_telemetry.set(collector)
            try:
                result, seconds = await measure(lambda: semantic.assess_stock_evidence(request, [fact], targets, []))
                outputs.append(result.model_dump(mode="json") if result else None)
                runs.append({"name": name, "seconds": seconds, "calls": collector["calls"]})
            finally:
                analysis_telemetry.reset(token)
    finally:
        await llm.aclose()
    identical = outputs[0] is not None and outputs[0] == outputs[1]
    return {"scope": "real_model_synthetic_evidence_exact_input_same_account_cache",
            "runs": runs, "same_output": identical,
            "passed": identical and runs[1]["calls"][0].get("cache_hit") is True}


async def run(output, real_model, source_artifact):
    report = {"end_to_end_measured": False, "quality_scope": "exact primitive output equivalence"}
    source = None
    if source_artifact:
        advice = json.loads(source_artifact.read_text(encoding="utf-8"))["advice"]
        ids = {fid for fact in advice["facts"] if fact["source_id"] == "OFFICIAL_DISCLOSURE_PDF_V1"
               for fid in fact.get("derived_from", [])}
        source = next((FactRecord.model_validate(f) for f in advice["facts"]
                       if f["fact_id"] in ids and reader.allowed_disclosure_url(f.get("source_url", ""))), None)
        if source is None:
            raise ValueError("Artifact has no verified official PDF source")
    try:
        report["pdf"] = await pdf_check(source)
        if real_model:
            report["model"] = await model_check()
        report["passed"] = all(item["passed"] for key, item in report.items() if key in {"pdf", "model"})
    finally:
        await reader.close_document_caches()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "deliverables/evidence_reuse.json")
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--source-artifact", type=Path,
                        help="Prior audit result with a verified official PDF source; fetches public PDF bytes")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.output, args.real_model, args.source_artifact)))
