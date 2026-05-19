"""
Companion slash commands.

  /mood              显示当前情感状态
  /mood-set ...      手动设置情感状态（调试）
  /heartbeat         显示主动消息队列状态 + 控制 / 触发心跳
  /agenda            显示今日事件列表（世界状态模拟器，见 core.md §4.8）
  /agenda-add ...    添加事件
  /agenda-done <id>  标记事件完成
  /agenda-ambient    追加一条环境事件
  /recall <date>     调取某日归档摘要

handler 签名约定（hermes_cli/plugins.py）：fn(raw_args: str, **kw) -> str | None
"""

from __future__ import annotations

import logging
import shlex
from datetime import datetime

from companion.emotion_state import load_emotion_state, update_emotion
from companion.heartbeat import enqueue, queue_path
from companion.world_state import (
    add_ambient,
    add_event,
    list_today,
    mark_done,
    recall,
)

logger = logging.getLogger(__name__)


def cmd_mood_show(raw_args: str = "", **kwargs) -> str:
    return load_emotion_state()


def cmd_mood_set(raw_args: str = "", **kwargs) -> str:
    """用法: /mood-set <valence> <arousal> <dominant> <note>
    例:   /mood-set 0.6 0.3 content 完成了一个困难问题
    """
    parts = (raw_args or "").strip().split(None, 3)
    if len(parts) < 4:
        return "用法: /mood-set <valence:-1~1> <arousal:0~1> <dominant> <note>"
    try:
        valence = float(parts[0])
        arousal = float(parts[1])
    except ValueError:
        return "valence / arousal 必须是数字"
    dominant = parts[2].strip()
    note = parts[3].strip().strip("'\"")
    state = update_emotion(valence, arousal, dominant, note)
    return (
        "✓ 情感状态已更新：\n"
        f"  valence={state['valence']:+.2f} "
        f"arousal={state['arousal']:.2f} "
        f"dominant={state['dominant']}"
    )


def cmd_heartbeat(raw_args: str = "", **kwargs) -> str:
    """无参=显示队列；'push <msg>' = 手动入队一条主动消息（调试用）。"""
    args = (raw_args or "").strip()
    if args.startswith("push "):
        msg = args[5:].strip()
        if not msg:
            return "用法: /heartbeat push <消息正文>"
        enqueue(msg)
        return f"✓ 已入队：{msg[:60]}"

    qp = queue_path()
    if not qp.exists() or qp.stat().st_size == 0:
        return f"队列为空（{qp}）。"
    text = qp.read_text(encoding="utf-8").strip()
    n = len([ln for ln in text.splitlines() if ln.strip()])
    return f"队列中有 {n} 条待注入消息（{qp}）。"


def register_commands(ctx) -> None:
    ctx.register_command("mood", cmd_mood_show,
                         description="显示当前情感状态")
    ctx.register_command("mood-set", cmd_mood_set,
                         description="手动设置情感状态（调试）",
                         args_hint="<valence> <arousal> <dominant> <note>")
    ctx.register_command("heartbeat", cmd_heartbeat,
                         description="显示/控制 companion 主动消息队列",
                         args_hint="[push <msg>]")
    ctx.register_command("agenda", cmd_agenda,
                         description="显示今日事件列表")
    ctx.register_command("agenda-add", cmd_agenda_add,
                         description="添加事件",
                         args_hint="<start> <end> <title> [kind]")
    ctx.register_command("agenda-done", cmd_agenda_done,
                         description="标记事件完成",
                         args_hint="<event_id>")
    ctx.register_command("agenda-ambient", cmd_agenda_ambient,
                         description="追加一条环境事件",
                         args_hint="<note>")
    ctx.register_command("recall", cmd_recall,
                         description="读取某日归档摘要",
                         args_hint="<YYYY-MM-DD>")


# ---------------- /agenda 系列 ----------------

def cmd_agenda(raw_args: str = "", **_kw) -> str:
    """显示今日事件列表（人类可读）。"""
    doc = list_today()
    schedule = doc.get("schedule", [])
    ambient = doc.get("ambient", [])
    lines = [f"# 今日日程（{doc.get('date', '?')}）"]

    if not schedule:
        lines.append("（无日程）")
    else:
        for ev in schedule:
            marker = {
                "done": "✓",
                "missed": "✗",
                "pending": "·",
            }.get(ev.get("status", "?"), "?")
            start = ev.get("start", "")
            end = ev.get("end", "")
            clock = start[-5:] if "T" in start else start
            end_clock = end[-5:] if "T" in end else end
            time_seg = f"{clock}–{end_clock}" if end_clock else clock
            lines.append(
                f"  {marker} [{ev.get('id', '?')}] {time_seg} "
                f"{ev.get('title', '(未命名)')}  «{ev.get('kind', 'self')}»"
            )

    if ambient:
        lines.append("")
        lines.append("# 环境")
        for a in ambient[-5:]:
            t = a.get("time", "")
            clock = t[-5:] if "T" in t else t
            lines.append(f"  · {clock} {a.get('note', '')}")
    return "\n".join(lines)


def cmd_agenda_add(raw_args: str = "", **_kw) -> str:
    """
    /agenda-add <start> <end> <title> [kind]

    时间格式：ISO（YYYY-MM-DDTHH:MM）或 HH:MM（自动补今天日期）。
    end 可填 "-" 表示不设结束时间。
    title 含空格请用引号包裹。kind 可选 self / interaction / ambient（默认 self）。
    """
    try:
        parts = shlex.split(raw_args or "")
    except ValueError as e:
        return f"参数解析失败：{e}"
    if len(parts) < 3:
        return ("用法: /agenda-add <start> <end> <title> [kind]\n"
                "示例: /agenda-add 14:00 15:00 \"和 Jifeng 讨论\" interaction")

    start = _normalize_time(parts[0])
    end_raw = parts[1]
    end = "" if end_raw in ("-", "_", "none", "None") else _normalize_time(end_raw)
    title = parts[2]
    kind = parts[3] if len(parts) >= 4 else "self"

    try:
        eid = add_event(start=start, end=end, title=title, kind=kind)
    except ValueError as e:
        return f"参数错误：{e}"
    except Exception as e:
        logger.warning("agenda-add failed: %s", e)
        return f"添加失败：{e}"
    return f"✓ 已添加事件 [{eid}] {title}"


def cmd_agenda_done(raw_args: str = "", **_kw) -> str:
    ev_id = (raw_args or "").strip()
    if not ev_id:
        return "用法: /agenda-done <event_id>"
    ok = mark_done(ev_id)
    return f"✓ 已标记 [{ev_id}] 完成" if ok else f"未找到事件 [{ev_id}]"


def cmd_agenda_ambient(raw_args: str = "", **_kw) -> str:
    note = (raw_args or "").strip()
    if not note:
        return "用法: /agenda-ambient <note>"
    try:
        add_ambient(note)
    except ValueError as e:
        return f"参数错误：{e}"
    return f"✓ 已记录环境事件：{note}"


def cmd_recall(raw_args: str = "", **_kw) -> str:
    date = (raw_args or "").strip()
    if not date:
        return "用法: /recall <YYYY-MM-DD>"
    return recall(date)


def _normalize_time(s: str) -> str:
    """HH:MM → 今天 ISO；其它原样返回让 add_event 校验。"""
    s = s.strip()
    if "T" in s or len(s) >= 10:
        return s
    today = datetime.now().strftime("%Y-%m-%d")
    if len(s) in (5, 8) and s[2] == ":":
        return f"{today}T{s[:5]}"
    return s
