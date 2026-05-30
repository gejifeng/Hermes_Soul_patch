"""
世界状态模拟器 —— 事件列表（agent 视角的"今日日程 + 环境事件"）。

设计参见 core_idea/core.md §4.8。本模块纯 Python，与 Hermes 零耦合，可独立测试。

数据布局（方案 C：当天活动 + 跨日归档）：

    $HERMES_HOME/companion/
    ├── events.json                  当天事件，pre_llm_call 每 turn 读取
    └── events/
        ├── 2026-05-18.jsonl         历史归档，一天一文件，append-only
        └── ...

events.json schema：

    {
      "date": "YYYY-MM-DD",
      "schedule": [
        {"id": str, "start": ISO, "end": ISO, "title": str,
         "kind": "self"|"interaction"|"ambient", "status": "pending"|"done"|"missed"}
      ],
      "ambient": [
        {"time": ISO, "note": str}
      ]
    }

设计要点：
  - 所有写入加 fcntl 排他锁，串行化"手工编辑"与"LLM 自动生成"两条路径。
  - 写入用临时文件 + os.replace 原子替换，避免崩溃写坏文件。
  - 路径动态求值（_events_path()），HERMES_HOME 在测试中 monkeypatch 后仍生效。
  - 任何读取失败都降级到空状态，hook/heartbeat 永不抛异常。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from companion.emotion_state import hermes_home

logger = logging.getLogger(__name__)

_KIND_SELF = "self"
_KIND_INTERACTION = "interaction"
_KIND_AMBIENT = "ambient"
_VALID_KINDS = {_KIND_SELF, _KIND_INTERACTION, _KIND_AMBIENT}

_STATUS_PENDING = "pending"
_STATUS_DONE = "done"
_STATUS_MISSED = "missed"

# 事件触发窗口（分钟）：start 时刻前后该窗口内 tick 命中则触发主动消息。
# 必须 >= heartbeat tick interval，否则可能错过。
_DUE_WINDOW_MIN = 10

# 单事件触发后多久不再重复（同一 event id）
_EVENT_COOLDOWN_SEC = 24 * 3600

_WEEKDAYS_ZH = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# ---------------- 路径与默认值 ----------------

def _companion_dir() -> Path:
    return hermes_home() / "companion"


def _events_path() -> Path:
    return _companion_dir() / "events.json"


def _archive_dir() -> Path:
    return _companion_dir() / "events"


def _archive_path(date_str: str) -> Path:
    return _archive_dir() / f"{date_str}.jsonl"


def _trigger_state_path() -> Path:
    """单独持久化事件触发 cooldown，与 heartbeat 解耦。"""
    return _companion_dir() / "world_trigger_state.json"


def _empty_doc(date_str: str) -> dict:
    return {"date": date_str, "schedule": [], "ambient": []}


# ---------------- 锁与原子写 ----------------

@contextmanager
def _file_lock(path: Path):
    """fcntl 排他锁。仅 POSIX 可用，与 heartbeat.py 一致的取舍。"""
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    # 用独立 lock 文件，避免 lock fd 与读写 fd 冲突
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8",
        dir=path.parent, suffix=".tmp", delete=False,
    ) as tf:
        json.dump(data, tf, ensure_ascii=False, indent=2)
        tmp = tf.name
    os.replace(tmp, path)


# ---------------- 读写 events.json ----------------

def _read_events() -> dict:
    """读取 events.json，失败一律返回今天的空文档。"""
    ep = _events_path()
    today = datetime.now().strftime("%Y-%m-%d")
    if not ep.exists():
        return _empty_doc(today)
    try:
        loaded = json.loads(ep.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("events.json 顶层不是对象")
        loaded.setdefault("date", today)
        loaded.setdefault("schedule", [])
        loaded.setdefault("ambient", [])
        return loaded
    except Exception:
        logger.warning("events.json 读取失败，回落空文档。path=%s", ep)
        return _empty_doc(today)


def _write_events(doc: dict) -> None:
    _atomic_write_json(_events_path(), doc)


# ---------------- 解析时间 ----------------

def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _fmt_clock(s: str) -> str:
    """ISO datetime → HH:MM；解析失败返回原串。"""
    dt = _parse_iso(s)
    return dt.strftime("%H:%M") if dt else s


# ---------------- 公开 API：今日简报 ----------------

def format_today_brief(now: datetime | None = None) -> str:
    """
    返回供 pre_llm_call 注入的自然语言简报。

    格式：三段式（已完成 / 当前进行 / 接下来）+ ambient 备注。
    不要把 JSON 喂给 LLM，只给可读叙述。
    """
    now = now or datetime.now()
    doc = _read_events()
    schedule = doc.get("schedule", [])
    ambient = doc.get("ambient", [])

    done: list[str] = []
    current: list[str] = []
    upcoming: list[str] = []

    for ev in schedule:
        start = _parse_iso(ev.get("start", ""))
        end = _parse_iso(ev.get("end", "")) if ev.get("end") else None
        title = ev.get("title", "(未命名事件)")
        status = ev.get("status", _STATUS_PENDING)
        clock = _fmt_clock(ev.get("start", ""))

        ev_id = ev.get("id", "")
        id_hint = f" id={ev_id}" if ev_id else ""

        if status == _STATUS_DONE:
            done.append(f"{clock} 完成了「{title}」({status}{id_hint})")
            continue
        if status == _STATUS_MISSED:
            done.append(f"{clock} 错过了「{title}」({status}{id_hint})")
            continue
        # pending
        if start and end and start <= now < end:
            current.append(f"现在（{clock} 起）正在「{title}」(pending{id_hint})")
        elif start and start <= now:
            # 已开始但无 end 字段 → 视作进行中
            current.append(f"{clock} 开始的「{title}」仍在进行(pending{id_hint})")
        else:
            upcoming.append(f"{clock} 计划「{title}」(pending{id_hint})")

    parts: list[str] = []
    weekday = _WEEKDAYS_ZH[now.weekday()]
    parts.append(f"日期：{doc.get('date', now.strftime('%Y-%m-%d'))}（{weekday}）")

    if done:
        parts.append("今天已经：" + "；".join(done) + "。")
    if current:
        parts.append("此刻：" + "；".join(current) + "。")
    if upcoming:
        parts.append("接下来：" + "；".join(upcoming) + "。")
    if not (done or current or upcoming):
        parts.append("今天没有预先安排的事件。")

    if ambient:
        # 按时间排序后取最近 3 条（按时间倒序选，再按时间正序展示）
        sortable = []
        for a in ambient:
            t = _parse_iso(a.get("time", "")) or datetime.min
            sortable.append((t, a))
        sortable.sort(key=lambda x: x[0])
        recent = [a for _, a in sortable[-3:]]
        notes = []
        for a in recent:
            clock = _fmt_clock(a.get("time", ""))
            note = a.get("note", "")
            if note:
                notes.append(f"{clock} {note}")
        if notes:
            parts.append("环境：" + "；".join(notes) + "。")

    return "\n".join(parts)


# ---------------- 公开 API：到期事件触发 ----------------

def _load_trigger_state() -> dict:
    p = _trigger_state_path()
    if not p.exists():
        return {}
    try:
        loaded = json.loads(p.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _save_trigger_state(state: dict) -> None:
    try:
        _atomic_write_json(_trigger_state_path(), state)
    except OSError as e:
        logger.warning("[world_state] save trigger state failed: %s", e)


def due_events(now: datetime | None = None) -> list[tuple[str, str]]:
    """
    返回当前 tick 应该主动提起的 (key, message) 列表，并持久化 cooldown。

    自管 trigger state（$HERMES_HOME/companion/world_trigger_state.json），
    与 heartbeat 模块解耦，方便独立调用与测试。

    触发规则：pending 事件的 start 落在 [now - DUE_WINDOW, now + DUE_WINDOW] 内，
    且该 event id 在 _EVENT_COOLDOWN_SEC 内未触发过。
    """
    import time as _time
    now = now or datetime.now()
    fires: list[tuple[str, str]] = []
    window = timedelta(minutes=_DUE_WINDOW_MIN)

    trigger_state = _load_trigger_state()
    doc = _read_events()
    changed = False

    for ev in doc.get("schedule", []):
        if ev.get("status") != _STATUS_PENDING:
            continue
        start = _parse_iso(ev.get("start", ""))
        if not start:
            continue
        if abs(start - now) > window:
            continue

        ev_id = ev.get("id") or ""
        if not ev_id:
            continue
        key = f"event:{ev_id}"
        last = trigger_state.get(key, 0)
        if (_time.time() - last) < _EVENT_COOLDOWN_SEC:
            continue

        title = ev.get("title", "(未命名事件)")
        kind = ev.get("kind", _KIND_SELF)
        clock = _fmt_clock(ev.get("start", ""))
        if kind == _KIND_INTERACTION:
            msg = f"[日程] {clock} 我们约好了「{title}」。"
        else:
            msg = f"[日程] {clock} 我准备开始「{title}」。"
        fires.append((key, msg))
        trigger_state[key] = _time.time()
        changed = True

    if changed:
        _save_trigger_state(trigger_state)
    return fires


# ---------------- 公开 API：跨日归档 ----------------

def roll_over_if_new_day(now: datetime | None = None) -> bool:
    """
    若 events.json.date != 今天 (now)，把旧 doc append 到 events/<旧 date>.jsonl，
    pending 项标记为 missed，然后用今天的空文档重置 events.json。

    幂等：当天重复调用不做事。返回是否实际执行了归档。
    """
    now = now or datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    with _file_lock(_events_path()):
        doc = _read_events()
        old_date = doc.get("date", "")
        if old_date == today_str:
            return False
        if not old_date:
            # 没有有效日期，直接重置，不归档（避免污染归档目录）
            _write_events(_empty_doc(today_str))
            return False

        # 标记未完成的 pending 为 missed
        for ev in doc.get("schedule", []):
            if ev.get("status") == _STATUS_PENDING:
                ev["status"] = _STATUS_MISSED

        # 归档：每条 schedule + ambient 单独 append 一行
        archive = _archive_path(old_date)
        archive.parent.mkdir(parents=True, exist_ok=True)
        try:
            with archive.open("a", encoding="utf-8") as f:
                for ev in doc.get("schedule", []):
                    f.write(json.dumps(
                        {"type": "schedule", **ev}, ensure_ascii=False) + "\n")
                for am in doc.get("ambient", []):
                    f.write(json.dumps(
                        {"type": "ambient", **am}, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("[world_state] roll over 归档写入失败: %s", e)
            # 归档失败也要重置，避免下次又当作"新一天"重复尝试
        _write_events(_empty_doc(today_str))
        logger.info("[world_state] rolled over %s → %s", old_date, today_str)
        return True


# ---------------- 公开 API：CRUD ----------------

def add_event(start: str, end: str, title: str,
              kind: str = _KIND_SELF) -> str:
    """添加事件，返回新 id。start/end 必须是 ISO 字符串。"""
    if kind not in _VALID_KINDS:
        raise ValueError(f"kind 必须是 {sorted(_VALID_KINDS)} 之一，得到 {kind!r}")
    if not _parse_iso(start):
        raise ValueError(f"start 不是合法 ISO datetime: {start!r}")
    if end and not _parse_iso(end):
        raise ValueError(f"end 不是合法 ISO datetime: {end!r}")
    if not title.strip():
        raise ValueError("title 不可为空")

    ev_id = uuid.uuid4().hex[:8]
    new_ev = {
        "id": ev_id,
        "start": start,
        "end": end,
        "title": title.strip(),
        "kind": kind,
        "status": _STATUS_PENDING,
    }
    with _file_lock(_events_path()):
        doc = _read_events()
        doc.setdefault("schedule", []).append(new_ev)
        _write_events(doc)
    return ev_id


def add_ambient(note: str, when: datetime | None = None) -> None:
    """添加 ambient 环境事件（雨/温度/室内灯光等）。"""
    if not note.strip():
        raise ValueError("note 不可为空")
    when = when or datetime.now()
    entry = {"time": when.isoformat(timespec="minutes"), "note": note.strip()}
    with _file_lock(_events_path()):
        doc = _read_events()
        doc.setdefault("ambient", []).append(entry)
        _write_events(doc)


def mark_done(event_id: str) -> bool:
    """把指定事件状态置为 done。返回是否找到并修改。"""
    return mark_status(event_id, _STATUS_DONE)


def mark_missed(event_id: str) -> bool:
    """把指定事件状态置为 missed。返回是否找到并修改。"""
    return mark_status(event_id, _STATUS_MISSED)


def mark_status(event_id: str, status: str) -> bool:
    """把指定事件状态置为 status。返回是否找到并修改。"""
    if status not in {_STATUS_PENDING, _STATUS_DONE, _STATUS_MISSED}:
        raise ValueError(f"status 必须是 pending/done/missed 之一，得到 {status!r}")
    with _file_lock(_events_path()):
        doc = _read_events()
        for ev in doc.get("schedule", []):
            if ev.get("id") == event_id:
                ev["status"] = status
                _write_events(doc)
                return True
    return False


def list_today() -> dict:
    """返回今天的事件文档副本（仅供 slash command / 调试用）。"""
    return _read_events()


# ---------------- 公开 API：历史回溯 ----------------

def recall(date: str) -> str:
    """
    读 events/<date>.jsonl 并生成自然语言摘要。
    date 必须是 'YYYY-MM-DD' 格式。文件不存在返回提示。
    """
    if not _parse_iso(date + "T00:00"):
        return f"日期格式无效：{date}（应为 YYYY-MM-DD）"
    ap = _archive_path(date)
    if not ap.exists():
        return f"{date} 没有归档记录。"

    schedule_lines: list[str] = []
    ambient_lines: list[str] = []
    try:
        for raw in ap.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            rtype = rec.get("type")
            if rtype == "schedule":
                status = rec.get("status", "?")
                clock = _fmt_clock(rec.get("start", ""))
                title = rec.get("title", "(未命名事件)")
                marker = {"done": "✓", "missed": "✗", "pending": "·"}.get(status, "?")
                schedule_lines.append(f"  {marker} {clock} {title}")
            elif rtype == "ambient":
                clock = _fmt_clock(rec.get("time", ""))
                note = rec.get("note", "")
                if note:
                    ambient_lines.append(f"  · {clock} {note}")
    except OSError as e:
        return f"读取 {date} 归档失败：{e}"

    out = [f"# {date} 回顾"]
    if schedule_lines:
        out.append("日程：")
        out.extend(schedule_lines)
    if ambient_lines:
        out.append("环境：")
        out.extend(ambient_lines)
    if not (schedule_lines or ambient_lines):
        out.append("（无记录）")
    return "\n".join(out)


# ---------------- 自动每日归档守护线程 ----------------
#
# 设计：长驻进程（hermes 长会话 / 独立 heartbeat）启动时调一次
# start_daily_archiver()，它会起一个 daemon 线程，每天 00:00:30 醒来
# 跑 roll_over_if_new_day。这样跨日归档不依赖用户交互、不依赖 tick 周期。
#
# 进程退出时 daemon 自动收回。pre_llm_call / heartbeat _tick 中的
# roll_over_if_new_day 调用作为兜底（处理"进程在午夜时没运行"的情况）。

_DAILY_ARCHIVER_LOCK = threading.Lock()
_DAILY_ARCHIVER_THREAD: threading.Thread | None = None
# 午夜后多久才触发，避开 00:00 整点的钟差和其他定时任务
_ARCHIVE_OFFSET_SEC = 30


def _seconds_until_next_archive(now: datetime) -> float:
    """到下一次归档时刻（明天 00:00:_ARCHIVE_OFFSET_SEC）的秒数。"""
    tomorrow = (now + timedelta(days=1)).date()
    target = datetime.combine(tomorrow, datetime.min.time()) + timedelta(seconds=_ARCHIVE_OFFSET_SEC)
    return max(1.0, (target - now).total_seconds())


def start_daily_archiver() -> threading.Thread | None:
    """启动每日自动归档守护线程（幂等：重复调用返回已有线程）。

    可通过 HERMES_COMPANION_AUTO_ARCHIVE=0 关闭。返回 None 表示未启动。
    """
    if os.environ.get("HERMES_COMPANION_AUTO_ARCHIVE", "1").strip().lower() in (
        "0", "false", "no", "off"
    ):
        return None

    global _DAILY_ARCHIVER_THREAD
    with _DAILY_ARCHIVER_LOCK:
        if _DAILY_ARCHIVER_THREAD is not None and _DAILY_ARCHIVER_THREAD.is_alive():
            return _DAILY_ARCHIVER_THREAD

        def _loop():
            logger.info("[world_state] daily archiver started")
            # 启动时先补一次：覆盖"进程在午夜后才启动"的场景
            try:
                roll_over_if_new_day()
                _seed_today_safe()
            except Exception as e:
                logger.warning("[world_state] startup roll_over failed: %s", e)
            while True:
                try:
                    delay = _seconds_until_next_archive(datetime.now())
                    time.sleep(delay)
                    roll_over_if_new_day()
                    _seed_today_safe()
                except Exception as e:
                    logger.warning("[world_state] daily archiver tick failed: %s", e)
                    # 失败也要继续 loop，但避免紧密重试
                    time.sleep(60)

        t = threading.Thread(target=_loop, daemon=True, name="companion-daily-archiver")
        t.start()
        _DAILY_ARCHIVER_THREAD = t
        return t


def _seed_today_safe() -> None:
    """归档后调一次 daily_seed.seed_today_if_empty()，任何异常都吞掉。

    延迟 import 避免 world_state ↔ daily_seed 循环依赖。
    """
    try:
        from companion.daily_seed import seed_today_if_empty
        seed_today_if_empty()
    except Exception as e:
        logger.warning("[world_state] daily seed failed: %s", e)
