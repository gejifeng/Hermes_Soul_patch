"""
LLM-facing agenda 工具的单元测试（设计见 core_idea/core.md §4.8）。

验证：
  - register_tools 调用 ctx.register_tool 三次，工具名/toolset/schema 正确
  - 各 handler 的 happy path 写入 events.json 并返回 JSON 字符串
  - 参数校验失败时返回 tool_error JSON 而不抛异常
  - 时间归一支持 HH:MM 与完整 ISO
  - end 缺省时默认 start + 30 分钟
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _fresh(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for m in ("companion.world_state", "companion.emotion_state",
              "companion.heartbeat", "plugin.tools"):
        sys.modules.pop(m, None)
    tools = importlib.import_module("plugin.tools")
    ws = importlib.import_module("companion.world_state")
    return tools, ws


# ---------------- register_tools ----------------

def test_register_tools_registers_three(monkeypatch, tmp_path):
    tools, _ = _fresh(monkeypatch, tmp_path)
    calls: list[dict] = []

    class _FakeCtx:
        def register_tool(self, **kwargs):
            calls.append(kwargs)

    tools.register_tools(_FakeCtx())

    names = [c["name"] for c in calls]
    assert names == ["agenda_add", "agenda_done", "agenda_ambient"]
    for c in calls:
        assert c["toolset"] == "companion_agenda"
        assert callable(c["handler"])
        assert "name" in c["schema"]
        assert "description" in c["schema"]
        assert "parameters" in c["schema"]


def test_register_tools_failure_is_swallowed(monkeypatch, tmp_path):
    tools, _ = _fresh(monkeypatch, tmp_path)

    class _BrokenCtx:
        def register_tool(self, **kwargs):
            raise RuntimeError("boom")

    # 不应抛
    tools.register_tools(_BrokenCtx())


# ---------------- agenda_add ----------------

def test_agenda_add_happy_path(monkeypatch, tmp_path):
    tools, ws = _fresh(monkeypatch, tmp_path)
    res = tools._handle_agenda_add({"start": "20:00", "title": "散步"})
    payload = json.loads(res)
    assert payload["ok"] is True
    assert payload["data"]["title"] == "散步"
    assert payload["data"]["kind"] == "interaction"  # 默认值
    assert payload["data"]["start"].endswith("T20:00")
    # end 默认 start + 30min
    assert payload["data"]["end"].endswith("T20:30")
    # 落地到 events.json
    today = ws.list_today()
    assert len(today["schedule"]) == 1
    assert today["schedule"][0]["title"] == "散步"


def test_agenda_add_with_explicit_end_and_iso(monkeypatch, tmp_path):
    tools, _ = _fresh(monkeypatch, tmp_path)
    res = tools._handle_agenda_add({
        "start": "2026-05-20T14:00",
        "end": "2026-05-20T15:30",
        "title": "讨论方案",
        "kind": "interaction",
    })
    payload = json.loads(res)
    assert payload["ok"] is True
    assert payload["data"]["end"] == "2026-05-20T15:30"


def test_agenda_add_kind_self(monkeypatch, tmp_path):
    tools, _ = _fresh(monkeypatch, tmp_path)
    res = tools._handle_agenda_add({
        "start": "09:00", "title": "晨读", "kind": "self",
    })
    payload = json.loads(res)
    assert payload["data"]["kind"] == "self"


def test_agenda_add_rejects_empty_title(monkeypatch, tmp_path):
    tools, _ = _fresh(monkeypatch, tmp_path)
    payload = json.loads(tools._handle_agenda_add({"start": "20:00", "title": "  "}))
    assert payload["ok"] is False
    assert "title" in payload["error"]


def test_agenda_add_rejects_bad_kind(monkeypatch, tmp_path):
    tools, _ = _fresh(monkeypatch, tmp_path)
    payload = json.loads(tools._handle_agenda_add({
        "start": "20:00", "title": "x", "kind": "bogus",
    }))
    assert payload["ok"] is False
    assert "kind" in payload["error"]


def test_agenda_add_rejects_missing_start(monkeypatch, tmp_path):
    tools, _ = _fresh(monkeypatch, tmp_path)
    payload = json.loads(tools._handle_agenda_add({"start": "", "title": "x"}))
    assert payload["ok"] is False


# ---------------- agenda_done ----------------

def test_agenda_done_marks_status(monkeypatch, tmp_path):
    tools, ws = _fresh(monkeypatch, tmp_path)
    # 先建一条
    res = tools._handle_agenda_add({"start": "20:00", "title": "散步"})
    ev_id = json.loads(res)["data"]["id"]

    payload = json.loads(tools._handle_agenda_done({"event_id": ev_id}))
    assert payload["ok"] is True
    assert payload["data"]["status"] == "done"
    assert ws.list_today()["schedule"][0]["status"] == "done"


def test_agenda_done_missing_id(monkeypatch, tmp_path):
    tools, _ = _fresh(monkeypatch, tmp_path)
    payload = json.loads(tools._handle_agenda_done({"event_id": "nonexistent"}))
    assert payload["ok"] is False


def test_agenda_done_empty_id(monkeypatch, tmp_path):
    tools, _ = _fresh(monkeypatch, tmp_path)
    payload = json.loads(tools._handle_agenda_done({"event_id": "  "}))
    assert payload["ok"] is False


# ---------------- agenda_ambient ----------------

def test_agenda_ambient_happy(monkeypatch, tmp_path):
    tools, ws = _fresh(monkeypatch, tmp_path)
    payload = json.loads(tools._handle_agenda_ambient({"note": "下雨了"}))
    assert payload["ok"] is True
    today = ws.list_today()
    assert len(today["ambient"]) == 1
    assert today["ambient"][0]["note"] == "下雨了"


def test_agenda_ambient_with_time(monkeypatch, tmp_path):
    tools, ws = _fresh(monkeypatch, tmp_path)
    payload = json.loads(tools._handle_agenda_ambient({
        "note": "起风了", "time": "13:42",
    }))
    assert payload["ok"] is True
    assert ws.list_today()["ambient"][0]["time"].endswith("T13:42")


def test_agenda_ambient_empty_note(monkeypatch, tmp_path):
    tools, _ = _fresh(monkeypatch, tmp_path)
    payload = json.loads(tools._handle_agenda_ambient({"note": ""}))
    assert payload["ok"] is False


def test_agenda_ambient_bad_time(monkeypatch, tmp_path):
    tools, _ = _fresh(monkeypatch, tmp_path)
    payload = json.loads(tools._handle_agenda_ambient({
        "note": "x", "time": "not-a-time",
    }))
    assert payload["ok"] is False
