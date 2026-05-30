"""
对话 ↔ 世界状态联动观察器。

world_state 本身只提供结构化事件 CRUD；本模块在 post_llm_call 后读取本轮
用户/助手文本，做低风险的自动联动：
  - 用户明确说完成/回来了/搞定了 → 标记当前或匹配的 pending 事件为 done
  - 用户明确取消/不去了/改天 → 标记当前或匹配的 pending 事件为 missed
  - 用户明确约定某个时间 → 写入 interaction 事件
  - 用户提到环境变化 → 写入 ambient 观察

规则刻意保守：只处理明显信号，避免把闲聊流水账写进 events.json。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta

from companion.world_state import (
    add_ambient,
    add_event,
    list_today,
    mark_done,
    mark_missed,
    roll_over_if_new_day,
)

logger = logging.getLogger(__name__)

_DONE_PATTERNS = (
    "做完了", "完成了", "搞定了", "弄好了", "结束了", "已经好了",
    "回来了", "到了", "吃完了", "写完了", "改完了", "处理完了",
)
_CANCEL_PATTERNS = (
    "取消", "不去了", "不做了", "不聊了", "算了", "改天", "推迟",
)
_NEGATED_DONE = ("没做完", "没有做完", "还没做完", "没完成", "还没完成")

_TIME_RE = re.compile(
    r"(?P<prefix>今天|今晚|明天晚上|明天|明晚|早上|上午|中午|下午|晚上)?"
    r"\s*(?P<hour>\d{1,2})\s*(?:点|:|：)\s*(?P<minute>\d{1,2})?"
)
_SCHEDULE_VERBS = (
    "提醒", "叫我", "喊我", "找我", "一起", "约", "聊", "讨论",
    "听", "看", "复盘", "整理", "检查",
)
_QUESTION_TIME = ("几点", "什么时间", "现在时间")
_AMBIENT_WORDS = (
    "下雨", "雨声", "打雷", "风很大", "窗外", "天黑", "天亮",
    "很热", "很冷", "好安静", "宿舍", "图书馆", "灯光",
)


def _enabled() -> bool:
    return os.environ.get("HERMES_COMPANION_WORLD_OBSERVER", "1").strip().lower() not in (
        "0", "false", "no", "off"
    )


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def _tokens(text: str) -> set[str]:
    # 中文没有可靠空格，这里只取连续英文数字和长度>=2的中文片段做弱匹配。
    chunks = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", text)
    return {c.lower() for c in chunks if c.strip()}


def _pending_events(now: datetime) -> list[dict]:
    events = []
    for ev in list_today().get("schedule", []):
        if ev.get("status") != "pending":
            continue
        start = _parse_iso(ev.get("start", ""))
        end = _parse_iso(ev.get("end", "")) if ev.get("end") else None
        ev["_start_dt"] = start
        ev["_end_dt"] = end
        events.append(ev)
    return events


def _event_score(ev: dict, text: str, now: datetime) -> int:
    score = 0
    title = ev.get("title", "")
    title_tokens = _tokens(title)
    text_tokens = _tokens(text)
    if title and title in text:
        score += 8
    score += min(6, len(title_tokens & text_tokens) * 2)

    start = ev.get("_start_dt")
    end = ev.get("_end_dt")
    if start and end and start <= now <= end:
        score += 5
    elif start and abs((now - start).total_seconds()) <= 2 * 3600:
        score += 3
    elif start and start <= now:
        score += 1
    return score


def _best_event_for_text(text: str, now: datetime) -> dict | None:
    scored = [(_event_score(ev, text, now), ev) for ev in _pending_events(now)]
    scored = [(score, ev) for score, ev in scored if score > 0]
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def _mark_status_from_text(text: str, now: datetime) -> list[dict]:
    updates: list[dict] = []
    if _contains_any(text, _NEGATED_DONE):
        return updates

    status = None
    if _contains_any(text, _DONE_PATTERNS):
        status = "done"
    elif _contains_any(text, _CANCEL_PATTERNS):
        status = "missed"
    if not status:
        return updates

    ev = _best_event_for_text(text, now)
    if not ev:
        return updates

    ev_id = ev.get("id", "")
    ok = mark_done(ev_id) if status == "done" else mark_missed(ev_id)
    if ok:
        updates.append({"action": f"mark_{status}", "id": ev_id, "title": ev.get("title", "")})
    return updates


def _coerce_time(match: re.Match, now: datetime) -> datetime | None:
    prefix = match.group("prefix") or ""
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    if hour > 23 or minute > 59:
        return None

    if prefix in ("下午", "晚上", "今晚", "明晚", "明天晚上") and hour < 12:
        hour += 12
    if prefix == "中午" and hour < 11:
        hour += 12

    day = now.date()
    if prefix in ("明天", "明晚", "明天晚上"):
        day = (now + timedelta(days=1)).date()
    dt = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
    return dt


def _has_similar_event(start: datetime, title: str) -> bool:
    title_tokens = _tokens(title)
    for ev in list_today().get("schedule", []):
        if ev.get("status") != "pending":
            continue
        ev_start = _parse_iso(ev.get("start", ""))
        if not ev_start or abs((ev_start - start).total_seconds()) > 15 * 60:
            continue
        if title_tokens & _tokens(ev.get("title", "")):
            return True
    return False


def _extract_schedule(text: str, now: datetime) -> list[dict]:
    updates: list[dict] = []
    if _contains_any(text, _QUESTION_TIME):
        return updates
    if not _contains_any(text, _SCHEDULE_VERBS):
        return updates

    for match in _TIME_RE.finditer(text):
        start = _coerce_time(match, now)
        if not start:
            continue
        # 没有“明天”前缀时，如果时间已经过去超过 30 分钟，视作回忆而非新约定。
        prefix = match.group("prefix") or ""
        if prefix not in ("明天", "明晚", "明天晚上") and start < now - timedelta(minutes=30):
            continue

        title = re.sub(r"\s+", " ", text).strip()
        title = re.sub(r"[。！？!?,，]+$", "", title)
        if len(title) > 48:
            title = title[:48] + "..."
        title = f"互动约定：{title}"
        if _has_similar_event(start, title):
            continue

        end = start + timedelta(minutes=30)
        try:
            ev_id = add_event(
                start.isoformat(timespec="minutes"),
                end.isoformat(timespec="minutes"),
                title,
                kind="interaction",
            )
        except ValueError:
            continue
        updates.append({"action": "add_interaction", "id": ev_id, "title": title})
        break
    return updates


def _record_ambient(text: str, now: datetime) -> list[dict]:
    if not _contains_any(text, _AMBIENT_WORDS):
        return []
    note = re.sub(r"\s+", " ", text).strip()
    if len(note) > 56:
        note = note[:56] + "..."
    try:
        add_ambient(f"互动提到：{note}", when=now)
    except ValueError:
        return []
    return [{"action": "add_ambient", "note": note}]


def observe_interaction(
    *,
    user_message: str,
    assistant_response: str = "",
    conversation_history=None,
    now: datetime | None = None,
) -> list[dict]:
    """观察一轮互动并更新 world_state。返回实际写入/更新的操作列表。"""
    if not _enabled() or not user_message:
        return []

    now = now or datetime.now()
    try:
        roll_over_if_new_day(now)
    except Exception:
        logger.debug("world_interaction: roll_over failed", exc_info=True)

    text = user_message.strip()
    updates: list[dict] = []
    try:
        updates.extend(_mark_status_from_text(text, now))
        updates.extend(_extract_schedule(text, now))
        updates.extend(_record_ambient(text, now))
    except Exception as e:
        logger.warning("world_interaction: observe failed %s: %s", type(e).__name__, e)
        return updates

    if updates:
        logger.info("world_interaction: updates=%s", updates)
    return updates
