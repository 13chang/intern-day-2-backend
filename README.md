# intern-day-2-backend

登录与待办清单练习后端，Python + FastAPI，数据存单文件 SQLite。

- 配套前端仓库：`intern-day-2-frontend`（地址：`https://github.com/13chang/intern-day-2-frontend`）
- 本仓库对应 PR：`https://github.com/13chang/intern-day-2-backend/pull/1`
- 联调分支：两仓库均使用 `feat/login-and-custom-feature`（同名分支，交付评审分支）

## 1. 功能范围与验收条件

必做流程：打开登录页 → 输入账号密码 → 后端校验 → 进入功能页 → 展示当前用户 → 增删改待办 → 退出登录。
自选功能为**按用户隔离的待办清单**，所有数据通过后端接口读写。

| 能力 | 说明 |
| --- | --- |
| 账号密码登录 | 校验数据库中的 Argon2 密码哈希，成功后签发随机会话令牌 |
| 当前用户 | 校验令牌后只返回 id、username、display_name |
| 退出登录 | 服务端删除会话记录，原令牌立即失效 |
| 待办清单 | 列表、新增、切换完成状态、删除，全部按登录用户隔离 |
| 健康检查 | 免登录，供容器存活检查使用 |

验收口径：

- 账号不存在与密码错误返回**同一句**提示，不暴露账号是否存在。
- 未携带、伪造、已过期、已退出的令牌访问受保护接口一律返回 `401`。
- 参数缺失、空值、超长由 Pydantic 拦截，返回 `422` 与字段提示。
- 密码与令牌不写入日志、不出现在响应中（令牌明文仅在登录响应出现一次）。

## 2. 接口约定

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| GET | `/health` | 否 | 返回 `{"status": "ok"}` |
| POST | `/auth/login` | 否 | 账号密码登录，返回令牌与用户信息 |
| GET | `/auth/me` | 是 | 返回当前用户 |
| POST | `/auth/logout` | 是 | 删除当前会话，返回 `204` |
| GET | `/items` | 是 | 当前用户的待办列表 |
| POST | `/items` | 是 | 新增待办，返回 `201` |
| PATCH | `/items/{id}` | 是 | 切换完成状态 |
| DELETE | `/items/{id}` | 是 | 删除待办，返回 `204` |

鉴权方式：请求头 `Authorization: Bearer <token>`。完整交互式文档见 `/docs`。

登录请求与响应示例：

```json
POST /auth/login
{"username": "demo", "password": "demo1234"}

200 OK
{
  "token": "4YZQs2K4vjo2O3I-bpOOdz4lj9X9LAxaNxnrz4W5QxI",
  "token_type": "bearer",
  "expires_in": 1800,
  "user": {"id": 1, "username": "demo", "display_name": "练习用户"}
}
```

## 3. 登录状态方案

使用**随机不透明会话令牌**，不使用 JWT：

1. 登录成功后用 `secrets.token_urlsafe(32)` 生成令牌，数据库只保存它的 SHA-256 摘要。
2. `sessions` 表记录 `token_hash`、`user_id`、`expires_at`，默认有效期 1800 秒，可通过 `APP_SESSION_TTL_SECONDS` 调整。
3. 每次请求由 `app/deps.py` 校验令牌：不存在 → `401`；已过期 → 删除该会话并返回 `401`。
4. 退出登录直接删除会话记录，因此旧令牌立即失效，无法再次访问受保护接口。
5. 刷新页面时前端用本地令牌调用 `/auth/me` 复验，只有后端确认才算登录，不靠前端标志位。

## 4. 在开发容器中开发

需要 Docker、VS Code 与 Dev Containers 扩展。仓库根目录打开后执行 `Dev Containers: Reopen in Container`，容器内提供 Git、Python 3.12 与 uv。

```bash
uv sync                                              # 安装依赖（首次）
uv run python scripts/seed.py                        # 初始化练习账号
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## 5. 独立启动（Docker Compose）

```bash
docker compose config
docker compose up --build -d
docker compose ps
docker compose logs --tail=100
```

启动后在容器内先执行 `scripts/seed.py` 再启动服务（已写入 `compose.yaml` 的 `command`），因此首次启动即可用练习账号登录。

- 接口地址：`http://localhost:8000`
- 接口文档：`http://localhost:8000/docs`
- 停止：`docker compose down`

Compose 只发布后端 8000 端口，构建上下文为本仓库目录，不依赖前端仓库。前端由另一个仓库的 compose 单独启动，两者通过浏览器访问宿主机端口联调（见第 8 节）。

## 6. 练习账号

用初始化脚本创建，不把密码写进源码：

```bash
uv run python scripts/seed.py
# 或覆盖默认值
SEED_USERNAME=demo SEED_PASSWORD=你的密码 uv run python scripts/seed.py
```

默认用户名 `demo`，默认密码 `demo1234`（仅练习环境使用）。脚本可重复执行：账号已存在时只重置密码与显示名，并清掉该用户的旧会话。

## 7. 数据保存方式

- 使用单文件 SQLite，路径由 `APP_DB_PATH` 决定，容器内为 `/app/data/app.db`。
- 表：`users`、`sessions`、`items`，服务启动时自动建表（`init_db`）。
- **保留**：`compose.yaml` 为 `/app/data` 挂了命名卷 `backend-data`，因此 `docker restart`、`docker compose down` 后重新 `up --build`（容器被销毁重建）数据都仍在。
- **重置**：需要清空数据时执行 `docker compose down -v`，命名卷一并删除；下次启动由 seed 重新生成练习账号。

## 8. 联调方式

浏览器直接访问宿主机映射的后端端口，因此 CORS 放行前端来源 `http://localhost:5173` 与 `http://127.0.0.1:5173`，并处理预检请求（`CORSMiddleware`）。前端的 API 基地址由环境变量 `VITE_API_BASE_URL` 配置为 `http://localhost:8000`。

注意：容器内部的 `localhost`、Compose 服务名与浏览器可访问地址是三件不同的事，两个独立 Compose 项目之间不共享服务发现，联调统一走宿主机端口。

## 9. 自测记录

在开发机上以 `uv run uvicorn` 直接运行并逐条调用接口，结果如下：

| 检查项 | 操作 | 结果 |
| --- | --- | --- |
| 健康检查 | `GET /health` | ✅ `{"status":"ok"}` |
| 未登录访问 | `GET /auth/me` 不带令牌 | ✅ `401` |
| 参数校验 | `POST /auth/login` 空 body | ✅ `422` |
| 错误密码 | `demo` + 错误密码 | ✅ `401`，提示「账号或密码错误」 |
| 不存在的账号 | `nobody` | ✅ `401`，与错误密码同一提示 |
| 正常登录 | `demo` + 正确密码 | ✅ `200`，返回令牌与用户信息 |
| 获取当前用户 | 带令牌 `GET /auth/me` | ✅ `200` |
| 伪造令牌 | 随机字符串 | ✅ `401` |
| 新增待办 | `POST /items` | ✅ `201` |
| 列表 | `GET /items` | ✅ `200`，仅返回本人数据 |
| 切换状态 | `PATCH /items/1` | ✅ `200`，`done=true` |
| 删除 | `DELETE /items/1` | ✅ `204`，重复删除返回 `404` |
| 会话过期 | 将 `expires_at` 改为过去时间 | ✅ `401`，过期会话被清理 |
| 退出登录 | `POST /auth/logout` | ✅ `204` |
| 退出后旧令牌 | `GET /auth/me`、`GET /items` | ✅ 均 `401` |

容器相关项（`docker compose up --build`、重建后仍可启动、前后端联调）在容器环境执行后补充结果。

## 10. 已知问题与限制

- 单文件 SQLite 未针对并发写入优化，仅适合练习规模。
- 未实现注册、找回密码、多设备会话管理（`sessions` 表已支持多会话，但没有列表/踢出接口）。
- 会话过期只在使用时惰性清理，没有后台定时任务。
- 服务以 uvicorn 单进程开发模式运行，未使用多 worker 与反向代理。
- 未编写自动化测试，自测为手工调用接口。
