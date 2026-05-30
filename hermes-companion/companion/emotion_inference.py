"""
v0.4 情感推断：根据最近一个 turn 的对话内容更新 EMOTION_STATE.md。

工作流：
    on_post_llm_call 触发 → 节流检查 → 拼装 prompt → 调用辅助 LLM
    → 解析 JSON → update_emotion()

设计要点：
  - 调用 Hermes 内置的 agent.auxiliary_client.call_llm()，复用用户主 provider，
    无需 companion layer 配置 API key。
  - 后台线程跑，永不阻塞主 turn。
  - 节流：两次推断至少间隔 MIN_INTERVAL_SEC（默认 60s），避免长会话刷爆 token。
  - 任何异常（Hermes 未安装、provider 不可用、JSON 解析失败）一律静默吞掉，
    保留旧状态，绝不污染主对话流。
  - 关闭开关：HERMES_COMPANION_INFERENCE=0
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Iterable

from companion.emotion_state import _read_state, update_emotion

logger = logging.getLogger(__name__)

# 模块级节流。多个 turn 并发触发时用锁兜底。
_last_run_ts: float = 0.0
_lock = threading.Lock()

MIN_INTERVAL_SEC = int(os.environ.get("HERMES_COMPANION_INFERENCE_INTERVAL", "60"))

_AUX_TASK = "title_generation"  # 复用已有的轻量任务配置；用户可在 config.yaml 覆盖
_MAX_HISTORY_CHARS = 1500

_SYSTEM_PROMPT = """你是情感状态分析器。读取下面一段最近的用户↔助手对话，
评估【助手】此刻应当保持的情感状态。严格输出一行 JSON，不要任何其它文字：

{"valence": -1..1, "arousal": 0..1, "energy": 0..1, "social_need": 0..1, "confidence": 0..1, "momentum": -1..1, "dominant": "<短英文标签>", "note": "<不超过30字的中文描述>"}

规则：
- valence: -1 极度消极、0 中性、+1 极度积极
- arousal: 0 平静、1 高度激活
- energy: 当前精力/行动余量，0 倦怠，1 很有精力
- social_need: 主动靠近/想联系用户的需求，0 不需要，1 很强
- confidence: 你对本次情绪判断的置信度，信息不足时降低
- momentum: 相比上次状态的情绪惯性/变化方向，负数回落，正数升温
- dominant 用小写英文短词，如 calm / curious / content / frustrated / excited / tender / weary
- 若对话很平淡，保持小幅度变化（不超过 ±0.15）
- 只输出一行 JSON，不要前后缀
"""

_JSON_LINE_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _enabled() -> bool:
    return os.environ.get("HERMES_COMPANION_INFERENCE", "1").strip().lower() not in (
        "0", "false", "no", "off"
    )


def _throttled() -> bool:
    """如果距上次运行不足 MIN_INTERVAL_SEC 返回 True。"""
    global _last_run_ts
    now = time.monotonic()
    with _lock:
        if now - _last_run_ts < MIN_INTERVAL_SEC:
            return True
        _last_run_ts = now
        return False


def _truncate_tail(text: str, limit: int = _MAX_HISTORY_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def _summarize_history(
    user_message: str,
    assistant_response: str,
    conversation_history: Iterable[dict] | None = None,
) -> str:
    """提取最后两 turn 的精简文本（含历史的上一 user/assistant）。"""
    parts: list[str] = []
    if conversation_history:
        hist = list(conversation_history)
        # 倒序找上一对 user/assistant 作为铺垫
        prev_assistant = ""
        prev_user = ""
        for msg in reversed(hist[:-1] if hist else []):
            if msg.get("role") == "assistant" and not prev_assistant:
                prev_assistant = msg.get("content", "") or ""
            elif msg.get("role") == "user" and not prev_user:
                prev_user = msg.get("content", "") or ""
            if prev_user and prev_assistant:
                break
        if prev_user:
            parts.append(f"[上一轮 用户] {_truncate_tail(prev_user, 400)}")
        if prev_assistant:
            parts.append(f"[上一轮 助手] {_truncate_tail(prev_assistant, 400)}")

    parts.append(f"[本轮 用户] {_truncate_tail(user_message, 600)}")
    parts.append(f"[本轮 助手] {_truncate_tail(assistant_response, 600)}")
    return "\n".join(parts)


def _parse_inference(raw: str) -> dict | None:
    if not raw:
        return None
    # reasoning 模型常先 think 后输出，可能在 thinking 里举例 JSON。
    # 取最后一个匹配作为最终答案。
    matches = list(_JSON_LINE_RE.finditer(raw))
    if not matches:
        return None
    for m in reversed(matches):
        try:
            obj = json.loads(m.group(0))
        except Exception:
            continue
        if not isinstance(obj, dict) or "valence" not in obj:
            continue
        try:
            return {
                "valence": float(obj.get("valence", 0.0)),
                "arousal": float(obj.get("arousal", 0.0)),
                "energy": float(obj.get("energy", obj.get("arousal", 0.0))),
                "social_need": float(obj.get("social_need", 0.35)),
                "confidence": float(obj.get("confidence", 0.55)),
                "momentum": float(obj.get("momentum", 0.0)),
                "dominant": str(obj.get("dominant", "")).strip(),
                "note": str(obj.get("note", "")).strip(),
            }
        except Exception:
            continue
    return None


def _call_aux_llm(messages: list[dict]) -> str:
    """调用 Hermes 辅助 LLM；Hermes 未安装时抛 ImportError。"""
    from agent.auxiliary_client import call_llm, extract_content_or_reasoning

    resp = call_llm(
        task=_AUX_TASK,
        messages=messages,
        temperature=0.3,
        max_tokens=600,  # reasoning 模型会先 think 再输出，给够空间
        # Qwen3+ 系列：关闭 thinking 模式直接输出 JSON。其他 provider 会忽略此字段。
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    return extract_content_or_reasoning(resp) or ""


def _infer_and_update(
    user_message: str,
    assistant_response: str,
    conversation_history,
) -> None:
    try:
        body = _summarize_history(user_message, assistant_response, conversation_history)
        cur = _read_state()
        sys_prompt = (
            _SYSTEM_PROMPT
            + f"\n当前 EMOTION_STATE: valence={cur['valence']:+.2f} "
              f"arousal={cur['arousal']:.2f} dominant={cur['dominant']}"
        )
        raw = _call_aux_llm([
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": body},
        ])
        parsed = _parse_inference(raw)
        if parsed is None:
            logger.info("emotion_inference: 解析失败，原始输出=%r", raw[:200])
            return
        update_emotion(**parsed, source="inference")
        logger.info(
            "emotion_inference: 更新 → valence=%+.2f arousal=%.2f dominant=%s",
            parsed["valence"], parsed["arousal"], parsed["dominant"],
        )
    except ImportError:
        logger.debug("emotion_inference: 未检测到 agent.auxiliary_client，跳过。")
    except Exception as e:
        logger.warning("emotion_inference: 推断失败 %s: %s", type(e).__name__, e)


def schedule_inference(
    *,
    user_message: str,
    assistant_response: str,
    conversation_history=None,
) -> bool:
    """post_llm_call 调用入口。

    返回是否真正发起了推断（False=被关闭或被节流）。
    始终在后台线程跑，立即返回，永不阻塞主 turn。
    """
    if not _enabled():
        return False
    if not assistant_response or not user_message:
        return False
    if _throttled():
        return False
    t = threading.Thread(
        target=_infer_and_update,
        args=(user_message, assistant_response, conversation_history),
        daemon=True,
        name="companion-emotion-inference",
    )
    t.start()
    return True


def reset_throttle_for_tests() -> None:
    """测试 helper：清零节流时间戳。"""
    global _last_run_ts
    with _lock:
        _last_run_ts = 0.0
