[[en]](README.md)

# Hermes_Soul_patch

> **Hermes Agent 的陪伴层插件** — 为你的 AI agent 注入情感状态、主动行为和模拟日常生活的世界状态。

---

## 概述

Hermes_Soul_patch（hermes-companion）是一个面向 [Hermes Agent](https://github.com/NousResearch/Hermes) 的**零侵入插件**，将原本被动响应的任务型助手转化为具备陪伴意识的 agent：
- **情感状态**: 动态追踪 valence/arousal 等维度，可从对话和工具调用结果自动推断
- **主动行为**: 由心跳机制驱动，当情绪激活度超阈值、日程到点和每日早安时主动发送消息
- **世界状态模拟器**: 按日维护 agent 视角的日程、环境事件和互动记录，让"它"也有自己的一天
- **每日自动种子**: 每天自动基于 SOUL.md 人设生成个性化基础日程
- **对话-世界联动观察器**: 自动从用户消息中识别完成/取消/约定/环境变化信号，实时更新世界状态

所有能力均通过 Hermes 正规扩展接口实现（`pre_llm_call`、`post_llm_call`、`post_tool_call`、`hermes cron`），**无需修改 Hermes 任何源码**。

---

## 架构概览

```
┌────────────────────────────── hermes-companion ──────────────────────────────┐
│                                                                               │
│  Plugin Layer          Companion Layer (纯 Python，零 Hermes 耦合)             │
│                                                                               │
│  hooks.py               emotion_state.py     读写 EMOTION_STATE.md            │
│    ├─ pre_llm_call      world_state.py       每日事件、跨日归档                │
│    │   时间 + 情感状态   world_interaction.py 对话联动世界状态                  │
│    │   队列消息          heartbeat.py         主动消息收集                      │
│    │                     emotion_inference.py 辅助 LLM 情感推断                │
│    │                     daily_seed.py        每日固定锚点 + LLM 增补          │
│    │                     time_context.py      时间格式化                        │
│    ├─ post_llm_call                                                │
│    │   情感推断              │
│    │   世界联动观察器                                                │
│    ├─ post_tool_call                                                         │
│    │   工具失败 → 下调情感                                     │
│    └─ on_session_start                                                          │
│    │   会话日志                                                            │
│                                                                               │
│  commands.py           8 个斜杠命令:                                          │
│    /mood /mood-set /heartbeat                                                  │
│    /agenda /agenda-add /agenda-done /agenda-ambient /recall                    │
│                                                                               │
│  tools.py              3 个 LLM 可调用工具:                                    │
│    agenda_add / agenda_done / agenda_ambient                                   │
│                                                                               │
│  数据文件（位于 $HERMES_HOME/）:                                               │
│    EMOTION_STATE.md      companion/events.json                                 │
│    companion_pending.txt  companion/events/YYYY-MM-DD.jsonl（归档）              │
│    companion/heartbeat_state.json                                              │
└──────────────────────────────┬────────────────────────────────────────────────┘
                               │ Hermes 插件自动发现
                               ▼
                    Hermes Agent 核心（不做任何修改）
```

---

## 功能详述

### 1. 情感状态

Agent 在 `$HERMES_HOME/EMOTION_STATE.md` 中维护多维情感状态：

| 维度 | 范围 | 说明 |
|------|------|------|
| `valence` | -1.0 ~ +1.0 | 正负情绪极性 |
| `arousal` | 0.0 ~ 1.0 | 激活程度（≥ 0.70 时触发主动消息） |
| `energy` | 0.0 ~ 1.0 | 精力/疲劳度 |
| `social_need` | 0.0 ~ 1.0 | 社交需求 |
| `confidence` | 0.0 ~ 1.0 | 自信心 |
| `momentum` | -1.0 ~ +1.0 | 行为动量 |
| `dominant` | 字符串 | 主导情绪标签（如 `calm`, `affectionate`, `frustrated`） |
| `note` | 字符串 | 自由文本描述 |

情感状态每 turn 通过 `pre_llm_call` 注入到 user message 末尾，LLM 每次回复时都能看到最新状态。

**自动更新路径**：
- **工具失败**: `post_tool_call` 检测到错误时自动下调 valence
- **情感推断 (v0.4)**: `post_llm_call` 触发后台辅助 LLM 从对话内容推断情感状态变化
- **手动设置**: `/mood-set` 斜杠命令

### 2. 主动心跳

心跳机制在以下时机收集主动消息：
- **激活度阈值**: `arousal >= 0.70`（可调），带情绪签名去重和冷却
- **到点事件**: 日程事件即将到点或已逾时
- **早安问候**: 每日 09:00 ± 窗口，仅一次

支持**三种投递路径**：
- **策略 1（CLI/TUI，推荐）**: 插件内线程 → `ctx.inject_message()`，零延迟
- **策略 2（Gateway 降级）**: 外部进程 → 队列文件 → `pre_llm_call` drain，下次用户输入时送达
- **策略 3（Gateway 原生推送）**: `hermes cron --deliver telegram`，真实平台推送通知

### 3. 世界状态模拟器

维护 `$HERMES_HOME/companion/events.json` —— agent 视角的今日事件列表：
- **日程事件**: agent 自己的活动（`self`）、与用户的约定（`interaction`）
- **环境观察**: 天气、地点、氛围等环境上下文
- **跨日自动归档**: 午夜时分昨日事件归档至 `events/YYYY-MM-DD.jsonl`，未完成事项标记为 `missed`

事件来源：
- 每日自动种子（固定锚点 + 人设驱动 LLM 生成）
- LLM 工具调用（`agenda_add`、`agenda_done`、`agenda_ambient`）
- 斜杠命令（`/agenda-add`、`/agenda-done` 等）
- 对话-世界联动观察器（自动识别完成/取消/约定/环境信号）
- 手动编辑 `events.json`

### 4. 对话-世界联动观察器

轮次后观察器，从用户消息中提取结构化信号：
- **完成信号** — "做完了", "搞定了", "回来了" → 将当前 pending 标记为 `done`
- **取消信号** — "不去了", "改天", "取消" → 标记为 `missed`
- **约定信号** — "明晚8点提醒我一起..." → 创建 `interaction` 事件
- **环境信号** — "下雨了", "窗外好安静" → 记录环境观察

采用保守的关键词匹配策略，避免将日常闲聊污染到事件列表中。环境变量 `HERMES_COMPANION_WORLD_OBSERVER=0` 可关闭。

### 5. 斜杠命令

| 命令 | 说明 |
|------|------|
| `/mood` | 查看当前情感状态 |
| `/mood-set <v> <a> <dominant> <note>` | 手动设置情感状态 |
| `/heartbeat` | 查看心跳队列状态 |
| `/heartbeat push <message>` | 向主动消息队列推送消息 |
| `/agenda` | 查看今日事件列表 |
| `/agenda-add <start> <end> <title>` | 添加日程事件 |
| `/agenda-done <id>` | 标记事件已完成 |
| `/agenda-ambient <note>` | 添加环境观察记录 |
| `/recall <YYYY-MM-DD>` | 调取指定日期归档摘要 |

### 6. LLM 可调用工具

注册了三个 `companion_agenda` 工具，agent 在对话中可自行调用：

| 工具 | 参数 | 使用场景 |
|------|------|----------|
| `agenda_add` | `start`, `title`, `end?`, `kind?` | 用户提出日程约定时写入事件 |
| `agenda_done` | `event_id` | 标记事件完成 |
| `agenda_ambient` | `note`, `time?` | 记录环境观察 |

---

## 安装

```bash
cd ~/.hermes/plugins
ln -sf /path/to/Hermes_Soul_patch/hermes-companion/plugin hermes-companion

hermes plugins enable hermes-companion
hermes plugins list   # 确认 hermes-companion 状态为 enabled
```

安装完成后，将 SOUL.md 放置于 `~/.hermes/SOUL.md` 以加载人设。

---

## 配置

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HERMES_HOME` | `~/.hermes` | 所有状态文件的根路径 |
| `HERMES_TIMEZONE` | 系统本地时区 | 时间标签的时区 |
| `HERMES_COMPANION_HEARTBEAT` | `1` | `0` 关闭插件内置心跳线程 |
| `HERMES_COMPANION_HEARTBEAT_INTERVAL` | `300` | 心跳检查间隔（秒） |
| `HERMES_COMPANION_AROUSAL_THRESHOLD` | `0.70` | 主动消息的 arousal 触发阈值 |
| `HERMES_COMPANION_AROUSAL_COOLDOWN` | `3600` | arousal 触发冷却（秒） |
| `HERMES_COMPANION_AROUSAL_REPEAT_COOLDOWN` | `21600` | 相同情绪签名冷却（6 小时） |
| `HERMES_COMPANION_MORNING_WINDOW_MIN` | `10` | 早安触发窗口（09:00 前后分钟数） |
| `HERMES_COMPANION_INFERENCE` | `1` | `0` 关闭情感自动推断 |
| `HERMES_COMPANION_INFERENCE_INTERVAL` | `60` | 推断最小间隔（秒） |
| `HERMES_COMPANION_AUTO_ARCHIVE` | `1` | `0` 关闭每日自动归档 |
| `HERMES_COMPANION_WORLD_OBSERVER` | `1` | `0` 关闭世界联动观察器 |
| `HERMES_COMPANION_DAILY_SEED` | `1` | `0` 关闭每日日程种子 |
| `HERMES_COMPANION_DAILY_SEED_LLM` | `1` | `0` 保留锚点但关闭 LLM 增补 |

### Gateway + Cron 配置

通过 Hermes cron 实现平台推送通知（Telegram/Discord）：

```bash
# 伴侣心跳 — 每 15 分钟检查
hermes cron create "*/15 * * * *" \
  "读取 ~/.hermes/companion/events.json 和 ~/.hermes/EMOTION_STATE.md；如果有到期日程或高激活状态，生成一条简短主动消息；否则静默。" \
  --name "companion-heartbeat" \
  --deliver telegram
```

当检测到已配置 `--deliver telegram` 的 cron 任务时，插件内置心跳线程会自动跳过，避免重复触发。

---

## 数据文件

位于 `$HERMES_HOME/`：

| 文件 | 用途 |
|------|------|
| `EMOTION_STATE.md` | 当前情感状态（Markdown 中的 JSON 块） |
| `companion_pending.txt` | 主动消息队列（策略 2 降级方案） |
| `companion/events.json` | 今日事件（日程 + 环境观察） |
| `companion/events/YYYY-MM-DD.jsonl` | 归档事件（一天一文件） |
| `companion/heartbeat_state.json` | 心跳触发冷却状态 |
| `companion/daily_seed.json` | （可选）自定义每日锚点 + LLM 配置 |

---

## 典型使用流程

```
用户: 晚上8点提醒我一起听歌
   │
   ├─→ agent 调用 agenda_add(start="20:00", title="一起听歌", kind="interaction")
   │       │
   │       └─→ 事件写入 events.json
   │
   ├─→ 下一轮: [今日日程] 注入，agent 看到自己的承诺
   │
   └─→ 20:00 心跳到点: 主动消息"晚上8点啦，该一起听歌了"
           │
           ├─ 策略 1（CLI/TUI）: ctx.inject_message() → 即时
           ├─ 策略 2（降级）: companion_pending.txt → 下次用户输入
           └─ 策略 3（cron）: hermes cron --deliver telegram → 真实推送

用户: 听完了
   │
   └─→ 世界联动观察器识别到"完了" → 标记事件 done
           │
           └─→ events.json 已更新，下一轮 [今日日程] 即刻反映
```

---

## 测试

```bash
cd hermes-companion
python -m pytest tests/ -v
```

零外部依赖：所有 Hermes 接口均被 mock，可在干净 Python 环境中运行。

---

## 设计原则

- **零侵入**: 仅使用 Hermes 正规扩展点，`hermes update` 后无需任何改动
- **临时注入**: `pre_llm_call` context append 到 user message，不破坏 prompt cache
- **保守自动化**: 世界联动观察器只在明确信号下动作，不将闲聊污染事件列表
- **边界清晰**: `events.json` 是短期工作记忆，长期语义记忆由 Honcho/记忆后端负责
- **进程安全**: 所有文件写入使用 `fcntl.flock` 加锁和原子替换

---

## 参阅

- 完整架构设计文档: [core_idea/core.md](core_idea/core.md)
