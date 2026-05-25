# hermes-plugin-http-backplane

Hermes Agent 进程内的**本地 HTTP 服务插件**。在 Hermes 跑长任务模式
（`chat` / `gateway run` / `cron run` / `mcp serve` / …）时，自动起一个
daemon 线程暴露两条路由 lane，给本机客户端（浏览器扩展、未来桌面/CLI/移动端）
和该插件自身的集成机制使用。

默认监听 `127.0.0.1:9394`（用 `HERMES_BACKPLANE_PORT` 覆盖）。

## 两条 lane

### `/hermes/*` — Hermes core 的 HTTP 表面

包装 Hermes 自身的 Python 子系统（`hermes_state.SessionDB`、`cron.jobs`、
`config.yaml`、`agent.model_metadata` 等）为 HTTP。Hermes Agent 官方 dashboard
（`hermes_cli/web_server.py`）暴露的是 `/api/*`；本插件镜像了里面跨任务通用的
那一部分，并对子路径 / method / payload 与上游保持对齐 —— **路由前缀不同
（`/hermes` vs `/api`），但 endpoint 之后的形状一致**，换 base URL 即可互通。

完整对照、缺口、扩展字段：见 [`docs/api-parity.md`](docs/api-parity.md)。

主要子模块（详见 `runtime/features/hermes_proxy/`）：

| 子模块 | 路径前缀 | 用途 |
|---|---|---|
| `cron` | `/hermes/cron/*` | 定时任务 CRUD + 输出索引 |
| `sessions` | `/hermes/sessions/*` | `SessionDB` 读 + mine-only 的写 |
| `settings.model_routes` | `/hermes/model/*` + `/hermes/main-provider-settings` + `/hermes/provider-models` | 模型 / provider 配置 |
| `settings.memory_routes` | `/hermes/memories*` | MEMORY.md / USER.md 视图（mine-only） |
| `settings.skills_routes` | `/hermes/skills*` | 技能列表 / 文件浏览 / 启停 |
| `attachments` | `/hermes/attachments*` | 会话附件上传/删除 |
| `integrations_admin` | `/hermes/integrations*` | 给 `hermes integration` CLI 调的管理面（**不是** agent tool） |

### `/integrations/<name>/*` — 集成路由

`runtime/features/integrations/` 把两类来源喂进同一个注册表：

- **Preset**：包内 `presets/<name>/`（自带 `lark`）
- **User integration**：`~/.hermes/integrations/<name>/`

实现上是一个 catch-all 路由 + 运行时可变 dict 派发（`runtime/dispatch.py`），
不用 aiohttp sub-app —— 这样 `add / replace / remove` 可以在 server 跑着的
任何时刻生效，不会撞 aiohttp 的"AppRunner.setup 之后 Application 冻结"约束。

每个集成是一个小 Python 包：`integration.yaml` + `__init__.py`
（或 re-export 的 `handler.py`）暴露 `setup(router) -> None`。`router`
支持 aiohttp 风格的 `add_get / add_post / add_delete / add_patch / add_put /
add_route` 和路径模板（`/items/{id}`、`/items/{id:[0-9]+}`）。

## CLI

整合进 Hermes 的伞形命令，**不是 agent tool**（lifecycle 该由 operator 触发，
agent 通过 shell 工具间接调用即可，参考 `skills/integration-management.md`）：

```bash
hermes integration list                              # 列已注册
hermes integration install <name> --from-path ./dir  # 装
hermes integration install <name> --handler-py @handler.py [--yaml @meta.yaml]
hermes integration remove <name>                     # 删
hermes integration reload <name>                     # 重新导入 + 原子替换 router
```

实现：`cli.py` 暴露 `register_subparser` / `run`，由 `__init__.py:register`
通过 `ctx.register_cli_command("integration", ...)` 挂到 `hermes` 主 CLI 上。
Backplane 在跑的时候，CLI 走 loopback HTTP 让 backplane 原子热重载；不在跑
的时候只做文件操作并提示重启生效。

## 关键设计点

- **CLI 模式不起 server**：`__init__.py:_is_agent_invocation` 镜像 Hermes
  自己的 `_AGENT_COMMANDS / _AGENT_SUBCOMMANDS`（见 `hermes_cli/main.py`
  附近），只在 `chat / acp / rl / gateway run / cron run|tick / mcp serve`
  这类长任务进程里才 `start_server()`。`hermes integration list` 这种一次性
  命令进程不抢端口。`HERMES_BACKPLANE_FORCE_START=1` 可绕过。
- **没有 agent tool**：`plugin.yaml` 的 `provides_tools: []`，`register(ctx)`
  只起 server + 注册 CLI 命令。集成管理的 lifecycle 通过 CLI + skill prompt
  暴露，不是 LLM tool 表面。
- **错误隔离**：HTTP handler 抛异常被 aiohttp 兜成 500，不传染 Hermes 主循环；
  集成 setup 失败被 dispatcher 单独 try/except，**坏的集成不会拖垮 server**。

## 文档

- [`docs/api-parity.md`](docs/api-parity.md) —— 与 Hermes 官方 `/api/*` 的逐端点
  对照（共有 / 官方独有 / 我们独有 / 全局残留 / 变更日志）。**持续维护，
  每次改 backplane 或 upstream 升级要同步更新。**
- [`skills/integration-management.md`](skills/integration-management.md) —— 给
  agent 复制到 `~/.hermes/skills/` 的提示词模板，教 agent "scaffold 集成代码
  + 把 install 命令交给用户跑"。

## 配置

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `HERMES_BACKPLANE_PORT` | `9394` | 监听端口 |
| `HERMES_HOME` | `~/.hermes` | Hermes 主目录（user integration 从这下面读） |
| `HERMES_BACKPLANE_FORCE_START` | unset | 设 `1` 强制起 server（绕过 CLI 模式判断；测试 / 调试用） |

## 开发

```bash
pip install -e .                # 装本地副本
hermes chat                     # 触发插件加载 + 起 server
curl http://127.0.0.1:9394/hermes/sessions   # smoke test
```

文件布局：

```
__init__.py                       # 插件入口；register(ctx) + 守护线程
cli.py                            # `hermes integration` 子命令实现
plugin.yaml                       # Hermes 插件 manifest
pyproject.toml                    # 包元数据 + entry-points
runtime/
  api.py                          # /integrations/<name>/* 注册表
  dispatch.py                     # catch-all 派发
  http_app.py                     # aiohttp Application 工厂
  common.py                       # json_error / strip_ok / read_json_object
  adapters/                       # 适配 Hermes core 的薄包装
  features/
    hermes_proxy/                 # /hermes/* lane（cron / sessions / settings / attachments / integrations_admin）
    integrations/                 # 集成 loader + manager + 预置
docs/api-parity.md                # 与官方 API 对照
skills/integration-management.md  # 集成管理 skill 模板
```

## License

跟随 Hermes Agent。
