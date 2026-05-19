"""
world_state 模块的单元测试。

不依赖任何 Hermes 模块，可独立运行：
    cd hermes-companion && python -m pytest tests/test_world_state.py -v

每个测试通过 monkeypatch HERMES_HOME 隔离到 tmp_path，避免污染用户真实 ~/.hermes。
"""

from __future__ import annotations

import importlib
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# 让 tests/ 可直接 import 同级的 companion/
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _fresh_world_state(monkeypatch, tmp_path: Path):
    """重新 import world_state 与 emotion_state，让 HERMES_HOME 在模块路径中生效。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sys.modules.pop("companion.world_state", None)
    sys.modules.pop("companion.emotion_state", None)
    importlib.import_module("companion.emotion_state")
    return importlib.import_module("companion.world_state")


# ---------------- 基础 CRUD ----------------

def test_read_events_empty_returns_today_doc(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    doc = ws.list_today()
    assert doc["schedule"] == []
    assert doc["ambient"] == []
    assert doc["date"] == datetime.now().strftime("%Y-%m-%d")


def test_add_event_persists_and_returns_id(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    today = datetime.now().strftime("%Y-%m-%d")
    eid = ws.add_event(
        start=f"{today}T14:00",
        end=f"{today}T15:00",
        title="测试事件",
        kind="self",
    )
    assert eid
    doc = ws.list_today()
    assert len(doc["schedule"]) == 1
    ev = doc["schedule"][0]
    assert ev["id"] == eid
    assert ev["title"] == "测试事件"
    assert ev["status"] == "pending"


def test_add_event_rejects_invalid_kind(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    today = datetime.now().strftime("%Y-%m-%d")
    with pytest.raises(ValueError):
        ws.add_event(start=f"{today}T14:00", end=f"{today}T15:00",
                     title="x", kind="bogus")


def test_add_event_rejects_invalid_start(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        ws.add_event(start="not-a-date", end="", title="x")


def test_add_event_rejects_empty_title(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    today = datetime.now().strftime("%Y-%m-%d")
    with pytest.raises(ValueError):
        ws.add_event(start=f"{today}T14:00", end=f"{today}T15:00", title="   ")


def test_mark_done(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    today = datetime.now().strftime("%Y-%m-%d")
    eid = ws.add_event(f"{today}T14:00", f"{today}T15:00", "x")
    assert ws.mark_done(eid) is True
    assert ws.list_today()["schedule"][0]["status"] == "done"
    assert ws.mark_done("nonexistent") is False


def test_add_ambient(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    ws.add_ambient("下雨了", when=datetime(2026, 5, 20, 13, 42))
    doc = ws.list_today()
    assert doc["ambient"][0]["note"] == "下雨了"
    assert "13:42" in doc["ambient"][0]["time"]


# ---------------- format_today_brief ----------------

def test_brief_empty_day(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    brief = ws.format_today_brief()
    assert "没有预先安排" in brief


def test_brief_classifies_done_current_upcoming(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    base = datetime(2026, 5, 20, 14, 30)  # "现在"
    today = base.strftime("%Y-%m-%d")
    eid_done = ws.add_event(f"{today}T09:00", f"{today}T10:00", "晨读", "self")
    ws.mark_done(eid_done)
    ws.add_event(f"{today}T14:00", f"{today}T15:00", "讨论架构", "interaction")
    ws.add_event(f"{today}T20:00", f"{today}T20:30", "散步", "self")
    # 强制 doc.date 等于 base 的日期，避免跨日干扰
    brief = ws.format_today_brief(now=base)
    assert "晨读" in brief and "完成" in brief
    assert "讨论架构" in brief and "此刻" in brief
    assert "散步" in brief and "接下来" in brief


def test_brief_includes_ambient_tail(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    base = datetime(2026, 5, 20, 14, 0)
    for i in range(5):
        ws.add_ambient(f"note-{i}", when=base - timedelta(minutes=i))
    brief = ws.format_today_brief(now=base)
    # 只取最近 3 条
    assert "note-0" in brief
    assert "note-4" not in brief


def test_brief_returns_str_even_on_corrupt_file(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    (tmp_path / "companion").mkdir(parents=True, exist_ok=True)
    (tmp_path / "companion" / "events.json").write_text("not json{{", encoding="utf-8")
    brief = ws.format_today_brief()
    assert isinstance(brief, str)
    assert "没有预先安排" in brief or brief  # 不抛异常即可


# ---------------- due_events ----------------

def test_due_events_fires_within_window(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    now = datetime.now()
    start = now + timedelta(minutes=3)  # 在 10min 窗口内
    eid = ws.add_event(
        start=start.isoformat(timespec="minutes"),
        end=(start + timedelta(hours=1)).isoformat(timespec="minutes"),
        title="即将开始的事件",
    )
    fires = ws.due_events(now=now)
    assert len(fires) == 1
    key, msg = fires[0]
    assert key == f"event:{eid}"
    assert "即将开始的事件" in msg


def test_due_events_skips_outside_window(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    now = datetime.now()
    start = now + timedelta(hours=3)
    ws.add_event(
        start=start.isoformat(timespec="minutes"),
        end=(start + timedelta(hours=1)).isoformat(timespec="minutes"),
        title="远在 3 小时后",
    )
    assert ws.due_events(now=now) == []


def test_due_events_respects_cooldown(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    now = datetime.now()
    start = now + timedelta(minutes=2)
    ws.add_event(
        start=start.isoformat(timespec="minutes"),
        end=(start + timedelta(hours=1)).isoformat(timespec="minutes"),
        title="x",
    )
    first = ws.due_events(now=now)
    assert len(first) == 1
    # 第二次同 tick 不应再触发（cooldown 持久化到了文件）
    second = ws.due_events(now=now)
    assert second == []


def test_due_events_skips_done(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    now = datetime.now()
    start = now + timedelta(minutes=2)
    eid = ws.add_event(
        start=start.isoformat(timespec="minutes"),
        end=(start + timedelta(hours=1)).isoformat(timespec="minutes"),
        title="x",
    )
    ws.mark_done(eid)
    assert ws.due_events(now=now) == []


def test_due_events_interaction_phrasing(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    now = datetime.now()
    start = now + timedelta(minutes=2)
    ws.add_event(
        start=start.isoformat(timespec="minutes"),
        end=(start + timedelta(hours=1)).isoformat(timespec="minutes"),
        title="一起讨论",
        kind="interaction",
    )
    fires = ws.due_events(now=now)
    assert "约好了" in fires[0][1]


# ---------------- roll_over_if_new_day ----------------

def test_roll_over_archives_and_marks_missed(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    # 手动写一个昨天日期的 events.json
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    events_path = tmp_path / "companion" / "events.json"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(json.dumps({
        "date": yesterday,
        "schedule": [
            {"id": "a", "start": f"{yesterday}T09:00", "end": f"{yesterday}T10:00",
             "title": "已完成的事", "kind": "self", "status": "done"},
            {"id": "b", "start": f"{yesterday}T14:00", "end": f"{yesterday}T15:00",
             "title": "没做完的事", "kind": "self", "status": "pending"},
        ],
        "ambient": [{"time": f"{yesterday}T12:00", "note": "晴"}],
    }, ensure_ascii=False), encoding="utf-8")

    rolled = ws.roll_over_if_new_day()
    assert rolled is True

    # 当天文档应被重置
    doc = ws.list_today()
    assert doc["date"] == today
    assert doc["schedule"] == []
    assert doc["ambient"] == []

    # 归档文件应存在，包含 missed 标记
    archive = tmp_path / "companion" / "events" / f"{yesterday}.jsonl"
    assert archive.exists()
    lines = [json.loads(l) for l in archive.read_text(encoding="utf-8").splitlines() if l.strip()]
    statuses = {l.get("id"): l.get("status") for l in lines if l.get("type") == "schedule"}
    assert statuses == {"a": "done", "b": "missed"}
    ambients = [l for l in lines if l.get("type") == "ambient"]
    assert len(ambients) == 1


def test_roll_over_is_idempotent_same_day(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    # 第一次调用会创建今日空文档但不归档
    assert ws.roll_over_if_new_day() is False
    # 再次调用同样不归档
    assert ws.roll_over_if_new_day() is False


# ---------------- recall ----------------

def test_recall_missing_archive(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    out = ws.recall("2025-01-01")
    assert "没有归档" in out


def test_recall_invalid_date_format(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    out = ws.recall("not-a-date")
    assert "格式无效" in out


def test_recall_reads_archive(monkeypatch, tmp_path):
    ws = _fresh_world_state(monkeypatch, tmp_path)
    archive_dir = tmp_path / "companion" / "events"
    archive_dir.mkdir(parents=True)
    (archive_dir / "2026-05-19.jsonl").write_text(
        json.dumps({"type": "schedule", "id": "x", "start": "2026-05-19T09:00",
                    "title": "晨读", "status": "done"}, ensure_ascii=False) + "\n"
        + json.dumps({"type": "ambient", "time": "2026-05-19T13:00",
                      "note": "下雨"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out = ws.recall("2026-05-19")
    assert "晨读" in out and "✓" in out
    assert "下雨" in out
