# 更新日志

## 1.5
2026-07-02

### 变更
- **插件更名**：display_name 改为「时间感知增强」
- **删除 `calendar_separator` 配置项**：默认「、」足够中文排版，硬编码到 `calendar_store.today_text` 默认参数
- **配置 description 加代码术语标注**：`time_guidance_prompt`(system_prompt)、`state_prompt`({daily_schedule_state})、三个 `*_provider_id`、`reminder_targets`(UMO)
- **优化默认 `time_guidance_prompt`**：规则 3 增加 `{daily_schedule_state}` 为「未指定」时的处理（按人设与时段常识自由演绎）
- **配置 hint 文案精简**：删除「可用 \n 换行」「与用户自定义事件合并显示」「系统提示词由插件内置」等冗余说明
- **修复 constants/schema 默认提示词不一致**：`constants.DEFAULT_TIME_GUIDANCE_PROMPT` 此前停留在 v1.3 版本，导致旧默认值回退路径失效；现同步到 v1.4 版本
- **`.gitignore` 加防御性忽略**：runtime yaml 文件 + `data/`（防 fallback 路径污染仓库）

### 移除
- `calendar_separator` schema 字段
- `main.py` 中 `_resolve_calendar_today` 的 separator 读取与传参

## 1.4.2
2026-07-01

### 变更
- **主动消息走 OnDecoratingResultEvent 装饰链**：在发送前手动遍历装饰钩子 handler（仿 `astrbot_plugin_proactive_chat`），让 splitter_w / attool / wakepro 等装饰器能改写主动消息结果链
- **取消 chat_memory 上下文拉取**：主动消息是「bot 主动开口」语境，不需要历史对话；同时减少 token 开销
- **删除 `<time>` 标签清洗**：CM 上下文取消后无来源，相关 `on_llm_response` 钩子 + `_TIME_TAG_PATTERN` 正则一并删除

### 移除
- `_fetch_history_contexts` / `_resolve_chat_memory` / `_build_chain` / `__init__._chat_memory`
- `strip_time_tags_from_response` 钩子 + `_TIME_TAG_PATTERN`
- `_conf_schema.json` 的 `reminder.include_history` / `reminder.history_rounds`

### 已知行为变化
- **未装 splitter_w 时主动消息一整条直发**：失去自切段打字节奏。装了 splitter_w 的部署不受影响

## 1.4.1
2026-07-01

### 变更
- **取消 config_version 链式迁移框架**：AstrBot 加载配置时按 schema 剥离非 schema 字段并立即覆盖磁盘，剥离发生在 plugin `__init__` 之前，migrate 函数永远读不到已删字段；该路径不可行
- **`schedule_templates` 新增「🌙 睡眠时段（预设）」模板**：v1.3 老睡眠功能的一键恢复路径

### 移除
- `migrate.py`、`_conf_schema.json` 的 `config_version`、`constants.LEGACY_DEFAULT_SLEEP_PROMPT`

### 升级建议
- v1.3.0 老用户升级后睡眠窗口若丢失：到 daily_schedule 配置组点「添加项」→「🌙 睡眠时段（预设）」一键恢复

## 1.4.0
2026-06-30

### 新增
- **单日日程表（时段状态感知）**：按时间段定义角色状态，命中时段描述通过 `{daily_schedule_state}` 占位符注入时间感知 prompt
- **template_list 表单编辑**：AstrBot 主 webui 表单化添加时段，支持跨午夜
- **`/schedule create|show|help` 命令**：LLM 自主生成日程表
- 启动时时段重叠检测（warning）

### 变更（破坏性）
- **删除 `sleep_mode_enabled` / `sleep_hours` / `sleep_prompt` 三字段**：合并到 `daily_schedule.schedule_templates`
- sleep_prompt 注入位点从「user message 后 extra」统一到「system_prompt 占位符」
- 默认时间引导 prompt 规则 3 改写为「参考当前时段状态自然带入人设」

### 移除
- `time_utils.is_sleep_time` / `get_sleep_prompt_if_active`
- `main.py._append_dynamic_content`（仅 sleep_prompt 用）

## 1.3.0
2026-06-29

### 新增
- **WebUI 任务 tab「新增任务」入口**：管理员可在 webui 安排一次性手动提醒，新增 `kind="user"` 任务分支
- **Web API（2 端点）**：`POST /tasks/create`（硬校验白名单 + 时间窗）、`GET /tasks/options`
- 任务列表新增「手动」badge

### 变更
- `_fire_with_llm` 新增 `user` kind 分支，prompt 语义为「到点了，请主动提起：X」
- 群聊 At 提取兼容 `user` kind 的 `target_user_id`
- Web API 总数 6 → 8

## 1.2.0
2026-06-28

### 新增
- **Plugin Pages WebUI**：概览 / 日历月视图 / 任务三视图，AstrBot 主 webui 侧边栏进入
- **Web API（6 端点）**：`/about` / `/dashboard/stats` / `/calendar/month` / `/tasks/list` / `/tasks/cancel` / `/config/schema`
- scheduler 暴露 `cancel_task` / `list_pending_detailed`

### 优化
- 截断 chip hover tooltip 自定义浮层（绕开浏览器原生 1-2s 延迟）

### 内部
- `builtin_events._lunar_to_solar` 加 `silent` 参数，除夕「先试 30 再试 29」改静默 fallback

## 1.1.0
2026-06-27

### 新增
- 内置事件新增「黄历」分类（默认关）
- 主动提醒：LLM 回复按空行切段发送，段间 0.5-2s 随机延迟模拟打字节奏
- 群聊 followup 自动 @ 发起人（At 组件 + 零宽空格防粘连）
- `log_with_bot_id` / `reminder_provider_id` 配置项

### 优化
- 配置项描述全部加 Emoji
- `schedule_followup` 工具 docstring 不再向 LLM 暴露 @ 机制

### 内部
- 重构日历提醒为「每 session 每天一条 task」模型：到点 fire 时动态查 store
- 用户 0 点后对日历的任何改动都会在 fire 时自动反映
- 当日无事件且无 followup 时跳过 fire

## 1.0.0
2026-06-26

### 首版

**时间感知**
- 固定时间规则 prompt 注入 `system_prompt` 末尾
- 跨午夜睡眠窗口判定 + 睡眠提示动态注入
- 时区支持

**智能日历**
- YAML 持久化 + `{calendar_today}` 占位符解析
- 用户事件 CRUD + 导入导出 + 重复规则

**现实日历事件（内置）**
- 5 类自动生成：法定节假日 / 传统农历节日 / 二十四节气 / 政治纪念日 / 国际西方节日
- 数据源：`chinese_calendar` + `lunar_python`
- 法定节假日区分正日子「X 节」与调休「X 节假期」
- 跨年与分类开关变更时自动 regen

**主动提醒**
- 当日事项在指定时刻主动发到白名单会话
- 后台调度循环：可中断睡眠、迟到容忍窗口、超窗丢弃
- LLM 工具 `schedule_followup`

**聊天命令**
- `/calendar show|add|del|create|export|import|help`
- `/calendar builtin_list` / `/calendar builtin_regen`

**配置组**
- `time_awareness` / `calendar` / `reminder`
