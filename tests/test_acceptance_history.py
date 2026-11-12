"""Acceptance must distinguish pending persistence from cross-account history."""
from types import SimpleNamespace

import pytest

from scripts.verify_requirements import histories_match, sample_request, wait_for_histories


def test_full_acceptance_explicitly_selects_deep_and_pending_never_allows_other_users():
    assert sample_request().research_mode == "deep"
    results = [{"status": 200}, {"status": 429}]
    assert histories_match([[], []], results, allow_pending=True)
    assert not histories_match([[], []], results)
    assert not histories_match([[{"id": "acceptance-conversation-1"}], []], results, allow_pending=True)
    assert not histories_match([[], [{"id": "acceptance-conversation-1"}]], results, allow_pending=True)


@pytest.mark.asyncio
async def test_eventual_history_waits_for_write_and_reports_missing_or_wrong_history():
    calls = []
    def delayed(user_id):
        calls.append(user_id)
        return [] if len(calls) == 1 else [{"id": "acceptance-conversation-0"}]
    results = [{"status": 200}]
    histories = await wait_for_histories(SimpleNamespace(list_conversations=delayed), results, timeout=.5)
    assert histories_match(histories, results) and len(calls) >= 2
    for rows in ([], [{"id": "another-account"}]):
        histories = await wait_for_histories(SimpleNamespace(list_conversations=lambda uid: rows), results, timeout=.01)
        assert not histories_match(histories, results)
