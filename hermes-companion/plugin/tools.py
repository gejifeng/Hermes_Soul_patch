"""
LLM 可调用的世界状态写入工具（设计见 core_idea/core.md §4.8）。

把"agent 在对话中和用户达成的约定"落地到 events.json：

  agenda_add        新增一个 schedule 事件（默认 kind=interaction）
  agenda_done       把某事件标记完成
  agenda_ambient    追加一条 ambient 环境观察

LLM 视角：

  用户：晚上 8 点提醒我去散步
  LLM ：[调用 agenda_add(start="20:00", title="散步", kind="interaction")]
  LLM ：好的，已经记下，20:00 提醒你。

下次 pre_llm_call 注入的 [今日日程] 里就会出现这条，heartbeat 也会在 20:00 触发主动消息。

Handler 签名：fn(args: dict, **kw) -> str（必须返回 JSON 字符串）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from companion.world_state import (
    _KIND_INTERACTION,
    _VALID_KINDS,
    add_ambient,
    add_event,
    mark_done,
)

logger = logging.getLogger(__name__)


# tool_result / tool_error 在 hermes 运行时由 tools.registry 提供，
# 单测环境下用本地 JSON stub，保证测试不依赖 hermes-agent。
try:
    from tools.registry import tool_error, tool_result  # type: ignore
except ImportError:  # pragma: no cover - 仅在 hermes 不可用时走
    def tool_result(data=None, **kwargs) -> str:
        payload = {"ok": True}
        if data is not None:
            payload["data"] = data
        payload.update(kwargs)
        return json.dumps(payload, ensure_ascii=False)

    def tool_error(message, **extra) -> str:
        payload = {"ok": False, "error": str(message)}
        payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)


# ---------------- 时间归一 ----------------

def _to_iso(s: str) -> str:
    """HH:MM → 今天 ISO；ISO 原样返回。空串返回空串。

    支持："14:00"、"14:00:00"、"2026-05-20T14:00"。
    """
    s = (s or "").strip()
    if not s:
        return ""
    if "T" in s or len(s) >= 16:
        return s
    if len(s) in (5, 8) and s[2] == ":":
        today = datetime.now().strftime("%Y-%m-%d")
        return f"{today}T{s[:5]}"
    return s  # 让 add_event 自己抛 ValueError


def _default_end(start_iso: str) -> str:
    """没传 end 时，默认 start + 30 分钟。失败返回空串。"""
    try:
        # 接受 "YYYY-MM-DDTHH:MM" 或 "YYYY-MM-DDTHH:MM:SS"
        fmt = "%Y-%m-%dT%H:%M" if len(start_iso) == 16 else "%Y-%m-%dT%H:%M:%S"
        dt = datetime.strptime(start_iso, fmt)
        from datetime import timedelta
        return (dt + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        return ""


# ---------------- agenda_add ----------------

AGENDA_ADD_SCHEMA = {
    "name": "agenda_add",
    "description": (
        "把一个新的日程/约定写入 companion 的世界状态 events.json。"
        "当用户和你（assistant）约定某个时间点要做某件事、或提到自己接下来要做什么时调用。"
        "例如『晚上 8 点提醒我散步』、『下午 3 点和我讨论方案』。"
        "事件会出现在下一次 [今日日程] 注入里，并在 start 时刻被 heartbeat 触发主动消息。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "start": {
                "type": "string",
                "description": (
                    "开始时间。优先 HH:MM（今天）或完整 ISO YYYY-MM-DDTHH:MM。"
                    "示例 '20:00' 或 '2026-05-20T20:00'。"
                ),
            },
            "title": {
                "type": "string",
                "description": "事件标题，简短一句话。例如『散步』、『和 X 讨论 Y 方案』。",
            },
            "end": {
                "type": "string",
                "description": (
                    "结束时间，可选。同 start 格式。不传则默认 start + 30 分钟。"
                ),
            },
            "kind": {
                "type": "string",
                "enum": ["self", "interaction", "ambient"],
                "description": (
                    "事件类型："
                    "self=companion 自己安排的活动；"
                    "interaction=与用户的约定（默认值）；"
                    "ambient=环境事件（一般请用 agenda_ambient）。"
                ),
            },
        },
        "required": ["start", "title"],
    },
}


def _handle_agenda_add(args: dict, **kw) -> str:
    start_raw = args.get("start", "")
    title = (args.get("title") or "").strip()
    end_raw = args.get("end", "")
    kind = (args.get("kind") or _KIND_INTERACTION).strip()

    if not title:
        return tool_error("title 不可为空")
    if kind not in _VALID_KINDS:
        return tool_error(
            f"kind 必须是 {sorted(_VALID_KINDS)} 之一",
            received=kind,
        )

    start_iso = _to_iso(start_raw)
    if not start_iso:
        return tool_error("start 不可为空")
    end_iso = _to_iso(end_raw) if end_raw else _default_end(start_iso)

    try:
        ev_id = add_event(start_iso, end_iso, title, kind=kind)
    except ValueError as e:
        return tool_error(str(e))

    logger.info("[tool agenda_add] id=%s %s %s", ev_id, start_iso, title)
    return tool_result({
        "id": ev_id,
        "start": start_iso,
        "end": end_iso,
        "title": title,
        "kind": kind,
    })


# ---------------- agenda_done ----------------

AGENDA_DONE_SCHEMA = {
    "name": "agenda_done",
    "description": (
        "把一个 events.json 中的事件标记为已完成（status=done）。"
        "当用户确认某件事已经做完时调用。需要事件 id（可从 [今日日程] 注入中读到）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "event_id": {
                "type": "string",
                "description": "事件 id，例如 'e1' 或 '8 位 hex'。",
            },
        },
        "required": ["event_id"],
    },
}


def _handle_agenda_done(args: dict, **kw) -> str:
    ev_id = (args.get("event_id") or "").strip()
    if not ev_id:
        return tool_error("event_id 不可为空")
    if mark_done(ev_id):
        logger.info("[tool agenda_done] id=%s", ev_id)
        return tool_result({"id": ev_id, "status": "done"})
    return tool_error(f"找不到事件 id={ev_id!r}")


# ---------------- agenda_ambient ----------------

AGENDA_AMBIENT_SCHEMA = {
    "name": "agenda_ambient",
    "description": (
        "追加一条 ambient 环境观察到 events.json。"
        "适合用户/你提到的非约定型背景信息：天气变化、室内环境、外部事件等。"
        "例如『窗外开始下雨了』、『家里很安静』。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "note": {
                "type": "string",
                "description": "环境观察描述，一句话。",
            },
            "time": {
                "type": "string",
                "description": (
                    "观察时间，可选。HH:MM（今天）或完整 ISO。不传则用当前时间。"
                ),
            },
        },
        "required": ["note"],
    },
}


def _handle_agenda_ambient(args: dict, **kw) -> str:
    note = (args.get("note") or "").strip()
    if not note:
        return tool_error("note 不可为空")

    when: datetime | None = None
    raw_time = (args.get("time") or "").strip()
    if raw_time:
        iso = _to_iso(raw_time)
        try:
            fmt = "%Y-%m-%dT%H:%M" if len(iso) == 16 else "%Y-%m-%dT%H:%M:%S"
            when = datetime.strptime(iso, fmt)
        except (ValueError, TypeError):
            return tool_error(f"time 不是合法时间: {raw_time!r}")

    try:
        add_ambient(note, when=when)
    except ValueError as e:
        return tool_error(str(e))

    logger.info("[tool agenda_ambient] %s", note)
    return tool_result({"note": note, "time": (when or datetime.now()).isoformat(timespec="minutes")})


# ---------------- 注册 ----------------

_TOOLS = (
    ("agenda_add", AGENDA_ADD_SCHEMA, _handle_agenda_add, "📝"),
    ("agenda_done", AGENDA_DONE_SCHEMA, _handle_agenda_done, "✅"),
    ("agenda_ambient", AGENDA_AMBIENT_SCHEMA, _handle_agenda_ambient, "🌧️"),
)


def register_tools(ctx) -> None:
    """注册三个 agenda 工具到 hermes 工具注册表。"""
    for name, schema, handler, emoji in _TOOLS:
        try:
            ctx.register_tool(
                name=name,
                toolset="companion_agenda",
                schema=schema,
                handler=handler,
                emoji=emoji,
            )
        except Exception as e:
            logger.warning("注册工具 %s 失败: %s", name, e)
