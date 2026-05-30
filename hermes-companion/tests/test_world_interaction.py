"""world_interaction 对话联动测试。"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _fresh(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for mod in (
        "companion.emotion_state",
        "companion.world_state",
        "companion.world_interaction",
    ):
        sys.modules.pop(mod, None)
    ws = importlib.import_module("companion.world_state")
    wi = importlib.import_module("companion.world_interaction")
    return ws, wi


def test_observer_marks_current_event_done(monkeypatch, tmp_path):
    ws, wi = _fresh(monkeypatch, tmp_path)
    now = datetime(2026, 5, 30, 14, 30)
    eid = ws.add_event("2026-05-30T14:00", "2026-05-30T15:00", "修改论文绪论", "self")

    updates = wi.observe_interaction(
        user_message="论文绪论改完了，我回来了",
        assistant_response="辛苦啦",
        now=now,
    )

    assert {"action": "mark_done", "id": eid, "title": "修改论文绪论"} in updates
    assert ws.list_today()["schedule"][0]["status"] == "done"


def test_observer_does_not_mark_negated_done(monkeypatch, tmp_path):
    ws, wi = _fresh(monkeypatch, tmp_path)
    ws.add_event("2026-05-30T14:00", "2026-05-30T15:00", "修改论文绪论", "self")

    updates = wi.observe_interaction(
        user_message="还没做完，别急",
        now=datetime(2026, 5, 30, 14, 30),
    )

    assert updates == []
    assert ws.list_today()["schedule"][0]["status"] == "pending"


def test_observer_adds_interaction_event_from_time_promise(monkeypatch, tmp_path):
    ws, wi = _fresh(monkeypatch, tmp_path)
    now = datetime(2026, 5, 30, 18, 0)

    updates = wi.observe_interaction(
        user_message="晚上8点提醒我一起听后摇",
        now=now,
    )

    assert updates and updates[0]["action"] == "add_interaction"
    ev = ws.list_today()["schedule"][0]
    assert ev["kind"] == "interaction"
    assert ev["start"] == "2026-05-30T20:00"
    assert "听后摇" in ev["title"]


def test_observer_skips_time_question(monkeypatch, tmp_path):
    ws, wi = _fresh(monkeypatch, tmp_path)

    updates = wi.observe_interaction(
        user_message="你看看几点啦",
        now=datetime(2026, 5, 30, 21, 20),
    )

    assert updates == []
    assert ws.list_today()["schedule"] == []


def test_observer_records_ambient_signal(monkeypatch, tmp_path):
    ws, wi = _fresh(monkeypatch, tmp_path)
    now = datetime(2026, 5, 30, 21, 20)

    updates = wi.observe_interaction(
        user_message="窗外开始下雨了，宿舍好安静",
        now=now,
    )

    assert updates and updates[0]["action"] == "add_ambient"
    assert "下雨" in ws.list_today()["ambient"][0]["note"]


def test_observer_avoids_duplicate_time_event(monkeypatch, tmp_path):
    ws, wi = _fresh(monkeypatch, tmp_path)
    now = datetime(2026, 5, 30, 18, 0)

    wi.observe_interaction(user_message="晚上8点提醒我一起听后摇", now=now)
    wi.observe_interaction(user_message="晚上8点提醒我一起听后摇", now=now + timedelta(minutes=1))

    assert len(ws.list_today()["schedule"]) == 1
