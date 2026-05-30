"""
heartbeat 模块测试（队列 + 触发器）。
"""

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture
def hb(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for m in (
        "companion.heartbeat",
        "companion.emotion_state",
        "companion.world_state",
        "companion.world_interaction",
    ):
        sys.modules.pop(m, None)
    import importlib
    return importlib.import_module("companion.heartbeat")


def test_queue_path_under_hermes_home(hb, tmp_path):
    assert hb.queue_path() == tmp_path / "companion_pending.txt"


def test_enqueue_and_drain(hb):
    hb.enqueue("第一条")
    hb.enqueue("第二条")
    text = hb.drain_pending()
    assert "第一条" in text
    assert "第二条" in text
    # drain 后清空
    assert hb.drain_pending() == ""


def test_drain_empty_when_no_file(hb):
    assert hb.drain_pending() == ""


def test_enqueue_strips_newlines(hb):
    hb.enqueue("multi\nline\nmessage")
    text = hb.drain_pending()
    assert "\n" not in text.strip()
    assert "multi line message" in text


def test_tick_enqueues_when_high_arousal(hb, monkeypatch):
    from companion.emotion_state import update_emotion
    update_emotion(0.5, 0.95, "excited", "very high")
    hb._tick()
    text = hb.drain_pending()
    assert "excited" in text
    assert "心跳" in text


def test_tick_silent_when_low_arousal_and_off_hours(hb, monkeypatch):
    import datetime as _dt
    from companion.emotion_state import update_emotion
    update_emotion(0.0, 0.2, "calm", "ok")

    # 强制非 09:xx 时段
    class _FakeDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 5, 20, 14, 30)

    monkeypatch.setattr(hb, "datetime", _FakeDT)
    hb._tick()
    assert hb.drain_pending() == ""


def test_collect_due_event_message(hb):
    from datetime import datetime, timedelta
    from companion.world_state import add_event

    now = datetime.now().replace(hour=20, minute=0, second=0, microsecond=0)
    add_event(
        now.isoformat(timespec="minutes"),
        (now + timedelta(minutes=30)).isoformat(timespec="minutes"),
        "测试提醒",
        kind="interaction",
    )

    msgs = hb.collect_heartbeat_messages(now)
    assert msgs == ["[日程] 20:00 我们约好了「测试提醒」。"]


def test_collect_arousal_respects_cooldown(hb):
    from companion.emotion_state import update_emotion

    update_emotion(0.8, 0.9, "eager", "想主动说一句", source="test")
    first = hb.collect_heartbeat_messages()
    second = hb.collect_heartbeat_messages()

    assert any("eager" in msg for msg in first)
    assert second == []
