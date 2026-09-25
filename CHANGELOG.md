# 更新日志

## 2.3.0 — 2026-09-25

### 变更

- **死代码/冗余参数清理**：删除无引用函数与方法（`timeline.subtract_slots`、`persona_resolver.resolve_persona_prompt`、`CalendarManager.get_events`/`update_event`、`BuiltinManager.file_path`、`DailyScheduleService.ensure_available`、`TimeContextService.resolve_schedule_state`）、重构后失效的参数（日程解析的 `max_slots`、`_resolve_provider_id` 的 session、`parse_year_month` 的 now、`generate_calendar_events` 的 max_events）、两个前端从不调用的 Web API 端点（`schedules/delete`、`config/schema`）与前端死样式（任务视图遗留 9 条 + 无引用图例 2 条；人格日程的 slot 状态/来源类为动态拼接，已保留）
- **日志规范修正**：`log_with_bot_id` 启用时前缀由 `[time_awareness:{platform_id}]` 改为 `[time_awareness][platform:{platform_id}]`（统一格式）；`/calendar create` 的 JSON 解析失败与 LLM 响应异常不再于 WARNING 回显模型原文/响应对象（原文保留在 DEBUG）
- **主动提醒能力整体移除**：删除自研提醒调度器、提醒服务与装饰链发送适配（约 1500 行实现）；同时移除 `reminder` 配置组、`schedule_followup` 工具、`create_external_task` 插件 API、Plugin Page「任务」视图与相关测试。定时提醒需求改用 AstrBot 内置 `future_task` 工具（主动 Agent 链路）。保留：对话时间感知、AI 日程、日历、静态日程与人格日程

测试：74 项。

## 2.2.0 — 2026-09-02

### 新增

- **今日安排摘要**：对话注入 `<TODAY_SCHEDULE>`（每段 `HH:MM-HH:MM 名称`），角色可自然提及未来安排（如「今晚有演习」），仅在相关时提及、不逐条复述
- **天气变化窗口**：阵雨/转场类天气带确定性变化窗口（如「15:00-17:00 阵雨，其余多云」），UI 卡片、对话 `<WEATHER>` 与生成 `<WEATHER_FORECAST>` 同源展示；各时段具体天气由 LLM 写进 state 的现场感描写
- **跨设备天气区分**：天气种子加入持久化实例盐——同一部署内所有 Persona 共用当天天气，不同设备同一天天气不同

### 变更

- **代码大幅精简**：Python 23,608 → 10,542 行（-55%），删除开发期文档/脚本/测试缓存与死代码；结构级去重（静态日程与 AI 优先级逻辑收拢 `domain/schedule.py`、Web API 统一 `@_guarded` 异常外壳、前端渲染助手抽取），行为与旧版逐字节等价
- **旧配置迁移下线**：删除 `services/config_migration.py` 与 `_conf_schema.json` 旧分组兼容副本（顶层/嵌套 `ai_generation` 旧键、迁移标记）；传感器与内置事件分类保留运行时旧形态回退，存量行为不受影响；直接升级自旧版本时旧 `ai_generation` 分组将回落默认值（README 有升级说明）
- **时段数上限改为期望**：`max_slots` 由硬上限改为期望值（约这个数、可多可少），协议仅保留防失控安全上限 60
- **日历条数上限改为期望**：`ai_generate_max_events` 由硬上限改为期望值，协议仅保留防失控安全上限 400
- **天气归属**：天气明确为角色所在世界的天气（由系统随机设定），演绎在角色世界时自然提及、演绎成在用户身边陪伴时可不必套用
- **提示词协议升级**（`PROMPT_REVISION` v11）：Step1 休整主题克制（休整类主题仅数值状态或 physical_state 明确支持时可选、不得与最近一天同类、一周占比 ≤1/3），示例改为活跃日；Step2 示例反照抄化、休整类主题 ≠ 全天睡觉；升级后旧快照自动失效重新生成
- **测试套件重写**：按单测优先原则收敛为 81 项纯逻辑测试（共享桩 `tests/_shared.py` + `conftest.py`，无假 LLM 全链路伪单测），保留 timeline 核心不变量与天气种子/时区回归

测试：81 项。

## 2.1.0 — 2026-08-30

### 新增

- **每日主题与状态色彩池**：自适应分组新增 `adaptive.theme_pool`/`style_pool`（可编辑列表）注入 Step1，由模型从中挑选当日主题类型与措辞风格；`adaptive.allow_custom_theme` 控制池外自创（默认开、禁「常规日」类空泛词）；主题/风格反重复窗口跟随 `adaptive.recent_days`（0=关闭）
- **日程结构反同构**：候选时段起止序列与最近日程完全相同时触发定向重生成；提示词要求「三餐+午休」骨架最多连续两天，允许省略或合并常规时段
- **随机天气**：AI 日程新增 `random_weather_enabled` 开关与 `weather_pool` 权重池（每行「天气,权重」，留空=内置默认）——白天/黑夜各自按权重随机一种天气，日期种子同日稳定；对话注入一句话基调（如「晴转多云，20~31°C」，由 LLM 自行演绎），日程生成注入 `<WEATHER_FORECAST>` 并允许把基调落到各时段（不得自创其他天气）；预报=实况；Plugin Page 概览页可查看今日随机天气

### 修复

- **计划清零不再被错误继承**：边界评估输出空 `unfinished_plans`（当日计划已全部完成）不再被视为缺失，不会把昨日未完成计划继承到次日
- **生成失败原因不再丢失**：失败日志与失败记录（`detail` 落盘、`/schedule show` 可见）现在携带 Provider 底层异常全文，不再只有 `error=llm_error` 类型；每次重试尝试输出 DEBUG 明细，便于部署端定位
- **日志分级规范**：INFO 只保留用户可感知的重要事件（生成就绪/失败、配置迁移、WebUI 写操作、提醒触发与发送），内部例行事件（生成锁回收、会话清理、每日扫描汇总、裁判评选、终态任务清理等）降为 DEBUG，避免 INFO 刷屏

### 变更

- **state 允许场景描写**：每段可带一句与状态绑定的进行时场景句（如「在食堂和同僚边吃边聊」），仍禁止指令口吻/提醒/承诺；示例补充「非常规日·外出日」
- **人设提级**：Step1 挑选主题/风格时把人设气质提升为与状态、实际日程并列的重要依据，减少不同角色同日选择趋同
- **提示词协议升级**（`PROMPT_REVISION` v6）：升级后今日快照一次性重新生成；旧快照缺 `daily_style` 按 null 兼容，不迁移历史数据
- **日志补全**：生成管线、生成排队（含各跳过原因）、每日扫描、配置迁移、WebUI 写操作与 `/schedule regenerate` 补齐缺失日志

测试：330 项。

## 2.0.0
2026-08-22

> 1.8.1 之后至 2.0.0 的全部开发内容（含 1.9.x–1.12.x 各阶段演进）统一为一次发布。AI 每日日程为全新功能，无历史数据兼容承诺。

### 新增

- **AI 每日日程**：按 Persona 每天自动生成贴合人设的角色作息，同一 Persona 的多个 Bot 共享、切换人格立即切换；当前时刻按「AI 日程 → 静态日程 → 无固定安排」解析；支持提前一天生成次日；允许稀疏时段与合法空日程；快照保留 30 天
- **人格日程 WebUI**：按 Persona/日期查看当天时间线，已执行、AI 与静态日程卡片只读，用户可新增/修改自己的时段覆盖未来（删除后底层自动恢复），跨冻结边界自动拆分处理
- **冻结时间线**：已过去的时段按实际时刻固化、不再被改写；进行中的时段显示为「正在执行」而非切成两半；历史日整日只读、未来日可编辑
- **自适应增强**（默认关闭）：最近 N 天日程参考与反重复、角色状态连续性回溯、多候选 + LLM 评选、上下文 Token 预算——让每日日程在稳定习惯之上产生有因果的日变化
- **配置页与 Plugin Page**：主配置页渐进显示（子配置随上级开关挂载）、静态日程折叠模板编辑器（内置时段/睡眠/午餐/晚餐预设）；Plugin Page 新增分页静态日程管理与任务「已结束」历史区
- **AI 日历**：单次生成条数上限可配置（1–200，默认 40）；月份按实际天数生成，非法日期自动拒绝

### 优化

- **生成质量**：提示词统一结构与措辞、移除身份信息禁令；Persona 与世界观完整传入不再截断；裁判补全人设全文与最近日程参考；重试按步骤独立（评估/规划/反重复/评选各自降级，失败自动回退不整份作废）
- **提醒**：去重与插件时区基准一致；崩溃恢复受迟到容忍窗口约束（离线过久不深夜补发）；failed 任务 7 天后清理；无事件也问候默认开启
- **体验**：日志与 `/calendar export` 完整输出不再截断；任务取消幂等；隐藏视图不再后台空转；统计卡片数值转义
- **安全**：修复取消任务确认弹窗与日历日详情的存储型 XSS；任务与快照文件权限加固

### 更新

- **配置键迁移**（旧键自动一次性迁移）：三个传感器布尔合并为 `time_sensors` 多选；内置事件总开关 + 5 分类合并为 `builtin_event_categories` 多选；`log` 配置组拆除（`log_with_bot_id` 提升为顶层）；「全局运行时参数」与「主动提醒设置」在配置页上移
- **优先级开关**：`ai_priority_over_static` 决定 AI 与静态日程谁优先（独立于自适应开关），静态日程始终保留兜底
- **隐私**：快照只保存 Persona 的本地 HMAC 摘要，不含原始 Persona ID、Bot/会话 ID、人设或聊天记录
- **其他**：Hook 执行顺序调整以避开与同类插件的加载顺序依赖；`docs/` 与 `scripts/` 不再随仓库分发

## 1.8.1
2026-07-25

### 修复

- 修复主动提醒缺少现实时间和动态时间感知的问题；统一注入中文 `<SYSTEM_REMINDER>` 及日程、工作日、农历、黄历、事项和上次对话状态。
- 修复主动消息经 BubbleReply 等装饰器处理时前置分段和群聊 `At` 丢失的问题。
- 前置分段发送失败时继续尽量发送剩余内容，但任务保持失败且不自动重发，避免重复消息。
- 修复动态时间上下文在时段边界附近可能使用不同时间快照的问题。
- 修复插件页版本号重复显示 `v`、插件显示名未使用 `display_name` 的问题。

### 变更

- 移除无条件追加的“用空行分段”提示；AstrBot 不会按分段插件安装状态自动注入该提示，实际分段由装饰插件负责。
- 日程时段说明统一为左闭右开 `[start_time, end_time)`，并补充相邻时段衔接提示。
- 建议关闭 AstrBot 自带的 `provider_settings.datetime_system_prompt`，避免与插件的现实时间重复注入。

## 1.8.0
2026-07-16

### 架构
- `main.py` 收敛为 AstrBot 生命周期、Hook、命令注册与兼容入口；新增 `services/` 应用服务层和 `integrations/` AstrBot 适配层
- 时间上下文组装迁移到 `TimeContextService`；主动提醒编排、任务校验迁移到 `ReminderService`
- Persona 解析与主动消息装饰/发送分别收口到 `integrations/persona_resolver.py`、`integrations/astrbot_sender.py`
- AI 日程表生成与 JSON 解析迁移到 `llm/schedule_generator.py`
- `CalendarStore` 改为插件实例依赖，保留模块默认实例兼容历史导入，避免多实例和测试间共享可变状态

### 提醒可靠性
- `reminder_tasks.yaml` 升级为 v2，所有内部时间统一保存为 UTC aware datetime；旧 v1 naive/aware 任务会按当前有效时区自动迁移
- 新增 `pending / processing / retry_wait / sent / failed` 状态机：任务发送成功后才确认完成，进程在 LLM 阶段退出时可重启恢复
- Provider/LLM 等发送前失败支持指数退避重试；新增 `reminder.reminder_max_attempts`（默认 3）
- 新任务首次持久化失败时回滚内存队列并向调用方报错，避免返回无法跨重启恢复的“成功”任务
- 进入消息装饰链前持久化 `delivery_started`。装饰/发送阶段失败不自动重试，避免其他分段插件已发送部分分段后整条重复
- 修复同一会话所有逾期任务被无条件合并的问题，现在只合并触发时间相差 60 秒以内的任务
- WebUI 任务页显示等待重试状态、下次重试时间；概览增加手动任务、重试和失败计数

### 性能与一致性
- `LastChatTracker` 改为内存更新 + 2 秒防抖刷盘，插件退出时强制 flush，不再每条用户/AI 消息同步重写 YAML
- WebUI 手动任务和插件间 API 共用同一套创建与校验逻辑
- `/calendar create`、`/schedule create` 留空 provider 时显式解析当前会话 Provider，适配 AstrBot 4.26.3 的 `llm_generate(chat_provider_id=...)` 契约
- 主动提醒和 AI 日程的人设解析与 `chat_memory` 保持同源：session 强制规则 → conversation persona → 会话默认 persona；主动消息通过实际平台实例解析 platform name
- 新增 `reminder.use_chat_memory_history` checkbox；启用时通过只读 `build_takeover_contexts()` 公开 API 完整遵循 CM 的 scope、过滤、前缀与预算配置，接管关闭或无安全用户范围时降级为空历史
- 关闭内置事件分类后立即清空内存中的 builtin 事件，避免主动提醒继续读取旧分类数据
- 精简配置页提示，统一“日历事项/日程表”术语，并修正时段匹配顺序和 Provider 回退说明

### 测试与兼容
- 新增纯逻辑测试，覆盖 UTC 转换、v1→v2 任务迁移、合并窗口、重试/失败边界、重启恢复、Store 隔离、日历/日程解析、Persona、ChatMemory 历史和 LastChat 防抖写盘
- 最低 AstrBot 版本声明更新为 `>=4.26.0,<5`

## 1.7.2
2026-07-15

### 新增
- **插件间 API**：暴露 `create_external_task(session, fire_at, hint, target_user_id="", source="")` 协程方法，其他插件可通过 `context.get_registered_star("time_awareness")` 拿到实例调用，到点主动发一条消息（与 WebUI 手动添加任务同型）。详见 README「插件间 API」一节

## 1.7.1
2026-07-12

### 新增
- **无事件也发问候**：`reminder` 组新增 `daily_greeting_without_events`（checkbox，默认关）。开启后即使当日无任何日历事件，仍会在 `reminder_time` 时刻主动发一条贴合此时段的日常问候；关闭则保持原行为（无事件就跳过不发）

### 优化
- **简化时段状态描述**：移除 state_prompt 中 XML 子标签的建议（如 `<SLEEP_MODE>`），代码本就不解析这些子标签；规则文本里对 `<SLEEP_MODE>` 的特殊处理也一并删除，所有时段一视同仁，state 直接写状态描述即可
- **AI 生成日程表示例同步**：示例 state 文本去掉时段名自报（如「晨起绵软」→「刚睁眼」），下次 `/schedule create` 不会被带偏

### 修复
- **主动消息使用真实生效 persona**：会话内切换 persona 后，主动提醒也能用切换后的 persona（之前会用默认 persona）

## 1.7.0
2026-07-10

### 新增
- **农历日期感知**：注入 `<LUNAR_STATE>` 标签告知今日农历日期，让传统节日/农事/生肖相关人设更贴合
- **黄历感知**：注入 `<ALMANAC_STATE>` 标签（干支/冲煞/宜忌），与农历一同启用语义最佳
- **WebUI 仪表盘时间感知面板**：概览页顶部 hero card——大字当前时间 + 时段问候语 + 日期行（含工作日彩色 chip + 农历年生肖 chip）+ 黄历 chip 行

### 配置
- `time_awareness` 组新增 `lunar_state_enabled` / `almanac_enabled`（默认分别开/关），集中在「时间感知」分组下
- 规则文本加 `<LUNAR_STATE>` / `<ALMANAC_STATE>` 标签清单与对应规则

### 文件结构
- 新增 `core/almanac_sensor.py` / `core/lunar_sensor.py`：实时计算，无状态无需 regen

## 1.6.0
2026-07-08

### 新增
- **工作日性质感知**：注入 `<WORKDAY_STATE>` 标签，标注今天是工作日/周末/调休工作日/节假日。让 LLM 能区分「周六」与「调休后要上班的周六」，自然体谅调休辛苦。基于 `chinese_calendar` 实时判定，无状态无需 regen
- **上次对话时间感知**：注入 `<LAST_CHAT>` 标签，告知 LLM 距上次用户发言/AI 回复的相对时间（如「用户 5 分钟前 · AI 3 分钟前」）。让 LLM 能基于间隔自然表达想念或保持连贯。数据自维护持久化，不依赖 chat_memory 等其他插件；命令消息不计入

### 配置
- `time_awareness` 组新增 `workday_state_enabled`（默认开）
- `time_awareness` 组新增 `last_chat_enabled`（默认开）
- 两者均受 `time_guidance_enabled` 总开关约束

### 规则文本更新
- `DEFAULT_TIME_GUIDANCE_PROMPT` 规则 1 列举所有可能出现的 XML 标签
- 规则 4 显式引用 `<LAST_CHAT>` 调整想念/连贯语气
- 新增规则 5：参考 `<WORKDAY_STATE>` 自然贴合日历性质（调休体谅、节假日庆祝），不直报数字

### 数据文件
- 新增 `data/plugin_data/time_awareness/last_chat_times.yaml`：记录每个会话的用户/AI 上次发言时间

## 1.5.1
2026-07-05

### 优化
- **时间数据不再破坏 Prompt 缓存**：动态时段状态与当日事项改注入到当前轮对话末尾，不再拼到稳定提示词区。命中模型缓存后费用约降至 10%；历史对话不再保留这些临时数据（每轮重新求值，对感知无影响）
- **主动消息也优化缓存前缀**：人设 → 时间规则 → 动态数据三段拼接，跨次主动消息调用共享稳定前缀
- **无事项时省略日历标签**：当日无事项时不再输出占位文本，模型从标签缺失自然推断「今日无特殊事件」

### 修复
- **跨日扫描竞态**：极少数情况下每日扫描会在午夜前几毫秒触发，导致当日提醒被误判为过期而整日不入队——已加缓冲修复
- **AI 生成日程表解析容错**：模型输出带 markdown 包裹的 JSON 时此前会报错——已修复

### 破坏性
- 移除配置项 `time_awareness.time_guidance_prompt`：时间规则与 XML 标签格式强耦合，开放自定义易破坏；现硬编码到源码，需要自定义请直接编辑 `constants.py`
- 移除配置项 `calendar.calendar_empty_text`：被「省略标签」机制取代
- 用户自定义过的上述字段升级后自动失效

## 1.5
2026-07-02

### 变更
- **插件更名**：display_name 改为「时间感知增强」
- **删除 `calendar_separator` 配置项**：默认「、」足够中文排版，硬编码到代码
- **配置项描述加 Emoji**，hint 文案精简
- 修复默认时间引导 prompt 停留在旧版本导致的回退路径失效

### 破坏性
- `calendar_separator` 字段移除（AstrBot 自动剥离）

## 1.4.2
2026-07-01

### 变更
- **主动消息支持消息装饰器**：安装了其他分段插件或 At 处理插件的部署，主动消息也会走切段、At 渲染等处理
- **主动消息不再拉取历史对话**：bot 主动开口语境不需要历史；同时减少 token 开销

### 已知行为变化
- 未安装其他分段插件时，主动消息会作为一整条消息直接发送

## 1.4.1
2026-07-01

### 变更
- `schedule_templates` 新增「🌙 睡眠时段（预设）」模板：v1.3 老睡眠功能的一键恢复路径

### 移除
- 取消 `config_version` 链式迁移框架（不可行路径，AstrBot 在插件加载前已剥离非 schema 字段）

### 升级建议
- v1.3.0 老用户升级后睡眠窗口若丢失：到 daily_schedule 配置组点「添加项」→「🌙 睡眠时段（预设）」一键恢复

## 1.4.0
2026-06-30

### 新增
- **单日日程表（时段状态感知）**：按时间段定义角色状态，命中时段描述注入到时间引导
- **表单化添加时段**：AstrBot 主 webui 表单编辑，支持跨午夜
- **`/schedule create|show|help` 命令**：LLM 自主生成日程表
- 启动时时段重叠检测（warning）

### 破坏性
- **合并睡眠配置到日程表**：删除 `sleep_mode_enabled` / `sleep_hours` / `sleep_prompt` 三字段，统一到 `daily_schedule.schedule_templates`

## 1.3.0
2026-06-29

### 新增
- **WebUI 任务 tab「新增任务」入口**：管理员可在 webui 安排一次性手动提醒
- 任务列表新增「手动」badge

## 1.2.0
2026-06-28

### 新增
- **Plugin Pages WebUI**：概览 / 日历月视图 / 任务三视图，AstrBot 主 webui 侧边栏进入
- 任务列表可取消、可查看触发时间

## 1.1.0
2026-06-27

### 新增
- 内置事件新增「黄历」分类（默认关）
- 主动提醒：模型回复按空行切段发送，段间随机延迟模拟打字节奏
- 群聊 followup 自动 @ 发起人

### 优化
- 配置项描述全部加 Emoji

## 1.0.0
2026-06-26

### 首版

**时间感知**
- 固定时间规则注入；跨午夜睡眠窗口判定；时区支持

**智能日历**
- YAML 持久化、用户事件 CRUD、导入导出、重复规则

**现实日历事件（内置）**
- 5 类自动生成：法定节假日（区分正日子与调休）/ 传统农历节日 / 二十四节气 / 政治纪念日 / 国际西方节日
- 跨年与分类开关变更时自动重新生成

**主动提醒**
- 当日事项在指定时刻主动发到白名单会话
- LLM 工具 `schedule_followup`

**聊天命令**
- `/calendar` 命令树（含内置事件列表 / 强制重新生成）
