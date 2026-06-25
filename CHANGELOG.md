# 更新日志

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
