# 第三天：Agent 交互原理与实现说明

> 说明：这篇是我自己对着代码和官方文档写的，描述的对象就是本仓库里实际运行的这套实现，
> 不是官网翻译。凡是没验证过的，我都写"没验证"。
>
> 实际版本：Python 3.12 · FastAPI 0.141 · agent-framework 1.24.0 · agent-framework-ag-ui 1.7.1 ·
> @copilotkit/react-core 1.84.1 · @copilotkit/runtime 1.84.1 · @ag-ui/client 1.0.3 · React 19.3 · Vite 8.3

## 0. 一句话概括今天的链路

用户在浏览器里打字 → React 页面上的 CopilotKit 把消息发给我自己起的 Node Runtime →
Runtime 用 `HttpAgent` 把同一批消息转成 AG-UI 请求打给 Python 的 `POST /agent` →
Python 先验登录令牌，再交给 Agent Framework 的 Agent → Agent 调模型、需要数据时调只读工具查 SQLite →
模型输出以 AG-UI 事件流回传 → Runtime 原样转发 → 页面按增量事件逐条刷新。

三个服务分别跑在三个容器里，浏览器只认宿主机端口：`5173`（前端）、`4000`（Runtime）、`8000`（后端）。

## 1. Microsoft Agent Framework 解决什么问题？Agent、模型、工具是什么关系

我理解的定位是：**它管的是"一次对话怎么跑完"这件事，而不是"模型怎么回答"。**

如果不用框架，手写一个能调工具的对话大概要自己处理这些：维护消息列表 → 按模型要求的格式带上工具
JSON Schema → 解析模型返回的"我要调 xxx 工具" → 执行工具 → 把结果包成模型要的消息格式 → 再请求一次
模型 → 判断还要不要再调工具 → 最后把增量文本吐出来。这一整套循环，加上超时、异常、多工具并发、
审批这类边界，才是框架真正接手的东西。

它把这件事拆成三个角色：

- **模型客户端（client）**：只负责"给一串消息，返回回复或工具调用请求"。在我这里就是
  `OpenAIChatCompletionClient`；没有凭证时我用自己写的 `MockChatClient` 顶替它。
- **工具（tool）**：一个普通 Python 函数，用 `@tool` 装饰后，框架从函数签名和类型注解生成参数
  Schema。我这里的工具是 `list_my_todos(only_pending: bool = False) -> str`。
- **Agent**：把 client、指令（instructions）、工具列表绑在一起的对象，对外提供"跑一次"的能力。
  它本身不实现模型，也不实现工具，只负责组织这一轮该做什么。

关系上比较关键的一点是：**工具执行是"客户端能力"，不是 Agent 的魔法。** Agent Framework 里这是
`FunctionInvocationLayer` 这一层中间件做的工作——它在 client 外面再包一层，看到模型返回工具调用就
执行、把结果塞回消息、再要一次模型。这一点我一开始理解错了，调试过程写在第 11 节。

## 2. CopilotKit 在 React 里做什么？和自己写一个输入框差在哪

一开始我以为它就是"一个长得比较好看的聊天框"，真正接完发现它管的东西比 UI 多：

1. **Provider 层**：`<CopilotKitProvider runtimeUrl agentId headers>` 建立和 Runtime 的连接，
   拉 `/info` 拿到有哪些 Agent、有哪些能力。`headers` 我传的是一个函数而不是对象，
   因为令牌要"每次请求前现取"——退出登录后立刻失效，而不是等页面重建。
2. **状态层**：会话 id、消息列表、当前是否在跑、错误状态，都在这层。
3. **事件消费层**：把 SSE 里的 `TEXT_MESSAGE_CONTENT` 一条条追加到对应消息上，
   把 `TOOL_CALL_START/RESULT` 渲染成"正在调用工具"的中间态。
4. **UI 层**：`<CopilotChat />` 输入框、消息气泡、自动滚到底、发送中禁用、错误提示。

自己写一个输入框，第 1、3 步全得自己来：自己拼请求体、自己解析 SSE（`data:` 分行、JSON 反序列化、
断流重连）、自己把增量拼成消息、自己处理工具中间态。真正麻烦的是第 3 步——不是渲染，是"事件乱序、
半截 JSON、流中断"这些情况。CopilotKit 的价值主要在这儿，UI 反而是附带的。

我这套实现里 `agentId` 在前端写的是 `todo-agent`，Runtime 里注册的 key 也是 `todo-agent`，
后端 Agent 的 `name` 是 `todo-assistant`。**前两个必须一致，第三个只是后端自己的标识**，
名字不一样不会报错，但演示时容易讲错，所以我在这里写清楚。

## 3. AG-UI 是什么？和普通 JSON 接口、HTTP、SSE 分别是什么关系

这是今天最容易混的地方，我按"协议 vs 传输"分开记：

- **HTTP**：传输层。决定"一个请求怎么发、响应怎么回来"。
- **SSE（`text/event-stream`）**：也是传输层的一种响应形态。服务器保持连接打开，持续写
  `data: {...}\n\n` 这样的块，浏览器边收边处理。**它不是 WebSocket**：SSE 是单向（服务端→浏览器）、
  纯 HTTP、不需要握手升级；WebSocket 是双向、需要 upgrade。我这里的请求方向是
  `POST /agent`（上行消息）+ SSE（下行事件），上行下行都走 HTTP，没有 WebSocket。
- **AG-UI**：跑在 SSE 上面的**事件协议**。它规定"一次运行里应该出现哪些事件、每个事件的字段叫什么"。

也就是说：AG-UI 不是模型、不是框架、也不是传输方式，它是**事件约定**。同一批事件也可以用
WebSocket 或轮询传，只是我今天用的是 SSE。

它和"自己随手定义一个 JSON 接口"的区别在于：普通接口一般是"一问一答"（请求 → 完整响应），
而 AG-UI 描述的是**一个有时间顺序的过程**。我实测到的事件顺序是：

```text
RUN_STARTED
TEXT_MESSAGE_START
TOOL_CALL_START → TOOL_CALL_ARGS → TOOL_CALL_END → TOOL_CALL_RESULT
TEXT_MESSAGE_END
TEXT_MESSAGE_START
TEXT_MESSAGE_CONTENT（多条，每条形如 {"delta": "..."}）
TEXT_MESSAGE_END
MESSAGES_SNAPSHOT
RUN_FINISHED
```

出错时不是换成 HTTP 500，而是在已经 200 的流里塞一条 `RUN_ERROR`。这一点后面第 7 节会用到。

## 4. 从点发送到显示回复，消息经过哪些组件

按我实际打的日志和抓到的请求，逐步写：

1. **`<CopilotChat />`**：拿到输入文本，交给 CopilotKit 的运行时对象，生成 `threadId` / `runId`，
   带上完整 `messages`（它每次都把整个会话带上）。
2. **`CopilotKitProvider` 的 `headers` 回调**：请求发出前调用一次，返回
   `{ Authorization: "Bearer <localStorage 里的令牌>" }`。
3. **浏览器 → Runtime**：`POST http://localhost:4000/api/copilotkit/agent/todo-agent/run`。
4. **Runtime 的 `agents` 工厂**：`({ request }) => ({ "todo-agent": new HttpAgent({...}) })`。
   这个函数每个请求都会跑一次，我在这里把该请求自己的 `authorization` 读出来放进 `HttpAgent.headers`。
   **这是整套实现里最关键的一处**——如果写成静态对象，所有用户会共用同一个 Agent 实例
   （要么都没令牌，要么都带同一个人的令牌）。
5. **Runtime → Python**：`POST http://host.docker.internal:8000/agent`，body 是 AG-UI 的
   `RunAgentInput`（`threadId` / `runId` / `messages` / `state` / `tools` / `context` / `forwardedProps`）。
6. **FastAPI 的 `dependencies=[Depends(require_agent_user)]`**：读 `Authorization`，查会话表。
   失败 → 直接 401，**不会进入模型**；成功 → 把用户塞进 `ContextVar`，继续。
7. **AG-UI 适配函数 `add_agent_framework_fastapi_endpoint`**：把 `RunAgentInput.messages` 转成
   Agent Framework 的 `Message` 列表，调 `agent.run(...)`，再把 Agent 的输出转成 AG-UI 事件。
8. **Agent**：拼上 instructions + 工具 Schema，调用 client（模型）。
9. **模型 → 工具**：如果模型说"我要调 `list_my_todos(only_pending=true)`"，FunctionInvocationLayer
   在**后端**执行这个函数 → 查 SQLite → 结果作为 `function_result` 消息塞回上下文 → 再请求模型。
10. **模型最终输出**：以增量文本回给 Agent，Agent 转成 `TEXT_MESSAGE_CONTENT` 事件。
11. **Python → Runtime → 浏览器**：Runtime 不重新组装，直接把 SSE 字节转发。
12. **`<CopilotChat />`**：按 `delta` 追加到当前助手消息，页面逐字出现；`RUN_FINISHED` 后结束"发送中"状态。

每个组件的"收到什么 / 返回什么"：

| 组件 | 收到 | 返回 |
| --- | --- | --- |
| `<CopilotChat />` | 用户输入、Agent 事件 | 渲染结果；把消息交给 Provider |
| `CopilotKitProvider` | `runtimeUrl`、`headers` 回调 | 带 Authorization 的 HTTP 请求 |
| Runtime | `RunAgentInput` + Authorization | 转发到 `/agent`，原样回 SSE |
| `require_agent_user` | `Authorization` 头 | 用户 dict（或 401） |
| AG-UI 适配层 | `RunAgentInput` | AG-UI 事件序列 |
| Agent | 消息列表 + 工具 | 文本增量 / 工具调用请求 |
| 模型服务 | 消息 + 工具 Schema | 文本增量或工具调用 |
| `list_my_todos` | `only_pending` | 一段中文文本（真实查库） |

## 5. Runtime 部署在哪里、为什么 Vite 不能当 Runtime

我把它做成了前端仓库里的一个独立 Node 服务：`server/index.mjs`，Express，监听 `4000`，
在 Compose 里是第二个服务 `intern-day-2-runtime`。它和前端共用同一份镜像，只是启动命令不同
（`node server/index.mjs`），这样不用把依赖装两遍。

职责是两件：**协议转换**（浏览器的请求 ↔ AG-UI 的 `RunAgentInput`）和**认证透传**。

Vite 为什么不能顶替它：Vite 的 dev server 本质是"按需编译 + 静态资源 + HMR"，它对外只有
"我要这个模块"和"代理到某处"两件事。CopilotKit 的 Runtime 需要的是一组有状态的运行时接口
（`/info`、`/agent/:id/run`、线程管理），还要维护 Agent 实例、跑事件流。用 Vite 的 `proxy`
只能把字节转走，`/info` 谁答？线程谁管？把这几件事写进 Vite 配置就等于自己实现一个 Runtime 了。
所以我保留了真实的 Runtime，没有走"直接接入"的捷径。

## 6. 流式回复如何逐步显示

页面不是"假打字"，因为浏览器 Network 里能看到一条长期打开的 `text/event-stream` 请求，
并且 `TEXT_MESSAGE_CONTENT` 是一条条到的。我实测到的结果是：一次回答被切成 39 条增量，
相邻两条间隔约 50ms，整段约 1.9 秒推完。
（条数和间隔由 Mock 的切分参数决定；换成真实模型时，切分点由模型服务的 token 流决定，
条数和长度都不固定。）

**这里踩过一个坑**：Mock 最初用 18 字一块、但块之间没有 `await`，
所有增量会在同一个事件循环 tick 内吐完，SSE 几毫秒就结束，浏览器只重绘一次——
看起来就是"整段文字瞬间出现"，完全没有打字效果。
根因不在前端，在于 Mock 没有模拟真实模型"一边算一边吐"的时间间隔。
现在的做法是每 2 字停 50ms（≈40 字/秒），并给整条流设 6 秒上限：
短回复正常打字，工具返回几十条待办时按比例压缩间隔，不会让人干等十几秒。

前端识别一次运行的方式：

- 开始：`RUN_STARTED` → 进入"发送中"，禁用重复发送。
- 增量：`TEXT_MESSAGE_CONTENT` 的 `delta` → 追加到当前消息，触发重渲染。
- 结束：`RUN_FINISHED` → 结束"发送中"。
- 失败：`RUN_ERROR` → 结束等待、显示错误。**注意它可能是 HTTP 200 的流里的一条事件**。
- 工具中间态：`TOOL_CALL_START` / `TOOL_CALL_RESULT` → 显示"正在查工具"。

还有一个工程细节：Express 这边不能加压缩或缓冲中间件，否则事件会被攒着一起发出去，
表现就是"转很久然后整段蹦出来"。我没有装这类中间件，`server/index.mjs` 里也没加任何缓冲逻辑。
（代理层同理：`nginx` 之类如果开了 `proxy_buffering`，要把 `text/event-stream` 排除掉。）

## 7. 登录凭证和模型 API Key 分别由谁保存、发给谁

这两个东西完全不是一回事，我特意分开写：

**登录令牌**

- 存：浏览器 `localStorage`（第二天的做法，前后端不同端口，用 Bearer 头比跨域 Cookie 简单）。
- 发：浏览器 → Runtime → Python，**每一跳都原样带同一个 `Authorization: Bearer ...`**。
- 校验：只在 Python 侧，查 `sessions` 表（库里只存令牌的 SHA-256 摘要）。
- Runtime 不解析也不记录它，只转发；它把令牌摘要当作"线程归属人"，用来避免别人拿着别人的
  `threadId` 读到我的会话。**这个摘要不是鉴权**，鉴权永远在 Python。

**模型 API Key**

- 存：只在后端容器的环境变量（`APP_LLM_API_KEY`），代码里读一次，不落盘、不打日志。
- 发：只有 Python 进程发往模型服务，方向是后端 → 外部，浏览器和 Runtime 都看不到。
- 为什么不放浏览器：放进去就等于公开。浏览器里的任何东西用户都能看到、能改、能复制，
  而且前端代码和 `VITE_` 环境变量最终都会进打包产物。密钥一旦进前端，等于发给了所有访问者。
- 也不放镜像构建参数：`docker build --build-arg` 的值会留在镜像历史里，`docker history` 就能看到。

还有一个实际踩到的点：**未登录时 Runtime 返回的 HTTP 状态是 200，不是 401。**
我用 Node 直接打 Runtime 验证过：

```text
无凭证 -> 200 data: {"type":"RUN_ERROR","message":"HTTP 401: {\"detail\":\"登录状态无效或已过期，请重新登录\"}", ...}
```

原因在链路顺序：Python 侧确实是 401（这一步我单独打过 `POST /agent`，确认是 401 + `WWW-Authenticate`），
但 `HttpAgent` 把上游的 401 包装成了一条 `RUN_ERROR` 事件，Runtime 又已经在往下游写 SSE 了，
HTTP 头早就发出去了，改不了状态码。所以前端的处理是：识别 `RUN_ERROR` 里的 401 语义 → 清本地状态 → 回登录页。
这条经验说明"接口有没有做鉴权"和"前端能不能看到 401"是两件事。

## 8. 对话上下文来自哪里？thread/run 与登录会话的区别

**上下文（今天的选择：浏览器主导）**

CopilotKit 每次运行都会把整个会话的 `messages` 带上，Runtime 原样转发，Python 把收到的
`messages` 直接交给 Agent。所以"第二轮能记住第一轮的昵称"，靠的是**浏览器把历史又发了一遍**，
后端没有存。我实测过：手动发 3 条消息（user / assistant / user），Mock 模型确实收到了两轮用户消息。

代价和边界：

- 刷新页面：前端状态丢失，对话从头开始（`localStorage` 里只有令牌，没有消息）。
- 服务重启：后端本来就没存，什么都不影响；Runtime 的内存线程记录会清空。
- 「新建对话」：换一个 `key` 重新挂载 `<CopilotChat />`，等于开一个新 `threadId`；旧对话不再出现在界面上，也找不回来。
- 退出/换账号：Provider 用 `key={user.id}` 重新挂载，界面上的会话立即清空。

**thread / run 和登录会话的区别**

- `threadId`：一次对话的标识，用来把多条消息归到同一个会话上。**它不是凭证**——
  它不证明"我是谁"，只说明"这堆消息属于同一次聊天"。任何人都能编一个 `threadId`。
- `runId`：一次运行（一问一答的一轮）的标识。同一个 `threadId` 下可以有很多 `runId`。
- 登录会话：`sessions` 表里的一行，代表"某个用户现在登录着"。它才决定"能看谁的数据"。

我用 `authorization_scope_resolver` 把 AG-UI 侧的授权作用域设成 `agent-user:<用户 ID>`，
作用是给服务端会话、快照、审批状态分区，避免不同账号共用一份状态。今天没有开服务端会话持久化
（没配 `snapshot_store`），所以现在这个作用域还只是"预埋"，真正起作用要等以后存历史。
这点我不想说得比实际更好。

## 9. 参考资料

- Microsoft Agent Framework 总览：<https://learn.microsoft.com/en-us/agent-framework/overview/>
- AG-UI 集成文档：<https://learn.microsoft.com/en-us/agent-framework/integrations/ag-ui/>
- Python AG-UI 包说明：<https://github.com/microsoft/agent-framework/blob/main/python/packages/ag-ui/README.md>
- CopilotKit 架构：<https://docs.copilotkit.ai/concepts/architecture>
- AG-UI 协议：<https://docs.ag-ui.com/introduction>
- 版本核对：我直接装到临时容器里读 `inspect.signature` 和 `.d.mts` 类型定义，
  没有照抄官网示例——官网示例里的 `createCopilotEndpointSingleRouteExpress` 在 1.84.1 里已经标了
  `@deprecated`，改用 `mode: "single-route"`。

## 10. 读完之后仍然没完全搞懂的问题

1. **AG-UI 的 `MESSAGES_SNAPSHOT` 到底该不该由前端信任？** 我在事件流里看到它会带一份消息快照，
   但 CopilotKit 自己也在本地维护消息列表。两者冲突时以谁为准、会不会导致消息重复，我没验证清楚。
2. **`authorization_scope_resolver` 与 `identifyUser` 的职责边界。** 前者在 Python 侧给 AG-UI
   作用域，后者在 Node Runtime 侧给线程归属，两层各管一段。如果以后把历史存到数据库，
   这两套"身份"要不要统一成一个，我没想明白。
3. **真实模型下工具调用的失败重试。** 现在工具只可能成功（只读、本地库）。如果工具抛异常，
   模型会收到什么、会不会自己改写参数重试、FunctionInvocationLayer 的 `max_invocation_exceptions`
   该怎么配，我没有实际跑过。

## 11. 一个只有调试才弄懂的细节：工具里拿到的用户为什么是 `None`

现象：链路全通了，AG-UI 事件也正常，但工具返回的是"当前请求没有可用的登录身份"，
日志里 `user_id` 是 `None`。

我的第一版鉴权依赖是这么写的（和第二天一样，同步函数）：

```python
def require_agent_user(credentials=Depends(bearer_scheme)) -> dict:
    user = get_current_user(credentials)   # 同步查 SQLite
    set_current_user(user)                 # 写 ContextVar
    return user
```

查了 FastAPI 的执行模型才想明白：**同步的依赖函数会被 `run_in_threadpool` 丢到工作线程执行**，
`ContextVar.set()` 改的是那个工作线程上下文的副本；请求协程自己的上下文里仍然是默认值 `None`。
所以工具读到的永远是 `None`。

改成 `async def`、把阻塞查库用 `run_in_threadpool` 显式包一层之后就好了：

```python
async def require_agent_user(credentials=Depends(bearer_scheme)) -> dict:
    user = await run_in_threadpool(get_current_user, credentials)
    set_current_user(user)
    return user
```

改完再测，工具日志里出现了 `user_id=1`，返回内容也变成了该用户自己的待办。
这个坑的价值在于：**`ContextVar` 不是"全局变量"，它跟协程/线程的上下文绑定**，
一旦中间隔了线程池，写和读就不在同一个上下文里了。

同一轮调试里还踩了两个相关的坑，一并记下来：

- **Mock 客户端的 `get_response` 不能写成 `async def`。** 协议要求它返回
  `Awaitable[ChatResponse]` 或 `ResponseStream`，写成协程函数会变成"返回一个协程对象"，
  框架拿到后报 `'coroutine' object has no attribute 'map'`。
- **`ResponseStream` 必须给 `finalizer`。** 不给的话 `get_final_response()` 会把收集到的
  update 列表当成结果返回，函数调用循环取 `response.usage_details` 时直接崩：
  `'list' object has no attribute 'usage_details'`。补上
  `finalizer=ChatResponse.from_updates` 才对。
- **自定义 client 必须显式继承 `FunctionInvocationLayer`**（或者直接用官方 client，
  它已经继承了）。不继承的话，模型返回的工具调用只会被当成事件发给前端，
  后端**根本不会执行工具**——现象是事件流里有 `TOOL_CALL_START`，却永远没有 `TOOL_CALL_RESULT`。
  这一条是我看事件流对比才发现的，光看代码看不出来。

## 12. 已知限制（如实标注）

- **没有接入真实模型。** 目前 `APP_LLM_MODE` 默认 `auto`，三项模型配置为空时退回 Mock。
  Mock 的回复是按规则拼的，不是模型生成的。链路上的鉴权、AG-UI 事件流、工具执行、用户隔离都是真的，
  但"模型能理解问题"这件事**尚未验证**——缺 `APP_LLM_BASE_URL` / `APP_LLM_MODEL` / `APP_LLM_API_KEY`。
- 对话历史只在浏览器内存里，刷新即失效，服务端不持久化。
- 前端打包产物偏大（CopilotKit 会带上代码高亮、图表等依赖，首包 2MB 左右）。
  这是练习项目用的开发服务器，没做拆包优化。
- 只做了一个只读工具，没有多 Agent、没有 RAG、没有审批流。
