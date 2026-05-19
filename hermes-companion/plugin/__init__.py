"""
Hermes Companion 插件注册入口（v0.2 完整版：时间 + 情感 + 主动消息 + v0.4 推断）。

Hermes 自动发现 ~/.hermes/plugins/hermes-companion/ 并调用 register(ctx)。

启用的能力：
  - pre_llm_call    每 turn 注入时间 / 情感状态 / 待发主动消息
  - post_llm_call   每 turn 完成后异步推断情感更新（v0.4）
  - post_tool_call  工具失败时小幅下调 valence
  - on_session_start 仅日志
  - slash 命令      /mood  /mood-set  /heartbeat
  - 后台 heartbeat 线程（策略 1：直接调 ctx.inject_message()，CLI 模式无延迟）

环境变量：
  HERMES_COMPANION_INFERENCE=0          关闭情感推断
  HERMES_COMPANION_INFERENCE_INTERVAL   推断最小间隔（秒，默认 60）
  HERMES_COMPANION_HEARTBEAT=0          关闭插件内 heartbeat 线程
  HERMES_COMPANION_HEARTBEAT_INTERVAL   心跳检查间隔（秒，默认 300）
  HERMES_COMPANION_AROUSAL_THRESHOLD    触发主动消息的 arousal 阈值（默认 0.75）
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

# 把仓库根目录加入 sys.path，让 `import companion.xxx` 可用
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from .commands import register_commands  # noqa: E402
from .hooks import register_hooks  # noqa: E402
from .tools import register_tools  # noqa: E402

logger = logging.getLogger(__name__)


def _heartbeat_enabled() -> bool:
    return os.environ.get("HERMES_COMPANION_HEARTBEAT", "1").strip().lower() not in (
        "0", "false", "no", "off"
    )


def _start_heartbeat_thread(ctx) -> None:
    """策略 1：插件内后台线程，直接 ctx.inject_message()。

    - CLI/TUI 模式下，arousal > 阈值时主动发起新 turn 或中断当前 turn 插入消息。
    - Gateway 模式下 ctx.inject_message() 返回 False，本线程自动降级到队列文件
      （走 pre_llm_call drain_pending 注入）。
    - 进程退出时 daemon 线程自动收回。
    """
    from companion.emotion_state import _read_state
    from companion.heartbeat import (
        AROUSAL_THRESHOLD, CHECK_INTERVAL, enqueue, queue_path,
    )

    threshold = AROUSAL_THRESHOLD
    interval = CHECK_INTERVAL
    greeted_date: dict[str, str] = {"day": ""}

    def _loop():
        logger.info("companion heartbeat thread started (interval=%ds)", interval)
        # 启动延后一下，避免插件刚加载就刷消息
        time.sleep(min(interval, 30))
        while True:
            try:
                state = _read_state()
                from datetime import datetime
                now = datetime.now()
                arousal = state.get("arousal", 0)

                msg: str | None = None
                if isinstance(arousal, (int, float)) and arousal > threshold:
                    msg = (
                        f"[心跳] 我现在处于 {state.get('dominant', 'unknown')} 状态。"
                        f"{state.get('note', '')}"
                    )
                else:
                    today = now.strftime("%Y-%m-%d")
                    if now.hour == 9 and now.minute < 10 and greeted_date["day"] != today:
                        msg = "[心跳] 早上好。今天有什么计划？"
                        greeted_date["day"] = today

                if msg:
                    ok = False
                    try:
                        ok = bool(ctx.inject_message(msg, role="user"))
                    except Exception as e:
                        logger.warning("inject_message error: %s", e)
                    if not ok:
                        # Gateway 模式或 CLI 不可用 → 降级到队列
                        try:
                            enqueue(msg)
                            logger.info("heartbeat fallback to queue: %s", queue_path())
                        except Exception as e:
                            logger.warning("enqueue fallback failed: %s", e)
            except Exception as e:
                logger.warning("heartbeat tick error: %s", e)
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name="companion-heartbeat")
    t.start()


def register(ctx) -> None:
    register_hooks(ctx)
    register_commands(ctx)
    # 注册 LLM 可调用的 agenda 工具（设计见 core_idea/core.md §4.8）
    try:
        register_tools(ctx)
    except Exception as e:
        logger.warning("agenda 工具注册失败: %s", e)
    # 启动每日自动归档线程（幂等）。HERMES_COMPANION_AUTO_ARCHIVE=0 可关闭。
    try:
        from companion.world_state import start_daily_archiver
        start_daily_archiver()
    except Exception as e:
        logger.warning("daily archiver 启动失败: %s", e)
    if _heartbeat_enabled():
        try:
            _start_heartbeat_thread(ctx)
        except Exception as e:
            logger.warning("heartbeat 线程启动失败: %s", e)
