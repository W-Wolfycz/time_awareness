# time_awareness — 时间感知增强

为 LLM 提供现实时间、角色时段状态、工作日/农历/黄历、日历事项、上次对话间隔和主动提醒能力。

## 功能

- 时间引导：让角色用“刚才”“大半夜”“好久不见”等自然表达时间。
- 单日日程表：按左闭右开时段 `[start_time, end_time)` 注入角色状态，支持跨午夜和 AI 生成。
- 时间传感器：工作日、农历、黄历、现实日历事件、上次对话间隔。
- AI 日历：按世界观生成全年节日、纪念日和季节事项。
- 主动提醒：在白名单会话发送日历提醒、问候和后续任务。
- Plugin Page：查看日历和提醒任务；配置仍使用 AstrBot 主配置页。

## 安装

将本目录放入 AstrBot 插件目录，重启 AstrBot 加载。

## 配置

### `time_awareness`

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `time_guidance_enabled` | 时间规则、现实时间和动态感知总开关 | `true` |
| `workday_state_enabled` | 工作日/周末/调休/节假日感知 | `true` |
| `last_chat_enabled` | 上次对话间隔感知 | `true` |
| `lunar_state_enabled` | 农历感知 | `true` |
| `almanac_enabled` | 黄历感知 | `false` |
| `use_astrbot_timezone` | 跟随 AstrBot 全局时区 | `false` |
| `timezone` | IANA 时区名 | `""` |

本插件会独立注入中文 `<SYSTEM_REMINDER>`。AstrBot 自带开关仍会注入小写 `<system_reminder>`；为避免重复占用 token，建议关闭 `provider_settings.datetime_system_prompt`。建议让本插件跟随 AstrBot 时区。

### `daily_schedule`

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `enable_schedule` | 启用日程表 | `false` |
| `schedule_templates` | 时段名称、起止时间和状态描述 | `[]` |
| `ai_generate_provider_id` | `/schedule create` 使用的模型，留空跟随当前模型 | `""` |
| `ai_generate_use_persona` | 生成时使用当前人设 | `true` |
| `ai_generate_worldview` | 生成使用的世界观/主题 | `""` |

时段采用左闭右开规则：开始时刻计入，结束时刻不计入。相邻时段请首尾衔接，例如 `07:00–07:30` 与 `07:30–08:00`；结束时间早于开始时间表示跨午夜。

### `calendar`

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `enable_calendar` | 注入当日事项 | `false` |
| `ai_generate_provider_id` | `/calendar create` 使用的模型 | `""` |
| `ai_generate_worldview` | 日历生成世界观/主题 | `""` |
| `enable_builtin_events` | 启用内置现实事件 | `true` |

内置事件分类：`legal_holidays` 法定节假日、`traditional` 传统节日、`solar_terms` 节气、`political` 政治纪念日、`international` 国际节日。分类开关位于 `calendar.builtin_events`。

### `reminder`

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `enable_reminder` | 启用主动提醒 | `false` |
| `reminder_targets` | 目标会话白名单（UMO，每行一个） | `[]` |
| `reminder_time` | 日历提醒时间（HH:MM） | `09:00` |
| `daily_greeting_without_events` | 无事件时也发送问候 | `false` |
| `reminder_max_late_minutes` | 迟到容忍分钟数 | `60` |
| `reminder_max_attempts` | 发送前失败最大重试次数 | `3` |
| `reminder_late_prompt` | 迟到补发提示 | 内置默认 |
| `followup_tool_enabled` | 注册 `schedule_followup` 工具 | `true` |
| `use_chat_memory_history` | 使用 ChatMemory 接管上下文 | `false` |
| `reminder_provider_id` | 主动提醒模型，留空跟随当前会话 | `""` |

主动提醒通过 `Context.llm_generate()` 独立调用，不经过 AstrBot 的 `on_llm_request`；启用时间感知后，插件会自行追加完整动态上下文。

### `log`

`debug_to_info`（默认 `false`）将 debug 日志改走 info；`log_with_bot_id`（默认 `false`）在日志前缀中显示平台 ID。

## 动态注入

静态规则 `<TIME_GUIDE>` 放在 `system_prompt`，动态内容放在当前轮末尾，不写入历史：

```xml
<SYSTEM_REMINDER>当前日期时间：YYYY-MM-DD HH:MM（时区），星期X</SYSTEM_REMINDER>
<SCHEDULE_STATE>当前命中的角色状态</SCHEDULE_STATE>
<WORKDAY_STATE>工作日性质</WORKDAY_STATE>
<LUNAR_STATE>农历日期</LUNAR_STATE>
<ALMANAC_STATE>黄历信息</ALMANAC_STATE>
<TODAY_EVENTS>当天事项</TODAY_EVENTS>
<LAST_CHAT>上次对话间隔</LAST_CHAT>
```

`<SCHEDULE_STATE>` 在日程未启用或未命中时为“未指定（按人设自然推断）”；其他标签在没有数据或关闭对应传感器时省略。普通消息链使用临时 `extra_user_content_parts`，主动提醒链将同一内容追加到本轮 `prompt`。

## 命令

| 命令 | 权限 | 作用 |
|---|---|---|
| `/calendar show [YYYY-MM]` | 所有人 | 查看月份事项 |
| `/calendar add <日期> [重复] <标题>` | 管理员 | 添加事项 |
| `/calendar del <id>` | 管理员 | 删除自定义事项 |
| `/calendar create` / `export` / `import` | 管理员 | 生成、导出、导入日历 |
| `/calendar builtin_list [分类]` / `builtin_regen` | 前者所有人，后者管理员 | 查看或刷新内置事件 |
| `/schedule create` | 管理员 | AI 生成日程表 |
| `/schedule show` / `help` | 所有人 | 查看日程或帮助 |

日期支持 `YYYY-MM-DD` 或 `MM-DD`；重复参数 `0` 表示仅当年，`1–4` 表示额外重复年数，`9` 表示每年重复。

## 主动提醒与插件协作

- 主动消息会进入结果装饰链；是否安装分段插件不会改变 LLM 提示词。没有分段插件时作为一条消息发送。
- `use_chat_memory_history` 默认关闭。开启后仅调用已激活 ChatMemory 的公开只读 API；接管未启用、范围不安全或查询失败时自动降级为空历史。
- Provider/LLM 等发送前失败按指数退避重试；进入装饰/发送阶段后不自动重发，避免部分分段已经发出却重复整条消息。

### 插件间 API

其他插件可通过 `context.get_registered_star("time_awareness")` 获取实例并调用：

```python
star_meta = self.context.get_registered_star("time_awareness")
if star_meta and star_meta.star_cls:
    task_id = await star_meta.star_cls.create_external_task(
        session="aiocqhttp:GroupMessage:group_demo",
        fire_at=fire_at,
        hint="该喝水了",
        source="plugin:water_reminder",
    )
```

`session` 必须在 `reminder_targets` 中，`fire_at` 必须是未来至少 60 秒且不超过 365 天的时间。

## 数据、WebUI 与依赖

- 数据目录：`data/plugin_data/time_awareness/`，包括日历、内置事件、提醒任务和上次对话时间。
- WebUI 提供概览、日历和任务视图；配置修改仍走 AstrBot 主配置页。
- Python ≥ 3.10；AstrBot `>=4.26.0,<5`；依赖 `pyyaml`、`chinese_calendar`、`lunar_python`。
