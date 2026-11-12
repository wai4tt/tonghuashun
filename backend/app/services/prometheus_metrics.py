"""把进程内指标渲染成 Prometheus 文本格式。

``ServiceMetrics`` 只保存单进程观测值，真正的长期可用性要靠 Prometheus 这类
外部系统抓取、聚合、再算 30 天 SLA。本模块负责把已有快照翻译成标准 exposition
格式（text/plain; version=0.0.4），不新增任何统计口径，也不读取数据库。
"""

from __future__ import annotations

from typing import Any

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
SERVICE_NAME = "wence-zhitou"


def _number(value: Any) -> str | None:
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return None
    if isinstance(value, int):
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return f"{number:.6f}".rstrip("0").rstrip(".")


class _Exposition:
    def __init__(self) -> None:
        self.lines: list[str] = []
        # 同名指标的 HELP/TYPE 只能出现一次，否则抓取端会判为重复元数据。
        self._described: set[str] = set()

    def metric(
        self,
        name: str,
        value: Any,
        *,
        help_text: str | None = None,
        kind: str = "gauge",
        labels: dict[str, Any] | None = None,
    ) -> None:
        rendered = _number(value)
        if rendered is None:
            return
        if help_text and name not in self._described:
            self._described.add(name)
            self.lines.append(f"# HELP {name} {help_text}")
            self.lines.append(f"# TYPE {name} {kind}")
        label_text = ""
        if labels:
            parts = []
            for key, label_value in labels.items():
                safe = str(label_value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
                parts.append(f'{key}="{safe}"')
            label_text = "{" + ",".join(parts) + "}"
        self.lines.append(f"{name}{label_text} {rendered}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _family(exposition: _Exposition, values: dict[str, Any] | None, prefix: str, help_text: str) -> None:
    for key, value in (values or {}).items():
        name = f"{prefix}_{key}"
        exposition.metric(name, value, help_text=f"{help_text} ({key})")


def render_prometheus(
    snapshot: dict[str, Any] | None,
    *,
    session_pool: dict[str, Any] | None = None,
    service: str = SERVICE_NAME,
) -> str:
    """把 ``ServiceMetrics.snapshot()`` 与线程池快照渲染成 exposition 文本。"""

    data = snapshot or {}
    out = _Exposition()
    out.metric(
        "wence_build_info", 1, help_text="服务进程信息；恒为 1，仅用于打标签。",
        kind="gauge", labels={"service": service},
    )
    out.metric("wence_uptime_seconds", data.get("uptime_seconds"),
               help_text="进程启动至今的秒数。", kind="gauge")
    out.metric("wence_requests_total", data.get("request_count"),
               help_text="已完成的 HTTP 请求数（含健康检查）。", kind="counter")
    out.metric("wence_request_failures_total", data.get("failure_count"),
               help_text="返回 5xx 或未完成的 HTTP 请求数。", kind="counter")
    out.metric("wence_observed_success_rate", data.get("observed_success_rate"),
               help_text="单进程观测到的请求成功率；不代表生产 SLA。", kind="gauge")

    for quantile in ("0.5", "0.95", "0.99"):
        key = "p50_latency_ms" if quantile == "0.5" else f"p{int(float(quantile) * 100)}_latency_ms"
        out.metric(
            "wence_http_latency_ms", data.get(key),
            help_text="HTTP 请求耗时分位（毫秒），最后一块响应体完成后计时。",
            kind="gauge", labels={"quantile": quantile},
        )
    out.metric("wence_http_latency_samples", data.get("latency_sample_count"),
               help_text="参与分位计算的样本数。", kind="gauge")

    analysis = data.get("analysis") or {}
    outcomes = analysis.get("outcomes") or {}
    for outcome, count in sorted(outcomes.items()):
        out.metric(
            "wence_analysis_total", count,
            help_text="按合规结论统计的分析次数。",
            kind="counter", labels={"outcome": outcome},
        )
    if not outcomes:
        out.metric("wence_analysis_total", 0, help_text="按合规结论统计的分析次数。",
                   kind="counter", labels={"outcome": "none"})
    out.metric("wence_analysis_failures_total", analysis.get("failure_count"),
               help_text="异常终止的分析次数。", kind="counter")
    out.metric("wence_analysis_completed_total", analysis.get("count"),
               help_text="分析调用总次数。", kind="counter")
    for quantile, key in (("0.5", "p50_latency_ms"), ("0.95", "p95_latency_ms"), ("0.99", "p99_latency_ms")):
        out.metric(
            "wence_analysis_latency_ms", analysis.get(key),
            help_text="组合分析耗时（毫秒），不把流式首包当作最终回答。",
            kind="gauge", labels={"quantile": quantile},
        )
    out.metric("wence_analysis_max_latency_ms", analysis.get("max_latency_ms"),
               help_text="观测到的分析最大耗时（毫秒）。", kind="gauge")
    out.metric("wence_analysis_within_3s_total", analysis.get("within_3_seconds_count"),
               help_text="完整分析在 3 秒内完成且给出结论的累计次数（PASS/REVIEW/BLOCK）。",
               kind="counter")
    for prefix, done_key, ok_key in (
        ("wence_lite_completed_total", "completed_lite_count", "completed_lite_within_3_seconds_count"),
        ("wence_instant_completed_total", "completed_instant_count", "completed_instant_within_3_seconds_count"),
    ):
        out.metric(prefix, analysis.get(done_key),
                   help_text="按范围统计的已完成精简/秒级咨询次数。", kind="counter")
        out.metric(prefix.replace("_completed_total", "_within_3s_total"), analysis.get(ok_key),
                   help_text="其中在 3 秒预算内完成的次数。", kind="counter")

    if session_pool:
        _family(out, {key: value for key, value in session_pool.items()
                      if isinstance(value, (int, float)) and not isinstance(value, bool)},
                "wence_session_pool", "登录会话线程池观测值")
    return out.text()