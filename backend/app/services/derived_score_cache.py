"""Account-scoped memo of deterministic derivations, preserving computation time."""
from collections import OrderedDict
import hashlib
import json
from time import monotonic

from backend.app.fact_taxonomy import fact_is_current
from backend.app.services.model_telemetry import analysis_telemetry


class DerivedScoreCache:
    def __init__(self, max_entries=256):
        self.max_entries = max_entries
        self.entries = OrderedDict()
        self.inputs = OrderedDict()

    def derive(self, facts, now, calculate):
        collector = analysis_telemetry.get()
        scope = collector.get("cache_scope") if collector else None
        if scope is None:
            return calculate(facts, now=now)
        # Look up the full input BEFORE calculating. Include current/expired
        # state so a time boundary cannot revive an obsolete calculation.
        input_key = hashlib.sha256(json.dumps([
            scope, calculate.__module__, calculate.__qualname__,
            [(f.model_dump(mode="json"), fact_is_current(f, now)) for f in facts],
        ], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        cached_input = self.inputs.get(input_key)
        if cached_input:
            stamp, output = cached_input
            if monotonic() - stamp < 60 and all(fact_is_current(f, now) for f in output):
                self.inputs.move_to_end(input_key)
                return [f.model_copy(deep=True) for f in output]
            self.inputs.pop(input_key, None)
        output = calculate(facts, now=now)
        by_id = {f.fact_id: f for f in facts}
        results = []
        for score in output:
            # Calculate first to find the actual inputs. Unused old records do
            # not change computation time; all original records still reach
            # the verifier and its conflict checks.
            parents = [by_id[parent].model_dump(mode="json") for parent in sorted(score.derived_from)]
            key = hashlib.sha256(json.dumps([scope, score.entity, score.field, score.value,
                score.derivation_rule, parents], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            cached = self.entries.get(key)
            if cached and fact_is_current(cached, now):
                self.entries.move_to_end(key)
                results.append(cached.model_copy(deep=True))
            else:
                self.entries[key] = score.model_copy(deep=True)
                results.append(score)
            while len(self.entries) > self.max_entries:
                self.entries.popitem(last=False)
        if results:
            self.inputs[input_key] = (monotonic(), [f.model_copy(deep=True) for f in results])
            while len(self.inputs) > self.max_entries:
                self.inputs.popitem(last=False)
        return results
