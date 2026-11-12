import copy

from streamlit.testing.v1 import AppTest

from frontend.manual_review import review_identity, review_items


def advice():
    return {"trace_id": "test-review", "conclusion": "待核实", "evidence": ["f1"],
            "cross_validation": {"status": "REVIEW", "issues": [{"code": "SOURCE_VALUE_CONFLICT", "message": "同日数值不一致", "fact_ids": ["f1"]}]},
            "compliance": {"status": "REVIEW", "reason": "需补充景气度"},
            "facts": [{"fact_id": "f1", "entity": "广告营销", "field": "pe_ttm", "value": 150.76,
                       "period": "2026-11-10", "source_id": "TEST", "source_url": "https://example.org/report"}]}


def test_review_identity_changes_with_evidence_or_conclusion():
    original = advice()
    changed = copy.deepcopy(original)
    changed["facts"][0]["value"] = 151
    assert review_identity(original) != review_identity(changed)
    changed = {**original, "conclusion": "新判断"}
    assert review_identity(original) != review_identity(changed)
    assert "同日数值不一致" in review_items(original)


def test_manual_review_records_opinion_without_overriding_machine_gate():
    data = advice()
    data["cross_validation"]["issues"][0]["fact_ids"].append("conflict-only")
    data["cross_validation"]["issues"][0]["evidence_details"] = [
        {"fact_id": "conflict-only", "entity": "广告营销", "field": "pe_ttm", "value": 151, "source_id": "SECOND"}]
    script = "import streamlit as st\nfrom frontend.manual_review import render_manual_review\n"
    script += "data = " + repr(data) + "\nrender_manual_review(data, key='test')\nst.session_state.machine_status = data['compliance']['status']\n"
    app = AppTest.from_string(script).run()
    assert not app.exception
    open_key = next(k for k in app.session_state.filtered_state if k.startswith("manual_review_") and k.endswith("_open"))
    app.session_state[open_key] = True
    app.run()
    assert not app.exception
    next(w for w in app.text_input if w.label == "复核人").set_value("测试复核人")
    next(w for w in app.text_area if w.label.startswith("逐项复核说明")).set_value("景气度尚未取得，保留待复核")
    app.session_state[open_key] = True
    app.button[0].click().run()
    assert not app.exception
    records = [v for k, v in app.session_state.filtered_state.items() if k.startswith("manual_review_") and k.endswith("_record")]
    assert len(records) == 1
    assert records[0]["opinion"] == "仍待补证"
    assert {f["资料编号"] for f in records[0]["evidence"]} == {"f1", "conflict-only"}
    assert records[0]["system_compliance"] == "REVIEW"
    assert app.session_state.machine_status == "REVIEW"
    next(w for w in app.selectbox if w.label == "人工复核意见").select("人工核对无异议")
    app.session_state[open_key] = True
    app.button[0].click().run()
    assert app.error
    assert len(app.get("download_button")) == 1

    for checkbox in app.checkbox:
        checkbox.check()
    next(w for w in app.text_area if w.label == "核对的原文链接与统计口径").set_value("https://example.org/report，同日同口径逐项核对")
    app.session_state[open_key] = True
    app.button[0].click().run()
    assert not app.exception and not app.error
    record = next(v for k, v in app.session_state.filtered_state.items() if k.startswith("manual_review_") and k.endswith("_record"))
    assert record["opinion"] == "人工核对无异议"
    assert record["system_compliance"] == app.session_state.machine_status == "REVIEW"
