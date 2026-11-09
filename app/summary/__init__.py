"""每日活动总结：统计、落库、Agent 生成、Worker 任务。

这一层只被两处使用：

- ``app/routers/summary.py``：查询已生成的日报、手动触发一次生成；
- ``app/worker.py``：ARQ Worker 里的定时调度与总结任务。

和 ``app/activity`` 的分工是：那边管"今天该做什么"（计划与状态流转），
这边管"昨天做了什么"（事后统计与回顾）。
"""
