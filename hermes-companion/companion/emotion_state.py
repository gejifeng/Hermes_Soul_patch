"""
EMOTION_STATE.md 读写 + 状态机。

存储路径：$HERMES_HOME/EMOTION_STATE.md（默认 ~/.hermes/EMOTION_STATE.md）

文件格式：
    # Emotion State

    ```json
    {
      "valence":  -1.0 ~ 1.0,
      "arousal":   0.0 ~ 1.0,
      "dominant":  str,
      "note":      str,
      "updated_at": ISO datetime
    }
    ```

设计要点：
  - 路径不在模块级固化（动态求 _state_path()）——HERMES_HOME 可能在测试中
    monkeypatch 后才生效，container 启动顺序等场景同理。
  - 写入用临时文件 + os.replace 原子替换，避免崩溃写坏文件。
  - 读取失败一律降级到 _DEFAULT_STATE，hook 永不抛异常。
  - 与 Hermes 零耦合，可独立 import 与测试。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


_DEFAULT_STATE: dict = {
    "valence": 0.2,
    "arousal": 0.4,
    "dominant": "calm",
    "note": "初始状态，等待交互。",
    "updated_at": "",
}

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


def hermes_home() -> Path:
    """返回 Hermes home 目录，遵循 HERMES_HOME 环境变量。"""
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _state_path() -> Path:
    return hermes_home() / "EMOTION_STATE.md"


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _read_state() -> dict:
    """读取 EMOTION_STATE.md，返回合并了默认字段的 dict。"""
    sp = _state_path()
    if not sp.exists():
        return _DEFAULT_STATE.copy()
    try:
        text = sp.read_text(encoding="utf-8")
        m = _JSON_BLOCK_RE.search(text)
        if not m:
            raise ValueError("EMOTION_STATE.md 中找不到 ```json 块")
        loaded = json.loads(m.group(1))
        state = _DEFAULT_STATE.copy()
        state.update(loaded)
        return state
    except Exception:
        logger.warning("EMOTION_STATE.md 读取失败，回落默认。path=%s", sp)
        return _DEFAULT_STATE.copy()


def _write_state(state: dict) -> None:
    """原子写入：先写临时文件再 replace。"""
    sp = _state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Emotion State\n\n"
        f"```json\n{json.dumps(state, ensure_ascii=False, indent=2)}\n```\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8",
        dir=sp.parent, suffix=".tmp", delete=False,
    ) as tf:
        tf.write(content)
        tmp_path = tf.name
    os.replace(tmp_path, sp)


def load_emotion_state() -> str:
    """读取当前状态，返回适合注入 user message 的 Markdown 文本块。"""
    state = _read_state()
    return (
        f"- 情绪极性（valence）: {state['valence']:+.2f}  "
        f"（-1=极度消极, 0=中性, +1=极度积极）\n"
        f"- 激活程度（arousal）: {state['arousal']:.2f}  "
        f"（0=平静, 1=高度激活）\n"
        f"- 主导情绪: **{state['dominant']}**\n"
        f"- 状态描述: {state['note']}\n"
        f"- 最后更新: {state.get('updated_at') or 'unknown'}"
    )


def update_emotion(valence: float, arousal: float, dominant: str, note: str) -> dict:
    """更新情感状态。由 post_tool_call hook / post_llm_call 推断 / /mood-set 调用。"""
    state = {
        "valence": _clamp(valence, -1.0, 1.0),
        "arousal": _clamp(arousal, 0.0, 1.0),
        "dominant": str(dominant).strip() or _DEFAULT_STATE["dominant"],
        "note": str(note).strip(),
        "updated_at": datetime.now().isoformat(timespec="minutes"),
    }
    _write_state(state)
    return state


def nudge_emotion(
    *,
    valence_delta: float = 0.0,
    arousal_delta: float = 0.0,
    dominant: str | None = None,
    note: str | None = None,
) -> dict:
    """读改写一次性增量更新。用于工具失败等小幅扰动。"""
    cur = _read_state()
    return update_emotion(
        valence=cur["valence"] + valence_delta,
        arousal=cur["arousal"] + arousal_delta,
        dominant=dominant or cur["dominant"],
        note=note if note is not None else cur["note"],
    )
