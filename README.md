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
- **主动提醒**：当日事项在指定时刻主动发到白名单会话；LLM 可通过 `schedule_followup` 工具自行安排后续主动消息；WebUI 可手动添加一次性任务
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
| `reminder_max_late_minutes` | 迟到容忍窗口（分钟，超出则丢弃） | 60 |
| `reminder_late_prompt` | 补发时附加的迟到提示词 | 内置默认 |
| `followup_tool_enabled` | 向 LLM 注册 `schedule_followup` 工具 | true |
| `reminder_provider_id` | 主动提醒使用的模型（留空跟随主模型） | "" |

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

- **概览**：事件总数、本月事件数、待触发任务、近 7 天任务、下个任务相对时间、启用状态指标卡
- **日历**：月视图 7×6 grid，内置事件（灰）与自定义事件（蓝）同屏颜色区分；前后翻月 + 「今」回当月；超长事件（如黄历）hover 显示完整内容
- **任务**：pending 任务列表按触发时间升序，可取消（二次确认）；管理员可手动添加一次性任务

配置修改仍走 AstrBot 主 webui 的「插件配置」页，WebUI 暂为只读视图（任务 tab 除外）。

## 推荐 companion 插件

主动消息发送前会走消息装饰器处理链。**强烈推荐配合 [splitter_w](https://github.com/Soulter/astrbot_plugin_splitter_w) 使用**——未装时主动消息会一整条直发，失去切段与打字节奏感。

## 文件结构

```
time_awareness/
├── main.py                   # Star 类 + 钩子 + 命令树 + 主动提醒
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
│   └── scheduler.py          # 提醒调度循环
├── pages/webui/              # 前端三件套（vanilla JS）
└── llm/
    ├── calendar_generator.py # AI 生成日历
    └── schedule_generator.py # AI 生成日程表
```

## 数据文件

位于 `data/plugin_data/time_awareness/`：

| 文件 | 用途 |
|------|------|
| `calendar_data.yaml` | 用户自定义事件 |
| `builtin_events.yaml` | 内置现实事件（每年自动重新生成） |
| `reminder_tasks.yaml` | 主动提醒任务队列 |
| `last_chat_times.yaml` | 各会话上次对话时间（用户/AI，用于 `<LAST_CHAT>` 注入） |

## 依赖

- Python ≥ 3.10
- AstrBot ≥ 4.24.0
- `pyyaml` / `chinese_calendar` / `lunar_python`
