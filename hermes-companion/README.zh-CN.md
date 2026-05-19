# hermes-companion

为 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 注入人设的陪伴层插件。通过 Hermes 官方扩展接口（plugin hooks / commands / tools）实现 **时间感知、情感状态、每日日程、主动消息、自动每日 seed**，对 Hermes 源码零修改。

> 姊妹项目：[hermes-time_perception-extension](https://github.com/gejifeng/hermes-time_perception-extension) —— 本插件 `time_context` 模块与之共享同一时区解析链。

English: [README.md](README.md) · 设计文档：[core_idea/core.md](../core_idea/core.md)

---

## 1. 已有功能

| 能力 | 机制 | 文件 |
|---|---|---|
| **时间感知** | `pre_llm_call` 注入 `[Current time: YYYY-MM-DD HH:MM <IANA时区> 星期X]`。时区委托 Hermes `hermes_time._resolve_timezone_name()`。 | [companion/time_context.py](companion/time_context.py) |
| **情感状态注入** | `pre_llm_call` 读 `EMOTION_STATE.md`（valence / arousal / 主导情绪 / 状态描述）并在 user message 末尾追加 `[Companion状态]` 块。 | [companion/emotion_state.py](companion/emotion_state.py) |
| **情感自动推断** | `post_llm_call` 触发后台 LLM 调用（`agent.auxiliary_client.call_llm`）根据最近一轮对话更新情感。 | [companion/emotion_inference.py](companion/emotion_inference.py) |
| **工具失败下调 valence** | `post_tool_call` 在 tool error 时小幅下调 valence。 | [plugin/hooks.py](plugin/hooks.py) |
| **世界状态（今日日程）** | 用 `fcntl.flock` 原子读写 `events.json`；每天 00:00:30 归档昨日，pending → missed；每 turn 注入 `[今日日程]` 简报。 | [companion/world_state.py](companion/world_state.py) |
| **每日日程 seeder（方案 C）** | 新一天：写入固定锚点 + 基于 `SOUL.md` 的 LLM 人设增补。复用 Hermes 辅助 LLM，支持 OpenAI 兼容 API。 | [companion/daily_seed.py](companion/daily_seed.py) |
| **LLM 可调用工具** | `agenda_add` / `agenda_done` / `agenda_ambient` —— LLM 把自己的承诺写回 `events.json`，闭环。 | [plugin/tools.py](plugin/tools.py) |
| **主动消息 — 策略 1** | 插件内后台线程 + `ctx.inject_message()`（CLI / TUI 即时）。 | [plugin/__init__.py](plugin/__init__.py) |
| **主动消息 — 策略 2** | 外部进程 → `companion_pending.txt` 文件队列（加锁）→ 下次 `pre_llm_call` drain（gateway 兜底）。 | [companion/heartbeat.py](companion/heartbeat.py) |
| **Slash 命令** | `/mood` `/mood-set` `/heartbeat` `/agenda` `/agenda-add` `/agenda-done` `/agenda-ambient` `/recall` | [plugin/commands.py](plugin/commands.py) |

主动消息触发逻辑：`arousal > HERMES_COMPANION_AROUSAL_THRESHOLD`（默认 `0.75`），或每日 09:00 一次早安。Gateway 模式下 `inject_message` 不可用时自动降级到文件队列。

---

## 2. 文件结构

```
hermes-companion/
├── plugin/                      # Hermes 插件入口层
│   ├── plugin.yaml              # 插件清单（Hermes 自动发现）
│   ├── __init__.py              # register(ctx)：hooks / commands / tools / 后台线程
│   ├── hooks.py                 # pre_llm_call / post_llm_call / post_tool_call / on_session_start
│   ├── commands.py              # /mood, /heartbeat, /agenda*, /recall
│   └── tools.py                 # agenda_add / agenda_done / agenda_ambient（LLM 工具）
├── companion/                   # 纯 Python，零 Hermes 耦合
│   ├── time_context.py          # 时间标签格式化（委托 hermes_time）
│   ├── emotion_state.py         # EMOTION_STATE.md 读写 + 状态机
│   ├── emotion_inference.py     # 异步 LLM 驱动状态更新
│   ├── world_state.py           # events.json 读写、归档、今日简报
│   ├── daily_seed.py            # 方案 C 每日 seed（锚点 + LLM）
│   └── heartbeat.py             # 独立 heartbeat 进程（兜底）
├── data/
│   ├── SOUL.md                  # 人设定义（部署到 ~/.hermes/SOUL.md）
│   ├── EMOTION_STATE.md         # 情感状态 JSON 块（运行时写）
│   ├── events.json.example      # events.json schema 样例
│   └── daily_seed.json.example  # daily_seed 配置样例
└── tests/                       # 87 个单测；零外部依赖（所有 Hermes API mock）
```

---

## 3. 安装部署

```bash
git clone https://github.com/gejifeng/Hermes_Soul_patch.git
cd Hermes_Soul_patch/hermes-companion

# 把插件目录软链到 Hermes 插件目录
ln -s "$PWD/plugin" ~/.hermes/plugins/hermes-companion

# 首次安装：部署人设 + 状态文件 + 可选配置
cp data/SOUL.md            ~/.hermes/SOUL.md
cp data/EMOTION_STATE.md   ~/.hermes/EMOTION_STATE.md
mkdir -p ~/.hermes/companion
cp data/events.json.example       ~/.hermes/companion/events.json
cp data/daily_seed.json.example   ~/.hermes/companion/daily_seed.json   # 可选

# 让 Hermes Python 找到 companion 包（任选其一）：
#   a) 在 Hermes venv 的 site-packages 里放一个软链；或
#   b) shell rc 里：
export PYTHONPATH="$PWD:$PYTHONPATH"

hermes plugins enable hermes-companion
hermes plugins list      # 确认 enabled
```

冒烟测试：

```bash
python -m pytest tests/ -v       # 期望：87 passed
python -c "from companion.time_context import format_current_time; print(format_current_time())"
```

---

## 4. 配置

### 4.1 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `HERMES_TIMEZONE` | 系统本地 | 时间标签时区。**优先级**：`hermes_time._resolve_timezone_name()`（与 Hermes 主程序一致）> `HERMES_TIMEZONE` > `~/.hermes/config.yaml: timezone` > 系统时区。 |
| `HERMES_HOME` | `~/.hermes` | 所有运行时状态文件根路径。 |
| `HERMES_COMPANION_INFERENCE` | `1` | `0` 关闭情感自动推断。 |
| `HERMES_COMPANION_INFERENCE_INTERVAL` | `60` | 两次推断最小间隔（秒）。 |
| `HERMES_COMPANION_HEARTBEAT` | `1` | `0` 关闭插件内 heartbeat 线程（改用外部进程时）。 |
| `HERMES_COMPANION_HEARTBEAT_INTERVAL` | `300` | 心跳检查间隔（秒）。 |
| `HERMES_COMPANION_AROUSAL_THRESHOLD` | `0.75` | 触发主动消息的 arousal 阈值。 |
| `HERMES_COMPANION_DAILY_SEED` | `1` | `0` 完全关闭每日 seeder。 |
| `HERMES_COMPANION_DAILY_SEED_LLM` | `1` | `0` 保留锚点写入但关闭 LLM 增补。 |

### 4.2 `~/.hermes/companion/daily_seed.json`（方案 C — 混合每日 seeder）

人设驱动的每日日程生成器。**Step A** 写入固定锚点；**Step B** 用小型 LLM 调用（默认走 Hermes `auxiliary.title_generation` 同款模型）基于 `SOUL.md` 产出 1–2 条人设风味的额外事件。

```json
{
  "enabled": true,
  "anchors": [
    {"start": "08:00", "end": "08:30", "title": "起床、给窗台的绿植浇水", "kind": "self"},
    {"start": "12:30", "end": "13:30", "title": "午餐", "kind": "self"},
    {"start": "18:30", "end": "19:30", "title": "晚餐", "kind": "self"},
    {"start": "22:30", "end": "23:00", "title": "睡前整理今日笔记", "kind": "self"}
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

说明：
- `llm.provider / model / base_url / api_key` 透传给 `auxiliary_client.call_llm`。`base_url` 非空时 `provider` 自动变 `"custom"` → **任何 OpenAI 兼容 API 都能接入**（Moonshot / DeepSeek / vLLM / Ollama OpenAI shim 等）。
- `kind` 取值：`self`（companion 自己的活动）/ `interaction`（与用户的约定）/ `ambient`（环境事件）。
- seeder 是**幂等的**：当天 `schedule` 已非空（你手动编辑过、或 LLM 已经写过）则直接跳过。手工编辑 `events.json` 绝不会被覆盖。
- 失败静默：LLM 缺失 / 解析失败 / 超时只记 warning，锚点照常落地，不阻塞 agent 启动。

### 4.3 `~/.hermes/SOUL.md`

自由格式的 Markdown，描述陪伴体的人设、语气、价值观、爱好和约束。被 `emotion_inference` 与 `daily_seed` 读取（最多前 2000 字符）。允许为空。

### 4.4 `~/.hermes/config.yaml` —— 辅助 LLM（推断 + seeder 共用）

`daily_seed` 与 `emotion_inference` 都走 Hermes 自身配置的 `auxiliary.{task}` 段。无需额外凭据，除非你想给陪伴体配一个不同于 Hermes 主对话的模型。

---

## 5. Slash 命令

```text
/mood                                       显示当前情感状态
/mood-set <valence> <arousal> <emotion> "<描述>"
                                            手动覆盖情感（如 /mood-set 0.6 0.3 content "解决了一个难 bug"）
/heartbeat                                  显示主动消息队列
/heartbeat push <text>                      入队一条测试主动消息

/agenda                                     显示今日日程简报
/agenda-add HH:MM[-HH:MM] "<title>" [kind]  手动添加事件（kind：self / interaction / ambient）
/agenda-done <event_id>                     标记事件已完成
/agenda-ambient "<note>" [HH:MM]            记录一条环境观察
/recall <YYYY-MM-DD>                        查阅某个归档日的事件
```

---

## 6. LLM 工具

`companion_agenda` toolset 暴露给 LLM 的三个工具：

- `agenda_add(start, title, end?, kind?)` —— 提交一个未来事件（"我们 8 点聊吧" → 工具调用）。
- `agenda_done(event_id)` —— 确认完成（"做完了" → 工具调用）。
- `agenda_ambient(note, time?)` —— 记录环境观察（"窗外开始下雨" → 工具调用）。

这三者构成闭环：LLM 每 turn 读 `[今日日程]` 注入块，通过工具写回，下 turn 再读到自己写下的承诺 —— 自然产生提醒和跟进。

---

## 7. 运行时数据布局（`$HERMES_HOME/`）

```
~/.hermes/
├── SOUL.md                          人设（你来写）
├── EMOTION_STATE.md                 情感 JSON 块（插件修改）
├── config.yaml                      Hermes 主配置（辅助 LLM 复用）
└── companion/
    ├── events.json                  今日 schedule + ambient + pending
    ├── events/<YYYY-MM-DD>.jsonl    归档（每天一份）
    ├── daily_seed.json              seeder 配置（可选）
    └── companion_pending.txt        主动消息队列（策略 2）
```

---

## 8. 主动消息两条路径

```
策略 1（推荐，CLI / TUI）：
  插件线程 → ctx.inject_message(content, role="user")
    └─ idle:    排为下一输入
    └─ running: 中断当前 turn 并插入
    └─ gateway: 返回 False → 自动 fallback 到策略 2

策略 2（兜底，gateway / 外部进程）：
  外部进程 → enqueue() → companion_pending.txt（fcntl.flock）
                            ↓ 下次用户输入
  pre_llm_call → drain_pending() → 注入到 user message
```

跑独立 heartbeat 进程并关插件内线程：

```bash
export HERMES_COMPANION_HEARTBEAT=0
python -m companion.heartbeat &
```

原生定时推送另有 Hermes 自带的 `hermes cron --deliver`（零代码，见 core.md §6.2）。

---

## 9. 测试

```bash
cd hermes-companion
python -m pytest tests/ -v
```

87 个测试用例，零外部依赖。所有 Hermes API（`auxiliary_client` / `tools.registry` / plugin context）都被 mock 过。Hermes 升级后跑一遍可快速回归。

---

## 10. 后续开发路线

**短期：**
- **v0.3 sender 隔离** —— 按 `sender_id` 区分情感/日程桶，让陪伴体在多平台（CLI / web / DM）行为一致。
- **agenda 周期事件** —— 支持"每周一"、"每个工作日早上"等重复锚点，不必每天 seed 配置里硬写。
- **agenda 冲突提示** —— 简报里软提示两个 `interaction` 事件时间重叠。
- **`/agenda` Web UI** —— 一个小本地网页用来读/改今日日程。

**中期：**
- **长期记忆层** —— 把 `events/*.jsonl` 归档做人设感知的滚动摘要，写入"陪伴体经历过什么"。
- **多人设切换** —— 支持多个 `SOUL.*.md`，按 session 选择。
- **可配置推断节奏** —— 不止 `INTERVAL`，支持事件驱动（只在情感波动 turn 触发）。

欢迎在 GitHub 提 issue / PR。

---

## 11. License

MIT.
