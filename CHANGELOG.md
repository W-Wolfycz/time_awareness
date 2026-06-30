# 更新日志

## 1.4.0
2026-06-30

### 新增
- **单日日程表（时段状态感知）**：按时间段定义角色状态（晨起/元气/入夜/睡眠...），命中时段的描述通过 `{daily_schedule_state}` 占位符注入到时间感知 prompt
- **配置 template_list 编辑**：AstrBot 主 webui 表单化添加时段，支持跨午夜（end < start 自动识别）
- **`/schedule create` 命令**：LLM 自主生成日程表（基于配置世界观或当前人设 prompt），与 `/calendar create` 调用模式一致
- **`/schedule show|help` 命令**：列出时段（高亮当前命中段）、显示帮助
- 启动时时段重叠检测（warning 不强制 reject；多段命中同一时刻取列表中靠前的）
- 引入 `config_version` 字段（自动管理，链式迁移框架）

### 变更（破坏性）
- **删除 sleep_mode_enabled / sleep_hours / sleep_prompt 三字段**：合并到 `daily_schedule.schedule_templates` 作为睡眠时段实例
- 自动迁移：v0 配置启用的睡眠窗口原样转为新格式（保留自定义 sleep_prompt 文本）
- sleep_prompt 注入位点从「user message 后 extra_user_content_parts」统一到「system_prompt 占位符」（与日程表机制一致；每轮 LLM 请求都带，等价强度）
- 默认时间引导 prompt 规则 3 改写为「参考下方当前时段状态自然带入人设」，新增「当前时段状态：{daily_schedule_state}」一行

### 移除
- `is_sleep_time` / `get_sleep_prompt_if_active` 工具函数（time_utils.py）
- `_append_dynamic_content` 钩子（main.py，仅 sleep_prompt 使用）

## 1.3.0
2026-06-30

### 新增
- **WebUI 任务 tab「新增任务」入口**：管理员可在 webui 直接安排一次性手动提醒，到点由 LLM 主动开口。新增 `kind="user"` 任务分支，与现有 calendar/followup 同群合并触发。
- **Web API（2 端点）**：
  - `POST /tasks/create`：硬校验 session 必须在 `reminder_targets` 白名单内；时间窗口 `now+60s ~ now+365d`；时区由后端 `get_tz()` 锚定（前端 datetime-local naive → 后端 `replace(tzinfo=tz)` localize）
  - `GET /tasks/options`：返回白名单会话列表、当前时区 label、默认时间（now+10min，naive）
- 任务列表新增「手动」badge 区分用户创建的提醒（绿色 success）

### 变更
- `_fire_with_llm` 新增 `user` kind 分支，prompt 语义为「到点了，请主动提起：X」（区别于 followup 的「承诺过」语境，避免 LLM 误判）
- 群聊 At 提取逻辑兼容 `user` kind 的 `target_user_id`
- `scheduler.Task.kind` 注释扩展为 `"calendar" | "followup" | "user"`，新增 `add_user_task` 入队方法
- Web API 端点总数从 6 → 8

## 1.2.0
2026-06-29

### 新增
- **Plugin Pages WebUI**：在 AstrBot 主 webui 侧边栏点开本插件即可访问，3 个视图：
  - **概览**：自定义/内置事件总数、本月事件数、待触发任务数（日历/后续分项）、近 7 天任务、下个任务相对时间、日历/主动提醒启用状态
  - **日历月视图**：自写 7×6 grid，builtin/custom 颜色区分；前后翻月 + 一键回当月；超长事件（如黄历）hover 立即显示完整内容 tooltip
  - **任务**：pending 任务列表（按 `fire_at` 升序），支持取消（带二次确认 modal）
- **Web API（6 端点）**：`/about` / `/dashboard/stats` / `/calendar/month`（builtin+custom 单端点双数据）/ `/tasks/list` / `/tasks/cancel` / `/config/schema`，统一 `{success, ...data | error}` 信封，鉴权继承 AstrBot 主 webui 登录态
- scheduler 暴露 `cancel_task` / `list_pending_detailed`，支撑 webui 取消与展示

### 优化
- 截断 chip 的 hover tooltip：自定义浮层（绕开浏览器原生 1-2s 延迟），跟随鼠标 + 自动避边；仅在文本被 ellipsis 截断时启用，完整显示的 chip 不交互

### 内部
- `builtin_events._lunar_to_solar` 加 `silent` 参数；除夕「先试 30 再试 29」的探索性 fallback 改静默调用，小月年份不再误刷 warning
- `main.py` 接入 `register_web_apis`

## 1.1.0
2026-06-27

### 新增
- 内置事件新增「黄历」分类（默认关）：每日干支/冲煞/宜忌，一年约 365 条
- 主动提醒：LLM 回复按空行切段发送，段间 0.5-2s 随机延迟模拟人工打字节奏
- 群聊 followup 主动消息自动 @ 发起人（参照 attool 渲染规则，前置 At 组件 + 零宽空格防粘连）
- `log_with_bot_id`：能拿到消息事件的日志前缀带 platform_id，便于多 Bot 实例环境下定位
- `reminder_provider_id`：主动提醒触发时使用的模型（留空跟随主模型），与人设模型解耦

### 优化
- 配置项描述全部加 Emoji，便于在 AstrBot 配置页快速识别
- 工具 `schedule_followup` docstring 精简，不再向 LLM 暴露 @ 机制

### 内部
- 重构日历提醒为「每 session 每天一条 task」模型：到点 fire 时动态查 store 拿当日事件，不再用 0 点扫描时的事件快照
- 用户在 0 点后对日历的任何改动（add/del/create/import/builtin_regen/开关分类）都会在 fire 时自动反映；fire 时先跑 builtin 新鲜度检查，应对分类开关变更
- 当日无事件且无 followup 时跳过 fire，不发空提醒
- Task 移除 `calendar_event_id` / `calendar_event_text` / `use_llm` 等遗留字段（旧 yaml 兼容加载）
- 日志等级修正：日历加载 I/O 异常 info→warning；LLM 响应异常 warning→error（与「LLM 触发失败」对齐）

## 1.0.0
2026-06-26

### 首版

**时间感知**
- 固定时间规则 prompt 注入 `system_prompt` 末尾
- 跨午夜睡眠窗口判定 + 睡眠提示动态注入本轮用户消息后
- 时区支持：跟随 AstrBot 全局时区 或 IANA 时区名

**智能日历**
- YAML 持久化（`calendar_data.yaml`），支持原子写入与手工编辑
- `{calendar_today}` 占位符解析（可用于时间引导 prompt / 睡眠提示文本）
- 用户事件 CRUD + 导入导出 + 重复规则（仅当年 / 连续 N 年 / 永久每年）

**现实日历事件（内置）**
- 5 类自动生成：法定节假日（含调休）/ 传统农历节日 / 二十四节气 / 政治纪念日 / 国际西方节日
- 数据源：`chinese_calendar`（每年更新调休）+ `lunar_python`（农历/节气）
- 法定节假日区分正日子「X 节」与调休「X 节假期」（含「先休后过节」自动归属）
- 农历节日跨年正确处理（如腊八/小年/除夕公历常落次年 1-2 月）
- 清明既是节气又是法定节假日：两类同时启用时只保留更正式的「清明节」
- 跨年（每年 1/1）+ 分类开关变更时自动 regen，存于 `builtin_events.yaml`

**主动提醒**
- 当日事项在指定时刻主动发到白名单会话（`reminder` 配置组）
- 后台调度循环：可中断睡眠、迟到容忍窗口、超窗丢弃
- 任务持久化（`reminder_tasks.yaml`）
- 携带 chat_memory 对话历史（按 session 范围，不限用户）作为 LLM contexts
- LLM 工具 `schedule_followup`：承诺「X 分钟后再说」时主动安排后续主动消息

**聊天命令**
- `/calendar show|add|del|create|export|import|help`
- `/calendar builtin_list` / `/calendar builtin_regen`：查看与强制重新生成内置事件

**配置组**
- `time_awareness`：时间引导 + 睡眠模式
- `calendar`：日历开关 + AI 生成 + 内置事件 5 类子开关
- `reminder`：主动提醒（默认关）

### 默认值

- `calendar_empty_text` 默认 `无`（无事项时 LLM 明确感知）
- 默认时间引导 prompt 规则 1 措辞调整；`{calendar_today}` 移至独立「当日事项：」一行
