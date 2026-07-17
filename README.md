# time_awareness — 时间感知增强

为 LLM 注入自然时间表达引导、单日时段状态（日程表）、可 AI 生成的世界观日历，以及现实日历事件（法定节假日、传统节日、节气、黄历等）。

## 功能

- **时间引导**：让 LLM 自然地用口语化方式表达时间（"刚才"、"大半夜"、"好久不见"），不刻意报时；并真实感知当前所处时段、今日事项、工作日性质、农历日期、上次对话间隔
- **单日日程表**：按时间段定义角色状态（晨起 / 元气 / 入夜 / 睡眠...），命中时段描述自动注入到时间感知；睡眠窗口视为一个普通时段统一管理
- **工作日感知**：实时判定今天是工作日 / 周末 / 调休工作日 / 节假日，让 LLM 自然区分"周六"与"调休要上班的周六"
- **农历日期感知**：实时计算今日农历日期（如「农历丙午马年六月初五」），让涉及传统节日 / 农事 / 生肖的人设自然贴合
- **上次对话时间感知**：记录每个会话的用户/AI 上次发言时间，让 LLM 基于"距上次互动多久"自然表达想念或保持连贯
- **AI 日历生成**：用户给一个世界观/主题，LLM 一次性生成一整年日历事项
- **现实日历事件**：法定节假日（含调休）/ 传统农历节日 / 二十四节气 / 政治纪念日 / 国际西方节日
- **黄历感知（独立标签）**：每日干支 / 冲煞 / 宜忌，走独立 `<ALMANAC_STATE>` 标签，不混入事件清单
- **主动提醒**：当日事项在指定时刻主动发到白名单会话；可选「无事件也发日常问候」；LLM 可通过 `schedule_followup` 工具自行安排后续主动消息；任务使用 UTC 持久化状态机，发送前失败可自动重试
- **Plugin Pages WebUI**：AstrBot 主 webui 侧边栏点开即用，含概览（时间感知 hero 面板）/ 日历月视图 / 任务列表

## 安装

将本目录放入 AstrBot 的插件目录，重启加载即可。

## 配置

五组配置项，均通过 AstrBot 配置页编辑：

### `time_awareness`（时间感知）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `time_guidance_enabled` | 启用时间引导注入 | true |
| `workday_state_enabled` | 启用工作日性质感知（`<WORKDAY_STATE>`） | true |
| `last_chat_enabled` | 启用上次对话时间感知（`<LAST_CHAT>`） | true |
| `lunar_state_enabled` | 启用农历日期感知（`<LUNAR_STATE>`） | true |
| `almanac_enabled` | 启用黄历感知（`<ALMANAC_STATE>`） | false |
| `use_astrbot_timezone` | 跟随 AstrBot 全局时区 | false |
| `timezone` | IANA 时区名（如 Asia/Shanghai） | "" |

> 时间引导规则文本硬编码在源码中，不开放 webui 编辑（与 XML 标签格式强耦合，自定义易破坏）。

### `daily_schedule`（单日日程表）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `enable_schedule` | 启用日程表时段感知 | false |
| `schedule_templates` | 时段定义列表（支持「⏰ 时段」与「🌙 睡眠时段（预设）」两种模板，含名称/起止时间/状态描述） | [] |
| `ai_generate_provider_id` | `/schedule create` 所用模型（留空跟随主模型） | "" |
| `ai_generate_use_persona` | `/schedule create` 是否联合当前人设作为输入 | true |
| `ai_generate_worldview` | `/schedule create` 使用的世界观/主题 | "" |

### `calendar`（日历）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `enable_calendar` | 启用当日事项感知（无事项时不输出日历标签） | false |
| `ai_generate_provider_id` | `/calendar create` 所用模型（留空跟随主模型） | "" |
| `ai_generate_worldview` | `/calendar create` 使用的世界观/主题 | "" |
| `enable_builtin_events` | 启用内置现实日历事件 | true |

### `calendar.builtin_events`（内置事件分类开关）

总开关 `calendar.enable_builtin_events` 关闭时本节失效。

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `legal_holidays` | 法定节假日（含调休） | true |
| `traditional` | 传统农历节日 | true |
| `solar_terms` | 二十四节气 | true |
| `political` | 政治纪念日 | false |
| `international` | 国际/西方节日 | false |

### `reminder`（主动提醒）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `enable_reminder` | 启用主动提醒 | false |
| `reminder_targets` | 提醒目标会话白名单 UMO（每行一个完整字符串） | [] |
| `reminder_time` | 当日事项发送时刻（HH:MM） | `09:00` |
| `daily_greeting_without_events` | 当日无事件也发日常问候 | false |
| `reminder_max_late_minutes` | 迟到容忍窗口（分钟，超出则丢弃） | 60 |
| `reminder_max_attempts` | Provider/LLM 等发送前失败的最大尝试次数 | 3 |
| `reminder_late_prompt` | 补发时附加的迟到提示词 | 内置默认 |
| `followup_tool_enabled` | 向 LLM 注册 `schedule_followup` 工具 | true |
| `use_chat_memory_history` | 主动提醒生成时复用 ChatMemory 当前接管上下文 | false |
| `reminder_provider_id` | 主动提醒使用的模型（留空跟随当前会话模型） | "" |

### `log`（日志）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `debug_to_info` | 把本插件 debug 日志改走 info 通道输出 | false |
| `log_with_bot_id` | 消息事件日志前缀变为 `[time_awareness:platform_id]` | false |

## 命令

### `/calendar` — 日历事项

| 命令 | 权限 | 说明 |
|------|------|------|
| `/calendar show [YYYY-MM]` | 所有成员 | 列出指定月份的事项（默认本月） |
| `/calendar add <日期> [重复] <标题>` | 管理员 | 新增事项 |
| `/calendar del <id>` | 管理员 | 按 id 删除用户事件（id 可只输前 8 位） |
| `/calendar create` | 管理员 | 按配置世界观重新生成完整月历并合并导入 |
| `/calendar export` | 管理员 | 输出当前数据为 YAML 文本 |
| `/calendar import` | 管理员 | 回复一条 YAML 文本消息后调用，批量导入 |
| `/calendar builtin_list [分类]` | 所有成员 | 列出当年所有内置事件（可按分类过滤） |
| `/calendar builtin_regen` | 管理员 | 强制重新生成当年内置事件 |
| `/calendar help` | 所有成员 | 命令菜单 |

### `/schedule` — 单日日程表

| 命令 | 权限 | 说明 |
|------|------|------|
| `/schedule create` | 管理员 | 按配置世界观（可选联合当前人设）让 LLM 生成时段表 |
| `/schedule show` | 所有成员 | 列出所有时段（高亮当前命中段） |
| `/schedule help` | 所有成员 | 命令菜单 |

### 通用约定

- **日期格式**：`YYYY-MM-DD` 或 `MM-DD`（自动补当前年份）
- **重复参数**（`/calendar add`）：`0`=仅当年（默认） / `1-4`=连续 N+1 年 / `9`=永久每年
- **内置事件分类过滤**（`/calendar builtin_list`）：`法定` / `传统` / `节气` / `政治` / `国际` / `黄历`
- **内置事件不允许单条删除**：regen 会复活。关闭整类用配置开关；批量定制直接编辑 `builtin_events.yaml`

## WebUI

在 AstrBot 主 webui 左侧「插件管理」找到 time_awareness，点「插件页」进入。三个视图：

- **概览**：事件总数、本月事件数、待触发/重试/失败任务、近 7 天任务、下个任务相对时间、启用状态指标卡
- **日历**：月视图 7×6 grid，内置事件（灰）与自定义事件（蓝）同屏颜色区分；前后翻月 + 「今」回当月；超长事件（如黄历）hover 显示完整内容
- **任务**：pending/retry_wait 任务按触发时间升序，可取消（二次确认）；等待重试任务显示原定时间和下次重试时间；管理员可手动添加一次性任务

配置修改仍走 AstrBot 主 webui 的「插件配置」页，WebUI 暂为只读视图（任务 tab 除外）。

## 插件间 API

其他插件可通过 `context.get_registered_star("time_awareness")` 拿到本插件实例，调用以下公共方法安排任务：

### `create_external_task(session, fire_at, hint, target_user_id="", source="") -> str`

到点主动发一条消息（与 WebUI 手动添加任务同型）。示例：

```python
star_meta = self.context.get_registered_star("time_awareness")
if star_meta and star_meta.star_cls:
    ta = star_meta.star_cls
    from datetime import datetime, timedelta, timezone
    fire_at = datetime.now(timezone.utc) + timedelta(hours=2)
    task_id = await ta.create_external_task(
        session="aiocqhttp:GroupMessage:10001",
        fire_at=fire_at,
        hint="该喝水了",
        source="plugin:water_reminder",
    )
```

**约束**：

- `session` 必须在 `reminder_targets` 白名单内
- `hint` 1-500 字
- `fire_at` 必须晚于当前 +60 秒、不超过 365 天；aware/naive 均可（naive 按插件当前有效时区解释）
- 返回 `task_id`（32 字符 hex），可用 `scheduler.cancel_task(task_id)` 取消
- 违反约束抛 `ValueError`

## 分段插件兼容

主动消息发送前会走消息装饰器处理链，可由其他分段插件进行切段和节奏处理。未安装分段插件时，主动消息会作为一整条消息直接发送。

### ChatMemory 历史注入

- `use_chat_memory_history` 默认关闭
- 启用后仅通过 `context.get_registered_star("chat_memory")` 调用 ChatMemory 公开 API，不直接导入对方插件模块
- ChatMemory 必须已经安装、激活且开启 `context_takeover`；不可用、接管关闭、没有当前 conversation 或查询失败时自动降级为不注入历史
- 通过 ChatMemory 的只读 `build_takeover_contexts()` API 构建上下文，自动遵循 CM 当前的跨会话/整群范围、状态过滤、内容白名单、Persona、前缀和预算配置
- 私聊自动使用 UMO 中的用户 ID；群聊仅在任务带目标用户时查询该用户。目标为空且 CM 开启 `full_group` 时只取当前 UMO + CID 的整群上下文，即使开启 `cross_session` 也不会扩大到其他会话或整个平台
- 历史只作为本次主动提醒的 LLM `contexts`，不会写入 time_awareness 自身数据文件

### 提醒重试语义

- Provider 查询、LLM 调用、空/异常模型响应等尚未进入发送阶段的失败，会按 1、2、4…分钟指数退避重试（上限 15 分钟），总尝试次数由 `reminder_max_attempts` 控制
- 进入 `on_decorating_result` 前会持久化 `delivery_started`。其他分段插件可能已经主动发送部分分段，因此装饰或最终发送失败会标记为 `failed`，不会自动重发整条消息
- 插件在 LLM 阶段退出时，`processing` 任务会在重启后恢复；若退出前已经进入装饰/发送阶段，则为避免重复消息转为 `failed`
- `reminder_tasks.yaml` v1 会在首次加载时自动迁移到 v2；历史 naive 时间按升级时的有效配置时区解释，再统一转换为 UTC

## 文件结构

```
time_awareness/
├── main.py                   # Star 生命周期 + Hook/命令注册 + 兼容入口
├── web_api.py                # Plugin Pages 后端 API（8 端点）
├── _conf_schema.json         # 五组配置 schema
├── metadata.yaml
├── requirements.txt
├── constants.py              # 默认提示词常量
├── utils/time_utils.py       # 时区与当前时间工具
├── core/
│   ├── _datafile.py          # YAML 读写
│   ├── calendar_store.py     # 日期匹配
│   ├── calendar_manager.py   # 用户事件 CRUD + 导入导出
│   ├── builtin_events.py     # 现实事件生成器
│   ├── builtin_manager.py    # 内置事件文件管理 + 跨年重新生成
│   └── scheduler.py          # UTC 任务状态机 + 调度循环
├── services/
│   ├── time_context_service.py # 动态时间状态组装
│   └── reminder_service.py     # 提醒校验、LLM 与发送编排
├── integrations/
│   ├── chat_memory_history.py # 可选 ChatMemory 历史读取
│   ├── persona_resolver.py     # AstrBot Persona 解析
│   └── astrbot_sender.py       # 主动消息装饰与发送适配
├── commands/
│   └── calendar_helpers.py     # 日历命令纯解析/展示助手
├── pages/webui/              # 前端三件套（vanilla JS）
├── llm/
│   ├── calendar_generator.py # AI 生成日历
│   └── schedule_generator.py # AI 生成日程表
└── tests/                    # 不依赖完整 AstrBot runtime 的纯逻辑测试
```

## 数据文件

位于 `data/plugin_data/time_awareness/`：

| 文件 | 用途 |
|------|------|
| `calendar_data.yaml` | 用户自定义事件 |
| `builtin_events.yaml` | 内置现实事件（每年自动重新生成） |
| `reminder_tasks.yaml` | 主动提醒任务队列（v2，UTC + 状态/重试信息） |
| `last_chat_times.yaml` | 各会话上次对话时间（用户/AI，用于 `<LAST_CHAT>` 注入） |

## 依赖

- Python ≥ 3.10
- AstrBot ≥ 4.26.0，< 5
- `pyyaml` / `chinese_calendar` / `lunar_python`
