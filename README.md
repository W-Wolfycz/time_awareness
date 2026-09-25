# 时笺 — TimeAwareness

为 LLM 提供现实时间、角色时段状态、工作日/农历/黄历、日历事项与上次对话间隔感知。

## 功能

- 时间引导：让角色用“刚才”“大半夜”“好久不见”等自然表达时间。
- 单日日程表：静态日程配置 + 按 Persona 每天自动生成、按时间冻结的 AI 日程快照（同一 Persona 的多个 Bot 共享）；用户可通过 WebUI 覆盖未来时段。
- 时间传感器：工作日、农历、黄历、现实日历事件、上次对话间隔。
- AI 日历：按世界观生成全年节日、纪念日和季节事项。
- Plugin Page：概览（含「查看今日随机天气」）、日历、静态日程管理，以及「人格日程」查看 + 用户覆盖层编辑；其他低频配置仍使用 AstrBot 主配置页。

## 安装

将本目录放入 AstrBot 插件目录，重启 AstrBot 加载。部署时仅复制插件源码（排除 `.git` 与运行数据文件），不要覆盖测试端独立的 git 状态或运行数据。

## 配置

AstrBot 主配置页采用渐进显示：日程、AI 日程、自适应增强和内置事件的子项只在对应上级开关启用后挂载；隐藏不影响已保存值。静态日程可用主配置页的折叠模板编辑器（内置「时段」「睡眠」「午餐」「晚餐」预设），也可用 Plugin Page 的分页编辑器；条目较多时后者更流畅。

### `time_awareness`

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `time_guidance_enabled` | 时间规则、现实时间和动态感知总开关 | `true` |
| `time_sensors` | 时间传感器多选：`workday` 工作日、`lunar` 农历、`almanac` 黄历；勾选任意一项即启用，全部不选则都不注入 | `["workday", "lunar"]` |
| `last_chat_enabled` | 上次对话间隔感知（独立开关）；ChatMemory 实际接管本轮时自动省略重复标签，但继续记录 | `true` |
| `use_astrbot_timezone` | 跟随 AstrBot 全局时区 | `false` |
| `timezone` | IANA 时区名 | `""` |

本插件独立注入中文 `<SYSTEM_REMINDER>`；AstrBot 自带开关仍会注入小写 `<system_reminder>`。为避免重复占用 token，建议关闭 `provider_settings.datetime_system_prompt`，并让本插件跟随 AstrBot 时区。

### `daily_schedule`

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `enable_schedule` | 启用日程表 | `false` |
| `schedule_templates` | 静态日程时段；主配置中排在 AI 日程之前，也可用 Plugin Page 分页管理 | `[]` |
| `ai_daily.enabled` | 启用每 Persona 每日 AI 日程 | `false` |
| `ai_daily.provider_id` | AI 每日日程生成模型；留空使用 AstrBot 全局默认 Provider | `""` |
| `ai_daily.use_persona` | 输入当前实际生效 Persona，建议保持开启 | `true` |
| `ai_daily.worldview` | 稳定世界观/长期背景，不等于每日 theme | `""` |
| `ai_daily.max_attempts` | 每个独立 LLM 步骤的最大尝试次数（1–5，包含首次） | `3` |
| `ai_daily.generation_time` | `HH:MM` 当天生成当天；`-HH:MM` 前一天提前生成次日 | `00:05` |
| `ai_daily.max_slots` | AI 输出时段数的期望值（约这个数，非硬上限；超出 60 条被安全上限截断） | `24` |
| `ai_daily.retention_days` | 所有 Persona 快照保留天数（包含当天） | `30` |
| `ai_daily.adaptive.enabled` | 自适应总开关（见下文） | `false` |
| `ai_daily.adaptive.recent_days` | 参考最近日程天数，并决定连续性回溯窗口；`0` 关闭（连续性窗口缺省 7 天），`1–30` 启用 | `0` |
| `ai_daily.adaptive.state_continuity_enabled` | 角色状态连续性 | `false` |
| `ai_daily.adaptive.candidate_count` | 独立规划方案数（每份消耗一次调用） | `1` |
| `ai_daily.adaptive.candidate_selection` | 方案选择方式：本地评分或额外 LLM 评选 | `heuristic` |
| `ai_daily.adaptive.context_token_budget` | 增强上下文 Token 上限；`0` 自动按 Provider 上下文窗口的 80% 分配 | `0` |
| `ai_daily.adaptive.max_concurrent_llm` | 插件 LLM 并发上限的旧位置；仅在 `runtime.llm_max_concurrency=0` 时回退使用 | `1` |
| `ai_daily.adaptive.acquire_timeout_seconds` | 并发等待超时（0–120） | `10` |
| `ai_daily.adaptive.theme_pool` | 每日主题类型池；Step1 从中挑选当日主题，清空=自由标签 | 内置 12 种 |
| `ai_daily.adaptive.style_pool` | 每日状态色彩池；Step1 从中挑选措辞风格，清空=关闭该维度 | 内置 10 种 |
| `ai_daily.adaptive.allow_custom_theme` | 池内无合适项时允许自创主题/风格（禁「常规日」类空泛词）；关闭=严格池内 | `true` |
| `ai_daily.ai_priority_over_static` | AI 优先于静态日程（关闭时静态优先）；不受 `adaptive.enabled` 控制 | `false` |
| `ai_daily.random_weather_enabled` | 启用随机天气：每天随机当日天气基调（白天/黑夜各一段），对话注入、日程生成参考 | `false` |
| `ai_daily.weather_pool` | 天气权重池（每行「天气,权重」）；留空=内置默认 | 内置 7 种 |

时段采用左闭右开规则：开始时刻计入，结束时刻不计入；`24:00` 只允许作为当日不可命中的右边界。相邻时段请首尾衔接（如 `07:00–07:30` 与 `07:30–08:00`）；结束时间早于开始时间表示跨午夜，写入快照时拆成两个非跨日片段。

AI 每日日程以「当前生效 Persona + 本地日期」为作用域：同一 Persona 的多个 Bot 共享同一份日程，切换 Persona 立即切换快照。`generation_time=HH:MM` 在当天该时刻生成当天日程；前导负号表示提前一天，例如 `-23:30` 会在今晚 23:30 生成明天的完整日程（快照日期、传感器和连续性均按明天计算）。首次遇到新 Persona 且当天缺快照时异步补生成；首次消息不会等待模型，本轮直接使用静态日程。AI 输出允许只描述有明确状态特征的稀疏时段，也允许合法的空日程（`[]`，落盘为可用快照）；当前时刻按「AI → 静态 → 无固定安排」解析。重叠、非法时间或危险文本仍会拒绝新结果并保留旧快照。Persona 是必备身份：解析失败时不生成快照，当前消息走静态日程/「无固定安排」。

**分层来源与冻结**（2.0.0）：快照持久化「已执行时间线 + 用户层 + AI 层 + 静态层」四层，读取时动态合并。已执行区间在每次修改操作时按服务端实际接收时刻冻结（非整分钟向上取整到下一整分钟），冻结后不可被任何来源改写或回填；用户层高于 AI 与静态日程，AI 与静态的优先级由 `ai_daily.ai_priority_over_static` 决定；用户时段互不重叠，删除未来用户时段后底层自动恢复。WebUI 中 AI 与静态日程卡片始终只读，用户通过新增/修改覆盖层影响未来时间线；`/schedule regenerate` 保留已执行部分、清空未执行用户层并替换未来 AI 日程。

**自适应增强**（`daily_schedule.ai_daily.adaptive`，默认全关）：让日程在保持角色稳定习惯的同时产生有因果的日变化。`recent_days=0` 关闭最近日程参考，`1–30` 决定参考与反重复范围（同时决定连续性回溯窗口）：与最近日程结构相同的输出会触发定向重生成，最近用过的主题/风格会被要求避开。角色状态连续性由评估回溯窗口内（缺省 7 天）最近一次可用状态，并携带自该边界日起的实际日程（含用户修改与静态兜底）推导今日初始状态；断链超窗视为全新开始，由评估依据人设与传感器推导初始状态；`daily_theme`（主题类型）与 `daily_style`（措辞风格）由评估从 `theme_pool`/`style_pool` 中挑选并一并输出。`candidate_count=N` 独立生成 N 份方案；`candidate_selection=heuristic` 本地评分，`llm` 额外调用一次模型评选并失败回退本地评分。`context_token_budget=0` 时以 Provider 上下文窗口 80% 为总输入目标，扣除固定 Prompt、Persona、世界观、传感器和静态日程后使用剩余额度；取不到上限时回退 8000 Token。重试按单步独立进行：状态评估、方案生成、重复检测与评选各自最多尝试 `max_attempts` 次，评估失败以空状态继续、评选失败回退本地评分、反重复失败保留原候选——不存在整工作流 × 单步骤的乘法重试。关闭自适应总开关时流程与基线一致（一次模型调用）。

生成输入包括日期、长期世界观、当前 Persona、已启用的传感器，以及启用随机天气时的当日两段天气基调预报（`<WEATHER_FORECAST>`）。Persona prompt 每日完整传入，不截断、不额外调用模型预压缩；插件不读取会话原始历史，避免跨会话传播。

### `calendar`

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `enable_calendar` | 注入当日事项 | `false` |
| `ai_generate_provider_id` | `/calendar create` 使用的模型 | `""` |
| `ai_generate_worldview` | 日历生成世界观/主题 | `""` |
| `ai_generate_max_events` | `/calendar create` 单次生成事项条数期望值（约这个数，非硬上限；超出 400 条被安全上限截断） | `40` |
| `builtin_event_categories` | 内置现实事件分类多选：`legal_holidays` 法定节假日、`traditional` 传统节日、`solar_terms` 节气、`political` 政治纪念日、`international` 国际节日；勾选任意一项即启用内置现实事件，全部不选则关闭 | `["legal_holidays", "traditional", "solar_terms"]` |

### `runtime`

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `llm_max_concurrency` | AI 日程与 `/calendar create` 共用的插件级并发上限（1–4）；`0`=默认 1 | `0` |
| `state_retention_days` | 过期日程生成状态与终态锁的回收窗口（1–365），不影响快照保留 | `7` |

### `log_with_bot_id`

顶层全局配置项（默认 `false`）：在日志前缀中显示平台 ID，便于多 Bot 区分。日志级别跟随 AstrBot 原生配置；插件级 debug 提级已移除（原 `log` 配置组拆除）。

## 动态注入

静态规则 `<TIME_GUIDE>` 放在 `system_prompt`，动态内容放在当前轮末尾，不写入历史：

```xml
<SYSTEM_REMINDER>当前日期时间：YYYY-MM-DD HH:MM（时区），星期X</SYSTEM_REMINDER>
<SCHEDULE_STATE>当前命中的角色状态</SCHEDULE_STATE>
<TODAY_SCHEDULE>今日时段安排摘要</TODAY_SCHEDULE>
<WEATHER>今日天气基调</WEATHER>
<WORKDAY_STATE>工作日性质</WORKDAY_STATE>
<LUNAR_STATE>农历日期</LUNAR_STATE>
<ALMANAC_STATE>黄历信息</ALMANAC_STATE>
<TODAY_EVENTS>当天事项</TODAY_EVENTS>
<LAST_CHAT>上次对话间隔</LAST_CHAT>
```

`<SCHEDULE_STATE>` 在日程未启用或未命中时为“无固定安排（按人设自然演绎）”；`<WEATHER>` 仅在启用随机天气时出现；其他标签在没有数据或关闭对应传感器时省略。普通消息链使用临时 `extra_user_content_parts`。ChatMemory takeover contexts 已逐条携带 `<cm_time>`，所以它实际接管本轮请求时不会重复注入 `<LAST_CHAT>`；本地时间记录仍继续维护。

## 命令

| 命令 | 权限 | 作用 |
|---|---|---|
| `/calendar show [YYYY-MM]` | 所有人 | 查看月份事项 |
| `/calendar add <日期> [重复] <标题>` | 管理员 | 添加事项 |
| `/calendar del <id>` | 管理员 | 删除自定义事项 |
| `/calendar create` / `export` / `import [replace]` | 管理员 | 按世界观重新生成并替换自定义日历、导出、导入（默认合并，加 `replace` 先清空再导入）；内置事件不受影响 |
| `/calendar builtin_list [分类]` / `builtin_regen` | 管理员 | 查看或刷新内置事件 |
| `/schedule show` / `static` / `help` | 所有人 | 查看今日实际日程、静态日程配置或帮助 |
| `/schedule regenerate` | 管理员 | 强制重生成当前 Persona 的今日 AI 日程（保留已执行部分、清空未执行用户层） |

日期支持 `YYYY-MM-DD` 或 `MM-DD`；重复参数 `0` 表示仅当年，`1–4` 表示额外重复年数，`9` 表示每年重复。

## 推荐配置组合

| 目标 | 关键配置 |
|---|---|
| 只需要时笺 | `time_guidance_enabled=true`，日程/日历保持关闭 |
| 静态角色作息 | `enable_schedule=true`、`ai_daily.enabled=false`、填写 `schedule_templates`（无每日 LLM 调用） |
| 基础 AI 每日日程 | `enable_schedule` 与 `ai_daily.enabled` 开启，`adaptive.enabled=false`（每 Persona/日期通常一次调用） |
| 低成本动态化 | `adaptive.enabled=true`、`recent_days=3`、`state_continuity_enabled=true`、`candidate_count=1`、`candidate_selection=heuristic` |
| 高质量多候选 | `adaptive.enabled=true`、`candidate_count=3`、`candidate_selection=llm`、并发 2–3（token 与并发占用明显增加） |

## 数据、WebUI 与依赖

- 数据目录：`data/plugin_data/time_awareness/`，包括日历、上次对话时间和按需创建的 AI 日程快照/HMAC secret。AI 每日日程关闭时不会创建对应文件；快照不保存原始 Bot/用户/群 ID、Persona prompt 或对话历史。
- WebUI 提供概览、日历、「静态日程」与「人格日程」视图。静态日程使用分页紧凑列表和单一编辑弹窗，保存时以 revision 防止多页面互相覆盖。人格日程页可按 Persona/日期查看快照：已执行、AI 与静态日程卡片始终只读，用户通过新增/修改用户时段覆盖未来时间线（完全未来可改全部字段，跨冻结点尾段仅可改结束时间或删除）；保存请求只提交用户层，服务端按最新冻结点重新合并。人工修改后的日程不会被 AI 每日补生成覆盖（`/schedule regenerate` 可恢复 AI 版本），并作为次日「昨日实际日程」参与连续性规划。
- Python ≥ 3.10；AstrBot `>=4.26.0,<5`；依赖 `pyyaml`、`chinese_calendar`、`lunar_python`，Windows 另按条件安装 `tzdata` 以支持 IANA 时区。

## 从 v2.2 升级到 v2.3

主动提醒能力已移除：`reminder` 配置组、`schedule_followup` 工具、`create_external_task` 插件 API、Plugin Page「任务」视图与 `reminder_tasks.yaml` 均不再使用（旧文件可手动删除，旧配置键由 AstrBot 依 schema 清理）。定时提醒需求请改用 AstrBot 内置的 `future_task` 工具（由 `provider_settings.proactive_capability.add_cron_tools` 控制）。

## 从 v2.1 升级到 v2.2

旧配置形态不再自动迁移（布尔开关→多选、两代 `ai_generation` 分组）；升级后请到配置页重设 AI 生成参数与传感器/内置事件多选；旧键会被 AstrBot 依 schema 自动清理。
