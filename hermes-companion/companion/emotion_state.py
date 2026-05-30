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
    "energy": 0.45,
    "social_need": 0.35,
    "confidence": 0.55,
    "momentum": 0.0,
    "dominant": "calm",
    "note": "初始状态，等待交互。",
    "updated_at": "",
    "source": "default",
    "event_log": [],
}

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


def hermes_home() -> Path:
    """返回 Hermes home 目录，遵循 HERMES_HOME 环境变量。"""
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _state_path() -> Path:
    return hermes_home() / "EMOTION_STATE.md"


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _coerce_state(state: dict) -> dict:
    """补齐并规范化状态字段，兼容旧版 EMOTION_STATE.md。"""
    out = _DEFAULT_STATE.copy()
    out.update(state or {})
    out["valence"] = _clamp(out.get("valence", 0.2), -1.0, 1.0)
    for key in ("arousal", "energy", "social_need", "confidence"):
        out[key] = _clamp(out.get(key, _DEFAULT_STATE[key]), 0.0, 1.0)
    out["momentum"] = _clamp(out.get("momentum", 0.0), -1.0, 1.0)
    out["dominant"] = str(out.get("dominant") or _DEFAULT_STATE["dominant"]).strip()
    out["note"] = str(out.get("note") or "").strip()
    out["source"] = str(out.get("source") or "unknown").strip()
    log = out.get("event_log")
    out["event_log"] = log[-12:] if isinstance(log, list) else []
    return out


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
        return _coerce_state(loaded)
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
    recent = state.get("event_log") or []
    recent_text = ""
    if recent:
        items = [
            f"{e.get('t', '?')} {e.get('dominant', '?')}({e.get('valence', 0):+.2f}/{e.get('arousal', 0):.2f})"
            for e in recent[-3:] if isinstance(e, dict)
        ]
        if items:
            recent_text = "\n- 最近情绪轨迹: " + "；".join(items)
    return (
        f"- 情绪极性（valence）: {state['valence']:+.2f}  "
        f"（-1=极度消极, 0=中性, +1=极度积极）\n"
        f"- 激活程度（arousal）: {state['arousal']:.2f}  "
        f"（0=平静, 1=高度激活）\n"
        f"- 能量（energy）: {state['energy']:.2f}；亲近需求（social_need）: {state['social_need']:.2f}\n"
        f"- 判断置信度（confidence）: {state['confidence']:.2f}；情绪惯性（momentum）: {state['momentum']:+.2f}\n"
        f"- 主导情绪: **{state['dominant']}**\n"
        f"- 状态描述: {state['note']}\n"
        f"- 最后更新: {state.get('updated_at') or 'unknown'}（source={state.get('source', 'unknown')}）"
        f"{recent_text}"
    )


def update_emotion(
    valence: float,
    arousal: float,
    dominant: str,
    note: str,
    *,
    energy: float | None = None,
    social_need: float | None = None,
    confidence: float | None = None,
    momentum: float | None = None,
    source: str = "manual",
) -> dict:
    """更新情感状态。由 post_tool_call hook / post_llm_call 推断 / /mood-set 调用。"""
    cur = _read_state()
    valence_f = _clamp(valence, -1.0, 1.0)
    arousal_f = _clamp(arousal, 0.0, 1.0)
    now = datetime.now().isoformat(timespec="minutes")
    dominant_s = str(dominant).strip() or _DEFAULT_STATE["dominant"]
    note_s = str(note).strip()
    log = list(cur.get("event_log") or [])
    log.append({
        "t": now,
        "valence": round(valence_f, 3),
        "arousal": round(arousal_f, 3),
        "dominant": dominant_s,
        "note": note_s[:80],
        "source": source,
    })
    state = {
        "valence": valence_f,
        "arousal": arousal_f,
        "energy": _clamp(energy if energy is not None else cur.get("energy", arousal_f), 0.0, 1.0),
        "social_need": _clamp(social_need if social_need is not None else cur.get("social_need", 0.35), 0.0, 1.0),
        "confidence": _clamp(confidence if confidence is not None else cur.get("confidence", 0.55), 0.0, 1.0),
        "momentum": _clamp(momentum if momentum is not None else (valence_f - cur.get("valence", 0.0)), -1.0, 1.0),
        "dominant": dominant_s,
        "note": note_s,
        "updated_at": now,
        "source": source,
        "event_log": log[-12:],
    }
    _write_state(state)
    return state


def nudge_emotion(
    *,
    valence_delta: float = 0.0,
    arousal_delta: float = 0.0,
    dominant: str | None = None,
    note: str | None = None,
    energy_delta: float = 0.0,
    social_need_delta: float = 0.0,
    confidence_delta: float = 0.0,
    source: str = "nudge",
) -> dict:
    """读改写一次性增量更新。用于工具失败等小幅扰动。"""
    cur = _read_state()
    return update_emotion(
        valence=cur["valence"] + valence_delta,
        arousal=cur["arousal"] + arousal_delta,
        energy=cur["energy"] + energy_delta,
        social_need=cur["social_need"] + social_need_delta,
        confidence=cur["confidence"] + confidence_delta,
        dominant=dominant or cur["dominant"],
        note=note if note is not None else cur["note"],
        source=source,
    )
