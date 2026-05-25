# HTTP API Parity — backplane plugin vs. Hermes Agent dashboard

跟踪本插件 (`hermes-plugin-http-backplane`) 的 `/hermes/*` HTTP 表面，
对比 Hermes Agent 官方 dashboard 服务的 `/api/*` 表面，记录三类状态：

- **A. 共有 (aligned)** —— 双方都有；payload + method 需要严格对齐，
  我们这边允许在不破坏对齐的前提下保留 mine-only additive 字段／参数
- **B. 官方独有 (gap to close)** —— 我们没有；候选补全列表
- **C. 我们独有 (mine-only)** —— 官方没有；候选向上游提 PR 的列表

> ⚠ 文档维护：每次改 backplane 或者 upstream 升级导致差异变化时，
> 更新对应表格的"对齐状态"列 + 末尾的 **变更日志** section。
> 字段名拼写以代码为准，不要从对话/PR 描述里抄。

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
| 主模型详情 | `GET /api/model/info` | `GET /hermes/model/info` | ✅ | — | adapter 仍解析 `base_url`（mine-only `/hermes/main-provider-settings` 用），handler 输出前 pop 掉 |
| 辅助槽位 | `GET /api/model/auxiliary` | `GET /hermes/model/auxiliary` | ✅ | — | — |
| 写主/辅助 | `POST /api/model/set` | `POST /hermes/model/set` | ✅ | — | 这一个 endpoint 取代了旧的 `POST /hermes/main-model` + `POST /hermes/auxiliary-models`；要清 `base_url` 走 mine-only `POST /hermes/main-provider-settings` |
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
| Provider settings | `GET /hermes/main-provider-settings` | maybe | 把主模型 + 凭据合一查；upstream 拆 `model/info` + `env` + `providers/oauth` |
| | `POST /hermes/main-provider-settings` | maybe | 同上写 |
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

---

## §E. 变更日志（append-only）

格式：日期 / 改动概述 / 影响表格 / 提交（如有）。

| 日期 | 改动 | 影响 | 提交 |
|---|---|---|---|
| 2026-05-25 | 首次建表；A 类 16 端点完成 path/method/payload 对齐（保留 mine-only additive）；删 `config_routes.py`，model 路由合并到 `model_routes.py`；skill bundle meta 拆出 `/hermes/skills/meta` | §A 全部，§C `skills/meta` 新增 | （未提交）|
| 2026-05-25 | cron `_clean_job` 增 4 字段 (`profile` / `profile_name` / `hermes_home` / `is_default_profile`)；cron list / get 从 ⚠ 升 ➕；cron update 标 🔒（intentional：白名单比 upstream 严格更安全）；§D 删掉已解决的 cron annotation 与 skill description/category 两条；§D 新增 `getHermesModelCatalog` TS 类型迁移项；确认 `_find_all_skills` 仅 `name`/`description`/`category` 三字段，我们是严格超集 | §A cron 4 行，§D 替换 | （未提交）|
| 2026-05-25 | 清掉 `cron/service.py` 里 `<!-- hermes-inbox-protocol-v1 -->` legacy 包袱：删 `_LEGACY_INBOX_PROTOCOL_MARKER` / 正则 / `_strip_legacy_inbox_protocol` / `_migrate_strip_legacy_protocol` / migration flag 文件路径与状态变量、`_clean_job` 中的 marker-strip 步骤、create/update 中的 defensive strip 调用、4 个 CRUD 入口的 `_migrate_strip_legacy_protocol()` 触发；同时清掉未用的 `import re` 和 `List` 导入；服务文件由 490 → 337 行。`_normalise_deliver`（`deliver="inbox"`→`"local"` 别名）保留 —— 另一桩 legacy concern，没动 | §A 文档备注（cron list/get 删 prompt 清洗行为说明） | （未提交） |
| 2026-05-25 | 整理 §A 备注列约定（不写纯 wire shape；只放非显然语义 / 不对齐理由 / 跨段引用 / 历史记录）；据此清掉 sessions 详情·消息流·删除 / cron delete·pause·resume·trigger / skills toggle 共 8 个冗余备注。`DELETE /hermes/sessions/{id}` 砍掉 mine-only `session_id` 字段（实际消费方 `store.ts:dropMessages` 只读 `ok`，没人用过该字段），状态从 ➕ 升 ✅；同步 extension 端 `DeleteSessionResponse` 类型与 `deleteHermesSession` 实现 | §A 行级备注；后端 `sessions/service.py:delete_session_response`；前端 `hermes-sessions.ts` | （未提交） |
| 2026-05-25 | 全 ➕ 字段消费方实测：`model/info` 砍 `config_path`/`config_exists`/`error`（仅死代码 wrapper 读）；`model/auxiliary` 砍 per-task `api_key` + 顶层 `config_path`/`config_exists`/`error`（全部 0 UI 消费，aux 状态 ➕→✅）；`model/set` 砍 `api_key` 入参（0 callers）；后端 `_AUX_SLOT_FIELDS` 同步去掉 `api_key`、`write_auxiliary_slot` 签名去掉 `api_key=`；前端 `hermes-agent-model.ts` 删 deprecated `getHermesAgentMainModel()` 死代码、`AuxiliaryTask.api_key` 字段、`AuxiliaryModelsResponse.config_path/config_exists`、`setHermesAuxiliarySlot` 的 `base_url/api_key` 参数；统计：✅ 8→9, ➕ 8→7 | §A 3 行 + 快速统计；后端 `adapters/hermes_agent_model.py`、`settings/model_config_service.py`、`settings/model_routes.py`；前端 `hermes-agent-model.ts` | （未提交） |
| 2026-05-25 | A 类剩余偏差对齐：cron PUT 砍 `_UPDATABLE_FIELDS` 白名单（🔒→✅），body 严格 `{updates:{...}}`；cron list/get/create/update/pause/resume/trigger/delete 全部接 `?profile=` 参数，新 `_call_for_profile` 镜像 upstream `_call_cron_for_profile` 模式（cron list/get ➕→✅）；model/info handler post-strip `base_url`（adapter 保留给 main-provider-settings；➕→✅）；model/set 砍 `base_url` 入参（➕→✅），extension `setHermesAgentMainModel` 内部改走 `saveHermesMainProviderSettings` 保持 `base_url:null` 清除流程；model/options 砍本地 catalog fallback（⚠→✅）helper 不可用时返 501；删 `model_catalog_service.build_model_catalog_response`（仅 fallback 用过）；extension cron update body wrap 成 `{updates}`；统计 ✅ 9→13，➕ 7→3（剩 sessions list 查询参数 / cron create 11 body 字段 / skills list 8 per-item 字段），🔒 1→0 | §A 7 行；§D 删 `?profile=` out-of-scope 项 改成"并发风险说明"；后端 `cron/service.py`、`cron/routes.py`、`settings/model_routes.py`、`settings/model_catalog_service.py`；前端 `hermes-cron.ts`、`hermes-agent-model.ts` | （未提交） |
| 2026-05-25 | skills 严格对齐：`GET /hermes/skills` 砍 8 个 per-item 富字段（➕→✅），仅保留 upstream 的 `name`/`description`/`category`/`enabled`；扩展 `GET /hermes/skills/meta` 增加 `items[]`（包含原来的 `path`/`origin`/`platforms`/`version`/`tags`/`created_at`/`updated_at`/`timestamp_source` + 已有的 bundle 统计），meta 路径保持 mine-only；extension `getHermesSkills` 并行 fetch 两边后按 `name` join，对外 `HermesSkillEntry` 类型不变，SettingsSkills UI 0 修改；统计 ✅ 13→14，➕ 3→2 | §A skills 行；§C skills/meta 备注；后端 `settings/skills_routes.py`；前端 `hermes-skills.ts` | （未提交） |
| 2026-05-25 | `PUT /hermes/cron/jobs/{id}` 收尾对齐：删 `update_job_response` 里"empty updates 拒绝"和 `_normalise_deliver` 两处 upstream 没有的额外行为，body 真正一字不差透给 `cron.jobs.update_job` —— mine-only 行为彻底清零，备注列从"body 严格 + 白名单已移除"简化为 `—` | §A cron 更新行；后端 `cron/service.py:update_job_response` | （未提交） |
| 2026-05-25 | mine-only cron 端点小整理：`GET /hermes/cron/output/index` 改名 `GET /hermes/cron/runs`（"runs" 跟 "jobs" 同级名词，"index" 命名意图模糊）；删 `GET /hermes/cron/output/{job_id}/{run_id}` —— extension 端 `getCronRun` 0 consumer，list 已经返回 body 没有单读必要；后端同步删 `output_service.get_run`、`handle_output_detail`、`cron/__init__.py` 的 `get_run` 导出；前端 `lib/cron-runs/client.ts` 改 URL + 删 `getCronRun` 函数；`POST /hermes/cron/parse-schedule` 保留（无更好替代：client-side 重写 schedule DSL parser 维护成本高，提交时才校验 UX 退化） | §C cron 增强项；后端 `cron/routes.py`、`output_service.py`、`__init__.py`；前端 `cron-runs/client.ts` | （未提交） |
| 2026-05-25 | 删 `POST /hermes/cron/parse-schedule` —— 此前留作 schedule 字符串可读性预览，但 owner 判断"纯 UX 糖，不值得为它维护一个 backend endpoint"。后端删 `handle_parse_schedule` + 路由 + `service.parse_schedule_preview` + `cron/__init__.py` 导出；前端删 `previewHermesCronSchedule` + `HermesCronParsePreviewResponse` + `useSchedulePreview` hook + `SchedulePreview` 组件 + `SettingsCron.tsx` 中两处使用；表单仍有 placeholder 字符串提示用户支持的格式（cron / `every Xm` / 时长 / ISO），提交时 Hermes core 会正常校验。前一条 changelog 里"保留 parse-schedule"的判断被本次推翻 | §C cron 增强项缩到 1 行；后端 `cron/routes.py`、`cron/service.py`、`cron/__init__.py`；前端 `lib/hermes-cron.ts`、`options/SettingsCron.tsx` | （未提交） |
| 2026-05-25 | 删 `POST /hermes/sessions/{id}/regenerate-title` —— extension 全仓 `grep regenerate-title / regenerateTitle / regenerateHermesTitle` 0 hit，upstream 也无对等。后端删 `handle_regenerate_title` + 路由 + `service.regenerate_title_response`（~70 行）+ routes.py 的 import；保留 service 里的 auto-title path（`_spawn_auto_title` / `_maybe_trigger_auto_title` / `_AUTO_TITLE_USER_MSG_LIMIT` / `_content_to_text`），那条是 append_message 异步触发的，仍在用 | §C 会话写入项 4→3；后端 `sessions/routes.py`、`sessions/service.py` | （未提交） |
| 2026-05-25 | 补全 upstream "Status / lifecycle" 一组（4 端点）：新 backplane feature `runtime/features/hermes_proxy/lifecycle/` 实现 `GET /hermes/status`（版本/paths/gateway liveness+state+platforms+exit_reason/active_sessions/config_version 聚合）、`POST /hermes/gateway/restart`、`POST /hermes/update`、`GET /hermes/actions/{name}/status`；spawn 模式镜像 upstream `_spawn_hermes_action`（whitelist + detached `Popen` + log file + `proc.poll()`），白名单仅 `{gateway-restart, hermes-update}`。Status payload 用 `_safe_import` 防御性导入 `gateway.status` / `gateway.config` / `hermes_state.SessionDB` / `hermes_cli` / `hermes_cli.config`，任一不可用就降级到 partial payload 而不 500。`pyproject.toml` packages 列表加 `lifecycle` 子包。前端新 `lib/hermes-lifecycle.ts` 客户端 + `options/SettingsStatus.tsx` 新 tab（Runtime 卡片 / Gateway 卡片 / 两个 Action 面板带日志 tail + fire-and-poll 模式，1s poll 频率，runtime 卡 10s 刷新），i18n 加 `options.nav.status`，index.tsx 加 nav button + render switch。§B 里 Status/lifecycle 一组整体搬到 §A；统计 ✅ 13→18（A 16→20），B 40+→36+ | §A 新增 Status/lifecycle 段（4 行）；§B 删 Status/lifecycle 段；快速统计；后端 `runtime/features/hermes_proxy/lifecycle/{__init__,routes,service}.py`（新建）、`runtime/features/hermes_proxy/__init__.py`、`pyproject.toml`；前端 `lib/hermes-lifecycle.ts`（新建）、`options/SettingsStatus.tsx`（新建）、`options/index.tsx`、`lib/i18n/{en,zh-CN}.ts` | （未提交） |

<!--
维护提示：
- 新增 endpoint：先放进 §C；如果发现 upstream 已有对应，挪到 §A 并填对齐状态
- upstream 加 endpoint：放进 §B；评估优先级
- 任一端 endpoint 改了 payload：更新 §A 对应行的 mine-only additive + 状态；在 §E 追一行
- 修了 §D 里某项：把那一项划掉/移除；在 §E 追一行
-->
