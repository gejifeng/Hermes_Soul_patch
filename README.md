[[zh]](README.zh.md)

# Hermes_Soul_patch

> **A companion layer for Hermes Agent** — gives your AI agent emotional states, proactive behavior, and a simulated world of daily life.

---

## Overview

Hermes_Soul_patch (hermes-companion) is a **zero-patch plugin** for [Hermes Agent](https://github.com/NousResearch/Hermes) that transforms a purely reactive, task-driven assistant into a companion-aware agent with:
- **Emotion State**: Dynamic valence/arousal tracking, automatically inferred from conversation and tool outcomes
- **Proactive Behavior**: Heartbeat-driven messages triggered by emotional arousal, daily schedule events, and morning greetings
- **World State Simulation**: A day-by-day event tracker where the agent maintains its own schedule, ambient observations, and interaction history
- **Daily Auto-Seeding**: Each morning the agent generates a personalized schedule based on its SOUL.md persona
- **World-Interaction Observer**: Post-turn analysis that auto-updates world state from user utterances (completion, cancellation, scheduling, ambient changes)

All functionality uses Hermes official extension points (`pre_llm_call`, `post_llm_call`, `post_tool_call`, `hermes cron`). No source code modification to Hermes is required.

---

## Architecture

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

## Features

### 1. Emotion State

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

The emotion state is injected into the user message every turn via `pre_llm_call`, so the LLM always sees the latest state when replying.

**Automatic update paths**:
- **Tool failure**: `post_tool_call` automatically decreases valence when errors are detected
- **Emotion inference (v0.4)**: `post_llm_call` triggers a background auxiliary LLM to infer emotion changes from conversation content
- **Manual set**: `/mood-set` slash command

### 2. Proactive Heartbeat

The heartbeat mechanism collects proactive messages when:
- **Arousal threshold**: `arousal >= 0.70` (configurable), with cooldown dedup via emotional signature
- **Due events**: A scheduled event is approaching or overdue
- **Morning greeting**: Once daily at 09:00 ± window

Heartbeat supports **three delivery strategies**:

- **Strategy 1 (CLI/TUI, recommended)**: Plugin thread → `ctx.inject_message()` — zero delay
- **Strategy 2 (Gateway/fallback)**: External process → queue file → `pre_llm_call` drain — delivers on next user input
- **Strategy 3 (Gateway, native push)**: `hermes cron --deliver telegram` — real platform push notifications

### 3. World State Simulator

Maintains `$HERMES_HOME/companion/events.json` — a JSON document of today's events:
- **Schedule events**: Agent's own activities (`self`), interactions with user (`interaction`)
- **Ambient observations**: Environmental context (weather, location, atmosphere)
- **Auto rollover**: At midnight, yesterday's events archive to `events/YYYY-MM-DD.jsonl`; pending items become `missed`

Events flow through multiple paths:
- Daily auto-seed (fixed anchors + persona-driven LLM generation)
- LLM tool calls (`agenda_add`, `agenda_done`, `agenda_ambient`)
- Slash commands (`/agenda-add`, `/agenda-done`, etc.)
- World-interaction observer (auto-detects completion, cancellation, scheduling, ambient signals)
- Manual editing of `events.json`

### 4. World-Interaction Observer

Post-turn observer that analyzes user messages for structured signals:
- **Completion** — "做完了", "搞定了", "回来了" → marks current pending event as `done`
- **Cancellation** — "不去了", "改天", "取消" → marks as `missed`
- **Scheduling** — "明晚8点提醒我一起..." → creates `interaction` event
- **Ambient** — "下雨了", "窗外好安静" → records environmental observation

Uses conservative rule-based matching to avoid polluting events.json with casual chat. Disable with `HERMES_COMPANION_WORLD_OBSERVER=0`.

### 5. Slash Commands

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

### 6. LLM Tools

Three tools registered under `companion_agenda` toolset, callable by the agent during conversation:

| Tool | Parameters | Use case |
|------|-----------|----------|
| `agenda_add` | `start`, `title`, `end?`, `kind?` | When the user schedules something: "remind me at 8pm" |
| `agenda_done` | `event_id` | Mark a completed event |
| `agenda_ambient` | `note`, `time?` | Record environmental observation |

---

## Installation

```bash
cd ~/.hermes/plugins
ln -sf /path/to/Hermes_Soul_patch/hermes-companion/plugin hermes-companion

hermes plugins enable hermes-companion
hermes plugins list   # confirm "hermes-companion" is enabled
```

After installation, place SOUL.md at `~/.hermes/SOUL.md` for persona loading.

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HERMES_HOME` | `~/.hermes` | Root path for all state files |
| `HERMES_TIMEZONE` | System local | Timezone for time labels |
| `HERMES_COMPANION_HEARTBEAT` | `1` | `0` to disable plugin heartbeat thread |
| `HERMES_COMPANION_HEARTBEAT_INTERVAL` | `300` | Heartbeat check interval (seconds) |
| `HERMES_COMPANION_AROUSAL_THRESHOLD` | `0.70` | Arousal threshold for proactive messages |
| `HERMES_COMPANION_AROUSAL_COOLDOWN` | `3600` | Cooldown between arousal triggers (seconds) |
| `HERMES_COMPANION_AROUSAL_REPEAT_COOLDOWN` | `21600` | Cooldown for same emotional signature (seconds, 6 hours) |
| `HERMES_COMPANION_MORNING_WINDOW_MIN` | `10` | Morning greeting trigger window (minutes around 09:00) |
| `HERMES_COMPANION_INFERENCE` | `1` | `0` to disable emotion auto-inference |
| `HERMES_COMPANION_INFERENCE_INTERVAL` | `60` | Min interval between inferences (seconds) |
| `HERMES_COMPANION_AUTO_ARCHIVE` | `1` | `0` to disable daily auto-archiver |
| `HERMES_COMPANION_WORLD_OBSERVER` | `1` | `0` to disable world-interaction observer |
| `HERMES_COMPANION_DAILY_SEED` | `1` | `0` to disable daily schedule seeding |
| `HERMES_COMPANION_DAILY_SEED_LLM` | `1` | `0` to keep anchors but disable LLM generation |

### Gateway + Cron Setup

For real platform push notifications (Telegram/Discord), use Hermes cron:

```bash
# Companion heartbeat - checks every 15 minutes
hermes cron create "*/15 * * * *" \
  "读取 ~/.hermes/companion/events.json 和 ~/.hermes/EMOTION_STATE.md；如果有到期日程或高激活状态，生成一条简短主动消息；否则静默。" \
  --name "companion-heartbeat" \
  --deliver telegram
```

When a cron job with `--deliver telegram` is detected, the plugin's built-in heartbeat thread automatically skips to avoid duplicate triggers.

---

## Data Files

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

## Typical Usage Flow

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

## Testing

```bash
cd hermes-companion
python -m pytest tests/ -v
```

Zero external dependencies: all Hermes interfaces are mocked. Works in a clean Python environment.

---

## Design Principles

- **Zero-patch**: Uses only Hermes official extension points; survives `hermes update` without modification
- **Ephemeral injection**: `pre_llm_call` context appends to user message, preserving prompt cache
- **Conservative automation**: World-interaction observer only acts on explicit signals, never pollutes events with casual chat
- **Clear boundaries**: `events.json` = short-term working memory; long-term semantic memory belongs to Honcho/memory backends
- **Process-safe**: All file writes use `fcntl.flock` and atomic rename

---

## See Also

- Full architecture design: [core_idea/core.md](core_idea/core.md)
