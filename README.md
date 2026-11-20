# 时笺 — TimeAwareness

为 LLM 提供现实时间、角色时段状态、工作日/农历/黄历、日历事项与上次对话间隔感知。

## 功能

- 时间引导：让角色用“刚才”“大半夜”“好久不见”等自然表达时间。
- 单日日程表：静态日程配置 + 按 Persona 每天自动生成、按时间冻结的 AI 日程快照（同一 Persona 的多个 Bot 共享）；用户可通过 WebUI 覆盖未来时段，也可开启工具让角色自己改写日程。
- 时间传感器：工作日、农历、黄历、现实日历事件、上次对话间隔，以及可选的未来 N 天日历事项预告。
- AI 日历：按世界观生成全年节日、纪念日和季节事项。
- Plugin Page：概览（含「查看今日随机天气」）、日历、静态日程管理，以及「人格日程」查看 + 用户覆盖层编辑；其他低频配置仍使用 AstrBot 主配置页。

## 安装

将本目录放入 AstrBot 插件目录，重启 AstrBot 加载。部署时仅复制插件源码（排除 `.git` 与运行数据文件），不要覆盖测试端独立的 git 状态或运行数据。

## 配置

AstrBot 主配置页采用渐进显示：日程、AI 日程与自适应增强的子项只在对应上级开关启用后挂载；人设预萃取与其模型选择只在「使用当前 Persona」开启后出现；隐藏不影响已保存值。静态日程可用主配置页的折叠模板编辑器（内置「时段」「睡眠」「午餐」「晚餐」预设），也可用 Plugin Page 的分页编辑器；条目较多时后者更流畅。

### `time_awareness`

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `time_guidance_enabled` | 时间规则、现实时间和动态感知总开关 | `true` |
| `time_sensors` | 时间传感器多选：`workday` 工作日、`lunar` 农历、`almanac` 黄历；勾选任意一项即启用，全部不选则都不注入 | `["workday", "lunar"]` |
| `upcoming_days` | 未来日历事项预告天数（1–14）：逐日注入日期、星期、工作日性质与当天日历事项，供模型回答「明天/后天有什么安排」；不注入 AI 生成的未来日程（那是 `ai_daily.plan_horizon_hours`）。`0` 关闭 | `0` |
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
| `ai_daily.generation_time` | `HH:MM` 当天生成当天；`-HH:MM` 前一天提前生成次日 | `00:05` |
| `ai_daily.max_slots` | AI 输出时段数的期望值（1–48，约这个数不是硬上限；模型输出超过 60 段整体判为异常并重试） | `24` |
| `ai_daily.retention_days` | 所有 Persona 快照保留天数（包含当天） | `30` |
| `ai_daily.adaptive.enabled` | 自适应总开关（见下文） | `false` |
| `ai_daily.adaptive.recent_days` | 参考最近日程天数，并决定连续性回溯窗口；`0` 关闭（连续性窗口缺省 7 天），`1–30` 启用 | `0` |
| `ai_daily.adaptive.state_continuity_enabled` | 角色状态连续性 | `false` |
| `ai_daily.adaptive.candidate_count` | 独立规划方案数（每份消耗一次调用） | `1` |
| `ai_daily.adaptive.candidate_selection` | 方案选择方式：本地评分或额外 LLM 评选 | `heuristic` |
| `ai_daily.adaptive.theme_pool` | 每日主题类型池；Step1 从中挑选当日主题，清空=自由标签 | 内置 12 种 |
| `ai_daily.adaptive.style_pool` | 每日状态色彩池；Step1 从中挑选措辞风格，清空=关闭该维度 | 内置 10 种 |
| `ai_daily.adaptive.allow_custom_theme` | 池内无合适项时允许自创主题/风格（禁「常规日」类空泛词）；关闭=严格池内 | `true` |
| `ai_daily.ai_priority_over_static` | AI 优先于静态日程（关闭时静态优先）；不受 `adaptive.enabled` 控制 | `false` |
| `ai_daily.allow_ai_edit` | 允许 AI 改写日程：给模型注入 `daily_schedule` 工具（查看日程并按「已过 / 可改 / 可覆写」增改删） | `false` |
| `ai_daily.plan_horizon_hours` | 计划注入窗口（小时，0–48）；0=不注入 `<SCHEDULE_PLAN>`。预生成天数按触发模式与窗口自动反推——保证任一时刻的未来注入窗口都有计划日程（24h → 2 天、48h → 3 天） | `24` |
| `ai_daily.persona_summary_enabled` | 启用**人设预萃取**：把角色提示词提炼成作息档案，让模型照着排一天；生成更快更省，日程也更贴人设 | `false` |
| `ai_daily.persona_summary_provider_id` | 萃取使用的模型（下拉选择）；**不选则跟随主对话模型**（`ai_daily.provider_id`）。建议与生成分开且只用 `@none` 思考档 | `""` |
| `ai_daily.edit_requires_admin` | 仅管理员可让 AI 改写日程；关闭时任何会话都能触发 | `false` |
| `ai_daily.random_weather_enabled` | 启用随机天气：每天随机当日天气基调（白天/黑夜各一段），对话注入、日程生成参考 | `false` |
| `ai_daily.weather_pool` | 天气权重池（每行「天气,权重」）；留空=内置默认 | 内置 7 种 |

时段采用左闭右开规则：开始时刻计入，结束时刻不计入；`24:00` 只允许作为当日不可命中的右边界。相邻时段请首尾衔接（如 `07:00–07:30` 与 `07:30–08:00`）；结束时间早于开始时间表示跨午夜，写入快照时拆成两个非跨日片段。

AI 每日日程以「当前生效 Persona + 本地日期」为作用域：同一 Persona 的多个 Bot 共享同一份日程，切换 Persona 立即切换快照。`generation_time=HH:MM` 在当天该时刻生成当天日程；前导负号表示提前一天，例如 `-23:30` 会在今晚 23:30 生成明天的完整日程（快照日期、传感器和连续性均按明天计算）。首次遇到新 Persona 且当天缺快照时异步补生成；首次消息不会等待模型，本轮直接使用静态日程。某天生成失败后，下一轮扫描会接着补（快照不存在就重新生成）；定时扫描不受限速约束，只有消息触发路径对「今天」做 30 分钟限速，避免上游故障时每条消息都打一次模型。扫描只覆盖最近 24 小时内活跃过的会话——某个人格整天没说话就不再为它预生成，下次开口时按当天补。`/schedule regenerate` 可立即重试。AI 输出允许只描述有明确状态特征的稀疏时段，也允许合法的空日程（`[]`，落盘为可用快照）；当前时刻按「AI → 静态 → 无固定安排」解析。重叠、非法时间或危险文本仍会拒绝新结果并保留旧快照。Persona 是必备身份：解析失败时不生成快照，当前消息走静态日程/「无固定安排」。

**分层来源与冻结**（2.0.0）：快照持久化「已执行时间线 + 用户层 + AI 层 + 静态层」四层，读取时动态合并。已执行区间在每次修改操作时按服务端实际接收时刻冻结（非整分钟向上取整到下一整分钟），冻结后不可被任何来源改写或回填；用户层高于 AI 与静态日程，AI 与静态的优先级由 `ai_daily.ai_priority_over_static` 决定；用户时段互不重叠，删除未来用户时段后底层自动恢复。WebUI 中 AI 与静态日程卡片始终只读，用户通过新增/修改覆盖层影响未来时间线；`/schedule regenerate` 保留已执行部分、清空未执行用户层并替换未来 AI 日程。

**AI 改写日程**（`daily_schedule.ai_daily.allow_ai_edit`，默认关闭）：开启后主模型会拿到 `daily_schedule` 工具（`list` / `add` / `update` / `delete`），工具给每段日程标一个状态：**已过**（时间已过去，不能改）、**可改**（`update`/`delete`，带 id）、**可覆写**（不能直接改，只能用 `add` 在同一时间段盖住）。写入落在与 WebUI 手动编辑相同的那一层，共用服务端校验（冻结点、不重叠、名称/状态长度上限），因此不会被每日 AI 补生成覆盖，`/schedule regenerate` 才会清空未执行的改写段；要撤销一次覆盖就 `delete` 它，底层内容原样恢复。写入有 5 分钟宽限（`domain/schedule_edit.py::EDIT_START_GRACE_MINUTES`）：LLM 生成与网络往返会让请求到达时冻结点已前移，起点落在窗内就静默前移到冻结点、不报错（绝不写过去），窗外的过去起点仍拒绝。`edit_requires_admin=true` 时只有发送者角色为管理员的会话能真正写入（只读查看不受限），其他会话的写入调用会被明确拒绝（工具保持可见，避免模型凭空声称已改）。工具注入需同时满足：`time_awareness.time_guidance_enabled`（模型要先看得到时间与日程上下文）、`enable_schedule` + `ai_daily.enabled`、且目标日期存在快照；过去日期与尚未生成的次日无法改写。

**日程文本的注入防线**：日程文本会被塞进 `<SCHEDULE_STATE>`、`<SCHEDULE_DONE>`、`<SCHEDULE_PLAN>`、`<TODAY_EVENTS>` 等标记块，所以 ① 人格日程与静态日程的保存都拒绝控制字符与尖括号（需要书写尖括号请用全角 ＜＞，AI 工具写入同样受这一道门约束）；② 注入前还会对全部插值内容做一次中和——尖括号换成全角、丢弃控制字符与零宽字符，这样即使绕过插件（例如在 AstrBot 主配置页直接编辑 `schedule_templates`）或读到历史数据，内容也无法闭合或伪造插件的标记块；③ `daily_schedule` 工具返回给模型的日程文本走同一条中和（工具结果不经插件的标签渲染路径，是独立的一条注入面）。

**人设预萃取**（`daily_schedule.ai_daily.persona_summary_enabled`，默认关闭）：角色提示词是为「对话扮演」写的，话术示例、好感度分级、emoji/格式规则通常占 60–80%，对日程生成是纯噪声，还会把语义重心带偏（实测：塔什干被生成成裁缝、数字少女被生成成人类起居）。启用后先用一次 LLM 调用把人设压成九项结构化摘要（身份/场所/作息锚点/体质/性格/兴趣/社交/限制/具体物品），其中「无依据但可推导」的作息锚点会标「（推断）」；摘要按「人设文本指纹 + 提示词版本」落盘缓存，人设一改自动重萃。实测单次生成输入 15.5k → 7.2k token（−53%）、延迟 −35%，且生成内容更贴设定。萃取模型可单独配置：生成用 flash、萃取用 pro 是实测最优组合。判定成功的标准是**压缩比例**：摘要 ≥ 原人设 80% 视为没压缩（等于复述），按失败处理；人设短于 100 字直接不萃取。阈值写死，不开放配置。萃取失败（网络/解析/未压缩）会**回退全量人设并记 WARNING**——宁可多花 token，也不用人设残缺的摘要去生成。

**请求超时与重试**：插件不设自有超时——AstrBot 的超时是 provider 级配置（构造时读一次）。需要更长的后台超时，就在 AstrBot 里**单独建一个模型提供商条目**（同一个 api_base/key，只把「超时时间」调大，例如 300 秒），再把 `ai_daily.provider_id` / `persona_summary_provider_id` 指过去：对话主链仍是短超时快速失败，后台批量有足够余量，也不用改 AstrBot 源码。重试次数由 `runtime.llm_max_attempts` 统一控制——`1–5` 时插件钉 `request_max_retries=1` 把重试权收回配置，`0` 时放手让 AstrBot 用自己的默认策略（每步仍至少打一次）。

**自适应增强**（`daily_schedule.ai_daily.adaptive`，默认全关）：让日程在保持角色稳定习惯的同时产生有因果的日变化。`recent_days=0` 关闭最近日程参考，`1–30` 决定参考与反重复范围（同时决定连续性回溯窗口）：与最近日程结构相同的输出会触发定向重生成，最近用过的主题/风格会被要求避开。角色状态连续性由评估回溯窗口内（缺省 7 天）最近一次可用状态，并携带自该边界日起的实际日程（含用户修改与静态兜底）推导今日初始状态；断链超窗视为全新开始，由评估依据人设与传感器推导初始状态；`daily_theme`（主题类型）与 `daily_style`（措辞风格）由评估从 `theme_pool`/`style_pool` 中挑选并一并输出。`candidate_count=N` 独立生成 N 份方案；`candidate_selection=heuristic` 本地评分，`llm` 额外调用一次模型评选并失败回退本地评分。增强上下文不做 token 裁剪：内容量由 `recent_days`（1–30 天，每天一条摘要）与单日的昨日状态自行界定，上下文窗口归 provider 管，插件不读也不算。重试按单步独立进行：状态评估、方案生成、重复检测与评选各自最多尝试 `runtime.llm_max_attempts` 次，评估失败以空状态继续、评选失败回退本地评分、反重复失败保留原候选——不存在整工作流 × 单步骤的乘法重试。关闭自适应总开关时流程与基线一致（一次模型调用）。

生成输入包括日期、长期世界观、当前 Persona（启用预萃取时为结构化摘要，关闭时为完整 prompt）、已启用的传感器，以及启用随机天气时的当日两段天气基调预报（`<WEATHER_FORECAST>`）。插件不读取会话原始历史，避免跨会话传播。

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
| `llm_max_attempts` | **所有 LLM 调用**（日程生成/评估/评选/人设萃取/日历创建）的最大尝试次数（1–5，含首次）；`0` = 使用 AstrBot 默认重试次数 | `3` |

### `log_with_bot_id`

顶层全局配置项（默认 `false`）：在日志前缀中显示平台 ID，便于多 Bot 区分。日志级别跟随 AstrBot 原生配置；插件级 debug 提级已移除（原 `log` 配置组拆除）。

## 动态注入

静态规则 `<TIME_GUIDE>` 放在 `system_prompt`，动态内容放在当前轮末尾，不写入历史：

```xml
<SYSTEM_REMINDER>当前日期时间：YYYY-MM-DD HH:MM（时区），星期X</SYSTEM_REMINDER>
<SCHEDULE_STATE>当前命中的角色状态</SCHEDULE_STATE>
<SCHEDULE_DONE>今天已经过去的时段</SCHEDULE_DONE>
<SCHEDULE_PLAN>从此刻起的行程计划（按天分段）</SCHEDULE_PLAN>
<WEATHER>今日天气基调</WEATHER>
<WORKDAY_STATE>工作日性质</WORKDAY_STATE>
<LUNAR_STATE>农历日期</LUNAR_STATE>
<ALMANAC_STATE>黄历信息</ALMANAC_STATE>
<TODAY_EVENTS>当天事项</TODAY_EVENTS>
<UPCOMING_DAYS>未来日历事项预告</UPCOMING_DAYS>
<LAST_CHAT>上次对话间隔</LAST_CHAT>
```

`<UPCOMING_DAYS>` 仅在配置了 `upcoming_days` 时出现，每行一天：`明天 09-26 工作日性质：事项`。前三行带「明天/后天/大后天」标记，其后只给日期；确认无事项写「无事项」。事项段要求 `calendar.enable_calendar` 开启且该年启用的内置分类都已生成——关闭、跨年尚未重生成，或某分类为空（例如 `chinese_calendar` 未覆盖该年导致法定节假日缺失）时整段省略，表示事项未知而非「没有事项」。

**预生成的局限**：未来几天的日程是在那些天还没发生时提前生成的，模型参考不到“当天实际发生了什么”（用户临时约的事、临时加班等都可能晚于预生成时刻）。若想让未来日程吸收今天真实发生的情况，只能手动 `/schedule regenerate [天数|all]` 重做（串行逐日，后一天参考前一天的收尾状态）；`all` 等于当前注入窗口的天数。

`<SCHEDULE_STATE>` 在日程未启用或未命中时为“无固定安排（按人设自然演绎）”；`<SCHEDULE_DONE>` / `<SCHEDULE_PLAN>` 按“已发生 / 未发生”切分同一份时间线——前者是冻结点之前已经过去的段（一行 `HH:MM-HH:MM 名称 | …`），后者是从此刻铺到 `ai_daily.plan_horizon_hours` 的行程，按天分段（`今天 10-20 周二`）且自带“还没发生、届时以实际情况为准”的说明句，没有时段的日子写「（未规划）」；两者都没有内容时整块省略；`<WEATHER>` 仅在启用随机天气时出现；其他标签在没有数据或关闭对应传感器时省略。这三块日程信息都只是**背景资料**：`<TIME_GUIDE>` 明确要求角色默认不播报——不要用“我现在在……”开头或“我等下要去……”结尾，同一段对话里说过的安排不重复；只在用户问时间/问你在做什么/问今天做了什么/问未来安排、或它确实影响本轮回复（在忙只能简短回、要晚点再聊、约时间）时才自然带一句，且只答被问的那件事；被问到时不得用「没干嘛」回避。普通消息链使用临时 `extra_user_content_parts`。ChatMemory takeover contexts 已逐条携带 `<cm_time>`，所以它实际接管本轮请求时不会重复注入 `<LAST_CHAT>`；本地时间记录仍继续维护。

## 命令

| 命令 | 权限 | 作用 |
|---|---|---|
| `/calendar show [YYYY-MM \| all]` | 所有人 | 查看某月事项；`all` 列出全部自定义事项（跨年份）与当年内置事件 |
| `/calendar add <日期> [重复] <标题>` | 管理员 | 添加事项 |
| `/calendar del <id>` | 管理员 | 删除自定义事项 |
| `/calendar create` / `export` / `import [replace]` | 管理员 | 按世界观重新生成并替换自定义日历、导出、导入（默认合并，加 `replace` 先清空再导入）；内置事件不受影响 |
| `/calendar builtin_list [分类]` / `builtin_regen` | 管理员 | 查看或刷新内置事件 |
| `/schedule show` / `static` / `help` | 所有人 | 查看今日实际日程、静态日程配置或帮助 |
| `/schedule summary` | 所有人 | 查看当前人格的人设摘要（身份/场所/作息锚点/体质/兴趣/社交/限制），未萃取时提示下一步 |
| `/schedule summary_rebuild` | 管理员 | 强制重萃取当前人格的人设摘要（清缓存、重跑一次 LLM） |
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

- 数据目录：`data/plugin_data/time_awareness/`，包括日历、上次对话时间、按需创建的 AI 日程快照/HMAC secret，以及启用预萃取后才出现的 `persona_summaries.yaml`（按人设指纹缓存的作息档案，最多 64 条，可直接删除以强制重萃）。AI 每日日程关闭时不会创建对应文件；快照不保存原始 Bot/用户/群 ID、Persona prompt 或对话历史。
- WebUI 提供概览、日历、「静态日程」与「人格日程」视图。静态日程使用分页紧凑列表和单一编辑弹窗，保存时以 revision 防止多页面互相覆盖。人格日程页可按 Persona/日期查看快照：已执行、AI 与静态日程卡片始终只读，用户通过新增/修改用户时段覆盖未来时间线（完全未来可改全部字段，跨冻结点尾段仅可改结束时间或删除）；保存请求只提交用户层，服务端按最新冻结点重新合并。人工修改后的日程不会被 AI 每日补生成覆盖（`/schedule regenerate` 可恢复 AI 版本），并作为次日「昨日实际日程」参与连续性规划。
- Python ≥ 3.10；AstrBot `>=4.26.0,<5`；依赖 `pyyaml`、`chinese_calendar`、`lunar_python`，Windows 另按条件安装 `tzdata` 以支持 IANA 时区。

## 从 v2.3.4 升级到 v2.3.5

- **重试次数换位置**：`daily_schedule.ai_daily.max_attempts` → `runtime.llm_max_attempts`（对生成/评估/评选/人设萃取/日历生成统一生效）。旧键会被 AstrBot 依 schema 清理，**值不会自动迁移**，原来配成 1 或 5 的需要在新位置重设（默认 3）。`0` 表示不钉插件重试、直接用 AstrBot 默认策略。
- **删除的配置**：`daily_schedule.ai_daily.adaptive.acquire_timeout_seconds`（并发队列不再设超时）、`daily_schedule.ai_daily.adaptive.context_token_budget`（插件不再设 token 上限）、`runtime.state_retention_days`（锁回收窗口写死 2 天）、以及与新式多选重复的旧开关 `time_awareness.workday_state_enabled` / `lunar_state_enabled` / `almanac_enabled`、`calendar.enable_builtin_events` / `builtin_events`、`daily_schedule.ai_daily.adaptive.max_concurrent_llm`。旧布尔开关同样不迁移——手改过它们的请到 `time_sensors` / `builtin_event_categories` 多选里重设。
- **新增（默认关闭）**：`ai_daily.persona_summary_enabled` + `ai_daily.persona_summary_provider_id`，启用后日程生成先用人设作息档案；配套命令 `/schedule summary`、`/schedule summary_rebuild`。
- **超时**：插件不再提供超时配置，也不需要对 AstrBot 打补丁。需要更长的后台超时请在 AstrBot 里单独建一个模型提供商条目，再让 `ai_daily.provider_id` 指过去。

## 从 v2.2 升级到 v2.3

主动提醒能力已移除：`reminder` 配置组、`schedule_followup` 工具、`create_external_task` 插件 API、Plugin Page「任务」视图与 `reminder_tasks.yaml` 均不再使用（旧文件可手动删除，旧配置键由 AstrBot 依 schema 清理）。定时提醒需求请改用 AstrBot 内置的 `future_task` 工具（由 `provider_settings.proactive_capability.add_cron_tools` 控制）。

## 从 v2.1 升级到 v2.2

旧配置形态不再自动迁移（布尔开关→多选、两代 `ai_generation` 分组）；升级后请到配置页重设 AI 生成参数与传感器/内置事件多选；旧键会被 AstrBot 依 schema 自动清理。
