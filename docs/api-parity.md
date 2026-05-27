# HTTP API Parity — backplane plugin vs. Hermes Agent dashboard

跟踪本插件 (`hermes-plugin-http-backplane`) 的 `/hermes/*` HTTP 表面，
对比 Hermes Agent 官方 dashboard 服务的 `/api/*` 表面，记录三类状态：

- **A. 共有 (aligned)** —— 双方都有；payload + method 需要严格对齐，
  我们这边允许在不破坏对齐的前提下保留 mine-only additive 字段／参数
- **B. 官方独有 (gap to close)** —— 我们没有；候选补全列表
- **C. 我们独有 (mine-only)** —— 官方没有；候选向上游提 PR 的列表

> ⚠ 文档维护：每次改 backplane 或者 upstream 升级导致差异变化时，
> 更新对应表格的"对齐状态"列即可。决策演进通过 git log 看，
> 不在文档里维护变更日志。字段名拼写以代码为准，不要从对话/PR 描述里抄。

## 来源 / 真实数据点

| 来源 | 文件 / 入口 |
|---|---|
| 我们的路由声明 | `runtime/features/hermes_proxy/*/routes.py` 里的 `web.{get,post,put,delete,patch}(...)` 调用 |
| 我们的注册入口 | `runtime/features/hermes_proxy/__init__.py:register` |
| 官方路由声明 | hermes-agent 仓库 `hermes_cli/web_server.py` 里的 `@app.{get,post,put,delete}` 装饰器 |
| 官方对应 pydantic body 模型 | 同文件，找 `class XxxCreate(BaseModel)` / `class XxxUpdate(BaseModel)` |
| 官方 helper 函数 | `hermes_cli/inventory.py`、`hermes_cli/skills_config.py`、`agent.model_metadata`、`agent.models_dev` |

快速枚举两边路由：

```bash
# 我们
grep -nE 'web\.(get|post|put|delete|patch)\("/hermes' \
  runtime/features/hermes_proxy/**/routes.py

# 官方（在 hermes-agent 仓库根目录跑）
grep -nE '^@app\.(get|post|put|delete|patch|websocket)' \
  hermes_cli/web_server.py
```

## 约定

| 符号 | 含义 |
|---|---|
| ✅ | URL / method / payload 都对齐 |
| ➕ | 在对齐的基础上有 mine-only additive 字段／参数（不破坏对齐） |
| ⚠ | 已对齐 method/path，但 payload 有非 additive 的偏差（待评估） |
| ❌ | 还没对齐 / 实现缺失 |
| 🔒 | 故意不对齐（有明确理由，注释里写清） |

错误响应体格式差异（mine `{ok:false, error}` vs upstream FastAPI 默认 `{detail}`）
作为**全局残留**列在最后一节，不在单条 endpoint 里重复标。

**"备注"列存放什么**：非显然的语义（"`?profile=` accepted-but-ignored"、
"不带 envelope"）、故意不对齐的理由（cron update 🔒 行）、跨段引用
（"见 §D"）、历史融合记录（"取代了旧的 POST /hermes/main-model + /auxiliary-models"）。
**不放纯 wire shape**——状态为 ✅ 时 shape 跟 upstream 等价，要查直接看上游源码；
状态为 ⚠/❌ 时差异点要写清是哪里偏差，而不是只贴形状。

## 快速统计

| 类别 | 端点数 | 备注 |
|---|---|---|
| **A. 共有** | 20 | 18 完全等同（✅）；2 保留 mine-only additive（➕：sessions list 查询参数 / cron create 11 个 body 字段——都实测有 UI 真实消费方）|
| **B. 官方独有** | 36+ | 见 §B；优先级待 owner 排（Status/lifecycle 一组已搬到 §A） |
| **C. 我们独有** | 21 | 见 §C；候选 PR 上游：memory, attachments, skills/meta |

---

## §A. 共有端点（aligned，必须保持对齐）

布局：official URL → 我们 URL → 状态 → mine-only additive → 备注。

### Sessions（4）

| 概念 | 官方 | 我们 | 状态 | Mine-only additive | 备注 |
|---|---|---|---|---|---|
| 列表 | `GET /api/sessions` | `GET /hermes/sessions` | ➕ | 查询参数：`source=<src>`、`exclude_sources=cron,api_server`（用于在 sidebar 隐藏 cron 会话等场景） | — |
| 详情 | `GET /api/sessions/{id}` | `GET /hermes/sessions/{id}` | ✅ | — | — |
| 消息流 | `GET /api/sessions/{id}/messages` | `GET /hermes/sessions/{id}/messages` | ✅ | — | — |
| 删除 | `DELETE /api/sessions/{id}` | `DELETE /hermes/sessions/{id}` | ✅ | — | — |

### Cron（8）

| 概念 | 官方 | 我们 | 状态 | Mine-only additive | 备注 |
|---|---|---|---|---|---|
| 列表 | `GET /api/cron/jobs` | `GET /hermes/cron/jobs` | ✅ | — | `?profile=all` 是默认；其他 profile 通过 `_call_for_profile` 跨 `$HERMES_HOME` 派发，与上游 `_call_cron_for_profile` 同一模式 |
| 详情 | `GET /api/cron/jobs/{id}` | `GET /hermes/cron/jobs/{id}` | ✅ | — | 未传 `?profile=` 时遍历已知 profile 自动定位 |
| 创建 | `POST /api/cron/jobs` | `POST /hermes/cron/jobs` | ➕ | **请求 body 多 11 个字段**：`model`、`provider`、`base_url`、`script`、`no_agent`、`context_from`、`enabled_toolsets`、`workdir`、`repeat`、`skills`、`skill`（upstream 严格限 `{prompt, schedule, name, deliver}`） | — |
| 更新 | `PUT /api/cron/jobs/{id}` | `PUT /hermes/cron/jobs/{id}` | ✅ | — | — |
| 删除 | `DELETE /api/cron/jobs/{id}` | `DELETE /hermes/cron/jobs/{id}` | ✅ | — | — |
| 暂停 | `POST .../pause` | `POST .../pause` | ✅ | — | — |
| 恢复 | `POST .../resume` | `POST .../resume` | ✅ | — | — |
| 触发 | `POST .../trigger` | `POST .../trigger` | ✅ | — | — |

### Skills（2）

| 概念 | 官方 | 我们 | 状态 | Mine-only additive | 备注 |
|---|---|---|---|---|---|
| 列表 | `GET /api/skills` | `GET /hermes/skills` | ✅ | — | 仅 4 字段 (`name`/`description`/`category`/`enabled`)；所有 mine-only 富数据（per-skill metadata + bundle 统计）都挪到 `GET /hermes/skills/meta`（§C），UI 客户端 fetch 两边按 `name` join |
| 启停 | `PUT /api/skills/toggle` | `PUT /hermes/skills/toggle` | ✅ | — | — |

### Models（4）

| 概念 | 官方 | 我们 | 状态 | Mine-only additive | 备注 |
|---|---|---|---|---|---|
| 主模型详情 | `GET /api/model/info` | `GET /hermes/model/info` | ✅ | `base_url` | mine-only `base_url` 透出，便于 UI 直接展示当前主模型的 endpoint |
| 辅助槽位 | `GET /api/model/auxiliary` | `GET /hermes/model/auxiliary` | ✅ | — | — |
| 写主/辅助 | `POST /api/model/set` | `POST /hermes/model/set` | ✅ | `base_url`（scope=main） | mine-only `base_url`：传 `null` 显式清除 `model.base_url`，省略则保持现状 |
| 模型选项目录 | `GET /api/model/options` | `GET /hermes/model/options` | ✅ | — | 严格 delegate 给 `hermes_cli.inventory.build_models_payload`；helper 不可用时返 501（删了原 fallback 路径） |

### Status / lifecycle（4）

| 概念 | 官方 | 我们 | 状态 | Mine-only additive | 备注 |
|---|---|---|---|---|---|
| 运行状态 | `GET /api/status` | `GET /hermes/status` | ✅ | — | 版本 / paths / gateway liveness+state+platforms+exit_reason / active_sessions / config_version |
| 重启 gateway | `POST /api/gateway/restart` | `POST /hermes/gateway/restart` | ✅ | — | fire-and-poll：返 `{ok, pid, name}`，配 `GET /hermes/actions/gateway-restart/status` 拉日志 |
| 自升级 | `POST /api/hermes/update` | `POST /hermes/update` | ✅ | — | 路径少一截（去掉 `/hermes/hermes/`，因为前缀已经是 `/hermes/`） |
| 异步任务状态 | `GET /api/actions/{name}/status` | `GET /hermes/actions/{name}/status` | ✅ | — | whitelist `{gateway-restart, hermes-update}`；其他 name → 404 |

---

## §B. 官方独有（gap to close）

按主题分组。优先级列由 owner 后续填。

### Status / lifecycle

✅ **已全部补齐到 §A**（4 个端点：`status` / `gateway/restart` / `update` / `actions/{name}/status`）。

### Sessions 扩展

| 端点 | 用途 | 优先级 |
|---|---|---|
| `GET /api/sessions/search?q=` | 跨会话全文搜（FTS5） | TBD |
| `GET /api/sessions/{id}/latest-descendant` | 取最新子孙会话（branch 形态） | TBD |

### Config

| 端点 | 用途 | 优先级 |
|---|---|---|
| `GET  /api/config` | 读 config.yaml（normalized） | TBD |
| `PUT  /api/config` | 写 config.yaml | TBD |
| `GET  /api/config/defaults` | DEFAULT_CONFIG 出码 | TBD |
| `GET  /api/config/schema` | 配置 schema | TBD |
| `GET  /api/config/raw` | raw text | TBD |
| `PUT  /api/config/raw` | raw text write | TBD |

### Env vars

| 端点 | 用途 | 优先级 |
|---|---|---|
| `GET    /api/env` | 列环境变量（redacted） | TBD |
| `PUT    /api/env` | 写一个 | TBD |
| `DELETE /api/env` | 删一个 | TBD |
| `POST   /api/env/reveal` | 揭密被 redact 的值 | TBD |

### Provider OAuth

| 端点 | 用途 | 优先级 |
|---|---|---|
| `GET    /api/providers/oauth` | 列已授权 provider | TBD |
| `DELETE /api/providers/oauth/{provider_id}` | 撤销 | TBD |
| `POST   /api/providers/oauth/{provider_id}/start` | 发起授权 | TBD |
| `POST   /api/providers/oauth/{provider_id}/submit` | 提交回调 | TBD |
| `GET    /api/providers/oauth/{provider_id}/poll/{session_id}` | 轮询 | TBD |
| `DELETE /api/providers/oauth/sessions/{session_id}` | 清会话 | TBD |

### Logs

| 端点 | 用途 | 优先级 |
|---|---|---|
| `GET /api/logs?file=&lines=&level=&component=&search=` | 日志流 | TBD |

### Profiles ("soul")

| 端点 | 用途 | 优先级 |
|---|---|---|
| `GET    /api/profiles` | 列 profile | TBD |
| `POST   /api/profiles` | 新建 | TBD |
| `GET    /api/profiles/{name}/setup-command` | 取 setup 命令 | TBD |
| `POST   /api/profiles/{name}/open-terminal` | 开终端 | TBD |
| `PATCH  /api/profiles/{name}` | 改 | TBD |
| `DELETE /api/profiles/{name}` | 删 | TBD |
| `GET    /api/profiles/{name}/soul` | 读 SOUL.md | TBD |
| `PUT    /api/profiles/{name}/soul` | 写 SOUL.md | TBD |

### Tools

| 端点 | 用途 | 优先级 |
|---|---|---|
| `GET /api/tools/toolsets` | 列已注册工具集 | TBD |

### Analytics

| 端点 | 用途 | 优先级 |
|---|---|---|
| `GET /api/analytics/usage` | token 用量 | TBD |
| `GET /api/analytics/models` | 模型用量 | TBD |

### WebSockets

| 端点 | 用途 | 优先级 |
|---|---|---|
| `WS /api/pty` | terminal 流 | TBD |
| `WS /api/ws` | 通用消息通道 | TBD |
| `WS /api/pub` | pub/sub | TBD |
| `WS /api/events` | 事件流 | TBD |

### Dashboard 自管（可能不必补 —— 是 UI 设置面，不是核心能力）

| 端点 | 用途 | 优先级 |
|---|---|---|
| `GET    /api/dashboard/themes` | 列主题 | LOW |
| `PUT    /api/dashboard/theme` | 切主题 | LOW |
| `GET    /api/dashboard/plugins` | 列前端插件 | LOW |
| `GET    /api/dashboard/plugins/rescan` | 重扫 | LOW |
| `GET    /api/dashboard/plugins/hub` | hub 索引 | LOW |
| `POST   /api/dashboard/agent-plugins/install` | 装 agent plugin | LOW |
| `POST   /api/dashboard/agent-plugins/{name}/enable` | 启用 | LOW |
| `POST   /api/dashboard/agent-plugins/{name}/disable` | 禁用 | LOW |
| `POST   /api/dashboard/agent-plugins/{name}/update` | 更新 | LOW |
| `DELETE /api/dashboard/agent-plugins/{name}` | 卸载 | LOW |
| `PUT    /api/dashboard/plugin-providers` | 配 plugin provider | LOW |
| `POST   /api/dashboard/plugins/{name}/visibility` | 控制可见性 | LOW |
| `GET    /dashboard-plugins/{plugin_name}/{file_path:path}` | 静态资源 | LOW |

---

## §C. 我们独有（mine-only，候选向上游 PR）

| 主题 | 端点 | PR 上游候选 | 备注 |
|---|---|---|---|
| 会话写入 | `POST /hermes/sessions` | maybe | upstream 只读 |
| | `PATCH /hermes/sessions/{id}` | maybe | 同上 |
| | `POST /hermes/sessions/{id}/messages` | maybe | 同上 |
| Cron 增强 | `GET /hermes/cron/runs` | yes | cron 运行结果列表（每条带 body），新标签页 feed 用 |
| Skills 浏览 | `GET /hermes/skills/meta` | yes | bundle stats (`skills_dirs`/`totals`/`origin_counts`) + per-skill rich metadata (`items[]` with `path`/`origin`/`platforms`/`version`/`tags`/`created_at`/`updated_at`/`timestamp_source`)；当 sidecar 给 `GET /hermes/skills` 用 |
| | `GET /hermes/skills/{name}/files` | yes | per-skill file list |
| | `GET /hermes/skills/{name}/file?path=` | yes | per-skill file read（路径穿越防护 + size cap） |
| **Memory** | `GET /hermes/memories` | **yes ⭐** | upstream 完全没有，干净候选 |
| | `GET /hermes/memories/{target}` | **yes ⭐** | 同上 |
| Provider credentials | `GET /hermes/provider-credentials?provider=` | maybe | 单 provider 的 `.env` 凭据键/值（mine-only；upstream 没等价接口） |
| | `POST /hermes/provider-credentials` | maybe | 仅写 `.env`，不动 `config.yaml: model.*` |
| | `GET /hermes/provider-models?provider=` | maybe | 单 provider 的 model list，upstream 没单独接口 |
| **Attachments** | `POST /hermes/attachments?session_id=&name=&mime=` | **yes ⭐** | 会话附件上传，upstream 完全没有 |
| | `DELETE /hermes/attachments?path=` | **yes ⭐** | 删单文件 |
| | `DELETE /hermes/attachments/session/{session_id}` | **yes ⭐** | 删整个会话目录 |
| Integrations | `GET /hermes/integrations` | no | 这个插件独有的概念（`/integrations/<name>/*` dispatcher 配套管理面） |
| | `POST /hermes/integrations/reload?name=` | no | 同上 |
| | `DELETE /hermes/integrations/{name}` | no | 同上 |
| Dispatcher | `* /integrations/{name}/{tail:.*}` | no | 这个插件独有 |

---

## §D. 全局残留（跨多个端点的偏差，未对齐）

| 项 | 说明 | 影响范围 | 修复成本 |
|---|---|---|---|
| 错误响应体 | mine `{ok:false, error}` vs upstream FastAPI 默认 `{detail}` | 所有 4xx/5xx | 中（要改 `common.json_error` + 客户端两边解析） |
| 跨 profile 写操作的并发风险 | `_call_for_profile` 在 `cron.jobs` 模块全局变量上做 swap+restore，对外部 `hermes cron run` 进程是 process-isolated 无影响；但同进程下如果有 in-process 调度（罕见）可能在 swap 窗口内瞬间读到错路径。Upstream `hermes_cli/web_server.py` 也有同样模式，已默认接受 | `GET/POST/PUT/DELETE /hermes/cron/jobs{,/{id},...}` | 不可避（要彻底安全得绕开 cron.jobs 直接读 jobs.json，舍弃 normalize/lock）|
| `getHermesModelCatalog` TS 类型 | 仓库 `hermes-my-browser-extension` 里 `HermesModelCatalogResponse` 仍是旧 `{providers (dict), catalog_source, ...}`，新 wire shape 是 upstream `{providers (list), model, provider}` | 跨仓库，UI 消费层 | 中（UI 重写消费代码 + 类型） |

<!--
维护提示：
- 新增 endpoint：先放进 §C；如果发现 upstream 已有对应，挪到 §A 并填对齐状态
- upstream 加 endpoint：放进 §B；评估优先级
- 任一端 endpoint 改了 payload：更新 §A 对应行的 mine-only additive + 状态
- 修了 §D 里某项：把那一项划掉/移除
- 决策演进通过 git log 看，不在文档里维护变更日志
-->
