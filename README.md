# Hermes_Soul_patch

> **A companion layer for Hermes Agent** — gives your AI agent emotional states, proactive behavior, and a simulated world of daily life.
>
> **Hermes Agent 的陪伴层插件** — 为你的 AI agent 注入情感状态、主动行为和模拟日常生活的世界状态。

---

## Overview | 概述

Hermes_Soul_patch (hermes-companion) is a **zero-patch plugin** for [Hermes Agent](https://github.com/NousResearch/Hermes) that transforms a purely reactive, task-driven assistant into a companion-aware agent with:
- **Emotion State**: Dynamic valence/arousal tracking, automatically inferred from conversation and tool outcomes
- **Proactive Behavior**: Heartbeat-driven messages triggered by emotional arousal, daily schedule events, and morning greetings
- **World State Simulation**: A day-by-day event tracker where the agent maintains its own schedule, ambient observations, and interaction history
- **Daily Auto-Seeding**: Each morning the agent generates a personalized schedule based on its SOUL.md persona
- **World-Interaction Observer**: Post-turn analysis that auto-updates world state from user utterances (completion, cancellation, scheduling, ambient changes)

Hermes_Soul_patch（hermes-companion）是一个面向 [Hermes Agent](https://github.com/NousResearch/Hermes) 的**零侵入插件**，将原本被动响应的任务型助手转化为具备陪伴意识的 agent：
- **情感状态**: 动态追踪 valence/arousal 等维度，可从对话和工具调用结果自动推断
- **主动行为**: 由心跳机制驱动，当情绪激活度阈值、日程到点和每日早安时主动发送消息
- **世界状态模拟器**: 按日维护 agent 视角的日程、环境事件和互动记录，让"它"也有自己的一天
- **每日自动种子**: 每天自动基于 SOUL.md 人设生成个性化基础日程
- **对话-世界联动观察器**: 自动从用户消息中识别完成/取消/约定/环境变化信号，实时更新世界状态

All functionality uses Hermes official extension points (`pre_llm_call`, `post_llm_call`, `post_tool_call`, `hermes cron`). No source code modification to Hermes is required.

所有能力均通过 Hermes 正规扩展接口实现（`pre_llm_call`、`post_llm_call`、`post_tool_call`、`hermes cron`），**无需修改 Hermes 任何源码**。

---

## Architecture | 架构概览

```
┌────────────────────────────── hermes-companion ──────────────────────────────┐
│                                                                               │
│  Plugin Layer          Companion Layer (pure Python, zero Hermes coupling)    │
│                                                                               │
│  hooks.py               emotion_state.py     Read/write EMOTION_STATE.md      │
│    ├─ pre_llm_call      world_state.py       Daily events, rollover, archive   │
│    │   time + emotion   world_interaction.py Auto-update from conversation     │
│    │   pending queue    heartbeat.py         Proactive message collection      │
│    │                     emotion_inference.py Aux-LLM emotion deduction        │
│    │                     daily_seed.py        Daily anchor + LLM events        │
│    │                     time_context.py      Time formatting                  │
│    ├─ post_llm_call                                                │
│    │   emotion inference    │
│    │   world observer                                                    │
│    ├─ post_tool_call                                                         │
│    │   tool failure → nudge emotion down                                   │
│    └─ on_session_start                                                          │
│    │   session logging                                                           │
│                                                                               │
│  commands.py          8 slash commands:                                         │
│    /mood /mood-set /heartbeat                                                  │
│    /agenda /agenda-add /agenda-done /agenda-ambient /recall                    │
│                                                                               │
│  tools.py             3 LLM-callable tools:                                     │
│    agenda_add / agenda_done / agenda_ambient                                   │
│                                                                               │
│  Data files at $HERMES_HOME/:                                                  │
│    EMOTION_STATE.md     companion/events.json                                   │
│    companion_pending.txt  companion/events/YYYY-MM-DD.jsonl (archives)        │
│    companion/heartbeat_state.json                                              │
└──────────────────────────────┬────────────────────────────────────────────────┘
                               │ Hermes plugin auto-discovery
                               ▼
                     Hermes Agent Core (unmodified)
```

---

## Features | 功能详述

### 1. Emotion State | 情感状态

The agent maintains a multi-dimensional emotional state in `$HERMES_HOME/EMOTION_STATE.md`:
| Dimension | Range | Description |
|-----------|-------|-------------|
| `valence` | -1.0 ~ +1.0 | Positive/negative polarity |
| `arousal` | 0.0 ~ 1.0 | Activation level (triggers proactive messages when ≥ 0.70) |
| `energy` | 0.0 ~ 1.0 | Energy/fatigue |
| `social_need` | 0.0 ~ 1.0 | Desire for interaction |
| `confidence` | 0.0 ~ 1.0 | Self-confidence |
| `momentum` | -1.0 ~ +1.0 | Behavioral momentum |
| `dominant` | string | Current dominant emotion label (e.g., `calm`, `affectionate`, `frustrated`) |
| `note` | string | Free-text description |

情感状态每 turn 通过 `pre_llm_call` 注入到 user message 末尾，LLM 每次回复时都能看到。

**自动更新路径**:
- **工具失败**: `post_tool_call` 检测到错误时自动下调 valence
- **情感推断 (v0.4)**: `post_llm_call` 触发后台辅助 LLM 从对话内容推断情感状态变化
- **手动设置**: `/mood-set` slash command

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

### 2. Proactive Heartbeat | 主动心跳

The heartbeat mechanism collects proactive messages when:
- **Arousal threshold**: `arousal >= 0.70` (configurable), with cooldown dedup via emotional signature
- **Due events**: A scheduled event is approaching or overdue
- **Morning greeting**: Once daily at 09:00 ± window

Heartbeat supports **two delivery strategies**:

**Strategy 1 (CLI/TUI, recommended)**: Plugin thread → `ctx.inject_message()` — zero delay.
**Strategy 2 (Gateway/fallback)**: External process → queue file → `pre_llm_call` drain — delivers on next user input.
**Strategy 3 (Gateway, native push)**: `hermes cron --deliver telegram` — real platform push notifications.

心跳机制在以下时机收集主动消息：
- **激活度阈值**: `arousal >= 0.70`（可调），带情绪签名去重和冷却
- **到点事件**: 日程事件即将到点或已逾时
- **早安问候**: 每日 09:00 ± 窗口，仅一次

### 3. World State Simulator | 世界状态模拟器

Maintains `$HERMES_HOME/companion/events.json` — a JSON document of today's events:
- **Schedule events**: Agent's own activities (`self`), interactions with user (`interaction`)
- **Ambient observations**: Environmental context (weather, location, atmosphere)
- **Auto rollover**: At midnight, yesterday's events archive to `events/YYYY-MM-DD.jsonl`; pending items become `missed`

Events flow through multiple input paths:
- Daily auto-seed (fixed anchors + persona-driven LLM generation)
- LLM tool calls (`agenda_add`, `agenda_done`, `agenda_ambient`)
- Slash commands (`/agenda-add`, `/agenda-done`, etc.)
- World-interaction observer (auto-detects completion, cancellation, scheduling, ambient signals)
- Manual editing of `events.json`

维护 `$HERMES_HOME/companion/events.json` —— agent 视角的今日事件：
- **日程事件**: agent 自己的活动（`self`）、与用户的约定（`interaction`）
- **环境观察**: 天气、地点、氛围等环境上下文
- **跨日自动归档**: 午夜时分昨日事件归档至 `events/YYYY-MM-DD.jsonl`，未完成事项标记为 `missed`

### 4. World-Interaction Observer | 对话-世界联动观察器

Post-turn observer that analyzes user messages for structured signals:
- **Completion** — "做完了", "搞定了", "回来了" → marks current pending event as `done`
- **Cancellation** — "不去了", "改天", "取消" → marks as `missed`
- **Scheduling** — "明晚8点提醒我一起..." → creates `interaction` event
- **Ambient** — "下雨了", "窗外好安静" → records environmental observation

Conservative rule-based matching avoids polluting events.json with casual chat.

轮次后观察器，从用户消息中提取结构化信号：
- **完成信号** — "做完了", "搞定了", "回来了" → 将当前 pending 标记为 `done`
- **取消信号** — "不去了", "改天", "取消" → 标记为 `missed`
- **约定信号** — "明晚8点提醒我一起..." → 创建 `interaction` 事件
- **环境信号** — "下雨了", "窗外好安静" → 记录环境观察

采用保守的关键词匹配策略，避免将日常闲聊污染到事件列表中。环境变量 `HERMES_COMPANION_WORLD_OBSERVER=0` 可关闭。

### 5. Slash Commands | 斜杠命令

| Command | Description |
|---------|-------------|
| `/mood` | Display current emotion state |
| `/mood-set <v> <a> <dominant> <note>` | Manually set emotion state |
| `/heartbeat` | Show heartbeat queue status |
| `/heartbeat push <message>` | Push a message to proactive queue |
| `/agenda` | Show today's event list |
| `/agenda-add <start> <end> <title>` | Add a schedule event |
| `/agenda-done <id>` | Mark event complete |
| `/agenda-ambient <note>` | Add ambient observation |
| `/recall <YYYY-MM-DD>` | Retrieve archived day summary |

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

### 6. LLM Tools | LLM 可调用工具

Three tools registered under `companion_agenda` toolset, callable by the agent during conversation:

| Tool | Parameters | Use case |
|------|-----------|----------|
| `agenda_add` | `start`, `title`, `end?`, `kind?` | When the user schedules something: "remind me at 8pm" |
| `agenda_done` | `event_id` | Mark a completed event |
| `agenda_ambient` | `note`, `time?` | Record environmental observation |

注册了三个 `companion_agenda` 工具，agent 在对话中可自行调用：
- `agenda_add` — 用户提出日程约定时写入事件
- `agenda_done` — 标记事件完成
- `agenda_ambient` — 记录环境观察

---

## Installation | 安装

```bash
cd ~/.hermes/plugins
ln -sf /path/to/Hermes_Soul_patch/hermes-companion/plugin hermes-companion

hermes plugins enable hermes-companion
hermes plugins list   # confirm "hermes-companion" is enabled
```

确认安装后，SOUL.md 应放置在 `~/.hermes/SOUL.md` 以加载人设。

---

## Configuration | 配置

### Environment Variables | 环境变量

| Variable | Default | Description |
|----------|---------|-------------|
| `HERMES_HOME` | `~/.hermes` | Root path for all state files |
| `HERMES_TIMEZONE` | System local | Timezone for time labels |
| `HERMES_COMPANION_HEARTBEAT` | `1` | `0` to disable plugin heartbeat thread |
| `HERMES_COMPANION_HEARTBEAT_INTERVAL` | `300` | Heartbeat check interval (seconds) |
| `HERMES_COMPANION_AROUSAL_THRESHOLD` | `0.70` | Arousal level threshold for proactive messages |
| `HERMES_COMPANION_AROUSAL_COOLDOWN` | `3600` | Cooldown between arousal triggers (seconds) |
| `HERMES_COMPANION_AROUSAL_REPEAT_COOLDOWN` | `21600` | Cooldown for same emotional signature (seconds, 6 hours) |
| `HERMES_COMPANION_MORNING_WINDOW_MIN` | `10` | Morning greeting trigger window (minutes around 09:00) |
| `HERMES_COMPANION_INFERENCE` | `1` | `0` to disable emotion auto-inference |
| `HERMES_COMPANION_INFERENCE_INTERVAL` | `60` | Min interval between inferences (seconds) |
| `HERMES_COMPANION_AUTO_ARCHIVE` | `1` | `0` to disable daily auto-archiver |
| `HERMES_COMPANION_WORLD_OBSERVER` | `1` | `0` to disable world-interaction observer |
| `HERMES_COMPANION_DAILY_SEED` | `1` | `0` to disable daily schedule seeding |
| `HERMES_COMPANION_DAILY_SEED_LLM` | `1` | `0` to keep anchors but disable LLM generation |

### Gateway + Cron Setup | Gateway 模式定时推送

For real platform push notifications (Telegram/Discord), use Hermes cron:

```bash
# Companion heartbeat - checks every 15 minutes
hermes cron create "*/15 * * * *" \
  "读取 ~/.hermes/companion/events.json 和 ~/.hermes/EMOTION_STATE.md；如果有到期日程或高激活状态，生成一条简短主动消息；否则静默。" \
  --name "companion-heartbeat" \
  --deliver telegram
```

When a cron job with `--deliver telegram` is detected, the plugin's built-in heartbeat thread automatically skips to avoid duplicate triggers.

当检测到已配置 `--deliver telegram` 的 cron 任务时，插件内置心跳线程会自动跳过，避免重复触发。

---

## Data Files | 数据文件

Located at `$HERMES_HOME/`:

| File | Purpose |
|------|---------|
| `EMOTION_STATE.md` | Current emotion state (JSON in Markdown block) |
| `companion_pending.txt` | Proactive message queue (Strategy 2 fallback) |
| `companion/events.json` | Today's events (schedule + ambient) |
| `companion/events/YYYY-MM-DD.jsonl` | Archived events (one file per day) |
| `companion/heartbeat_state.json` | Cooldown state for heartbeat triggers |
| `companion/daily_seed.json` | (Optional) Custom daily seed anchors + LLM config |

---

## Typical Usage Flow | 典型使用流程

```
User: 晚上8点提醒我一起听歌
   │
   ├─→ agent calls agenda_add(start="20:00", title="一起听歌", kind="interaction")
   │       │
   │       └─→ event written to events.json
   │
   ├─→ Next turn: [今日日程] injected, agent sees its own commitment
   │
   └─→ 20:00 heartbeat tick: proactive message "晚上8点啦，该一起听歌了"
           │
           ├─ Strategy 1 (CLI/TUI): ctx.inject_message() → instant
           ├─ Strategy 2 (fallback): companion_pending.txt → next user input
           └─ Strategy 3 (cron): hermes cron --deliver telegram → real push

User: 听完了
   │
   └─→ world-interaction observer detects "完了" → mark event done
           │
           └─→ events.json updated, reflected in next turn's [今日日程]
```

---

## Testing | 测试

```bash
cd hermes-companion
python -m pytest tests/ -v
```

Zero external dependencies: all Hermes interfaces are mocked. Works in a clean Python environment.

零外部依赖：所有 Hermes 接口均被 mock，可在干净 Python 环境中运行。

---

## Design Principles | 设计原则

- **Zero-patch**: Uses only Hermes official extension points; survives `hermes update` without modification
- **Ephemeral injection**: `pre_llm_call` context appends to user message, preserving prompt cache
- **Conservative automation**: World-interaction observer only acts on explicit signals, never pollutes events with chatter
- **Clear boundaries**: `events.json` = short-term working memory; long-term semantic memory belongs to Honcho/memory backends
- **Process-safe**: All file writes use `fcntl.flock` and atomic rename

- **零侵入**: 仅使用 Hermes 正规扩展点，`hermes update` 后无需任何改动
- **临时注入**: `pre_llm_call` context append 到 user message，不破坏 prompt cache
- **保守自动化**: 世界联动观察器只在明确信号下动作，不将闲聊污染事件列表
- **边界清晰**: `events.json` 是短期工作记忆，长期语义记忆由 Honcho/记忆后端负责
- **进程安全**: 所有文件写入使用 `fcntl.flock` 加锁和原子替换

---

## License

See core_idea/core.md for full architecture design documentation.

完整架构设计文档见 core_idea/core.md。
