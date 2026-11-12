# 2026-10-22 优化说明：能力补齐、延迟与观测

本轮优化的目标是：在**不大改前端**的前提下，按图片里的15项技术要求与任务清单，把能落地的差距补上——
组合配置的算术层、数值级幻觉检测、登录防护、Prometheus 观测、意图识别与取数重叠——并把改动做成可自检、
可回滚、不破坏既有419项测试的形态。

对照依据：`docs/technical-requirements.md`、`docs/technical-requirements-recheck-20261002.md`
（上一轮核查结论：11项已实现，4项未达标/未验证）。

## 1. 本轮新增与修改

### 1.1 新增模块

| 文件 | 作用 | 说明 |
|---|---|---|
| `backend/app/services/portfolio_optimizer.py` | 组合配置算术层 | 风险平价、逆波动、等权三种候选；单票/行业上限约束投影；相关性矩阵、有效持仓数、HHI、风险贡献、行业暴露、再平衡与换手率。纯标准库，无模型调用。 |
| `backend/app/services/numeric_grounding.py` | 数值级幻觉检测 | 从回答文本抽取百分比/倍数/金额/评分，与所引 `FactRecord` 数值对账，输出 `risk=none/low/medium/high/unverifiable` 与无出处数字清单。 |
| `backend/app/services/prometheus_metrics.py` | 观测出口 | 把 `ServiceMetrics.snapshot()` 渲染为 Prometheus 文本（`wence_*` 指标族），含 3 秒内完成数、模式分布、会话池水位。 |
| `backend/app/services/login_guard.py` | 登录失败限流 | 账号+来源IP 维度计数，窗口/锁定/容量均可配，`Retry-After` 可直接回给客户端。 |

### 1.2 新增接口

| 接口 | 用途 |
|---|---|
| `POST /api/v1/portfolio/plan` | 组合诊断与再平衡。入参不足一条持仓时 422；缺少历史序列的标的按给定波动率或等权处理，并在 `warnings` 说明。 |
| `POST /api/v1/research/grounding-check` | 任意文字 + 事实组的数值对账，可单独调用，也被深度分析复用。 |
| `GET /api/v1/metrics/prometheus` | 与 `GET /api/v1/metrics` 同源，文本格式，供 Prometheus 抓取。 |

### 1.3 修改点

- `POST /api/v1/auth/login`：连续失败达阈值返回 `429` + `Retry-After`；成功即清零。默认 5 次/10 分钟、锁 60 秒。
- `POST /api/v1/portfolio/analyze`（deep）：当请求同时带 `research_direction` 与 `research_target` 时，
  取数与意图识别并行启动（`_start_prefetch`/`_take_prefetch`）；识别结果与方向一致则复用，不一致则丢弃重取。
  `WENCE_SPECULATIVE_PREFETCH=off` 可关闭。预取失败一律退回原串行路径，不影响结论。
- `AdvicePackage` 新增 `numeric_grounding` 字段（默认 `None`），深度分析返回时自动填数值对账结果。
- `GET /api/v1/metrics` 增加 `login_throttle` 段。

**约束可行性口径**：`portfolio/plan` 返回的 `constraint_violations.relaxed` 是"上限本身不可行"的披露位。
例如 3 条持仓配 20% 单票上限（满仓需 33.3%）时，系统放宽到最小可行上限并写进 `warnings`，
而不是把权重归一化后悄悄越过上限、或留下无法解释的空仓；没有填写行业归属时不执行行业上限。

### 1.4 测试与自检

- `tests/test_optimizations_20261022.py`：13 个用例，覆盖约束投影、风险平价/逆波动、相关性、序列清洗、
  数值对账（含单位换算与小比例）、Prometheus 格式、登录限流锁定与过期、三个新接口、预取复用/丢弃。
- `scripts/verify_optimizations.py`：一键跑完整流程（健康→深度分析→组合诊断→数值对账→观测→登录限流→并发突发），
  固定语义夹具 + 内存 SQLite，不出网。`--burst/--timeout/--json-out` 可调。
  自检会把精简闸门临时放宽到突发数（线上仍是 16 + 0.05s 排队），目的是观察整条链路而不是排队策略；
  429 由登录限流用例单独覆盖。

## 2. 对15项要求的影响（诚实口径）

| 项 | 本轮改变 | 现在能否宣布达标 |
|---|---|---|
| 1 第三方大模型主题研判 | 无改动 | 已实现（前提：配置真实模型） |
| 2 市场解读/行业追踪/个股推荐 | 组合配置从"给区间"升级为"给可复现权重" | 机制已实现；定时推送仍未做 |
| 3 可解释输出与风险提示 | 数值对账把"每个数字有没有出处"变成可核验字段 | 已实现，且新增数值级校验 |
| 4 多智能体协同 | 无改动 | 已实现（固定编排，非通用DAG引擎） |
| 5 交叉验证与一致性 | 新增"数字 vs 事实"这一层一致性 | 已实现有限检测 |
| 6 多任务并发与聚合 | 意图识别与取数重叠，少一次串行等待 | 已实现 |
| 7 多源接入 | 无改动 | 代码已接入；行情仍受上游401限制 |
| 8 溯源与幻觉检测 | **由"引用链可追溯"扩展到"数字级对账"** | 增强；仍不能证明每句自然语言真实 |
| 9 核心数据秒级延迟 | 预取重叠压缩关键路径 | 仍**未**达标：无上游产生时间戳的可测链路 |
| 10 100人并发咨询 | 无改动 | 仍**未**达标：上一轮100突发仅84完成 |
| 11 完整响应≤3秒 | 预取重叠 + 组合诊断零模型调用 | 仍**未**达标：真模型完整分析 4.5 秒级 |
| 12 可用性≥99.9% | Prometheus 出口让长周期观测可落地 | 仍**未**验证：需生产多主机长期样本 |
| 13 可视化 | 前端未大改；新接口返回结构化面板数据 | 已实现 |
| 14 多轮与上下文 | 无改动 | 已实现 |
| 15 协作与推导展示 | 无改动 | 已实现 |

**不能因为本轮优化就宣布15项全部达标。** 第9、10、11、12 项取决于真实上游、真实模型和长期生产观测，
代码侧只能降低路径成本、把观测做出来，不能替代事实测量。

## 3. 完整运行流程（runbook）

本轮在**没有命令执行能力**的环境里完成代码修改，因此下列命令需在装有依赖的机器上执行一遍：

```bash
cd D:/tonghuashun/tonghuashun

# 1) 静态检查与全量回归
python -m compileall -q backend frontend scripts tests
python -m pytest -q                     # 既有419项 + 本轮13项

# 2) 一键自检（不出网，含并发突发）
python scripts/verify_optimizations.py --burst 20 --json-out deliverables/optimization_20261022.json
python scripts/verify_optimizations.py --burst 100   # 本机容量参考，不等于生产SLA

# 3) 起服务，手工确认新接口
python -m uvicorn backend.app.main:app --port 8000
curl http://127.0.0.1:8000/api/v1/health
curl http://127.0.0.1:8000/api/v1/metrics/prometheus | head
streamlit run frontend/streamlit_app.py --server.port 8501
```

Prometheus 抓取（可选）：

```yaml
scrape_configs:
  - job_name: wence-zhitou
    metrics_path: /api/v1/metrics/prometheus
    static_configs:
      - targets: ["127.0.0.1:8000"]
```

建议告警：`wence_analysis_within_3s_total / wence_analysis_completed_total` 低于 0.9；
`wence_requests_total - wence_request_failures_total` 的成功率跌破 99.9%；`wence_session_pool_active` 长期贴上限。

## 4. 已知边界

- `portfolio_optimizer` 的相关性来自历史收盘价；没有序列的标的按 0 相关处理并写进 `warnings`，
  不会假装知道相关性。风险平价是乘法不动点迭代，不收敛时退回逆波动。
- `numeric_grounding` 是正则抽取，只覆盖百分比、倍数、金额、评分四类，且只对账"被引用的事实"；
  它降低幻觉风险，不构成正确性证明。
- 预取只在方向与标的都明确时启用；方向由推理得出时仍走串行，避免取错资料再回滚。
- 登录限流是进程内计数：多 worker 部署时每个 worker 独立计数，若要全局一致需换 Redis 等共享存储。