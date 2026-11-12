"""人工复核工作单；独立于机器审核，不改写建议或审核状态。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import streamlit as st

CHECKS = (
    "已核对行业名称、分类体系和板块代码，避免不同层级行业混用",
    "已核对指标单位、统计期和行情时点；缺失值未按零处理，亏损行业的负市盈率未当作低估",
    "已通过公告、交易所或统计发布原文核对关键事实；同源转载和派生评分未算作独立来源",
    "已逐项检查证据缺口、观点分歧和适用条件，记录无法确认的内容",
)


def review_identity(advice: dict) -> str:
    # 同一 trace 下预览更新、资料变化或结论变化后，旧复核记录均不可套用。
    raw = json.dumps(advice, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def review_items(advice: dict) -> list[str]:
    from frontend.answer_visibility import verification_notes
    items = [issue.get("message") for issue in (advice.get("cross_validation") or {}).get("issues", [])]
    items += verification_notes(advice)
    reason = (advice.get("compliance") or {}).get("reason")
    if reason:
        items.append(reason)
    for result in advice.get("agent_results", []):
        for requirement in result.get("data_requirements", []):
            items.append("待补充资料：" + (requirement if isinstance(requirement, str) else json.dumps(requirement, ensure_ascii=False)))
    return list(dict.fromkeys(str(item) for item in items if item))


def render_manual_review(advice: dict, *, key: str) -> None:
    from frontend.presentation import advice_facts, plain_language
    facts = advice_facts(advice)
    digest = review_identity({**advice, "facts": facts})
    user = st.session_state.get("auth_user") or {}
    prefix = f"manual_review_{user.get('id', user.get('username', 'local'))}_{key}_{digest}"
    with st.expander("交叉检验与人工复核工作单", icon=":material/fact_check:",
                     key=prefix + "_open", on_change="rerun") as panel:
        if not panel.open:
            return
        st.write("系统会自动检查引用、同项数值与分项观点。人工复核需要打开原始资料，核对日期、口径及关键判断。")
        st.caption("复核意见仅保留在当前登录会话，可下载留存；不会修改系统审核状态，也不会解除风险拦截。重新分析后请复核新结果。")
        with st.expander("操作步骤与填写示例", icon=":material/help_outline:"):
            from frontend.review_guide import render_review_guide
            render_review_guide()
        items = review_items(advice)
        st.markdown("**本次待核实事项**")
        for index, item in enumerate(items, 1):
            st.write(f"{index}. {plain_language(item)}")
        if not items:
            st.caption("未列出具体问题，仍需核对下面的证据与检查清单。")
        # 被建议引用排除的冲突事实仍是复核依据，保留核验器保存的原始数值。
        issues = (advice.get("cross_validation") or {}).get("issues", [])
        merged = {f.get("fact_id"): f for f in facts}
        for issue in issues:
            for detail in issue.get("evidence_details", []):
                if detail.get("fact_id"):
                    merged.setdefault(detail["fact_id"], detail)
        facts = list(merged.values())
        relevant = set(advice.get("evidence") or []) | {fid for issue in issues for fid in issue.get("fact_ids", [])}
        by_id = {f.get("fact_id"): f for f in facts}
        pending = list(relevant)
        while pending:
            for parent in (by_id.get(pending.pop(), {}).get("derived_from") or []):
                if parent not in relevant:
                    relevant.add(parent)
                    pending.append(parent)
        rows = [{"资料编号": f.get("fact_id"), "对象": f.get("entity"),
                 "指标": f.get("source_field") or f.get("field"), "数值": str(f.get("value", "")),
                 "单位": f.get("unit") or "未提供", "统计期": f.get("period") or "未提供",
                 "采集时间": str(f.get("snapshot_time") or "未提供"), "来源": f.get("source_id"),
                 "原文": f.get("source_url"), "派生依据": "、".join(f.get("derived_from") or [])}
                for f in facts if f.get("fact_id") in relevant]
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch",
                         column_config={"原文": st.column_config.LinkColumn("原文", display_text="查看原文")})
        else:
            st.warning("当前没有可展示的相关原始资料，需先补齐证据。")
        st.info("建议顺序：补齐缺项 → 对照独立原文 → 核对同日同口径数据 → 记录分歧 → 刷新资料并重新分析。多名智能体引用同一份数据不等于多来源验证。")
        with st.form(prefix + "_form"):
            checked = [st.checkbox(label, key=f"{prefix}_check_{i}") for i, label in enumerate(CHECKS)]
            reviewer = st.text_input("复核人", max_chars=80, key=prefix + "_reviewer")
            decision = st.selectbox("人工复核意见", ["仍待补证", "存在异议", "人工核对无异议"], key=prefix + "_decision")
            references = st.text_area("核对的原文链接与统计口径", key=prefix + "_references",
                                      placeholder="填写实际核对的原文链接、发布日期、指标统计期、单位及行业分类口径。")
            notes = st.text_area("逐项复核说明（含缺失项与分歧处理）", key=prefix + "_notes",
                                 placeholder="逐项记录：已核实什么、仍缺什么、冲突如何处理，以及为何选择本次复核意见。")
            submitted = st.form_submit_button("保存本次复核记录")
        if submitted:
            if not reviewer.strip() or not notes.strip():
                st.error("请填写复核人和逐项复核说明。")
            elif decision == "人工核对无异议" and (not all(checked) or not references.strip() or not rows):
                st.error("记录无异议前，请完成全部检查、填写原文依据，并补齐可核对资料。")
            else:
                st.session_state[prefix + "_record"] = {
                    "trace_id": advice.get("trace_id"), "advice_sha256": digest,
                    "reviewed_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
                    "reviewer": reviewer.strip(), "opinion": decision, "references": references.strip(),
                    "notes": notes.strip(), "checks": dict(zip(CHECKS, checked)), "pending_items": items,
                    "system_cross_validation": (advice.get("cross_validation") or {}).get("status"),
                    "system_compliance": (advice.get("compliance") or {}).get("status"),
                    "evidence": rows,
                }
                st.success("复核意见已保存在当前会话。请下载留存；系统审核状态保持原值。")
        record = st.session_state.get(prefix + "_record")
        if record:
            st.caption(f"已记录：{record['reviewer']} · {record['opinion']} · {record['reviewed_at']}")
            st.download_button("下载人工复核记录", json.dumps(record, ensure_ascii=False, indent=2),
                               file_name=f"人工复核-{digest[:12]}.json", mime="application/json", key=prefix + "_download")
