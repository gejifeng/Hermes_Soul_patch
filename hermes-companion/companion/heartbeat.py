"""
独立 heartbeat 进程（策略 2 降级方案）。

用法：
    python -m companion.heartbeat              # 前台
    python -m companion.heartbeat &            # 后台

监控触发条件，把主动消息追加到 $HERMES_HOME/companion_pending.txt。
pre_llm_call hook 在用户下次输入时读取并注入到 user message，延迟一个 turn。

适用：外部独立监控进程、跨进程隔离的部署场景。
首选：插件内线程 + ctx.inject_message()（无延迟，见 plugin/__init__.py）。
"""

from __future__ import annotations

import fcntl
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from companion.emotion_state import _read_state, hermes_home

logger = logging.getLogger("companion.heartbeat")

CHECK_INTERVAL = int(os.environ.get("HERMES_COMPANION_HEARTBEAT_INTERVAL", "300"))
AROUSAL_THRESHOLD = float(os.environ.get("HERMES_COMPANION_AROUSAL_THRESHOLD", "0.75"))


def queue_path() -> Path:
    return hermes_home() / "companion_pending.txt"


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
    """读取并清空 pending 队列。pre_llm_call 调用。

    多进程安全：持有排它锁期间 read+truncate+release。
    返回 "" 表示队列为空。
    """
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


# ---------------------------------------------------------------------------
# 内置触发器（最小集）。可被覆盖：用户可以写自己的 heartbeat 脚本调用 enqueue()。
# ---------------------------------------------------------------------------

_GREETED_DATE: str | None = None


def _tick() -> None:
    global _GREETED_DATE
    state = _read_state()
    now = datetime.now()

    # 世界状态：跨日归档 + 到期事件入队（设计见 core_idea/core.md §4.8）
    try:
        from companion.world_state import due_events, roll_over_if_new_day
        roll_over_if_new_day(now)  # 幂等
        for _key, msg in due_events(now):
            enqueue(msg)
    except Exception as e:
        logger.warning("world_state tick failed: %s", e)

    # 高激活提醒
    arousal = state.get("arousal", 0)
    if isinstance(arousal, (int, float)) and arousal > AROUSAL_THRESHOLD:
        enqueue(
            f"[心跳] 我注意到我现在处于 {state.get('dominant', 'unknown')} 状态。"
            f"{state.get('note', '')}"
        )
        return

    # 每天 09:00 早安一次
    today = now.strftime("%Y-%m-%d")
    if now.hour == 9 and now.minute < 10 and _GREETED_DATE != today:
        enqueue("[心跳] 早上好。今天有什么计划？")
        _GREETED_DATE = today


def run_forever(interval: int = CHECK_INTERVAL) -> None:
    logger.info("heartbeat started (interval=%ds, threshold=%.2f)",
                interval, AROUSAL_THRESHOLD)
    # 启动每日自动归档。守护线程，随进程退出。
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
