# 2026-11-04 全面回归与开源实现调研

本轮保留已有3秒证据研判和完整后台研究的边界，检查登录、画像、五类研究入口、资料取数、规则/模型编排、证据核验、审核、缓存、取消、历史和前端。没有安装新的金融框架或增加运行时依赖。

## GitHub 调研与实际采用

| 开源项目 | 已阅读的一手实现 | 本项目的取舍与落地 |
| --- | --- | --- |
| [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)（Apache-2.0） | [每次运行的配置上下文](https://github.com/TauricResearch/TradingAgents/blob/main/tradingagents/dataflows/config.py)、图编排与README | 借鉴显式运行上下文和并行任务边界。现有 ContextVar 和并行编排继续保留；新增页面所选标的的请求快照，解决“当前标的”混用历史公司资料的问题。没有引入交易执行能力。 |
| [aio-libs/async-lru](https://github.com/aio-libs/async-lru)（MIT） | [任务合并、取消和事件循环管理](https://github.com/aio-libs/async-lru/blob/master/async_lru/__init__.py)、README | 参考共享任务的生命周期原则，在现有模型缓存及公开事实缓存增加当前任务所有权检查。被取消或已被替换的加载任务不能再发布缓存，保持原有账号隔离和深拷贝。 |
| [fastapi/full-stack-fastapi-template](https://github.com/fastapi/full-stack-fastapi-template)（MIT） | [测试夹具](https://github.com/fastapi/full-stack-fastapi-template/blob/master/backend/tests/conftest.py)、[后端测试工作流](https://github.com/fastapi/full-stack-fastapi-template/blob/master/.github/workflows/test-backend.yml) | 参考独立测试环境、明确的测试结果和清理流程。新增多轮完整回归工具，每轮独立日志/JUnit；真实数据验收与离线回归分开，失败也退出测试会话。 |

本轮为阅读公开源码后独立实现兼容改动，没有复制整套代码或引入上述项目依赖。调研文件内容标识：TradingAgents config `f7ae14f361453d2a61b635eaa8c67225d096a32f`，async-lru implementation `189ec5b0d9a62e1862d44e09161456e30398ae76`，FastAPI conftest `7cdabf3c4526a703dfa026e7b18c84b0c82d0afd`。这些是当时读取的文件blob SHA，链接使用仓库默认分支。

## 已复现并修复的问题

1. 极端数值会导致500：例如数值为10的400次方时，Python浮点转换或isfinite抛出OverflowError。新增有限数值读取工具，覆盖规则派生、专业智能体、证据覆盖、百分比和咨询展示；无效输入保留为不能用于研判的资料。比例换算溢出也不会再生成Infinity。对正常科学计数法、千分位字符串保持支持，拒绝“1,2”等歧义格式。
2. “分析当前标的”在历史有多只股票时混用资料，以及行业页指定一个行业仍混用另一个行业：前端将所选标的复制进本次请求，后端仅用于解析这种指代。用户明确提到另一只股票时仍以问题为准。没有选择且资料涉及多个实体时明确追问，不拼接成同一个实体的判断。行业、基金和可转债同样执行范围核对。
3. 已取消加载任务覆盖重试缓存：复现取消后的旧加载任务仍返回，覆盖新任务结果。模型缓存和公开资料缓存仅允许当前登记的任务发布结果，失去所有权的结果不会污染后续请求。

新增47项回归用例，覆盖上述缺陷及数值边界、百分比溢出、代码范围、重试竞态、并发合并和修改结果副本等情况。原有账号隔离、证据时效、冲突审核和3秒响应检查继续执行。

## 复测方法

```powershell
.venv\Scripts\python.exe scripts/verify_comprehensive.py --rounds 3 --live --live-rounds 3
.venv\Scripts\python.exe scripts/verify_prepared_runtime.py
```

第一条先串行跑3轮全部pytest用例，启用未等待协程及未处理线程异常检查，再检查全部Python源文件语法、依赖和Git差异，最后串行运行3轮实际登录/数据库/问财验收。每轮输出单独日志、JUnit和结构化汇总。第二条使用真实在线模型准备一份完整报告，同时反复提交即时研判；完整报告待复核时必须拒绝交付，不能将其计为完整报告成功。

真实预取耗时和咨询耗时分别统计。网络、设备卡顿、CPU负载和完整在线报告生成不构成无条件3秒保证。所有真实用例使用明确命名的QA账号和测试画像，没有使用真实用户持仓，也不把密码或令牌写入报告。

本次实际结果与仍需复核事项见 `outputs/testing/20261104-comprehensive/summary.md`，总机器记录见 `outputs/testing/20261104-comprehensive/final/comprehensive.json`。
