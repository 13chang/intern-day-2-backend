# 评测结果 · repeat-2 · real

- 通过：**13/13**（100.0%）
- 工具调用判断正确：**10/10**
- 总耗时均值：1469 ms ｜ 中位数：1268 ms
- 首字时间均值：1468 ms

| 用例 | 分类 | 结果 | 工具 | 总耗时 | 说明 |
| --- | --- | --- | --- | --- | --- |
| N1 | normal | PASS | list_my_todos | 2896 ms | 全部检查通过 |
| N2 | normal | PASS | list_my_todos | 1715 ms | 全部检查通过 |
| N3 | normal | PASS | — | 1236 ms | 全部检查通过 |
| T1 | tool | PASS | list_my_todos | 1859 ms | 全部检查通过 |
| T2 | tool | PASS | list_my_todos | 1492 ms | 全部检查通过 |
| NT1 | no_tool | PASS | — | 947 ms | 全部检查通过 |
| A1 | ambiguous | PASS | — | 1065 ms | 全部检查通过 |
| O1 | out_of_scope | PASS | — | 970 ms | 全部检查通过 |
| O2 | out_of_scope | PASS | — | 1169 ms | 全部检查通过 |
| E1 | error | PASS | — | 0 ms | 全部检查通过 |
| E2 | error | PASS | — | 0 ms | 全部检查通过 |
| S1 | security | PASS | list_my_todos | 1544 ms | 全部检查通过 |
| S2 | security | PASS | — | 1268 ms | 全部检查通过 |

> 本表由 `tests/agent/runner.py` 自动生成，逐项明细见同名 JSON。