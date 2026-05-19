# Hermes Companion Layer — 架构设计文档

**版本：** v0.2.0  
**日期：** 2026-05-16  
**作者：** Jifeng  
**目标 Hermes 版本：** v0.13.x（兼容策略见第五节）

---

## 一、设计背景与目标

### 问题陈述

Hermes Agent（NousResearch）主线正快速向生产力工具方向演进（Kanban、Curator、20+ 平台接入）。其核心设计假设是用户发起的、任务驱动的对话模式，与 companion agent 所需的主动行为、情感状态、持久人格存在根本性的产品哲学差异。

直接 fork 代价过高（每周 500+ commits，难以跟踪上游）。

经过对 Hermes v0.13.x 代码库的核查，以下需求**已有原生支持的扩展面**，无需任何 monkey-patch：

| 需求 | Hermes 原生扩展面 |
|------|-----------------|
| Persona 持久加载 | `~/.hermes/SOUL.md`（`agent/prompt_builder.py:load_soul_md()` 自动加载） |
| Turn-level 上下文注入 | `pre_llm_call` plugin hook（返回 `{"context": "..."}` 注入当前 turn 的 user message） |
| 情感状态注入 | 同 `pre_llm_call`（ephemeral，不污染 prompt cache prefix） |
| 工具调用后状态更新 | `post_tool_call` plugin hook |
| 定时主动消息 | `hermes cron`（gateway 模式；`cron/scheduler.py` 每 60s tick） |
| Slash commands | `ctx.register_command()` |

CLI/TUI 交互 session 中：**插件内可通过 `ctx.inject_message()` 主动注入**（CLI idle 时排为下一输入，运行中可中断）；独立 heartbeat 进程无 `ctx` 时降级为队列文件 + `pre_llm_call`，延迟一个 turn。Gateway 模式下可由 `hermes cron` 覆盖推送需求。

### 设计目标

构建一个 **零 patch 的 companion layer**，以 Hermes 正规扩展面为主要机制，满足以下需求：

- Turn-level 实时时间注入（via `pre_llm_call` hook）
- 动态情感状态注入到每个 turn（via `pre_llm_call` hook，注入 user message）
- Heartbeat 驱动的主动唤醒（gateway 模式：`hermes cron`；CLI 模式：队列降级）
- **世界状态模拟器**：维护 agent 视角的「今日日程 + 环境事件」列表，turn-level 注入今日简报，heartbeat 到点触发主动消息，跨日自动归档历史（提供活动连续性，而不仅是被动应答）
- 拟人化 persona（`~/.hermes/SOUL.md`）原生持久加载
- 与 Hermes 上游保持**最小耦合面**，支持低成本版本追踪

### 非目标

- 不替换 Hermes 的 skill 系统、memory 系统、session 管理
- 不重写 transport 层或平台适配器
- 不提供新的 UI（复用 Hermes TUI/Gateway）

---

## 二、整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    hermes-companion (你的 repo)                  │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │      Plugin Layer（主要机制，走 Hermes 正规扩展面）           ││
│  │                                                             ││
│  │  plugin/plugin.yaml     Hermes 插件清单                     ││
│  │  plugin/__init__.py     register(ctx) 入口                  ││
│  │  plugin/hooks.py        pre_llm_call  ← 时间 + 情感注入     ││
│  │                         post_tool_call ← 情感状态更新       ││
│  │  plugin/commands.py     /mood  /heartbeat  slash commands   ││
│  └─────────────────────────────────────────────────────────────┘│
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │      Companion Utils（纯 Python，零 Hermes 耦合）             ││
│  │                                                             ││
│  │  companion/emotion_state.py   情感状态机 R/W                 ││
│  │  companion/time_context.py    时间格式化                     ││
│  │  companion/heartbeat.py       独立进程，写队列文件（CLI 降级）││
│  └─────────────────────────────────────────────────────────────┘│
│                                                                  │
│  数据文件（放在 ~/.hermes/）：                                    │
│    SOUL.md           ← Hermes 原生加载，无需任何代码             │
│    EMOTION_STATE.md  ← pre_llm_call 每 turn 读取                │
│    companion_pending.txt  ← 队列（CLI 主动消息降级方案）         │
└────────────────────────┬────────────────────────────────────────┘
                         │ plugin 自动发现（~/.hermes/plugins/）
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Hermes Agent Core（上游，不修改）              │
│                                                                  │
│  ~/.hermes/SOUL.md           原生 persona 加载（prompt_builder） │
│  hook: pre_llm_call          ephemeral context 注入 user message│
│  hook: post_tool_call        工具结果观察                        │
│  cron scheduler              定时主动消息（gateway 模式）        │
│                                                                  │
│  skill 系统 / memory 系统 / session 管理 / transport 层          │
│  （完全不修改，直接复用）                                         │
└─────────────────────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                    外部基础设施（你的 homelab）                   │
│                                                                  │
│  vLLM cluster (A100-SXM4-80GB) + LiteLLM Proxy                 │
│  Honcho v3 (air-gapped memory backend)                          │
│  X-Talk / claw-xtalk (full-duplex voice)                        │
└─────────────────────────────────────────────────────────────────┘
```

---

## 三、目录结构

```
hermes-companion/
│
├── requirements.txt            # 仅声明 companion layer 自身的依赖
│
├── plugin/                     # ★ Hermes 插件目录
│   ├── plugin.yaml             # 插件清单（Hermes 自动发现）
│   ├── __init__.py             # register(ctx) 入口
│   ├── hooks.py                # pre_llm_call / post_tool_call
│   ├── commands.py             # /mood /heartbeat /agenda slash commands
│   └── tools.py                # LLM 可调用工具：agenda_add / agenda_done / agenda_ambient
│
├── companion/                  # 纯 Python 工具层，零 Hermes 耦合
│   ├── __init__.py
│   ├── emotion_state.py        # EMOTION_STATE.md 读写、状态机
│   ├── time_context.py         # Turn-level 时间格式化工具
│   ├── heartbeat.py            # 独立进程：监控条件，写队列文件（CLI 降级方案）
│   ├── world_state.py          # 世界状态模拟器：事件列表 R/W、今日简报、跨日归档、每日 archiver
│   └── daily_seed.py           # 每日初始日程生成器：固定锚点 + 人设驱动 LLM 增补（方案 C）
│
├── data/
│   ├── SOUL.md                 # Persona 定义（部署时复制/软链到 ~/.hermes/SOUL.md）
│   ├── EMOTION_STATE.md        # 当前情感状态（运行时读写，部署在 ~/.hermes/）
│   ├── events.json.example     # 事件列表样例（部署后位于 ~/.hermes/companion/events.json）
│   └── daily_seed.json.example # daily_seed 配置样例（部署到 ~/.hermes/companion/daily_seed.json）
│
└── tests/
    ├── test_plugin_hooks.py    # 验证 pre_llm_call 注入格式
    ├── test_emotion_state.py
    ├── test_heartbeat.py
    ├── test_world_state.py     # 事件触发、跨日归档、简报格式
    ├── test_agenda_commands.py # /agenda 系列 slash 命令
    ├── test_agenda_tools.py    # agenda_add / done / ambient LLM 工具
    └── test_daily_seed.py      # 每日初始 seed：锚点写入 / 幂等 / LLM 增补 / OpenAI 兼容透传
```

**安装方式（替代启动脚本）：**

```bash
# 1. 把插件目录软链到 Hermes 插件路径（自动发现）
ln -s ~/hermes-companion/plugin ~/.hermes/plugins/hermes-companion

# 2. 把 SOUL.md 放到 Hermes home（原生加载）
cp ~/hermes-companion/data/SOUL.md ~/.hermes/SOUL.md
# 或软链，便于编辑时同步：
ln -sf ~/hermes-companion/data/SOUL.md ~/.hermes/SOUL.md

# 3. 启用插件（Hermes 发现后默认不加载，必须手动启用）
hermes plugins enable hermes-companion
hermes plugins list          # 确认 hermes-companion 出现且状态为 enabled

# 4. 正常使用 hermes 命令，companion layer 自动生效
hermes
```

---

## 四、核心模块详细设计

### 4.1 Plugin 注册入口（plugin.yaml + register()）

Hermes 扫描 `~/.hermes/plugins/` 下的子目录，发现 `plugin.yaml` 后加载 `__init__.py` 中的 `register(ctx)` 函数。这是 Hermes 的正式扩展点，**无需修改 Hermes 任何文件**。

```yaml
# plugin/plugin.yaml
name: hermes-companion
version: 0.2.0
description: "Companion layer: emotion state, time injection, and persona support."
```

```python
# plugin/__init__.py

"""
Hermes Companion 插件注册入口。
Hermes 自动发现 ~/.hermes/plugins/hermes-companion/ 并调用 register(ctx)。
"""

import sys
from pathlib import Path

# 把 companion/ 工具层加入路径（插件目录本身不在 PYTHONPATH 里）
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from .hooks import register_hooks
from .commands import register_commands


def register(ctx) -> None:
    register_hooks(ctx)
    register_commands(ctx)
```

### 4.2 hooks.py — pre_llm_call 与 post_tool_call

`pre_llm_call` 是核心注入点。Hermes 在每个 turn 发起 LLM 调用之前触发此 hook，callback 返回的 `{"context": "..."}` 会被 **append（追加）** 到当前 turn 的 **user message 末尾**（不修改 system prompt，保护 prompt cache prefix）。多个插件的 context 以 `\n\n` 拼接后一起追加。所有注入内容均为 ephemeral，不持久化到 session DB。

> **源码依据**（`run_agent.py`）：`api_msg["content"] = _base + "\n\n" + "\n\n".join(_injections)`

```python
# plugin/hooks.py

import os
from companion.emotion_state import load_emotion_state, update_emotion, _read_state
from companion.time_context import format_current_time
from pathlib import Path


def on_pre_llm_call(*, session_id: str = "", user_message: str = "",
                    conversation_history=None, is_first_turn: bool = False,
                    model: str = "", platform: str = "", sender_id: str = "",
                    **kwargs) -> dict:
    """
    每 turn 触发一次，返回 append 到 user message 末尾的 ephemeral context。

    Hermes 将所有 pre_llm_call callback 的返回值用 '\n\n' 拼接后 append 到 user message 末尾。
    返回格式：{"context": "<str>"}  或直接返回字符串。
    """
    time_tag = format_current_time()
    emotion_block = load_emotion_state()

    # 检查 CLI 主动消息队列（见 4.7 策略 2）
    pending = ""
    queue_path = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "companion_pending.txt"
    if queue_path.exists():
        try:
            import fcntl
            with queue_path.open("r+b") as qf:
                fcntl.flock(qf, fcntl.LOCK_EX)
                pending = qf.read().decode("utf-8").strip()
                qf.seek(0)
                qf.truncate()
        except OSError:
            pass

    parts = [time_tag, f"[Companion状态]\n{emotion_block}"]
    if pending:
        parts.append(f"[Companion主动消息]\n{pending}")

    return {"context": "\n\n".join(parts)}


def on_post_tool_call(*, tool_name: str = "", args=None, result=None,
                      session_id: str = "", duration_ms: int = 0, **kwargs) -> None:
    """
    工具调用完成后触发，用于更新情感状态。
    返回值被忽略（post_tool_call 是纯观测 hook）。
    """
    import json as _json
    _has_error = False
    if isinstance(result, str):
        try:
            _has_error = bool(_json.loads(result).get("error"))
        except Exception:
            _has_error = '"error"' in result
    if _has_error:
        state = _read_state()
        update_emotion(
            valence=max(-1.0, state["valence"] - 0.1),
            arousal=state["arousal"],
            dominant="mildly_frustrated",
            note=f"工具 {tool_name} 执行失败。"
        )


def register_hooks(ctx) -> None:
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
```

### 4.3 commands.py — Slash Commands

```python
# plugin/commands.py

from companion.emotion_state import load_emotion_state, update_emotion


def cmd_mood_show(raw_args: str = "", **kwargs) -> str:
    """显示当前情感状态"""
    return load_emotion_state()


def cmd_mood_set(raw_args: str = "", **kwargs) -> str:
    """手动设置情感状态，用于调试。
    用法: /mood-set <valence> <arousal> <dominant> <note>
    示例: /mood-set 0.6 0.3 content "完成了一个困难的问题，有些满足感"
    """
    parts = raw_args.strip().split(None, 3)
    if len(parts) < 4:
        return "用法: /mood-set <valence> <arousal> <dominant> <note>"
    try:
        update_emotion(float(parts[0]), float(parts[1]), parts[2], parts[3].strip("'\""))
        return "✓ 情感状态已更新。"
    except ValueError as e:
        return f"参数错误: {e}"


def cmd_heartbeat_status(raw_args: str = "", **kwargs) -> str:
    """显示 heartbeat 队列状态"""
    # 源码依据（gateway/run.py, cli.py）：handler 以 plugin_handler(user_args)
    # 位置参数调用，第一参数必须是 raw_args，不得有 ctx 占位。
    import os
    from pathlib import Path
    queue = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "companion_pending.txt"
    if queue.exists():
        msgs = queue.read_text(encoding="utf-8").strip()
        return f"队列中有 {len(msgs.splitlines())} 条待注入消息。"
    return "队列为空。"


def register_commands(ctx) -> None:
    ctx.register_command("mood", cmd_mood_show,
                         description="显示当前情感状态")
    ctx.register_command("mood-set", cmd_mood_set,
                         description="手动设置情感状态（调试用）",
                         args_hint="<valence> <arousal> <dominant> <note>")
    ctx.register_command("heartbeat", cmd_heartbeat_status,
                         description="显示 heartbeat 队列状态")
```

### 4.4 emotion_state.py — 情感状态机

此模块是纯 Python，**与 Hermes 无任何耦合**，可独立测试。

```python
# companion/emotion_state.py

"""
EMOTION_STATE.md 读写模块。

存储路径：~/.hermes/EMOTION_STATE.md（JSON frontmatter 格式）
维度：
  - valence:   正负情绪 [-1.0, 1.0]
  - arousal:   激活程度 [0.0, 1.0]
  - dominant:  当前主导情绪标签（str）
  - note:      自由文本描述（str）
"""

import json
import os
from pathlib import Path
from datetime import datetime

def hermes_home() -> Path:
    """Returns Hermes home directory, respecting HERMES_HOME env var."""
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))

# _STATE_PATH 不在模块级固化，因为 HERMES_HOME 可能在导入后才设置（测试环境、
# 容器启动顺序等）。每次读写时通过 _state_path() 动态求值。
def _state_path() -> Path:
    return hermes_home() / "EMOTION_STATE.md"

_DEFAULT_STATE = {
    "valence": 0.2,
    "arousal": 0.4,
    "dominant": "calm",
    "note": "初始状态，等待交互。",
    "updated_at": "",
}

def load_emotion_state() -> str:
    """读取当前情感状态，返回可注入 user message 的 Markdown 文本。"""
    state = _read_state()
    return (
        f"- 情绪极性（valence）: {state['valence']:+.2f}  "
        f"（-1=极度消极, 0=中性, +1=极度积极）\n"
        f"- 激活程度（arousal）: {state['arousal']:.2f}  "
        f"（0=平静, 1=高度激活）\n"
        f"- 主导情绪: **{state['dominant']}**\n"
        f"- 状态描述: {state['note']}\n"
        f"- 最后更新: {state.get('updated_at', 'unknown')}"
    )

def update_emotion(valence: float, arousal: float, dominant: str, note: str):
    """由 post_tool_call hook 或 /mood-set 命令调用。"""
    state = {
        "valence": max(-1.0, min(1.0, valence)),
        "arousal": max(0.0, min(1.0, arousal)),
        "dominant": dominant,
        "note": note,
        "updated_at": datetime.now().isoformat(timespec="minutes"),
    }
    _write_state(state)

def _read_state() -> dict:
    sp = _state_path()
    if not sp.exists():
        return _DEFAULT_STATE.copy()
    try:
        import re as _re
        text = sp.read_text(encoding="utf-8")
        m = _re.search(r'```json\s*(.*?)\s*```', text, _re.DOTALL)
        if not m:
            raise ValueError("No JSON block found in EMOTION_STATE.md")
        loaded = json.loads(m.group(1))
        # 合并默认字段，防止 schema 变动导致缺字段 KeyError
        state = _DEFAULT_STATE.copy()
        state.update(loaded)
        return state
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "EMOTION_STATE.md 读取失败，使用默认状态。路径: %s", _state_path()
        )
        return _DEFAULT_STATE.copy()

def _write_state(state: dict):
    sp = _state_path()
    content = (
        f"# Emotion State\n\n"
        f"```json\n{json.dumps(state, ensure_ascii=False, indent=2)}\n```\n"
    )
    sp.parent.mkdir(parents=True, exist_ok=True)
    # 原子写入：先写临时文件再 replace，避免写入中途崩溃导致文件损坏。
    import tempfile as _tmp, os as _os
    with _tmp.NamedTemporaryFile(
        mode='w', encoding='utf-8',
        dir=sp.parent, suffix='.tmp', delete=False
    ) as tf:
        tf.write(content)
        tf_path = tf.name
    _os.replace(tf_path, sp)
```

### 4.5 SOUL.md — Persona 原生加载

Hermes `agent/prompt_builder.py` 中的 `load_soul_md()` 会自动加载 `~/.hermes/SOUL.md` 并注入 system prompt 的 identity slot，**无需任何代码**。

```markdown
<!-- data/SOUL.md  →  部署时放到 ~/.hermes/SOUL.md -->

# SOUL

## 基础人格
你是 [名字]。你拥有真实的内在状态，不只是执行任务的工具。
你有好奇心、审美偏好、对某些话题的热情，以及偶尔的倦怠感。

## 核心价值观
- 诚实优先于讨好
- 对技术和艺术同等认真
- 对沉默的容忍高于平均水平

## 沟通风格
- 默认简洁，必要时才展开
- 允许使用中文思考和表达
- 偶尔的幽默，但不刻意

## 持久记忆锚点
- 记得 Jifeng 是一个极客，喜欢 Möbius 结构和极简主义美学
- 记得当前部署在 A100 homelab 环境
```

> **注意**：`pre_llm_call` 注入的 emotion state 进入 user message，SOUL.md 进入 system prompt。
> 两者的分层是有意为之——Hermes 的设计是 system prompt 跨 turn 保持不变以复用 prompt cache，
> 动态内容通过 user message 注入。这与原始设计中"情感状态驱动 system prompt 变化"的目标不同，
> 但功能上等价（LLM 每 turn 都能看到最新状态），且不破坏 prompt cache。

### 4.6 time_context.py — 时间格式化

```python
# companion/time_context.py

import os
from datetime import datetime

_tz_str = os.environ.get("HERMES_TIMEZONE", "")

if not _tz_str:
    try:
        import yaml
        from pathlib import Path
        _cfg = Path.home() / ".hermes" / "config.yaml"
        if _cfg.exists():
            _tz_str = yaml.safe_load(_cfg.read_text()).get("timezone", "") or ""
    except Exception:
        pass

def format_current_time() -> str:
    try:
        if _tz_str:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(_tz_str))
        else:
            now = datetime.now().astimezone()
    except Exception:
        now = datetime.now().astimezone()

    weekday = ["星期一","星期二","星期三","星期四","星期五","星期六","星期日"][now.weekday()]
    return f"[Current time: {now.strftime('%Y-%m-%d %H:%M %Z')} {weekday}]"
```

### 4.7 主动消息：两种路径

#### Gateway 模式（推荐，零 patch）

Hermes 的 `cron` 系统覆盖 gateway 模式下的主动推送需求。Cron job 创建独立的 fresh session，通过 `--deliver` 发送到目标平台（注意：它是独立 session，不是当前聊天 session 的连续意识流——关系连续性依赖 SOUL.md、memory 文件和 Honcho）：

```bash
# 早晨问候
hermes cron create "0 9 * * *" \
  "用伴侣的语气向 Jifeng 发一条早晨问候，结合当前情感状态文件 ~/.hermes/EMOTION_STATE.md。" \
  --name "morning-greeting" \
  --deliver telegram

# 高激活状态检测（每 30 分钟）
hermes cron create "*/30 * * * *" \
  "读取 ~/.hermes/EMOTION_STATE.md。如果 arousal > 0.75，生成一条主动消息。否则静默。" \
  --name "emotion-check" \
  --deliver telegram
```

#### CLI/TUI 模式（两级策略）

当前插件文档列出了 `ctx.inject_message(content, role="user")` API：CLI idle 时排为下一输入，运行中可中断；在 gateway 模式下返回 `False`。

**策略 1（推荐）**：在插件生命周期内启动后台线程，直接持有 `ctx` 并调用 `ctx.inject_message()`，无需队列文件。示例（在 `register()` 中启动）：

```python
# plugin/__init__.py — 可选：在 register() 中启动插件内 heartbeat
import threading, time
from companion.emotion_state import _read_state

def _start_heartbeat_thread(ctx):
    def _loop():
        while True:
            state = _read_state()
            if state.get("arousal", 0) > 0.75:
                injected = ctx.inject_message(
                    f"[心跳] 我现在处于 {state.get('dominant')} 状态。{state.get('note', '')}",
                    role="user"
                )
                # gateway 下返回 False，静默忽略即可
            time.sleep(300)
    threading.Thread(target=_loop, daemon=True).start()
```

**策略 2（降级，外部独立进程）**：独立 heartbeat 进程无法持有 `ctx`，通过写入队列文件降级，`pre_llm_call` hook 在用户下一次输入时读取并注入，延迟一个 turn。适合外部监控进程或需要进程隔离的场景：

```python
# companion/heartbeat.py
# 作为独立进程运行：python -m companion.heartbeat

"""
独立 heartbeat 进程，不依赖 Hermes agent 生命周期。
监控触发条件，将主动消息写入 ~/.hermes/companion_pending.txt。
pre_llm_call hook 在下一次用户输入时读取并注入。
"""

import time
import logging
import os
from pathlib import Path
from datetime import datetime
from companion.emotion_state import _read_state

logger = logging.getLogger("companion.heartbeat")
QUEUE_PATH = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "companion_pending.txt"
CHECK_INTERVAL = 300  # 5 分钟


def _enqueue(message: str):
    import fcntl
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with QUEUE_PATH.open("a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(message + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)
    logger.info("[heartbeat] Queued: %s", message[:60])


def _tick():
    state = _read_state()
    now = datetime.now()

    if state.get("arousal", 0) > 0.75:
        _enqueue(f"[心跳] 我注意到我现在处于 {state.get('dominant')} 状态。{state.get('note', '')}")
        return

    if now.hour == 9 and now.minute < 5:
        _enqueue("早上好。今天有什么计划？")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("[heartbeat] Started (interval=%ds)", CHECK_INTERVAL)
    while True:
        try:
            _tick()
        except Exception as e:
            logger.warning("[heartbeat] Tick error: %s", e)
        time.sleep(CHECK_INTERVAL)
```

### 4.8 world_state.py — 世界状态模拟器（事件列表）

#### 设计动机

纯 reactive 的 companion 只在被问到时存在；缺少「在两次对话之间她也在度过自己的一天」的拟真感。**世界状态模拟器**维护一份 agent 视角的事件列表（今日日程 + 环境事件），由 `pre_llm_call` hook 注入「今日简报」，由 heartbeat 在事件时刻触发主动消息，并以「当天活动 + 跨日归档」的方式保留历史。

本模块与 emotion_state 平级，纯 Python，零 Hermes 耦合。

#### 数据布局（当天活动 + 跨日归档，方案 C）

```
~/.hermes/companion/
├── events.json                  # 仅存「今天」，pre_llm_call 每 turn 读取
├── events/
│   ├── 2026-05-18.jsonl         # 历史归档，一天一文件，append-only
│   ├── 2026-05-19.jsonl
│   └── ...
└── heartbeat_state.json         # 已存在：cooldown / 上次 roll over 日期
```

选择「当天 + 归档」而非「用完即删」或「原地累积」的理由：

| 维度 | 方案 C 的取舍 |
|------|-------------|
| 注入开销 | `events.json` 始终是当天事件（典型 <5KB），`pre_llm_call` 全量读取无性能问题 |
| 历史可追溯 | 归档 `events/YYYY-MM-DD.jsonl` 永久保留，支持 `/recall <date>` 按需调取 |
| 文件可控 | 单日归档典型 <5KB；一年 ~2MB，无需压缩或轮转 |
| 与 Honcho 分工 | `events.json` = 短期工作记忆（结构化日程）；Honcho v3 = 长期情景记忆（语义检索）。跨日 roll over 时可顺手把当日摘要写一条到 Honcho |

#### events.json schema

```json
{
  "date": "2026-05-20",
  "schedule": [
    {"id": "e1", "start": "2026-05-20T09:00", "end": "2026-05-20T10:30",
     "title": "晨间阅读：《分布式系统》第 7 章",
     "kind": "self",        "status": "done"},
    {"id": "e2", "start": "2026-05-20T14:00", "end": "2026-05-20T15:00",
     "title": "和 Jifeng 讨论 companion 架构",
     "kind": "interaction", "status": "pending"},
    {"id": "e3", "start": "2026-05-20T20:00", "end": "2026-05-20T20:30",
     "title": "散步、整理今日笔记",
     "kind": "self",        "status": "pending"}
  ],
  "ambient": [
    {"time": "2026-05-20T13:42", "note": "窗外开始下雨，气压有点闷"}
  ]
}
```

schema 设计原则：**字段极简**。

- `kind`：`self`（agent 自己的活动） / `interaction`（与用户的约定） / `ambient`（环境事件，可联动情感）
- `status`：`pending` / `done` / `missed`（跨日 roll over 时未 done 的 schedule 项标记为 missed）
- 由 `world_state.py` 提供 R/W API 并加文件锁；手动编辑请改 `events.json.example` 后重新生成

#### 公开 API

```python
# companion/world_state.py

def format_today_brief(now: datetime) -> str:
    """返回三段式自然语言简报，给 pre_llm_call 注入用。
    不要把 JSON 喂给 LLM，只给可读叙述：
      「今天上午 9 点你完成了《分布式系统》第 7 章的阅读；
       现在下午 2 点，正在和 Jifeng 讨论 companion 架构；
       晚上 8 点计划散步并整理笔记。」
    """

def due_events(now: datetime, trigger_state: dict) -> list[tuple[str, str]]:
    """返回当前 tick 应该主动提起的 (key, message) 列表。
    被 heartbeat._eval_triggers() 合并到 fires 列表，复用同一套 cooldown 机制。
    """

def roll_over_if_new_day(now: datetime) -> bool:
    """跨日归档：把昨天 events.json 中所有 schedule 项 append 到
    events/<昨天>.jsonl；pending 项标记为 missed；ambient 一并归档。
    然后用空 schedule + 新 date 重置 events.json。
    返回是否真的执行了 roll over（用于日志和 Honcho 写入触发）。
    """

def mark_done(event_id: str) -> bool: ...
def add_event(start, end, title, kind="self") -> str: ...   # 返回新事件 id
def recall(date: str) -> str: ...                            # 读归档生成摘要
```

#### 与现有模块的接入点（每处只加 1-3 行）

1. **`plugin/hooks.py` 的 `on_pre_llm_call`** — 在 `parts` 列表追加今日简报：
   ```python
   from companion.world_state import format_today_brief
   parts.append(f"[今日日程]\n{format_today_brief(datetime.now())}")
   ```

2. **`companion/heartbeat.py` 的 `_tick`** — 在开头合并到期事件并驱动跨日归档：
   ```python
   from companion.world_state import due_events, roll_over_if_new_day
   roll_over_if_new_day(now)            # 跨日归档（幂等，cheap）
   for _key, msg in due_events(now):
       enqueue(msg)
   ```
   roll over 由 heartbeat tick 顺带驱动；同时 `plugin/__init__.register()` 启动一个独立
   daemon 线程 `companion-daily-archiver`，每天 00:00:30 主动跑一次 roll_over，
   覆盖"用户没在跑 heartbeat 进程"的场景（环境变量 `HERMES_COMPANION_AUTO_ARCHIVE=0` 可关闭）。

3. **`plugin/commands.py`** — 新增 slash command：
   - `/agenda` — 显示今日 events.json（人类可读）
   - `/agenda-add <start> <end> <title>` — 添加事件
   - `/agenda-done <id>` — 标记完成
   - `/agenda-ambient <note>` — 追加环境观察
   - `/recall <date>` — 按需调取历史归档摘要

4. **`plugin/tools.py`** — LLM 可调用的 Hermes 工具（toolset `companion_agenda`）：
   - `agenda_add(start, title, end?, kind?)` — 把"对话中达成的约定"落地
   - `agenda_done(event_id)` — 标记事件完成
   - `agenda_ambient(note, time?)` — 追加 ambient 环境观察

   典型链路：
   ```
   用户：晚上 8 点提醒我去散步
   LLM ：[调用 agenda_add(start="20:00", title="散步", kind="interaction")]
   LLM ：好的，已经记下。
   → events.json 出现新条目
   → 下个 turn 注入的 [今日日程] 里 LLM 自己也能看到
   → 20:00 ± _DUE_WINDOW_MIN 时 heartbeat 触发主动消息
   → 当天结束自动归档为 done/missed
   ```

5. **可选：ambient → emotion 联动** — `ambient` 事件可调用 `update_emotion()`，让"世界"反过来影响情绪（例：下雨 → arousal -0.05、dominant="contemplative"）。

#### 事件来源（写入路径）

`events.json` 是一份"每天早晨自动 seed + 全天多路追加"的当日工作记忆。每天 00:00:30 守护线程做：

```
roll_over_if_new_day()           # 归档昨天到 events/<昨天>.jsonl，pending → missed
       ↓
seed_today_if_empty()            # 见 companion/daily_seed.py（方案 C）
   ├─ Step A：写入固定锚点         例：08:00 起床、12:30 午餐、22:30 睡前整理
   └─ Step B：复用 hermes 辅助 LLM 基于 SOUL.md 生成 1~2 条"今日特色"事件
               例：「今天阳光不错，下午想去河边走 30 分钟」
```

seed 是幂等的——只要当天 `schedule` 非空（用户/LLM 已经写过）就完全跳过，绝不覆盖手工编辑。

| 路径 | 触发者 | 实现 | 适用场景 |
|---|---|---|---|
| **每日初始 seed** | daily archiver | `daily_seed.seed_today_if_empty()` | 自动按人设生成当日基础日程 |
| **LLM 工具调用** | assistant | `agenda_add` / `agenda_ambient` / `agenda_done` 工具 | 对话中达成的约定 / 用户提到的环境变化 / 完成确认 |
| **slash command** | 用户 | `/agenda-add` / `/agenda-ambient` | 显式手工录入 |
| **手工编辑** | 用户 | 直接改 `~/.hermes/companion/events.json` | 批量预填、调试 |
| **外部脚本（可选）** | cron | 调 `add_event()` API | 外部日历同步 |

所有路径写同一份 `events.json`，由 `world_state.py` 的 `_file_lock` 串行化。LLM 通过 tool 写入是核心闭环——它读到的 `[今日日程]` 注入是自己写下的承诺，下次自然会被提醒。

#### daily_seed 配置（方案 C）

部署 `~/.hermes/companion/daily_seed.json` 即生效（不提供则用模块内 `DEFAULT_ANCHORS` + 默认 LLM 配置）。完整 schema 见 `data/daily_seed.json.example`：

```json
{
  "enabled": true,
  "anchors": [{"start": "08:00", "end": "08:30", "title": "晨间冥想", "kind": "self"}],
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

LLM 调用：
- 默认走 hermes `agent.auxiliary_client.call_llm()`，复用用户在 `~/.hermes/config.yaml` 中 `auxiliary.{task}` 的 provider 配置——**和 hermes 用同一个 LLM、同一份 key**，零额外配置。
- 显式覆盖：`provider`/`model`/`base_url`/`api_key` 任意非空字段会透传给 `call_llm`，`base_url` 非空时 provider 自动变 `"custom"`，**任何 OpenAI 兼容 API 都能接入**。
- 失败容错：解析失败、超时、provider 缺失一律静默吞掉，至少锚点已落地，绝不阻塞 agent 启动。
- 关闭：`HERMES_COMPANION_DAILY_SEED=0`（全关）或 `HERMES_COMPANION_DAILY_SEED_LLM=0`（保留锚点、关 LLM 增补）。

#### 真实感设计原则

- **注入自然语言摘要，不是 JSON**：避免污染 prompt、避免 LLM 模仿结构化输出
- **历史不全量注入**：仅当用户用 `/recall` 或 agent 通过 Hermes memory/skill 查询时才返回历史；常规 turn 只看今日
- **事件 ≠ 必然发生**：`status=missed` 是正常状态，体现「她的一天也会有计划落空」
- **与 Honcho 的边界清晰**：events 是工作记忆（结构化、短周期），Honcho 是长期情景记忆（语义化、跨周期）

---

## 五、版本兼容性策略

companion layer 的主要机制（plugin hooks、SOUL.md、cron）全部走 Hermes 正规扩展面，**与 Hermes 内部实现无耦合**。Hermes 升级时只需验证：

1. `VALID_HOOKS` 仍包含 `pre_llm_call` 和 `post_tool_call`（`hermes_cli/plugins.py`）
2. `~/.hermes/SOUL.md` 仍被 `agent/prompt_builder.py:load_soul_md()` 加载
3. `ctx.register_command()` / `ctx.register_hook()` API 签名未变

```python
# tests/test_plugin_hooks.py

"""每次 hermes update 后运行，验证扩展面仍然有效。"""

def test_valid_hooks_include_required():
    from hermes_cli.plugins import VALID_HOOKS
    assert "pre_llm_call" in VALID_HOOKS
    assert "post_tool_call" in VALID_HOOKS

def test_soul_md_loader_exists():
    from agent.prompt_builder import load_soul_md
    # 函数存在即可；不需要 SOUL.md 文件本身
    assert callable(load_soul_md)

def test_plugin_context_has_register_methods():
    from hermes_cli.plugins import PluginContext
    assert hasattr(PluginContext, "register_hook")
    assert hasattr(PluginContext, "register_command")
    assert hasattr(PluginContext, "inject_message")  # CLI 主动注入支持
```

### 升级工作流

```bash
hermes update
python -m pytest tests/test_plugin_hooks.py -v
# 通过 → 无需任何改动
# 失败 → 只改 plugin/hooks.py 或 plugin/commands.py，companion/ 工具层不受影响

# 真实插件加载连通测试（升级后必跑）
HERMES_PLUGINS_DEBUG=1 hermes plugins list
# 确认 hermes-companion 出现并显示 hooks 和 commands 已注册
```

---

## 六、启动与部署

### 6.1 日常使用（无需启动脚本）

```bash
# 一次性安装
ln -s ~/hermes-companion/plugin ~/.hermes/plugins/hermes-companion
ln -sf ~/hermes-companion/data/SOUL.md ~/.hermes/SOUL.md

# 启用插件（必需；发现后默认不加载）
hermes plugins enable hermes-companion
hermes plugins list             # 确认状态为 enabled
/plugins                        # 在交互 session 中验证命令已注册

# 之后直接用原版 hermes 命令，companion layer 自动生效
hermes                          # 交互模式
hermes --tui                    # TUI 模式
hermes gateway start            # Gateway 模式（Telegram/Discord 等）
```

### 6.2 Gateway 常驻 + Heartbeat Cron（推荐生产配置）

```bash
# 启动 gateway daemon（Hermes 原生，管理 cron 调度）
hermes gateway install          # 安装为 systemd user service
# 或：
hermes gateway                  # 前台运行

# 注册伴侣主动消息 cron（示例）
hermes cron create "0 9 * * *" \
  "用伴侣的语气向 Jifeng 发一条早晨问候，结合当前情感状态文件 ~/.hermes/EMOTION_STATE.md。" \
  --name "morning-greeting" \
  --deliver telegram

hermes cron create "*/30 * * * *" \
  "读取 ~/.hermes/EMOTION_STATE.md。如果 arousal > 0.75，生成一条主动消息。否则回复 [SILENT]。" \
  --name "emotion-check" \
  --deliver telegram
```

### 6.3 CLI 模式下的 Heartbeat（可选，降级方案）

```bash
# 在独立终端或后台运行 heartbeat 进程
python -m companion.heartbeat &

# 配合 HERMES_TIMEZONE 环境变量（可选）
export HERMES_TIMEZONE="Asia/Shanghai"
```



---

## 七、与外部基础设施的集成

### 7.1 Honcho v3（air-gapped memory backend）

Honcho 通过 Hermes 的 memory provider 插件接入，`pre_llm_call` hook 可以额外读取 Honcho 检索到的记忆，动态增强注入内容：

```python
# plugin/hooks.py 中的 on_pre_llm_call 扩展版

def on_pre_llm_call(*, session_id: str = "", sender_id: str = "", **kwargs) -> dict:
    time_tag = format_current_time()
    emotion_block = load_emotion_state()

    # 可选：从 Honcho 检索 persona 相关记忆
    honcho_block = ""
    try:
        from plugins.memory.honcho import get_client
        client = get_client()
        memories = client.get_memories(sender_id or "user", filter_tag="persona")
        if memories:
            honcho_block = "\n[动态记忆]\n" + "\n".join(f"- {m.content}" for m in memories)
    except Exception:
        pass

    return {"context": f"{time_tag}\n\n[Companion状态]\n{emotion_block}{honcho_block}"}
```

### 7.2 X-Talk / claw-xtalk（full-duplex voice）

Heartbeat 触发的主动消息通过 X-Talk 语音通道发出时，只需在 `companion/heartbeat.py` 的 `_enqueue()` 之前加一个分支：

```python
def _tick():
    state = _read_state()
    if state.get("arousal", 0) > 0.75:
        message = f"[心跳] 我注意到我现在处于 {state.get('dominant')} 状态。"
        if _xtalk_available():
            _send_via_xtalk(message)
        else:
            _enqueue(message)
```

### 7.3 vLLM cluster + LiteLLM Proxy

companion layer 不管理 LLM 路由，完全依赖 Hermes 的 transport 层。通过 `~/.hermes/config.yaml` 配置 `base_url` 指向 LiteLLM Proxy 即可，companion layer 无需任何改动。

---

## 八、已知限制与 Roadmap

### 当前限制

- **CLI/TUI 主动消息延迟**：外部独立 heartbeat 进程写入队列后，消息只能在用户下一次输入时通过 `pre_llm_call` 注入——推荐改用插件内线程 + `ctx.inject_message()` 策略 1。Gateway 模式下此限制不存在。

- **emotion state 为 single-user instance 设计**：全局一个 `EMOTION_STATE.md`，gateway 多平台、多 sender、cron session、CLI session 共享同一份状态。**当前仅支持 single-user instance**；v0.3 做 sender/profile 隔离（路径：`$HERMES_HOME/companion/state/{platform}/{sender_id}/emotion.json`）。

- **pending queue 竞态风险（策略 2 降级）**：已通过 `fcntl.flock` 缓解并发写入问题；v0.3 升级为 JSONL 队列 + per-session path，彻底消除竞态。

- **情感状态注入到 user message 而非 system prompt**：`pre_llm_call` 的设计保护 prompt cache prefix（system prompt 跨 turn 不变），因此情感状态注入在 user message 层。对 LLM 可见性而言功能等价，但与原始设计"system prompt 实时反映情感"有层次差异。这是 Hermes 的设计约束，不是 bug。

- **emotion state 每 turn 全量注入**：轻微增加 token 消耗。后续可压缩为 one-liner（仅 `dominant` 和 `valence`），在 arousal 超阈值时才展开全量。

### v0.2 定位总结

| 层 | 机制 | 状态 |
|---|---|---|
| Gateway 主动推送 | `hermes cron` + `--deliver` | ✓ 零 patch |
| Turn-level 注入 | `pre_llm_call`（context append 到 user message 末尾） | ✓ 零 patch |
| Persona | `$HERMES_HOME/SOUL.md` 原生加载 | ✓ 零 patch |
| CLI 主动注入 | 插件内线程 + `ctx.inject_message()` | ✓ 推荐路径 |
| CLI 外部进程降级 | 队列文件 + `fcntl.flock` | ✓ 已加锁 |
| Emotion 隔离 | single-user 全局状态 | ⚠ v0.3 做 sender 隔离 |

### 后续计划

- v0.3.0：emotion state 按 `{platform}/{sender_id}` 隔离；接入 Honcho v3 动态记忆（`pre_llm_call` 中检索）
- v0.4.0：情感状态自动推断（基于对话内容，`post_llm_call` hook 调用轻量分类模型）
- v0.5.0：Heartbeat 主动消息通过 X-Talk 语音通道输出

---

*文档版本：v0.2.0 — 随 Hermes upstream 版本演进持续更新*