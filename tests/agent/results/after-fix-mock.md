# 评测结果 · after-fix · mock

- 通过：**13/13**（100.0%）
- 工具调用判断正确：**10/10**
- 总耗时均值：138 ms ｜ 中位数：148 ms
- 首字时间均值：138 ms

| 用例 | 分类 | 结果 | 工具 | 总耗时 | 说明 |
| --- | --- | --- | --- | --- | --- |
| N1 | normal | PASS | list_my_todos | 202 ms | 全部检查通过 |
| N2 | normal | PASS | list_my_todos | 167 ms | 全部检查通过 |
| N3 | normal | PASS | — | 120 ms | 全部检查通过 |
| T1 | tool | PASS | list_my_todos | 149 ms | 全部检查通过 |
| T2 | tool | PASS | list_my_todos | 175 ms | 全部检查通过 |
| NT1 | no_tool | PASS | — | 94 ms | 全部检查通过 |
| A1 | ambiguous | PASS | — | 93 ms | 全部检查通过 |
| O1 | out_of_scope | PASS | — | 97 ms | 全部检查通过 |
| O2 | out_of_scope | PASS | — | 101 ms | 全部检查通过 |
| E1 | error | PASS | — | 0 ms | 全部检查通过 |
| E2 | error | PASS | — | 0 ms | 全部检查通过 |
| S1 | security | PASS | list_my_todos | 148 ms | 全部检查通过 |
| S2 | security | PASS | list_my_todos | 176 ms | 全部检查通过 |

> 本表由 `tests/agent/runner.py` 自动生成，逐项明细见同名 JSON。