"""Render the server's evidence dossier and reuse identical text in exports."""
import streamlit as st
from frontend.presentation import plain_language, fact_source

STANCES = {"favorable": "支持因素", "caution": "不利因素", "neutral": "中性与待核实"}


def report_sections(advice):
    if (advice.get("compliance") or {}).get("status") == "BLOCK":
        return []
    report = advice.get("research_report") or {}
    sections = []
    if not report:
        return sections
    sections.append(("研究证据对照", [report.get("summary"), report.get("method"), *report.get("limitations", [])]))
    for section in report.get("sections", []):
        lines = []
        for item in section.get("evidence", []):
            lines.append(f"{STANCES.get(item.get('stance'), '待核实')} · {item.get('observation', '')}"
                         f"（{item.get('period') or '统计期未注明'}）\n{item.get('interpretation', '')}"
                         f"\n来源：{fact_source({'source_id': item.get('source')})}")
        lines.extend("判断限制：" + value for value in section.get("limitations", []))
        lines.extend("重新评估条件：" + value for value in section.get("change_conditions", []))
        sections.append((section.get("entity", "") + " · " + section.get("title", ""), lines))
    return sections


def render_research_report(advice, *, key, compact=False):
    report = advice.get("research_report") or {}
    if not report or not report_sections(advice):
        return
    st.markdown("**研究证据对照**")
    st.write(plain_language(report.get("summary")))
    st.caption(plain_language(report.get("method")))
    for limitation in report.get("limitations", []):
        st.caption(plain_language(limitation))
    with st.expander("展开支持因素、不利因素与重新评估条件", key=f"research_report_{key}",
                     expanded=not compact, on_change="rerun") as panel:
        if panel.open:
            for title, lines in report_sections(advice)[1:]:
                with st.container(border=True):
                    st.markdown("**" + plain_language(title).replace("*", "") + "**")
                    for line in lines:
                        st.write(plain_language(line))
