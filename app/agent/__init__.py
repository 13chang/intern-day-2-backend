"""第三天新增的 Agent 相关代码。

拆成几块，避免互相纠缠：

- ``config``   读环境变量决定用真实模型还是 Mock
- ``identity`` 用 ContextVar 把「当前已验证的登录用户」传给工具函数
- ``tools``    只读工具，工具只认 ContextVar，不接受模型传入的用户 ID
- ``mock``     没有模型凭证时跑通用链路的假模型
- ``factory``  把上面几块拼成一个 Agent Framework 的 Agent

这里刻意不做聚合导入：agent_framework 的导入不便宜，
测试鉴权和待办的用例没有理由为了它们多加载一遍。
"""
