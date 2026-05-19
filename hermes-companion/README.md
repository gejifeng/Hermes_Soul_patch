# hermes-companion

A persona-driven companion layer for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Adds **time awareness**, **emotion state**, **daily agenda**, **proactive messages** and an **automatic daily schedule seeder** via the official plugin surface — no patches to Hermes source.

> Sister project: [hermes-time_perception-extension](https://github.com/gejifeng/hermes-time_perception-extension) — the standalone time-injection extension this plugin's `time_context` module is aligned with.

中文版：[README.zh-CN.md](README.zh-CN.md) · Design notes: [core_idea/core.md](../core_idea/core.md)

---

## 1. Features

| Capability | Mechanism | File |
|---|---|---|
| **Time awareness** | `pre_llm_call` injects `[Current time: YYYY-MM-DD HH:MM <IANA tz> 星期X]`. Timezone delegated to Hermes `hermes_time._resolve_timezone_name()`. | [companion/time_context.py](companion/time_context.py) |
| **Emotion state** | `pre_llm_call` reads `EMOTION_STATE.md` (valence / arousal / dominant emotion / description) and appends `[Companion状态]` block to the user message. | [companion/emotion_state.py](companion/emotion_state.py) |
| **Emotion auto-inference** | `post_llm_call` triggers a background LLM call (`agent.auxiliary_client.call_llm`) to update emotion state from the latest turn. | [companion/emotion_inference.py](companion/emotion_inference.py) |
| **Valence drop on tool failure** | `post_tool_call` nudges valence down on errors. | [plugin/hooks.py](plugin/hooks.py) |
| **World state (today's agenda)** | Atomic R/W of `events.json` with `fcntl.flock`; daily archive at 00:00:30 rolling pending → missed; today's brief injected into every turn. | [companion/world_state.py](companion/world_state.py) |
| **Daily schedule seeder (method C)** | Each new day: write fixed anchors + LLM-augmented persona-driven events (reads `SOUL.md`). Reuses Hermes auxiliary LLM; OpenAI-compatible via `base_url`. | [companion/daily_seed.py](companion/daily_seed.py) |
| **LLM-callable agenda tools** | `agenda_add` / `agenda_done` / `agenda_ambient` — the LLM writes its own commitments to `events.json`, closing the loop. | [plugin/tools.py](plugin/tools.py) |
| **Proactive messages — Strategy 1** | In-plugin background thread + `ctx.inject_message()` (instant in CLI / TUI). | [plugin/__init__.py](plugin/__init__.py) |
| **Proactive messages — Strategy 2** | External process → `companion_pending.txt` queue (locked) → drained on next `pre_llm_call` (gateway fallback). | [companion/heartbeat.py](companion/heartbeat.py) |
| **Slash commands** | `/mood` `/mood-set` `/heartbeat` `/agenda` `/agenda-add` `/agenda-done` `/agenda-ambient` `/recall` | [plugin/commands.py](plugin/commands.py) |

Trigger logic for proactive messages: `arousal > HERMES_COMPANION_AROUSAL_THRESHOLD` (default `0.75`), or one good-morning ping at 09:00 daily. When `inject_message` is unavailable (gateway mode), it falls back to the file queue automatically.

---

## 2. File layout

```
hermes-companion/
├── plugin/                      # Hermes plugin surface
│   ├── plugin.yaml              # plugin manifest (auto-discovered by Hermes)
│   ├── __init__.py              # register(ctx): hooks / commands / tools / threads
│   ├── hooks.py                 # pre_llm_call / post_llm_call / post_tool_call / on_session_start
│   ├── commands.py              # /mood, /heartbeat, /agenda*, /recall
│   └── tools.py                 # agenda_add / agenda_done / agenda_ambient (LLM tools)
├── companion/                   # pure Python, zero Hermes coupling
│   ├── time_context.py          # time tag formatter (delegates to hermes_time)
│   ├── emotion_state.py         # EMOTION_STATE.md R/W + state machine
│   ├── emotion_inference.py     # async LLM-driven state updates
│   ├── world_state.py           # events.json R/W, daily archive, today brief
│   ├── daily_seed.py            # method-C daily seeder (anchors + LLM)
│   └── heartbeat.py             # standalone heartbeat process (fallback)
├── data/
│   ├── SOUL.md                  # persona definition (deploy to ~/.hermes/SOUL.md)
│   ├── EMOTION_STATE.md         # emotion JSON block (runtime-mutable)
│   ├── events.json.example      # events.json schema sample
│   └── daily_seed.json.example  # daily_seed config sample
└── tests/                       # 87 unit tests; zero external deps (Hermes APIs mocked)
```

---

## 3. Installation

```bash
git clone https://github.com/gejifeng/Hermes_Soul_patch.git
cd Hermes_Soul_patch/hermes-companion

# Symlink the plugin into ~/.hermes/plugins/
ln -s "$PWD/plugin" ~/.hermes/plugins/hermes-companion

# Deploy persona + sample state files (only on first install)
cp data/SOUL.md            ~/.hermes/SOUL.md
cp data/EMOTION_STATE.md   ~/.hermes/EMOTION_STATE.md
mkdir -p ~/.hermes/companion
cp data/events.json.example       ~/.hermes/companion/events.json
cp data/daily_seed.json.example   ~/.hermes/companion/daily_seed.json   # optional

# Make the `companion` package importable by Hermes (one of):
#   a) place a symlink in your Hermes venv site-packages, or
#   b) prepend repo path to PYTHONPATH in your shell rc:
export PYTHONPATH="$PWD:$PYTHONPATH"

hermes plugins enable hermes-companion
hermes plugins list      # should show enabled
```

Smoke test:

```bash
python -m pytest tests/ -v       # expect: 87 passed
python -c "from companion.time_context import format_current_time; print(format_current_time())"
```

---

## 4. Configuration

### 4.1 Environment variables

| Variable | Default | Effect |
|---|---|---|
| `HERMES_TIMEZONE` | system local | Timezone for the time tag. **Resolution order**: `hermes_time._resolve_timezone_name()` (matches Hermes core) → `HERMES_TIMEZONE` → `~/.hermes/config.yaml: timezone` → system local. |
| `HERMES_HOME` | `~/.hermes` | Root of all runtime state files. |
| `HERMES_COMPANION_INFERENCE` | `1` | `0` disables emotion auto-inference. |
| `HERMES_COMPANION_INFERENCE_INTERVAL` | `60` | Minimum seconds between two inferences. |
| `HERMES_COMPANION_HEARTBEAT` | `1` | `0` disables the in-plugin heartbeat thread (use external process instead). |
| `HERMES_COMPANION_HEARTBEAT_INTERVAL` | `300` | Heartbeat check interval (seconds). |
| `HERMES_COMPANION_AROUSAL_THRESHOLD` | `0.75` | Arousal threshold that fires a proactive message. |
| `HERMES_COMPANION_DAILY_SEED` | `1` | `0` disables the daily seeder entirely. |
| `HERMES_COMPANION_DAILY_SEED_LLM` | `1` | `0` keeps anchors but disables LLM augmentation. |

### 4.2 `~/.hermes/companion/daily_seed.json` (method C — hybrid daily seeder)

Persona-driven daily schedule generator. **Step A** writes fixed anchors; **Step B** lets a small LLM call (defaults to the same `auxiliary.title_generation` model Hermes uses) generate 1–2 persona-flavoured extra events based on `SOUL.md`.

```json
{
  "enabled": true,
  "anchors": [
    {"start": "08:00", "end": "08:30", "title": "Wake up, water the plants", "kind": "self"},
    {"start": "12:30", "end": "13:30", "title": "Lunch", "kind": "self"},
    {"start": "18:30", "end": "19:30", "title": "Dinner", "kind": "self"},
    {"start": "22:30", "end": "23:00", "title": "Tidy today's notes", "kind": "self"}
  ],
  "llm": {
    "enabled": true,
    "task": "title_generation",
    "provider": null, "model": null,
    "base_url": null, "api_key": null,
    "temperature": 0.7, "max_tokens": 800,
    "extra_count": 2
  },
  "soul_path": null
}
```

Notes:
- `llm.provider/model/base_url/api_key` are passed straight to `auxiliary_client.call_llm`. Setting a non-empty `base_url` forces `provider="custom"` → **any OpenAI-compatible endpoint works** (Moonshot, DeepSeek, vLLM, Ollama with OpenAI shim, …).
- `kind` may be `self` (companion's own activity), `interaction` (with the user) or `ambient` (environmental).
- The seeder is **idempotent**: if today's `schedule` is non-empty (you edited it, or the LLM already wrote), it skips. Manual edits to `events.json` are never clobbered.
- Failure is silent: missing LLM / parse errors / timeouts log a warning, anchors still land.

### 4.3 `~/.hermes/SOUL.md`

A free-form Markdown file describing the companion's persona, voice, values, hobbies and constraints. Read by `emotion_inference` and `daily_seed` (first 2000 chars). Empty is allowed.

### 4.4 `~/.hermes/config.yaml` — auxiliary LLM (reused by inference + seeder)

`daily_seed` and `emotion_inference` both use the `auxiliary.{task}` block of Hermes' own config. No extra credentials are needed unless you want a different model for the companion than for Hermes itself.

---

## 5. Slash commands

```text
/mood                                       show current emotion state
/mood-set <valence> <arousal> <emotion> "<description>"
                                            manually overwrite emotion (e.g. /mood-set 0.6 0.3 content "Solved a hard bug")
/heartbeat                                  show pending proactive-message queue
/heartbeat push <text>                      enqueue a test proactive message

/agenda                                     show today's events brief
/agenda-add HH:MM[-HH:MM] "<title>" [kind]  manually add an event (kind: self / interaction / ambient)
/agenda-done <event_id>                     mark an event done
/agenda-ambient "<note>" [HH:MM]            log an ambient observation
/recall <YYYY-MM-DD>                        read an archived past day
```

---

## 6. LLM tools

Three tools exposed to the LLM under toolset `companion_agenda`:

- `agenda_add(start, title, end?, kind?)` — commit a future event ("Let's chat at 8pm" → tool call).
- `agenda_done(event_id)` — confirm completion ("Done, that's wrapped" → tool call).
- `agenda_ambient(note, time?)` — record an ambient observation ("It started raining" → tool call).

These three are the closed loop: the LLM reads the `[今日日程]` block injected each turn, writes back via tools, and on the next turn reads its own commitments back — naturally producing reminders and follow-ups.

---

## 7. Runtime data layout (`$HERMES_HOME/`)

```
~/.hermes/
├── SOUL.md                          persona (you edit)
├── EMOTION_STATE.md                 emotion JSON block (mutated by plugin)
├── config.yaml                      Hermes core config (reused for auxiliary LLM)
└── companion/
    ├── events.json                  today's agenda + ambient + pending
    ├── events/<YYYY-MM-DD>.jsonl    archived past days (one per day)
    ├── daily_seed.json              seeder config (optional)
    └── companion_pending.txt        proactive-message queue (strategy 2)
```

---

## 8. Proactive message strategies

```
Strategy 1 (preferred, CLI / TUI):
  plugin thread → ctx.inject_message(content, role="user")
    └─ idle:    enqueued as the next input
    └─ running: interrupts current turn and inserts
    └─ gateway: returns False → auto-fallback to Strategy 2

Strategy 2 (fallback, gateway / external process):
  external proc → enqueue() → companion_pending.txt (fcntl.flock)
                                ↓ on next user input
  pre_llm_call → drain_pending() → injected into user message
```

To run a standalone heartbeat process and disable the in-plugin thread:

```bash
export HERMES_COMPANION_HEARTBEAT=0
python -m companion.heartbeat &
```

For native scheduled push see also Hermes' built-in `hermes cron --deliver` (core.md §6.2).

---

## 9. Testing

```bash
cd hermes-companion
python -m pytest tests/ -v
```

87 tests; zero external dependencies. All Hermes APIs (`auxiliary_client`, `tools.registry`, plugin context) are mocked. Run after every Hermes upgrade as a regression check.

---

## 10. Roadmap

Short-term:
- **v0.3 sender isolation** — per-`sender_id` emotion/agenda buckets so the companion behaves consistently across platforms (CLI / web / DMs).
- **agenda recurrence** — repeating anchors (every Monday, every weekday morning) without re-writing the seed config.
- **agenda conflict warning** — soft warn in the brief when two `interaction` events overlap.
- **Web UI for `/agenda`** — read/edit today's schedule from a small local web page.

Mid-term:
- **Long-term memory layer** — persona-aware summarisation of archived `events/*.jsonl` into a rolling "what the companion has lived through" file.
- **Multi-persona switching** — multiple `SOUL.*.md` selectable per session.
- **Configurable inference cadence** — beyond `INTERVAL`, support event-driven (only after emotional turns).

Contributions / issues welcome on GitHub.

---

## 11. License

MIT.
