# 更新日志

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
