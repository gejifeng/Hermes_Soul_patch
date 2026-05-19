"""
Hermes Companion 插件 hooks。

挂载点：
  - pre_llm_call    每 turn LLM 调用前注入 [时间 + 情感状态 + 待发主动消息]
  - post_llm_call   每 turn 完成后调用辅助 LLM 推断情感更新（v0.4，节流+后台线程）
  - post_tool_call  工具失败时小幅降低 valence
  - on_session_start 仅打日志，便于排查插件加载
"""

from __future__ import annotations

import json
import logging

from companion.emotion_inference import schedule_inference
from companion.emotion_state import load_emotion_state, nudge_emotion
from companion.heartbeat import drain_pending
from companion.time_context import format_current_time
from companion.world_state import format_today_brief, roll_over_if_new_day

logger = logging.getLogger(__name__)


# --- pre_llm_call -----------------------------------------------------------

def on_pre_llm_call(*, session_id: str = "", user_message: str = "",
                    conversation_history=None, is_first_turn: bool = False,
                    model: str = "", platform: str = "", sender_id: str = "",
                    **kwargs) -> dict:
    """返回 {"context": "..."}，被 append 到当前 turn 的 user message 末尾。"""
    parts: list[str] = [format_current_time()]

    try:
        parts.append(f"[Companion状态]\n{load_emotion_state()}")
    except Exception as e:
        logger.warning("emotion 注入失败: %s", e)

    try:
        # 顺手触发跨日归档（幂等，cheap）。即使没有 heartbeat 进程在跑，
        # 任何 hermes 交互都会保证 events.json 不会停留在旧日期。
        roll_over_if_new_day()
        brief = format_today_brief()
        if brief:
            parts.append(f"[今日日程]\n{brief}")
    except Exception as e:
        logger.warning("world_state 注入失败: %s", e)

    try:
        pending = drain_pending()
        if pending:
            parts.append(f"[Companion主动消息]\n{pending}")
    except Exception as e:
        logger.warning("pending 注入失败: %s", e)

    return {"context": "\n\n".join(parts)}


# --- post_llm_call (v0.4 emotion inference) ---------------------------------

def on_post_llm_call(*, session_id: str = "", user_message: str = "",
                     assistant_response: str = "",
                     conversation_history=None, model: str = "",
                     platform: str = "", **kwargs) -> None:
    """每 turn 完成后异步推断情感状态更新。永不阻塞。"""
    try:
        schedule_inference(
            user_message=user_message,
            assistant_response=assistant_response,
            conversation_history=conversation_history,
        )
    except Exception as e:
        logger.warning("schedule_inference 失败: %s", e)


# --- post_tool_call ---------------------------------------------------------

def _result_has_error(result) -> bool:
    if result is None:
        return False
    if isinstance(result, dict):
        return bool(result.get("error") or result.get("is_error"))
    if isinstance(result, str):
        s = result.lstrip()
        if s.startswith("{"):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    return bool(obj.get("error") or obj.get("is_error"))
            except Exception:
                pass
        return '"error"' in s or '"is_error": true' in s
    return False


def on_post_tool_call(*, tool_name: str = "", args=None, result=None,
                      session_id: str = "", duration_ms: int = 0,
                      task_id: str = "", tool_call_id: str = "",
                      **kwargs) -> None:
    if not _result_has_error(result):
        return
    try:
        nudge_emotion(
            valence_delta=-0.1,
            dominant="mildly_frustrated",
            note=f"工具 {tool_name} 执行失败。",
        )
    except Exception as e:
        logger.warning("emotion nudge 失败: %s", e)


# --- on_session_start -------------------------------------------------------

def on_session_start(*, session_id: str = "", platform: str = "", **kwargs) -> None:
    logger.info("companion: session_start sid=%s platform=%s", session_id, platform)


def register_hooks(ctx) -> None:
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("on_session_start", on_session_start)
