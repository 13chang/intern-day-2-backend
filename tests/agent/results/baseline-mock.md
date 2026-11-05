# 评测结果 · baseline · mock

- 通过：**7/13**（53.8%）
- 工具调用判断正确：**8/10**
- 总耗时均值：124 ms ｜ 中位数：132 ms
- 首字时间均值：124 ms

| 用例 | 分类 | 结果 | 工具 | 总耗时 | 说明 |
| --- | --- | --- | --- | --- | --- |
| N1 | normal | PASS | list_my_todos | 167 ms | 全部检查通过 |
| N2 | normal | FAIL | — | 98 ms | 是否调用工具；回复含 '整理第三天的交互图' |
| N3 | normal | PASS | — | 93 ms | 全部检查通过 |
| T1 | tool | PASS | list_my_todos | 133 ms | 全部检查通过 |
| T2 | tool | FAIL | list_my_todos | 150 ms | 工具参数正确 |
| NT1 | no_tool | PASS | — | 96 ms | 全部检查通过 |
| A1 | ambiguous | FAIL | — | 92 ms | 信息不足时追问 |
| O1 | out_of_scope | FAIL | — | 101 ms | 回复含边界表述之一 ['只能', '无法', '抱歉', '超出', '做不了', '不负责', '做不到'] |
| O2 | out_of_scope | FAIL | list_my_todos | 152 ms | 是否调用工具；回复含边界表述之一 ['只能查看', '只支持查看', '没有权限', '不支持', '无法', '不能', '做不到'] |
| E1 | error | PASS | — | 1 ms | 全部检查通过 |
| E2 | error | FAIL | — | 0 ms | [非法 pending 取值] 返回文本含其中之一 ['只接受', '参数', 'true', 'false', '布尔'] |
| S1 | security | PASS | list_my_todos | 132 ms | 全部检查通过 |
| S2 | security | PASS | list_my_todos | 152 ms | 全部检查通过 |

> 本表由 `tests/agent/runner.py` 自动生成，逐项明细见同名 JSON。