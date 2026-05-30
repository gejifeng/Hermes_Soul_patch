"""
plugin/hooks.py 集成测试：pre_llm_call 注入、post_tool_call 失败下调、
post_llm_call 触发情感推断（mock 辅助 LLM）。
"""

import importlib
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    for m in (
        "companion.emotion_state", "companion.heartbeat",
        "companion.emotion_inference", "companion.time_context",
        "companion.world_state", "companion.world_interaction",
        "plugin.hooks",
    ):
        sys.modules.pop(m, None)
    hooks = importlib.import_module("plugin.hooks")
    es = importlib.import_module("companion.emotion_state")
    inf = importlib.import_module("companion.emotion_inference")
    inf.reset_throttle_for_tests()
    return hooks, es, inf


def test_pre_llm_call_includes_time_and_emotion(fresh):
    hooks, es, _ = fresh
    es.update_emotion(0.4, 0.3, "content", "测试中")
    out = hooks.on_pre_llm_call()
    ctx = out["context"]
    assert "[Current time:" in ctx
    assert "[Companion状态]" in ctx
    assert "content" in ctx
    assert "[Companion主动消息]" not in ctx


def test_pre_llm_call_includes_pending_when_queued(fresh):
    hooks, _, _ = fresh
    from companion.heartbeat import enqueue
    enqueue("[心跳] 测试主动消息")
    out = hooks.on_pre_llm_call()
    assert "[Companion主动消息]" in out["context"]
    assert "测试主动消息" in out["context"]
    # 注入后队列被 drain
    out2 = hooks.on_pre_llm_call()
    assert "[Companion主动消息]" not in out2["context"]


def test_post_tool_call_nudges_on_error_dict(fresh):
    hooks, es, _ = fresh
    es.update_emotion(0.5, 0.4, "calm", "ok")
    hooks.on_post_tool_call(tool_name="read_file", result={"error": "ENOENT"})
    state = es._read_state()
    assert state["dominant"] == "mildly_frustrated"
    assert state["valence"] == pytest.approx(0.4)
    assert "read_file" in state["note"]


def test_post_tool_call_nudges_on_error_json_string(fresh):
    hooks, es, _ = fresh
    es.update_emotion(0.5, 0.4, "calm", "ok")
    hooks.on_post_tool_call(tool_name="web_get", result='{"error":"timeout"}')
    state = es._read_state()
    assert state["dominant"] == "mildly_frustrated"


def test_post_tool_call_noop_on_success(fresh):
    hooks, es, _ = fresh
    es.update_emotion(0.5, 0.4, "calm", "ok")
    hooks.on_post_tool_call(tool_name="web_get", result='{"status":"ok"}')
    state = es._read_state()
    assert state["dominant"] == "calm"
    assert state["valence"] == 0.5


def test_post_llm_call_schedules_inference(fresh, monkeypatch):
    hooks, es, inf = fresh

    captured = {}

    def fake_call_aux(messages):
        captured["messages"] = messages
        return '{"valence": 0.55, "arousal": 0.5, "dominant": "warm", "note": "聊得很开心"}'

    monkeypatch.setattr(inf, "_call_aux_llm", fake_call_aux)

    hooks.on_post_llm_call(
        session_id="s1",
        user_message="今天累了想聊聊",
        assistant_response="抱抱，慢慢说，我在听。",
        conversation_history=[
            {"role": "user", "content": "今天累了想聊聊"},
            {"role": "assistant", "content": "抱抱，慢慢说，我在听。"},
        ],
        model="x",
        platform="cli",
    )

    # 等待后台线程完成
    import time
    for _ in range(50):
        state = es._read_state()
        if state["dominant"] == "warm":
            break
        time.sleep(0.05)

    state = es._read_state()
    assert state["dominant"] == "warm"
    assert state["valence"] == pytest.approx(0.55)
    assert "messages" in captured
    assert any("[本轮 用户]" in m.get("content", "") for m in captured["messages"])


def test_post_llm_call_updates_world_state(fresh, monkeypatch):
    hooks, _, inf = fresh
    monkeypatch.setattr(inf, "_call_aux_llm", lambda messages: '{"valence":0.1,"arousal":0.1,"dominant":"calm","note":"ok"}')

    hooks.on_post_llm_call(
        user_message="明晚8点提醒我一起听后摇",
        assistant_response="好，我记下。",
    )

    from companion.world_state import list_today
    doc = list_today()
    assert len(doc["schedule"]) == 1
    assert doc["schedule"][0]["kind"] == "interaction"
    assert "听后摇" in doc["schedule"][0]["title"]


def test_post_llm_call_throttled(fresh, monkeypatch):
    hooks, _, inf = fresh

    calls = {"n": 0}

    def fake_call_aux(messages):
        calls["n"] += 1
        return '{"valence":0.1,"arousal":0.1,"dominant":"calm","note":"x"}'

    monkeypatch.setattr(inf, "_call_aux_llm", fake_call_aux)

    hooks.on_post_llm_call(user_message="a", assistant_response="b")
    hooks.on_post_llm_call(user_message="c", assistant_response="d")
    hooks.on_post_llm_call(user_message="e", assistant_response="f")

    import time
    time.sleep(0.3)

    # 第一次会跑，后两次被节流
    assert calls["n"] == 1


def test_post_llm_call_silent_on_aux_failure(fresh, monkeypatch):
    hooks, es, inf = fresh
    es.update_emotion(0.0, 0.5, "calm", "init")

    def boom(messages):
        raise RuntimeError("provider down")

    monkeypatch.setattr(inf, "_call_aux_llm", boom)

    # 不应抛异常
    hooks.on_post_llm_call(user_message="hi", assistant_response="hi back")

    import time
    time.sleep(0.2)

    # 状态保持不变
    state = es._read_state()
    assert state["dominant"] == "calm"


def test_inference_disabled_via_env(fresh, monkeypatch):
    hooks, _, inf = fresh
    monkeypatch.setenv("HERMES_COMPANION_INFERENCE", "0")

    called = {"n": 0}

    def fake(messages):
        called["n"] += 1
        return "{}"

    monkeypatch.setattr(inf, "_call_aux_llm", fake)
    hooks.on_post_llm_call(user_message="a", assistant_response="b")

    import time
    time.sleep(0.2)
    assert called["n"] == 0
