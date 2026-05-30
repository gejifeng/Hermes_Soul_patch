"""
Heartbeat 触发器与 pending 队列。

CLI/TUI 首选：插件内线程调用 collect_heartbeat_messages() 后 ctx.inject_message()。
Gateway 下 ctx.inject_message() 不可用，本模块只能降级到 pending 队列；真正跨平台主动推送
应使用 Hermes cron --deliver。
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

from companion.emotion_state import _read_state, hermes_home

logger = logging.getLogger("companion.heartbeat")


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，使用默认值 %s", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        logger.warning("%s=%r 小于最小值 %s，使用默认值 %s", name, raw, minimum, default)
        return default
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r 不是数字，使用默认值 %.2f", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


CHECK_INTERVAL = _env_int("HERMES_COMPANION_HEARTBEAT_INTERVAL", 300, minimum=5)
# 0.70 比旧版 0.75 更接近“想主动说一句”的阈值；冷却负责防刷屏。
AROUSAL_THRESHOLD = _env_float("HERMES_COMPANION_AROUSAL_THRESHOLD", 0.70, minimum=0.0, maximum=1.0)
AROUSAL_COOLDOWN_SEC = _env_int("HERMES_COMPANION_AROUSAL_COOLDOWN", 3600, minimum=60)
AROUSAL_REPEAT_COOLDOWN_SEC = _env_int("HERMES_COMPANION_AROUSAL_REPEAT_COOLDOWN", 6 * 3600, minimum=60)
MORNING_WINDOW_MIN = _env_int("HERMES_COMPANION_MORNING_WINDOW_MIN", 10, minimum=1)


def queue_path() -> Path:
    return hermes_home() / "companion_pending.txt"


def _heartbeat_state_path() -> Path:
    return hermes_home() / "companion" / "heartbeat_state.json"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False,
    ) as tf:
        json.dump(data, tf, ensure_ascii=False, indent=2)
        tmp = tf.name
    os.replace(tmp, path)


def _load_heartbeat_state() -> dict:
    p = _heartbeat_state_path()
    if not p.exists():
        return {}
    try:
        loaded = json.loads(p.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _save_heartbeat_state(state: dict) -> None:
    try:
        _atomic_write_json(_heartbeat_state_path(), state)
    except OSError as e:
        logger.warning("save heartbeat state failed: %s", e)


def enqueue(message: str) -> None:
    """加锁追加消息到 pending 队列。多进程安全。"""
    qp = queue_path()
    qp.parent.mkdir(parents=True, exist_ok=True)
    line = message.strip().replace("\n", " ")
    if not line:
        return
    with qp.open("a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    logger.info("queued: %s", line[:80])


def drain_pending() -> str:
    """读取并清空 pending 队列。pre_llm_call 调用。"""
    qp = queue_path()
    if not qp.exists():
        return ""
    try:
        with qp.open("r+b") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                data = f.read().decode("utf-8", errors="replace")
                f.seek(0)
                f.truncate()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return data.strip()
    except OSError as e:
        logger.warning("drain_pending failed: %s", e)
        return ""


def _cooldown_ready(store: dict, key: str, now_ts: float, cooldown: int) -> bool:
    last = store.get(key, 0)
    try:
        last_f = float(last)
    except (TypeError, ValueError):
        last_f = 0.0
    return (now_ts - last_f) >= cooldown


def _format_arousal_message(state: dict) -> str:
    dominant = state.get("dominant", "unknown")
    note = str(state.get("note", "")).strip()
    energy = state.get("energy")
    social_need = state.get("social_need")
    extras = []
    if isinstance(energy, (int, float)):
        extras.append(f"energy={energy:.2f}")
    if isinstance(social_need, (int, float)):
        extras.append(f"social_need={social_need:.2f}")
    suffix = f"（{'，'.join(extras)}）" if extras else ""
    if note:
        return f"[心跳] 我现在有点 {dominant}。{note}{suffix}"
    return f"[心跳] 我现在有点 {dominant}，想主动确认一下你的状态{suffix}。"


def _round_state_value(value) -> float | None:
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    return None


def _arousal_signature(state: dict) -> str:
    """Return a stable content signature so unchanged mood does not repeat hourly."""
    payload = {
        "dominant": str(state.get("dominant", "unknown")).strip(),
        "note": str(state.get("note", "")).strip(),
        "valence": _round_state_value(state.get("valence")),
        "arousal": _round_state_value(state.get("arousal")),
        "energy": _round_state_value(state.get("energy")),
        "social_need": _round_state_value(state.get("social_need")),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def collect_heartbeat_messages(now: datetime | None = None) -> list[str]:
    """收集本 tick 应发送的主动消息，并持久化冷却状态。

    不直接发送、不直接入队，方便插件内线程和独立进程复用同一套触发规则。
    """
    now = now or datetime.now()
    now_ts = time.time()
    messages: list[str] = []
    pulse = _load_heartbeat_state()
    changed = False

    # 世界状态：跨日归档 + 到期事件。
    try:
        from companion.world_state import due_events, roll_over_if_new_day
        roll_over_if_new_day(now)
        for _key, msg in due_events(now):
            messages.append(msg)
    except Exception as e:
        logger.warning("world_state tick failed: %s", e)

    state = _read_state()
    arousal = state.get("arousal", 0)
    if isinstance(arousal, (int, float)) and arousal >= AROUSAL_THRESHOLD:
        key = "arousal"
        if _cooldown_ready(pulse, key, now_ts, AROUSAL_COOLDOWN_SEC):
            signature = _arousal_signature(state)
            previous_signature = pulse.get("arousal_signature")
            repeat_ready = _cooldown_ready(
                pulse, "arousal_repeat", now_ts, AROUSAL_REPEAT_COOLDOWN_SEC,
            )
            if signature != previous_signature or repeat_ready:
                messages.append(_format_arousal_message(state))
                pulse[key] = now_ts
                pulse["arousal_signature"] = signature
                pulse["arousal_repeat"] = now_ts
                changed = True

    today = now.strftime("%Y-%m-%d")
    if now.hour == 9 and now.minute < MORNING_WINDOW_MIN and pulse.get("greeted_date") != today:
        messages.append("[心跳] 早上好。今天有什么计划？")
        pulse["greeted_date"] = today
        changed = True

    if changed:
        _save_heartbeat_state(pulse)
    return messages


def _tick() -> None:
    for msg in collect_heartbeat_messages():
        enqueue(msg)


def run_forever(interval: int = CHECK_INTERVAL) -> None:
    logger.info(
        "heartbeat started (interval=%ds, threshold=%.2f, cooldown=%ds)",
        interval, AROUSAL_THRESHOLD, AROUSAL_COOLDOWN_SEC,
    )
    try:
        from companion.world_state import start_daily_archiver
        start_daily_archiver()
    except Exception as e:
        logger.warning("daily archiver 启动失败: %s", e)
    while True:
        try:
            _tick()
        except Exception as e:
            logger.warning("tick error: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    run_forever()
