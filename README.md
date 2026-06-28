# time_awareness — 时间感知与智能日历

为 LLM 注入自然时间表达引导、睡眠时段状态提示，整合 AI 世界观日历、现实日历事件（节日/节气）、与主动提醒。

## 功能

- **时间引导 prompt**：固定时间规则追加到 `system_prompt` 末尾，引导 LLM 自然表达时间
- **睡眠模式**：检测到处于睡眠窗口（支持跨午夜）时，把睡眠提示动态注入本轮用户消息后
- **日历占位符**：在时间引导 prompt / 睡眠提示中写 `{calendar_today}` 即可在请求时替换为当日事项
- **用户日历**：YAML 持久化 + 重复规则（仅当年 / 连续 N 年 / 永久每年） + AI 一次性生成完整月历
- **现实日历事件**：法定节假日（含调休）/ 传统农历节日 / 二十四节气 / 政治纪念日 / 国际西方节日 / 黄历（每日干支/宜忌）
- **主动提醒**：当日事项在指定时刻主动发到白名单会话；LLM 可通过 `schedule_followup` 工具自行安排后续主动消息；回复按段发送（段间 0.5-2s 随机延迟模拟打字节奏），群聊 followup 自动 @ 发起人
- **Plugin Pages WebUI**：AstrBot 主 webui 侧边栏点开即用，含概览统计 / 日历月视图（builtin+custom 同屏）/ pending 任务列表（可取消）
- **聊天命令**：`/calendar show|add|del|create|export|import|help` + `builtin_list|builtin_regen`

## 安装

将本目录放入 AstrBot 的插件目录，重启加载即可。

## 配置

四组配置项，均通过 AstrBot 配置页编辑：

### `time_awareness`（时间感知）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `time_guidance_enabled` | 启用时间引导 prompt 注入 system_prompt | true |
| `time_guidance_prompt` | 时间引导 prompt 内容（支持 `{calendar_today}`） | 内置默认 |
| `use_astrbot_timezone` | 跟随 AstrBot 全局时区 | false |
| `timezone` | IANA 时区名（如 Asia/Shanghai） | "" |
| `sleep_mode_enabled` | 启用睡眠模式 | false |
| `sleep_hours` | 睡眠时段（支持跨午夜，如 `22:00-8:00`） | `22:00-8:00` |
| `sleep_prompt` | 睡眠提示文本（支持 `{calendar_today}`） | 内置默认 |

### `calendar`（日历）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `enable_calendar` | 启用 `{calendar_today}` 解析 | false |
| `calendar_separator` | 多条事项分隔符 | `、` |
| `calendar_empty_text` | 当日无事项时的默认文本 | `无` |
| `ai_generate_provider_id` | AI 生成所用模型（留空跟随主模型） | "" |
| `ai_generate_worldview` | `/calendar create` 使用的世界观/主题 | "" |
| `enable_builtin_events` | 启用内置现实日历事件（总开关） | true |

### `calendar.builtin_events`（内置事件分类开关）

总开关 `calendar.enable_builtin_events` 关闭时本节失效。

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `legal_holidays` | 法定节假日（含调休） | true |
| `traditional` | 传统农历节日 | true |
| `solar_terms` | 二十四节气 | true |
| `political` | 政治纪念日 | false |
| `international` | 国际/西方节日 | false |
| `almanac` | 黄历（每日干支/冲煞/宜忌，一年约 365 条） | false |

### `reminder`（主动提醒）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `enable_reminder` | 启用主动提醒 | false |
| `reminder_targets` | 提醒目标会话白名单（每行一个 UMO 字符串） | [] |
| `reminder_time` | 当日事项发送时刻（HH:MM） | `09:00` |
| `reminder_max_late_minutes` | 迟到容忍窗口（分钟，超出则丢弃） | 60 |
| `reminder_late_prompt` | 补发时附加的迟到提示词 | 内置默认 |
| `followup_tool_enabled` | 向 LLM 注册 `schedule_followup` 工具 | true |
| `include_history` | 主动提醒携带对话历史（从 chat_memory 插件拉取） | true |
| `history_rounds` | 对话历史轮数（1-50，每轮 = user + assistant） | 10 |
| `reminder_provider_id` | 主动提醒使用的模型（留空跟随主模型） | "" |

### `log`（日志）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `debug_to_info` | 调试日志提级：把本插件 debug 日志改走 info 通道输出 | false |
| `log_with_bot_id` | 日志区分机器人实例：能拿到事件的日志前缀变为 `[time_awareness:platform_id]` | false |

## 命令

| 命令 | 权限 | 说明 |
|------|------|------|
| `/calendar show [YYYY-MM]` | 所有成员 | 列出指定月份的事项（默认本月） |
| `/calendar add <日期> [重复] <标题>` | 管理员 | 新增事项 |
| `/calendar del <id>` | 管理员 | 按 id 删除用户事件（id 可只输前 8 位） |
| `/calendar create` | 管理员 | 按配置里的世界观重新生成完整月历并合并导入 |
| `/calendar export` | 管理员 | 输出当前数据为 YAML 文本 |
| `/calendar import` | 管理员 | 回复一条 YAML 文本消息后调用，批量导入 |
| `/calendar builtin_list [分类]` | 所有成员 | 列出当年所有内置事件（可按分类过滤） |
| `/calendar builtin_regen` | 管理员 | 强制重新生成当年内置事件 |
| `/calendar help` | 所有成员 | 命令菜单 |

**日期格式**：`YYYY-MM-DD` 或 `MM-DD`（自动补当前年份）

**重复参数**：
- `0` = 仅当年（默认）
- `1-4` = 从基准年起连续 N+1 年
- `9` = 永久每年重复

**内置事件分类过滤**（`builtin_list` 参数）：`法定` / `传统` / `节气` / `政治` / `国际` / `黄历`

**内置事件单条删除**：不允许——regen 会复活。如需关闭整类请用配置开关；如需批量定制请直接编辑 `builtin_events.yaml`。

## WebUI

在 AstrBot 主 webui 左侧「插件管理」找到 time_awareness，点「插件页」按钮进入。三个视图：

- **概览**：事件总数、本月事件数、待触发任务、近 7 天任务、下个任务相对时间、日历/主动提醒启用状态等指标卡
- **日历**：月视图 7×6 grid，内置事件（灰）与自定义事件（蓝）同屏颜色区分；前后翻月 + 「今」回当月；超长事件（如黄历）被截断时 hover 立即显示完整内容
- **任务**：pending 任务列表按触发时间升序，可取消（带二次确认）

配置修改仍走 AstrBot 主 webui 的「插件配置」页，WebUI 暂为只读视图。

## 文件结构

```
time_awareness/
├── main.py                   # Star 类 + on_llm_request 钩子 + 命令树 + 主动提醒
├── web_api.py                # Plugin Pages 后端 API（6 端点）
├── _conf_schema.json         # 三组配置
├── metadata.yaml
├── requirements.txt
├── constants.py              # 默认提示词常量
├── utils/time_utils.py       # get_tz / get_now / 睡眠窗口判定
├── core/
│   ├── _datafile.py          # YAML 原子读写
│   ├── calendar_store.py     # 内存单例 + 日期匹配（用户+builtin 合并）
│   ├── calendar_manager.py   # 用户事件 CRUD + 导入导出
│   ├── builtin_events.py     # 5 类现实事件生成器
│   ├── builtin_manager.py    # 内置事件文件管理 + 跨年 regen
│   └── scheduler.py          # 提醒调度循环（含 cancel_task / list_pending_detailed）
├── pages/webui/              # 前端三件套（vanilla JS，无框架）
│   ├── index.html
│   ├── app.js
│   └── style.css
└── llm/
    └── calendar_generator.py # AI 生成日历
```

## 数据文件

位于 `data/plugin_data/time_awareness/`：

| 文件 | 用途 |
|------|------|
| `calendar_data.yaml` | 用户自定义事件 |
| `builtin_events.yaml` | 内置现实事件（每年自动 regen） |
| `reminder_tasks.yaml` | 主动提醒任务队列 |

## 依赖

- Python ≥ 3.10
- AstrBot ≥ 4.24.0
- `pyyaml` / `chinese_calendar` / `lunar_python`

## 与其他插件协作

- **chat_memory**（推荐）：主动提醒调 LLM 时拉取该会话对话历史作为 contexts，让提醒内容更连贯。未安装时降级为不带历史。
