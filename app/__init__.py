"""应用包。

这里做两件"必须最早发生"的事：

1. **配日志**。之前没有这一步，root logger 停在默认 WARNING，于是
   「MODEL_MODE=mock…」的警告能看到、「MODEL_MODE=real：真实模型 model=…」这句
   INFO 永远不打印 —— 排查"页面说真实模型未启用"时，服务明明起来了却没有任何
   日志能说明它到底用了什么。
2. **读 .env**。Agent 在 ``app.main`` 导入时就被构建好
   （``add_agent_framework_fastapi_endpoint`` 会立刻调 ``build_agent()``），
   那一刻读到的 ``MODEL_MODE`` 就定死了本进程用 Mock 还是真实模型。
   包初始化早于所有子模块，是唯一"一定在它之前"的位置；写在 ``main.py``
   只能放在 import 之后，规则上也和导入顺序打架。

``.env`` 的细节见 ``app.envfile``：真实环境变量优先，``APP_SKIP_ENV_FILE=1`` 可跳过。
"""

import logging
import os

# level 可调：演示时想看每一步就 DEBUG，生产想安静就 WARNING
logging.basicConfig(
    level=os.environ.get("APP_LOG_LEVEL", "INFO").upper(),
    format="%(levelname)s:     %(message)s",
)

from app.envfile import load_env_file  # noqa: E402  (必须在 basicConfig 之后)

load_env_file()
