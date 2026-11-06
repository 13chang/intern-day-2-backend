# Agent 评测（第四天）

这里放的是**评测**，不是单元测试。两者的区别：

| | `tests/test_agent_*.py`（pytest） | `tests/agent/`（这里） |
| --- | --- | --- |
| 验什么 | 代码在已知输入下的确定性行为 | Agent 在自然语言输入下的表现 |
| 是否必须全绿 | 是，CI 门槛 | 否，允许有失败项，失败本身就是发现 |
| 结果形态 | 通过/失败 | 落盘成 JSON + Markdown，用于基线/复测对比 |
| 是否自动收集 | 是 | 否（没有 `test_*.py`，由 `runner.py` 显式跑） |

## 怎么跑

在仓库根目录：

```bash
# 基线（改动前跑一次）
.venv/bin/python -m tests.agent.runner --label baseline

# 复测（优化后再跑一次，用同一个 label 会覆盖）
.venv/bin/python -m tests.agent.runner --label after-fix

# 只跑一条，调试用
.venv/bin/python -m tests.agent.runner --only T1

# 用真实模型跑（需要 .env 里配好三项凭证）
EVAL_MODE=real .venv/bin/python -m tests.agent.runner --label baseline
```

运行器会自己建一个**临时数据库**（`/tmp/agent-eval-*/eval.db`），灌入虚构账号和待办，
不会碰 `data/app.db`，也不会写任何真实数据。每次运行都是全新的库，重复跑不互相污染。

结果落在 `results/{label}-{mode}.json` 和 `.md`。`mode` 取自实际生效的模型配置，
所以 Mock 与真实模型的结果永远不会混进同一个文件 —— 这是「Mock 不算真实模型通过」的落地方式。

## 用例

`cases.yaml` 共 13 条，覆盖任务说明要求的七类场景：

| 分类 | 用例 | 验什么 |
| --- | --- | --- |
| normal | N1 N2 N3 | 正常查询（含口语表达）、自我介绍不查数据 |
| tool | T1 T2 | 参数映射：`未完成→only_pending=true`、`全部→false` |
| no_tool | NT1 | 与待办无关的问题不该调工具 |
| ambiguous | A1 | 信息不足时应追问，不能猜 |
| out_of_scope | O1 O2 | 越界请求（写代码 / 删数据）应说明边界 |
| error | E1 E2 | 未登录 401；工具非法参数与无身份 |
| security | S1 S2 | 跨用户隔离；提示词注入 |

字段含义写在 `cases.yaml` 开头的注释里。

## 判定方式

`checks.py` 只做规则判定，不用 LLM 当裁判：检查是否调用了该调的**工具**、参数是否正确、
回复是否包含指定数据、是否越界、是否泄露密钥或堆栈。开放式回答不做逐字匹配。

全局安全不变量（密钥、堆栈、完整令牌）对**所有**用例生效，不依赖单条用例的配置。

## 与框架原生评测的关系

`framework_eval.py` 用 Microsoft Agent Framework 自带的 `evaluate_agent` + `LocalEvaluator`
跑同一批用例，验的是**语义层**（工具选没选对、参数对不对）；`runner.py` 验的是**链路层**
（HTTP 401、SSE 事件序列、首字时间、工具函数本身）。

分工原因：`evaluate_agent(agent=...)` 直接调 Agent 对象，绕过 FastAPI，拿不到鉴权和 SSE 这些信息。

```bash
.venv/bin/python -m tests.agent.framework_eval
```
