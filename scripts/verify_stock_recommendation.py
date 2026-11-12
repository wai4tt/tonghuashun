"""验证本地官方技能；--live 使用合成画像运行真实模型及同花顺只读推荐链路。"""
from __future__ import annotations

import argparse
import asyncio
import ast
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def installed_skills():
    lock_path = ROOT / '.agents/skills/.skills_store_lock.json'
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    checks = []
    for slug, meta in lock['skills'].items():
        directory = lock_path.parent / slug
        scripts = sorted((directory / 'scripts').glob('*.py'))
        constants = {}
        for script in scripts:
            for node in ast.parse(script.read_text(encoding='utf-8')).body:
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in {'SKILL_NAME', 'SKILL_VERSION', 'SKILL_ID'}:
                            constants[target.id] = node.value.value
        skill_file = directory / 'SKILL.md'
        digest = hashlib.sha256(skill_file.read_bytes()).hexdigest() if skill_file.is_file() else None
        checks.append({'skill': slug, 'skill_file': skill_file.is_file(),
                       'checksum_ok': digest == meta.get('skill_sha256'),
                       'script_count': len(scripts), 'gateway_version': constants.get('SKILL_VERSION'),
                       'source': meta['zip_url']})
    return checks


async def live_report():
    from dotenv import load_dotenv
    from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
    from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM, make_investment_agents
    from backend.app.data_provider import IwencaiSkillHubProvider
    from backend.app.models import OrchestrationRequest
    from backend.app.semantic import SemanticService
    from backend.app.services.research import AutomatedResearchPipeline
    from backend.app.services.stock_recommendation import StockRecommendationService

    load_dotenv(ROOT / '.env')
    config = LLMConfig.from_env()
    provider = IwencaiSkillHubProvider.from_env()
    if config is None or provider is None:
        if provider:
            await provider.aclose()
        return {'status': 'not_configured', 'model_configured': config is not None,
                'provider_configured': provider is not None}
    llm = OpenAICompatibleLLM(config)
    agents, _ = make_investment_agents(llm)
    semantic = SemanticService(llm)
    coordinator = CoordinatorAgent(agents, verify_facts, basic_compliance_check, semantic=semantic)
    pipeline = AutomatedResearchPipeline(provider)
    service = StockRecommendationService(pipeline, coordinator)
    request = OrchestrationRequest(
        query='推荐两只符合我的投资偏好的股票', research_direction='个股研究',
        profile={'user_id': 'SYNTHETIC_SKILLHUB_SMOKE', 'confirmed': True,
                 'risk_level': 'R3', 'horizon_months': 24, 'max_drawdown': .20,
                 'liquidity_need': '中', 'preferred_product_types': ['权益类']},
    )
    started = perf_counter()
    gateway_attempts = []
    async def observe_attempt(event):
        gateway_attempts.append({'skill': event['skill_id'], 'http_status': event['status_code'],
                                 'status': event['status'], 'fact_count': event['fact_count']})
    provider.call_observer = observe_attempt
    try:
        understanding = await semantic.understand(request)
        print(json.dumps({'intent': understanding.intent.value, 'action': understanding.action,
                          'confidence': understanding.confidence}, ensure_ascii=False), flush=True)
        if not service.should_run(request, understanding):
            return {'status': 'dispatch_failed', 'action': understanding.action}
        prepared, audit, advice = await service.run(request, understanding, progress_sink=lambda stage:
            print(json.dumps({'stage': stage}, ensure_ascii=False), flush=True))
        report = advice.stock_recommendation
        return {'status': 'verified' if report and report.recommendations else 'review',
                'synthetic_profile': True, 'intent': understanding.intent.value,
                'action': understanding.action, 'elapsed_seconds': round(perf_counter() - started, 3),
                'provider': audit.provider, 'fact_count': audit.fetched_fact_count,
                'recovered_metrics': [{'entity': f.entity, 'field': f.field, 'value': f.value,
                    'unit': f.unit, 'period': f.period, 'parent_count': len(f.derived_from)}
                    for f in prepared.facts if f.source_id == 'DERIVED_RECOMMENDATION_HISTORY_V1'],
                'pdf_excerpt_count': sum(f.field == 'announcement_excerpt' for f in prepared.facts),
                'successful_capabilities': audit.successful_capabilities,
                'empty_capabilities': audit.empty_capabilities,
                'failed_capabilities': audit.failed_capabilities,
                'capability_errors': audit.capability_errors,
                'gateway_attempts': gateway_attempts,
                'screening_query': report.screening_query if report else None,
                'recommendation_count': len(report.recommendations) if report else 0,
                'candidates': [{'symbol': c.symbol, 'status': c.status,
                                'missing_fields': c.missing_fields, 'reasons': c.reasons}
                               for c in report.candidates] if report else [],
                'conclusion': advice.conclusion}
    finally:
        await pipeline.cache.aclose()
        await provider.aclose()
        await llm.aclose()


async def main(args):
    checks = installed_skills()
    report = {'time': datetime.now(timezone.utc).isoformat(), 'installed_skills': checks}
    report['installation_ok'] = bool(checks) and all(c['skill_file'] and c['script_count'] and c['checksum_ok'] for c in checks)
    if args.live:
        report['live'] = await live_report()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['installation_ok'] and (not args.live or report['live']['status'] == 'verified') else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='发送真实只读数据及模型请求')
    parser.add_argument('--output', type=Path, default=ROOT / 'deliverables/stock_recommendation_skills.json')
    raise SystemExit(asyncio.run(main(parser.parse_args())))
