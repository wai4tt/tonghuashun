"""不属于某个专业智能体的领域服务。"""

from importlib import import_module

_EXPORTS = {
    "MAX_STORED_EVIDENCE": "history", "facts_as_dicts": "history",
    "summarise_advice": "history", "used_fact_ids_of": "history",
    "assess_profile": "profile", "confirm_profile": "profile",
    "ServiceMetrics": "monitoring", "AutomatedResearchPipeline": "research",
    "derive_scoring_facts": "research",
}


def __getattr__(name):
    """Loading one lightweight helper must not initialize the entire research stack."""
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{_EXPORTS[name]}"), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))

__all__ = [
    "AutomatedResearchPipeline",
    "MAX_STORED_EVIDENCE",
    "ServiceMetrics",
    "assess_profile",
    "confirm_profile",
    "derive_scoring_facts",
    "facts_as_dicts",
    "summarise_advice",
    "used_fact_ids_of",
]
